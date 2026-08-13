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
from source.bgstats_sync import limpiar_nombre_prefijo, sync as bgstats_sync
from source.calendario_sync import sync as calendario_sync
from source.pdf_pipeline import index_pdf
from source.duracion_model import entrenar as entrenar_duracion, predecir as predecir_duracion

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
                "categoria_compra — clasificacion manual: 'en_linea', 'tienda_fisica', 'amigos', 'regalo', "
                "'viaje' (NULL si no hay acquired_from registrado), usar para agrupar gasto por tipo de "
                "compra, "
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
                "- bgstats.calendario_eventos(tipo — 'visita' o 'vacacion', nombre, fecha_inicio, fecha_fin, "
                "jugador_uuid — FK a jugadores si el nombre de la visita coincide con un jugador registrado) "
                "— fecha_fin es EXCLUSIVA (formato ICS), el ultimo dia real es fecha_fin - 1 dia. Usar para "
                "preguntas de cuanto se juega cuando alguien visita, o que se jugo en vacaciones: unir con "
                "partidas via p.fecha::date BETWEEN ce.fecha_inicio AND (ce.fecha_fin - INTERVAL '1 day')\n"
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


@app.get("/juegos/catalogo")
def juegos_catalogo():
    """Nombres tal cual BGG de toda tu biblioteca de BG Stats (no solo lo ya
    indexado) — para autocompletar al agregar un reglamento nuevo y evitar
    que el mismo juego termine indexado bajo nombres ligeramente distintos."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT nombre FROM bgstats.juegos ORDER BY nombre;")
    result = [row[0] for row in cur.fetchall()]
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


MI_NOMBRE = "Alberto Qu"


JUGADOR_ANONIMO_GENERICO = "Jugador anónimo"

# nombres de oponentes automa/bot (BG Stats los registra como "jugador" con
# nombre propio, ej. "Vanderbot Jr 🤖") -- no son companeros reales
FILTRO_NO_BOT = "COALESCE(pj.nombre_anonimo, j.nombre) NOT LIKE '%%🤖%%'"


@app.get("/bgstats/companeros")
def bgstats_companeros(modo: str = "jugadores"):
    """modo=jugadores (default): solo companeros ligados a un jugador real
    (jugador_uuid, con perfil en bgstats.jugadores) — excluye nombres sueltos
    de texto libre por partida (nombre_anonimo, ej. "Frank Munoz", "Jairo"),
    el cajon generico "Jugador anonimo", y a los oponentes automa/bot (esos
    salen en /bgstats/top-juegos?modo=solo, agrupados por juego).
    modo=todos: lo anterior + los nombres de texto libre + el cajon generico + bots.
    """
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute(
        f"""
        SELECT COALESCE(pj.nombre_anonimo, j.nombre) AS nombre, pj.partida_uuid,
               pj.gano
        FROM bgstats.partida_jugadores pj
        LEFT JOIN bgstats.jugadores j ON j.uuid = pj.jugador_uuid
        WHERE COALESCE(pj.nombre_anonimo, j.nombre) IS NOT NULL
          AND COALESCE(pj.nombre_anonimo, j.nombre) != %s
          AND (%s OR (pj.nombre_anonimo IS NULL AND j.nombre != %s AND {FILTRO_NO_BOT}))
        """,
        (MI_NOMBRE, modo == "todos", JUGADOR_ANONIMO_GENERICO),
    )
    conteo: dict[str, dict] = {}
    for nombre_crudo, partida_uuid, gano in cur.fetchall():
        nombre = limpiar_nombre_prefijo(nombre_crudo)
        stats = conteo.setdefault(nombre, {"partidas": set(), "victorias": 0})
        stats["partidas"].add(partida_uuid)
        if gano:
            stats["victorias"] += 1
    cur.close()
    conn.close()

    result = [
        {"nombre": nombre, "partidas": len(v["partidas"]), "victorias": v["victorias"]}
        for nombre, v in conteo.items()
    ]
    result.sort(key=lambda r: r["partidas"], reverse=True)
    return result


@app.get("/bgstats/resumen")
def bgstats_resumen():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*), COUNT(DISTINCT juego_uuid),
               ROUND(SUM(duracion_min) / 60.0, 1), MIN(fecha), MAX(fecha)
        FROM bgstats.partidas
        """
    )
    partidas, juegos_distintos, horas_totales, primera, ultima = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM bgstats.juegos WHERE es_propio")
    juegos_propios = cur.fetchone()[0]
    cur.close()
    conn.close()

    meses_transcurridos = None
    promedio_mensual = None
    if primera and ultima:
        meses_transcurridos = max(1, (ultima.year - primera.year) * 12 + (ultima.month - primera.month) + 1)
        promedio_mensual = round(partidas / meses_transcurridos, 1)

    return {
        "partidas": partidas,
        "juegos_distintos": juegos_distintos,
        "juegos_propios": juegos_propios,
        "horas_totales": horas_totales,
        "primera_partida": primera,
        "ultima_partida": ultima,
        "promedio_partidas_mes": promedio_mensual,
    }


@app.get("/bgstats/top-juegos")
def bgstats_top_juegos(limite: int = 15, modo: str = "todos"):
    """modo=todos (default): todas las partidas, agrupadas por juego.
    modo=solo: solo partidas sin companero humano real -- 1 sola persona,
    tag "Solo" oficial de BG Stats, o con un oponente automa/bot -- agrupadas
    por juego, mostrando que bot(s) se enfrentaron si aplica.
    """
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    if modo == "solo":
        cur.execute(
            f"""
            WITH conteo_jugadores AS (
                SELECT partida_uuid, COUNT(*) AS n FROM bgstats.partida_jugadores GROUP BY partida_uuid
            ),
            partida_tiene_bot AS (
                SELECT DISTINCT pj.partida_uuid
                FROM bgstats.partida_jugadores pj
                LEFT JOIN bgstats.jugadores j ON j.uuid = pj.jugador_uuid
                WHERE {FILTRO_NO_BOT.replace('NOT LIKE', 'LIKE')}
            ),
            partidas_solo AS (
                SELECT p.uuid, p.juego_uuid, p.duracion_min, p.tag_digital
                FROM bgstats.partidas p
                LEFT JOIN conteo_jugadores cj ON cj.partida_uuid = p.uuid
                WHERE p.tag_solo
                   OR p.uuid IN (SELECT partida_uuid FROM partida_tiene_bot)
                   OR COALESCE(cj.n, 0) <= 1
            )
            SELECT g.nombre, COUNT(*) AS partidas, ROUND(SUM(ps.duracion_min) / 60.0, 1) AS horas,
                   BOOL_OR(ps.tag_digital) AS digital
            FROM partidas_solo ps
            JOIN bgstats.juegos g ON g.uuid = ps.juego_uuid
            GROUP BY g.nombre
            ORDER BY partidas DESC
            LIMIT %s
            """,
            (limite,),
        )
        filas = cur.fetchall()

        # nombres de bot por juego, deduplicados correctamente (no por partida)
        cur.execute(
            f"""
            SELECT g.nombre, ARRAY_AGG(DISTINCT COALESCE(pj.nombre_anonimo, j.nombre) ORDER BY COALESCE(pj.nombre_anonimo, j.nombre))
            FROM bgstats.partidas p
            JOIN bgstats.juegos g ON g.uuid = p.juego_uuid
            JOIN bgstats.partida_jugadores pj ON pj.partida_uuid = p.uuid
            LEFT JOIN bgstats.jugadores j ON j.uuid = pj.jugador_uuid
            WHERE {FILTRO_NO_BOT.replace('NOT LIKE', 'LIKE')}
            GROUP BY g.nombre
            """
        )
        bots_por_juego = {nombre: bots for nombre, bots in cur.fetchall()}
        cur.close()
        conn.close()

        result = [
            {
                "juego": r[0], "partidas": r[1], "horas": float(r[2] or 0), "digital": r[3],
                "bots": ", ".join(bots_por_juego[r[0]]) if r[0] in bots_por_juego else None,
            }
            for r in filas
        ]
        return result

    cur.execute(
        """
        SELECT j.nombre, COUNT(*) AS partidas, ROUND(SUM(p.duracion_min) / 60.0, 1) AS horas,
               BOOL_OR(p.tag_digital) AS digital
        FROM bgstats.partidas p JOIN bgstats.juegos j ON j.uuid = p.juego_uuid
        GROUP BY j.nombre
        ORDER BY partidas DESC
        LIMIT %s
        """,
        (limite,),
    )
    result = [
        {"juego": r[0], "partidas": r[1], "horas": float(r[2] or 0), "digital": r[3], "bots": None}
        for r in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return result


DIAS_SEMANA = ["Domingo", "Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado"]


@app.get("/bgstats/cuando-juegas")
def bgstats_cuando_juegas():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT EXTRACT(DOW FROM fecha)::int AS dow, COUNT(*)
        FROM bgstats.partidas GROUP BY dow ORDER BY dow
        """
    )
    por_dia_semana = [{"dia": DIAS_SEMANA[dow], "partidas": n} for dow, n in cur.fetchall()]

    cur.execute(
        """
        SELECT to_char(fecha, 'YYYY-MM') AS mes, COUNT(*)
        FROM bgstats.partidas
        WHERE fecha >= (CURRENT_DATE - INTERVAL '12 months')
        GROUP BY mes ORDER BY mes
        """
    )
    por_mes = [{"mes": mes, "partidas": n} for mes, n in cur.fetchall()]
    cur.close()
    conn.close()
    return {"por_dia_semana": por_dia_semana, "por_mes": por_mes}


@app.get("/bgstats/clima")
def bgstats_clima():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            CASE WHEN c.precipitacion_mm > 0 THEN 'lluvia' ELSE 'sin_lluvia' END AS condicion,
            CASE
                WHEN c.temp_media_c < 15 THEN 'frio'
                WHEN c.temp_media_c < 22 THEN 'templado'
                ELSE 'calido'
            END AS rango_temp,
            COUNT(*)
        FROM bgstats.partidas p
        JOIN bgstats.lugares l ON l.uuid = p.lugar_uuid
        JOIN bgstats.clima_diario c ON c.lugar_uuid = l.uuid AND c.fecha = p.fecha::date
        GROUP BY condicion, rango_temp
        """
    )
    filas = cur.fetchall()
    cur.close()
    conn.close()

    lluvia = sum(n for cond, _, n in filas if cond == "lluvia")
    sin_lluvia = sum(n for cond, _, n in filas if cond == "sin_lluvia")
    por_temp: dict[str, int] = {}
    for _, rango, n in filas:
        por_temp[rango] = por_temp.get(rango, 0) + n

    return {
        "partidas_con_clima": lluvia + sin_lluvia,
        "lluvia": lluvia,
        "sin_lluvia": sin_lluvia,
        "por_temperatura": [{"rango": r, "partidas": n} for r, n in por_temp.items()],
    }


@app.get("/bgstats/top-lugares")
def bgstats_top_lugares(limite: int = 15):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT l.nombre, COUNT(*) AS partidas, l.lat, l.lon
        FROM bgstats.partidas p JOIN bgstats.lugares l ON l.uuid = p.lugar_uuid
        GROUP BY l.nombre, l.lat, l.lon
        ORDER BY partidas DESC
        LIMIT %s
        """,
        (limite,),
    )
    result = [
        {"lugar": r[0], "partidas": r[1], "lat": r[2], "lon": r[3]} for r in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return result


@app.get("/bgstats/propios-sin-jugar")
def bgstats_propios_sin_jugar():
    """Juegos propios (es_propio) sin ninguna partida directa ni usados como
    expansion en otra partida — la lista detras del numero que muestra
    /bgstats/coleccion en juegos_propios_sin_jugar."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT nombre, min_jugadores, max_jugadores, min_duracion_min, max_duracion_min, rating
        FROM bgstats.juegos j
        WHERE es_propio AND NOT es_expansion
          AND NOT EXISTS (SELECT 1 FROM bgstats.partidas p WHERE p.juego_uuid = j.uuid)
          AND NOT EXISTS (SELECT 1 FROM bgstats.partidas p WHERE j.uuid = ANY(p.expansiones_usadas))
        ORDER BY nombre
        """
    )
    result = [
        {
            "nombre": r[0], "min_jugadores": r[1], "max_jugadores": r[2],
            "min_duracion_min": r[3], "max_duracion_min": r[4], "rating": r[5],
        }
        for r in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return result


@app.get("/bgstats/coleccion")
def bgstats_coleccion():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute(
        """
        SELECT ROUND(SUM(c.price_paid_mxn) FILTER (WHERE c.status_owned AND c.categoria_compra IS DISTINCT FROM 'regalo')::numeric, 2),
               COUNT(*) FILTER (WHERE c.status_owned AND NOT j.es_expansion),
               COUNT(*) FILTER (WHERE c.status_prev_owned AND NOT c.status_owned AND NOT j.es_expansion),
               COUNT(*) FILTER (WHERE c.status_wishlist)
        FROM bgstats.colecciones c
        JOIN bgstats.juegos j ON j.uuid = c.juego_uuid
        """
    )
    gasto_total, copias_propias, copias_ya_no_tiene, en_wishlist = cur.fetchone()

    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE es_propio AND NOT es_expansion),
               COUNT(*) FILTER (WHERE es_propio AND NOT es_expansion AND NOT EXISTS (
                   SELECT 1 FROM bgstats.partidas p WHERE p.juego_uuid = j.uuid
               ) AND NOT EXISTS (
                   SELECT 1 FROM bgstats.partidas p WHERE j.uuid = ANY(p.expansiones_usadas)
               ))
        FROM bgstats.juegos j
        """
    )
    juegos_propios_total, juegos_propios_sin_jugar = cur.fetchone()

    cur.execute(
        """
        SELECT COALESCE(categoria_compra, 'sin_categoria'), ROUND(SUM(price_paid_mxn)::numeric, 2), COUNT(*)
        FROM bgstats.colecciones
        WHERE status_owned AND categoria_compra IS DISTINCT FROM 'regalo'
        GROUP BY COALESCE(categoria_compra, 'sin_categoria')
        ORDER BY 2 DESC NULLS LAST
        """
    )
    por_categoria = [
        {"categoria": r[0], "gasto_mxn": float(r[1] or 0), "juegos": r[2]} for r in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT fuente_compra, ROUND(SUM(price_paid_mxn)::numeric, 2), COUNT(*)
        FROM bgstats.colecciones
        WHERE fuente_compra IS NOT NULL AND status_owned AND categoria_compra IS DISTINCT FROM 'regalo'
        GROUP BY fuente_compra
        ORDER BY 2 DESC NULLS LAST
        LIMIT 8
        """
    )
    top_fuentes = [
        {"fuente": r[0], "gasto_mxn": float(r[1] or 0), "juegos": r[2]} for r in cur.fetchall()
    ]
    cur.close()
    conn.close()

    return {
        "gasto_total_mxn": float(gasto_total or 0),
        "copias_propias": copias_propias,
        "copias_ya_no_tiene": copias_ya_no_tiene,
        "en_wishlist": en_wishlist,
        "juegos_propios_total": juegos_propios_total,
        "juegos_propios_sin_jugar": juegos_propios_sin_jugar,
        "por_categoria": por_categoria,
        "top_fuentes": top_fuentes,
    }


@app.get("/bgstats/duracion/juegos")
def bgstats_duracion_juegos():
    """Juegos con datos de BGG cacheados (los unicos que se pueden predecir)."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT j.nombre, j.min_jugadores, j.max_jugadores
        FROM bgstats.juegos j
        JOIN bgg_data.juegos_detalle d ON d.bgg_id = j.bgg_id
        WHERE d.peso_complejidad IS NOT NULL AND NOT j.es_expansion
        ORDER BY j.nombre
        """
    )
    result = [{"nombre": r[0], "min_jugadores": r[1], "max_jugadores": r[2]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return result


@app.get("/bgstats/duracion/entrenamiento")
def bgstats_duracion_entrenamiento(incluir_amigos: bool = True):
    """Diagnostico del modelo de duracion: MAE de cada candidato, MAE del
    baseline (promedio simple) para comparar, y coeficientes activos.
    incluir_amigos=True (default) suma partidas de bgg_data.plays_amigos
    (solo en memoria, esa tabla nunca se fusiona con bgstats.partidas)."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    r = entrenar_duracion(conn, incluir_amigos=incluir_amigos)
    conn.close()
    if r is None:
        raise HTTPException(status_code=422, detail="No hay suficientes datos para entrenar el modelo")
    return {
        "n": r["n"],
        "ganador": r["ganador"],
        "mae_por_modelo": {k: round(v, 1) for k, v in r["mae_por_modelo"].items()},
        "mae_baseline": round(r["mae_baseline"], 1),
        "coeficientes": {k: round(v, 2) for k, v in r["coeficientes"].items()},
    }


@app.get("/bgstats/duracion/predecir")
def bgstats_duracion_predecir(
    juego: str, num_jugadores: int, lugar_categoria: str | None = None, grupo_social: str | None = None,
    usa_expansion: bool = False, incluir_amigos: bool = True,
):
    """Predice duracion_min para un juego (por nombre) + numero de
    jugadores + categoria de lugar opcional (ver duracion_model.CATEGORIAS_LUGAR)
    + grupo social opcional (ver duracion_model.CATEGORIAS_GRUPO). temp_media_c
    y tag_digital usan el valor tipico (mediana) ya que no se conocen de
    antemano para una partida futura."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.peso_complejidad, d.dependencia_idioma, d.min_playtime, d.max_playtime, d.calificacion_promedio
        FROM bgstats.juegos j
        JOIN bgg_data.juegos_detalle d ON d.bgg_id = j.bgg_id
        WHERE j.nombre = %s
        LIMIT 1
        """,
        (juego,),
    )
    fila = cur.fetchone()
    if fila is None:
        conn.close()
        raise HTTPException(status_code=404, detail=f"'{juego}' no tiene datos de BGG cacheados")

    r = entrenar_duracion(conn, incluir_amigos=incluir_amigos)
    conn.close()
    if r is None:
        raise HTTPException(status_code=422, detail="No hay suficientes datos para entrenar el modelo")

    peso, dependencia, min_pt, max_pt, calificacion = fila
    estimado = predecir_duracion(
        r,
        {
            "peso_complejidad": peso,
            "dependencia_idioma": dependencia,
            "calificacion_promedio": calificacion,
            "num_jugadores": num_jugadores,
            "min_playtime": min_pt,
            "max_playtime": max_pt,
            "usa_expansion": float(usa_expansion),
        },
        categoria_lugar=lugar_categoria,
        grupo_social=grupo_social,
    )
    return {
        "juego": juego, "num_jugadores": num_jugadores,
        "lugar_categoria": lugar_categoria, "grupo_social": grupo_social,
        "duracion_estimada_min": round(estimado), "mae_modelo": round(r["mae_por_modelo"][r["ganador"]], 1),
    }


@app.post("/bgstats/sync")
def bgstats_sync_endpoint():
    if not os.path.exists(BGSTATS_EXPORT_PATH):
        raise HTTPException(status_code=404, detail=f"No existe {BGSTATS_EXPORT_PATH}")
    resultado = bgstats_sync(BGSTATS_EXPORT_PATH)
    try:
        resultado["calendario"] = calendario_sync()
    except Exception as e:
        # los calendarios de iCloud son un extra sobre el sync principal de BG Stats;
        # si fallan (link vencido, red) no debe tumbar la sincronizacion de partidas/juegos
        resultado["calendario_error"] = str(e)
    return resultado


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
