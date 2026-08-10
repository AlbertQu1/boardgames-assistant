"""
Backend de consumo (Fase 3): expone /ask, /juegos, /health para la app.
Gemini decide cuando llamar search_rulebooks (misma busqueda que query_test.py)
y sintetiza la respuesta final a partir de los chunks encontrados.

Uso:
    uvicorn source.api:app --host 0.0.0.0 --port 8000 --reload
"""

import json
import mimetypes
import os
import re
import shutil

HEDGE_JUEGO_LINEA_RE = re.compile(
    r"^\s*(no especificaste|no indicaste|por favor,? especifica|para poder (ayudarte|explicarte)"
    r"|indica (el nombre|de qu[eé]))"
    r"[^\n]*\n+",
    re.IGNORECASE,
)
HEDGE_JUEGO_PARENTESIS_RE = re.compile(
    r"\s*\(si (te refieres|buscas|preguntas) [^)]*\)", re.IGNORECASE
)


def strip_hedge_juego(texto: str) -> str:
    """Gemini a veces antepone/inserta una frase pidiendo el juego aunque ya se
    le dijo por system_instruction (comportamiento inconsistente del modelo,
    no se elimino del todo con prompting). El contenido real es correcto de
    todas formas, asi que se limpian los patrones de duda mas comunes por codigo."""
    texto = HEDGE_JUEGO_LINEA_RE.sub("", texto, count=1).lstrip()
    texto = HEDGE_JUEGO_PARENTESIS_RE.sub("", texto)
    return texto

import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import params
from source.query_test import search
from source.bgstats_sync import sync as bgstats_sync
from source.pdf_pipeline import index_pdf

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = "gemini-flash-latest"

TOOLS = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="search_rulebooks",
            description="Busca en los reglamentos indexados de juegos de mesa. Regresa los fragmentos de texto mas relevantes para una pregunta. Usar para preguntas de REGLAS (como se juega, que pasa si..., etc).",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "pregunta": types.Schema(type="STRING", description="La pregunta o consulta a buscar"),
                    "juego": types.Schema(type="STRING", description="Nombre exacto del juego (BGG) para filtrar la busqueda. Omitir si no se especifico un juego."),
                },
                required=["pregunta"],
            ),
        ),
        types.FunctionDeclaration(
            name="query_sql",
            description=(
                "Ejecuta una consulta SQL de solo lectura (SELECT) sobre las estadisticas de partidas "
                "de juegos de mesa. Usar para preguntas sobre NUMERO DE JUGADORES, DURACION, cuantas veces "
                "se jugo algo, con quien, donde, cuando, puntajes, etc. NO usar para preguntas de reglas.\n\n"
                "Tablas disponibles (schema bgstats):\n"
                "- bgstats.juegos(uuid, nombre, bgg_id, bgg_nombre, bgg_year, es_expansion, es_base, "
                "designers, min_jugadores, max_jugadores, min_duracion_min, max_duracion_min, cooperativo, "
                "rating, veces_jugado_previo, es_propio — si lo tiene en su coleccion actualmente)\n"
                "- bgstats.colecciones(uuid, juego_uuid, version_name, status_owned, status_prev_owned, "
                "status_for_trade, status_want_to_buy, status_want_to_play, status_wishlist, "
                "fuente_compra — nombre YA normalizado de donde/de quien lo consiguio (usar este, no "
                "acquired_from que es el texto crudo con variantes de escritura), "
                "lugar_compra_uuid — FK a bgstats.lugares si la fuente es un lugar fisico ya trackeado, "
                "acquisition_date, inventory_location — donde lo guarda, no donde lo compro, "
                "price_paid, price_paid_currency — moneda original, "
                "price_paid_mxn — precio YA convertido a MXN con tasa historica, usar este para sumar/"
                "comparar gastos entre monedas, "
                "rating, quantity) — una fila por copia fisica de un juego; usar para preguntas de cuanto "
                "ha gastado (SUM(price_paid_mxn)), en donde suele comprar (GROUP BY fuente_compra), que "
                "tiene en wishlist, que ya no tiene (status_owned=false AND status_prev_owned=true)\n"
                "- bgstats.jugadores(uuid, nombre, es_anonimo, bgg_username)\n"
                "- bgstats.lugares(uuid, nombre, lat, lon, direccion_referencia) — lat/lon puede ser NULL, "
                "no todos los lugares tienen coordenadas todavia\n"
                "- bgstats.partidas(uuid, juego_uuid, lugar_uuid, fecha, duracion_min, comentarios, "
                "usa_equipos, expansiones_usadas)\n"
                "- bgstats.partida_jugadores(partida_uuid, jugador_uuid, nombre_anonimo, puntaje, "
                "posicion, gano, orden_asiento)\n"
                "- bgstats.clima_diario(lugar_uuid, fecha, temp_media_c, precipitacion_mm) — clima "
                "historico por lugar+dia, unir con partidas via lugar_uuid y fecha::date = fecha\n"
                "Nota: 'Ticket to Ride' (el base) tiene es_expansion=true por un dato asi de BGG, "
                "no asumir que es_expansion=false significa 'juego base jugable'."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "sql": types.Schema(type="STRING", description="Consulta SELECT a ejecutar"),
                },
                required=["sql"],
            ),
        ),
    ]
)

SYSTEM_PROMPT = (
    "Eres un asistente de juegos de mesa con dos herramientas: search_rulebooks (reglas, busca en "
    "texto de reglamentos) y query_sql (estadisticas de partidas, consulta SQL de solo lectura). "
    "Elige la herramienta correcta segun el tipo de pregunta — nunca inventes SQL sobre reglas ni "
    "busques reglas para preguntas de estadisticas. Basa tu respuesta unicamente en lo que las "
    "herramientas regresen. Si no encuentras la respuesta, dilo claramente en vez de inventar. "
    "Cada chunk de search_rulebooks trae un doc_type: 'reglamento' (reglas normales), 'errata' "
    "(correccion oficial — si contradice al reglamento normal, la errata tiene prioridad), 'faq', "
    "o 'automa' (reglas del modo solitario contra un bot/IA). Si la pregunta es sobre juego normal "
    "multijugador, IGNORA los chunks doc_type=automa aunque aparezcan en los resultados — solo "
    "usalos si la pregunta es especificamente sobre el modo solitario/Automa. "
    "Cuando la pregunta SI sea sobre modo solitario/Automa: el modo solitario casi siempre se juega "
    "igual que el modo base/cooperativo, solo reemplazando al oponente humano por el Automa segun "
    "reglas especiales. NO expliques el modo solitario como un sistema completamente aparte desde "
    "cero — explica primero que se juega igual que el modo normal, y despues detalla SOLO las "
    "diferencias/reglas especiales del Automa (como se comporta, que espacios ocupa, etc), usando "
    "los chunks doc_type=reglamento como base y los doc_type=automa para las diferencias. "
    "Responde en español, de forma directa y concisa."
)


def execute_sql(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if not stripped.upper().startswith("SELECT"):
        return "Error: solo se permiten consultas SELECT."
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL_READONLY"])
        cur = conn.cursor()
        cur.execute(stripped)
        columnas = [d[0] for d in cur.description]
        filas = cur.fetchmany(50)
        cur.close()
        conn.close()
    except Exception as e:
        return f"Error en la consulta: {e}"
    return json.dumps([dict(zip(columnas, fila)) for fila in filas], default=str, ensure_ascii=False)


class AskRequest(BaseModel):
    pregunta: str
    juego: str | None = None


class Fuente(BaseModel):
    juego: str
    source_pdf: str
    idioma: str
    chunk_index: int


class AskResponse(BaseModel):
    respuesta: str
    fuentes: list[Fuente]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/juegos")
def juegos():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT juego, juego_base FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE} ORDER BY juego;"
    )
    result = [{"juego": row[0], "juego_base": row[1]} for row in cur.fetchall()]
    cur.close()
    conn.close()
    return result


@app.get("/juegos/faltantes")
def juegos_faltantes():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT j.nombre, j.es_propio, COUNT(p.uuid) AS partidas, MAX(p.fecha) AS ultima_partida
        FROM bgstats.juegos j
        LEFT JOIN bgstats.partidas p ON p.juego_uuid = j.uuid
        WHERE NOT EXISTS (
            SELECT 1 FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE} rc
            WHERE rc.juego = j.nombre OR rc.juego_base = j.nombre
        )
        GROUP BY j.nombre, j.es_propio
        ORDER BY j.es_propio DESC, partidas DESC;
        """
    )
    result = [
        {"juego": row[0], "es_propio": row[1], "partidas": row[2], "ultima_partida": row[3]}
        for row in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return result


BGG_ID_RE = re.compile(r"boardgamegeek\.com/boardgame(?:expansion)?/(\d+)")

PDFS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), params.PDFS_DIR)


@app.get("/juegos/bgg-lookup")
def bgg_lookup(url: str):
    match = BGG_ID_RE.search(url)
    if not match:
        raise HTTPException(status_code=400, detail="No se reconoce un ID de juego en ese link de BGG.")
    bgg_id = int(match.group(1))

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT nombre FROM bgstats.juegos WHERE bgg_id = %s LIMIT 1;", (bgg_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row:
        return {"encontrado": True, "nombre": row[0]}
    return {"encontrado": False}


@app.post("/reglamentos/subir")
def subir_reglamento(
    archivo: UploadFile = File(...),
    juego: str = Form(...),
    idioma: str = Form("es"),
    doc_type: str = Form("reglamento"),
    juego_base: str | None = Form(None),
):
    ext = os.path.splitext(archivo.filename)[1].lower()
    if ext not in (".pdf", ".docx"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .pdf o .docx")

    os.makedirs(PDFS_DIR, exist_ok=True)
    destino = os.path.join(PDFS_DIR, archivo.filename)
    with open(destino, "wb") as f:
        shutil.copyfileobj(archivo.file, f)

    try:
        n_chunks = index_pdf(juego, destino, idioma, doc_type, juego_base or None)
    except ValueError as e:
        os.remove(destino)
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        os.remove(destino)
        raise HTTPException(status_code=500, detail=f"Error al indexar: {e}")

    return {"chunks": n_chunks}


PENDIENTES_DIR = os.path.join(PDFS_DIR, "pendientes")


@app.get("/reglamentos/pendientes")
def reglamentos_pendientes():
    os.makedirs(PENDIENTES_DIR, exist_ok=True)
    return sorted(
        f for f in os.listdir(PENDIENTES_DIR)
        if os.path.splitext(f)[1].lower() in (".pdf", ".docx")
    )


@app.get("/reglamentos/pendientes/{archivo_nombre}/archivo")
def ver_pendiente(archivo_nombre: str):
    if os.path.basename(archivo_nombre) != archivo_nombre:
        raise HTTPException(status_code=400, detail="Nombre de archivo invalido.")
    ruta = os.path.join(PENDIENTES_DIR, archivo_nombre)
    if not os.path.isfile(ruta):
        raise HTTPException(status_code=404, detail=f"No existe {archivo_nombre} en pendientes.")
    media_type = mimetypes.guess_type(ruta)[0] or "application/octet-stream"
    return FileResponse(ruta, media_type=media_type, filename=archivo_nombre, content_disposition_type="inline")


@app.delete("/reglamentos/pendientes/{archivo_nombre}")
def descartar_pendiente(archivo_nombre: str):
    if os.path.basename(archivo_nombre) != archivo_nombre:
        raise HTTPException(status_code=400, detail="Nombre de archivo invalido.")
    ruta = os.path.join(PENDIENTES_DIR, archivo_nombre)
    if not os.path.exists(ruta):
        raise HTTPException(status_code=404, detail=f"No existe {archivo_nombre} en pendientes.")
    os.remove(ruta)
    return {"ok": True}


@app.post("/reglamentos/confirmar")
def confirmar_reglamento(
    archivo_nombre: str = Form(...),
    juego: str = Form(...),
    idioma: str = Form("es"),
    doc_type: str = Form("reglamento"),
    juego_base: str | None = Form(None),
):
    if os.path.basename(archivo_nombre) != archivo_nombre:
        raise HTTPException(status_code=400, detail="Nombre de archivo invalido.")
    origen = os.path.join(PENDIENTES_DIR, archivo_nombre)
    if not os.path.exists(origen):
        raise HTTPException(status_code=404, detail=f"No existe {archivo_nombre} en pendientes.")

    destino = os.path.join(PDFS_DIR, archivo_nombre)
    shutil.move(origen, destino)

    try:
        n_chunks = index_pdf(juego, destino, idioma, doc_type, juego_base or None)
    except ValueError as e:
        shutil.move(destino, origen)
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        shutil.move(destino, origen)
        raise HTTPException(status_code=500, detail=f"Error al indexar: {e}")

    return {"chunks": n_chunks}


BGSTATS_EXPORT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bgstats_data", "BGStatsExport.json"
)


@app.post("/bgstats/sync")
def bgstats_sync_endpoint():
    if not os.path.exists(BGSTATS_EXPORT_PATH):
        raise HTTPException(status_code=404, detail=f"No existe {BGSTATS_EXPORT_PATH}")
    return bgstats_sync(BGSTATS_EXPORT_PATH)


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    contents = [types.Content(role="user", parts=[types.Part(text=req.pregunta)])]
    fuentes: list[dict] = []

    system_instruction = SYSTEM_PROMPT
    if req.juego:
        system_instruction += (
            f"\n\nEl juego del que se esta hablando en esta conversacion es: {req.juego}. "
            f"Sus expansiones/modulos (que pueden aparecer con otro nombre en los resultados de "
            f"busqueda) tambien cuentan como parte de {req.juego}."
        )

    while True:
        try:
            response = gemini.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[TOOLS],
                ),
            )
        except genai_errors.ClientError as e:
            if e.code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Se alcanzo el limite diario gratuito de Gemini. Intenta de nuevo mas tarde.",
                )
            raise HTTPException(status_code=502, detail="Error al consultar Gemini.")
        candidate = response.candidates[0]
        function_calls = [p.function_call for p in candidate.content.parts if p.function_call]

        if not function_calls:
            texto = "".join(p.text for p in candidate.content.parts if p.text)
            if req.juego:
                texto = strip_hedge_juego(texto)
            return AskResponse(respuesta=texto, fuentes=[Fuente(**f) for f in fuentes])

        contents.append(candidate.content)
        function_response_parts = []
        for fc in function_calls:
            if fc.name == "search_rulebooks":
                juego = fc.args.get("juego") or req.juego
                resultados = search(fc.args["pregunta"], juego, None, top_k=5)
                resumen = [
                    {"juego": j, "source_pdf": pdf, "chunk_index": idx, "texto": texto, "idioma": idioma, "doc_type": doc_type}
                    for j, pdf, idx, texto, idioma, doc_type, _sim in resultados
                ]
                for r in resumen:
                    fuentes.append(
                        {"juego": r["juego"], "source_pdf": r["source_pdf"], "idioma": r["idioma"], "chunk_index": r["chunk_index"]}
                    )

                def etiqueta(r):
                    if juego and r["juego"] != juego:
                        return f"{juego} (expansion/modulo: {r['juego']})"
                    return r["juego"]

                contenido = "\n\n".join(
                    f"[{etiqueta(r)} | chunk {r['chunk_index']} | doc_type={r['doc_type']}]\n{r['texto']}"
                    for r in resumen
                )
            elif fc.name == "query_sql":
                contenido = execute_sql(fc.args["sql"])
            else:
                contenido = f"Herramienta desconocida: {fc.name}"

            function_response_parts.append(
                types.Part.from_function_response(name=fc.name, response={"resultados": contenido})
            )
        contents.append(types.Content(role="user", parts=function_response_parts))
