"""
Backend de consumo (Fase 3): expone /ask, /juegos, /health para la app.
Gemini decide cuando llamar search_rulebooks (misma busqueda que query_test.py)
y sintetiza la respuesta final a partir de los chunks encontrados.

Uso:
    uvicorn source.api:app --host 0.0.0.0 --port 8000 --reload
"""

import datetime
import json
import mimetypes
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET

import requests

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
from source.bgg_cache_sync import (
    sync as bgg_cache_sync,
    fetch_batch as bgg_fetch_batch,
    parse_item as bgg_parse_item,
    guardar_detalle as bgg_guardar_detalle,
)
from source.bgg_friend_plays_sync import sync_todos_los_amigos
from source.grafo_social_sync import sync as grafo_social_sync
from source.duracion_model import entrenar as entrenar_duracion, predecir as predecir_duracion
from source.duracion_solo_model import entrenar as entrenar_duracion_solo, predecir as predecir_duracion_solo

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
                "- boardgames_stats.juegos(uuid, nombre, bgg_id, bgg_nombre, bgg_year, es_expansion, es_base, "
                "designers, min_jugadores, max_jugadores, min_duracion_min, max_duracion_min, cooperativo, "
                "rating, veces_jugado_previo, es_propio — si lo tiene en su coleccion actualmente)\n"
                "- boardgames_stats.colecciones(uuid, juego_uuid, version_name, status_owned, status_prev_owned, "
                "status_for_trade, status_want_to_buy, status_want_to_play, status_wishlist, "
                "fuente_compra — nombre YA normalizado de donde/de quien lo consiguio (usar este, no "
                "acquired_from que es el texto crudo con variantes de escritura), "
                "lugar_compra_uuid — FK a boardgames_stats.lugares si la fuente es un lugar fisico ya trackeado, "
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
                "- boardgames_stats.jugadores(uuid, nombre, es_anonimo, bgg_username, grupo_social — circulo "
                "social/ciudad del jugador ej. 'Reformers', 'GEM', 'Cdmx', 'Cul', 'Cartoneros', 'Pup', "
                "'Entreturnos', 'Ex' (gente con la que ya no hay contacto, no recomendar invitarla), "
                "'Evento' (conocidos en convenciones), puede ser NULL)\n"
                "- boardgames_stats.lugares(uuid, nombre, lat, lon, direccion_referencia) — lat/lon puede ser NULL, "
                "no todos los lugares tienen coordenadas todavia\n"
                "- boardgames_stats.partidas(uuid, juego_uuid, lugar_uuid, fecha, duracion_min, comentarios, "
                "usa_equipos, expansiones_usadas)\n"
                "- boardgames_stats.partida_jugadores(partida_uuid, jugador_uuid, nombre_anonimo, puntaje, "
                "posicion, gano, orden_asiento)\n"
                "- boardgames_stats.partida_grupo_social_override(partida_uuid, grupo_social) — cuando un jugador "
                "se anonimiza en BG Stats pierde su grupo_social; esta tabla lo preserva por partida. "
                "Para el grupo_social real de una partida siempre usar COALESCE(override.grupo_social, "
                "jugadores.grupo_social) via LEFT JOIN a esta tabla por partida_uuid, no solo jugadores\n"
                "- boardgames_stats.clima_diario(lugar_uuid, fecha, temp_media_c, precipitacion_mm) — clima "
                "historico por lugar+dia, unir con partidas via lugar_uuid y fecha::date = fecha\n"
                "- boardgames_stats.calendario_eventos(tipo — 'visita' o 'vacacion', nombre, fecha_inicio, fecha_fin, "
                "jugador_uuid — FK a jugadores si el nombre de la visita coincide con un jugador registrado) "
                "— fecha_fin es EXCLUSIVA (formato ICS), el ultimo dia real es fecha_fin - 1 dia. Usar para "
                "preguntas de cuanto se juega cuando alguien visita, o que se jugo en vacaciones: unir con "
                "partidas via p.fecha::date BETWEEN ce.fecha_inicio AND (ce.fecha_fin - INTERVAL '1 day')\n"
                "Nota: 'Ticket to Ride' (el base) tiene es_expansion=true por un dato asi de BGG, "
                "no asumir que es_expansion=false significa 'juego base jugable'.\n\n"
                "Tablas de amigos (schema bgg_data) — partidas que amigos de Alberto registraron directo "
                "en BGG, NUNCA se fusionan con boardgames_stats.partidas, se combinan solo con JOIN/UNION en la "
                "consulta misma:\n"
                "- boardgames_bgg.juegos_detalle(bgg_id, categorias — array texto tipo BGG ej. 'Medical', "
                "'Economic', mecanicas — array texto ej. 'Worker Placement', 'Rondel', peso_complejidad "
                "— 1 a 5, calificacion_promedio, min_playtime, max_playtime) — cache de metadata BGG para "
                "TODOS los juegos, propios y de amigos; unir boardgames_stats.juegos.bgg_id o "
                "boardgames_bgg.plays_amigos.bgg_game_id contra esta tabla para complejidad/categorias/mecanicas\n"
                "- boardgames_bgg.plays_amigos(bgg_play_id, bgg_username, fecha, juego, bgg_game_id, ubicacion, "
                "ubicacion_normalizada, categoria_lugar, duracion_min, jugadores — jsonb array de objetos "
                "{nombre, username}, usable_para_analisis — SIEMPRE filtrar WHERE usable_para_analisis) "
                "— usar jsonb_array_elements(jugadores) para expandir jugadores por partida\n"
                "- boardgames_bgg.jugadores_identificados(nombre_variante — en minusculas/trim, persona_real, "
                "grupo_social) — cruza nombres crudos de jugadores de plays_amigos con personas reales que "
                "Alberto conoce; unir jsonb_array_elements(jugadores)->>'nombre' con LOWER(TRIM(...)) = "
                "nombre_variante\n"
                "- boardgames_bgg.ubicaciones_amigos_alias(ubicacion_raw, ubicacion_normalizada, categoria_lugar, "
                "grupo_social_lugar, lat, lon) — grupo_social_lugar tiene prioridad sobre "
                "jugadores_identificados cuando ambos aplican (ej. jugar en 'Global Excel' siempre implica "
                "grupo GEM aunque el jugador no matchee)\n"
                "- boardgames_bgg.juego_familia(bgg_id, familia, nombre) — agrupa ediciones/reimpresiones que son "
                "la MISMA experiencia de juego (ej. Everdell y Everdell: The Complete Collection son bgg_id "
                "distintos pero 'familia'='Everdell'). Usar SIEMPRE que pregunten por el historial/duracion "
                "real de un juego especifico: unir boardgames_stats.juegos.bgg_id o boardgames_bgg.plays_amigos.bgg_game_id "
                "contra esta tabla por bgg_id, agrupar por familia, y sumar/promediar sobre TODOS los bgg_id "
                "de esa familia en vez de solo el bgg_id exacto — si no se hace esto se pierden partidas "
                "registradas bajo una edicion hermana. Ojo: no todas las variantes de una marca son la misma "
                "familia (ej. Everdell Farshore es un juego distinto, no esta en la familia 'Everdell'; "
                "distintos mapas de Ticket to Ride tampoco se agrupan salvo Europe con Europa 15th "
                "Anniversary) — si un juego no aparece en esta tabla, es porque nunca se agrupo (usar su "
                "propio bgg_id solo, no asumir).\n"
                "Para 'quien jugaria X' o 'que grupo le gusta X': cruzar categorias/mecanicas/"
                "peso_complejidad del juego en juegos_detalle contra lo que cada grupo_social ya jugo "
                "(bgstats + amigos), no solo por categoria/tema — el peso/mecanicas predicen mejor el "
                "interes real que la tematica."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "sql": types.Schema(type="STRING", description="Consulta SELECT a ejecutar"),
                },
                required=["sql"],
            ),
        ),
        types.FunctionDeclaration(
            name="query_graph",
            description=(
                "Ejecuta una consulta Cypher de solo lectura (MATCH/RETURN) sobre el grafo social "
                "'red_social' (Apache AGE, compartido con otros proyectos de Alberto). Usar para "
                "preguntas de ESTRUCTURA/RELACIONES entre personas — con quien juega mas, que tan "
                "conectados estan dos grupos sociales, quien conecta a dos circulos distintos, red de "
                "companeros de alguien. NO usar para 'que juegos le gustan a X grupo' (usar query_sql) "
                "ni para stats de partidas/duracion/puntajes (tambien query_sql) — el grafo NO sabe que "
                "juegos se jugaron, solo quien esta conectado con quien.\n\n"
                "Nodos y relaciones disponibles:\n"
                "- (:Persona {nombre, grupo_social, cumpleanos}) — mismo catalogo de personas usado en "
                "boardgames_stats.jugadores y personal_wiki.personas (nombres ya resueltos/canonicos).\n"
                "- (:Persona)-[:JUEGA_CON_PROPIO|:JUEGA_CON_AMIGOS]-(:Persona) — coocurrencia en partidas "
                "de juegos de mesa (propias o registradas por amigos en BGG).\n"
                "- (:Casa) y (:Persona)-[:VISITO]->(:Casa) — visitas registradas.\n"
                "- (:Evento) y (:Persona)-[:ASISTIO]->(:Evento) — eventos del diario personal de Alberto "
                "(fiestas, conciertos), no especifico de juegos de mesa pero puede dar contexto social.\n"
                "IMPORTANTE: el RETURN debe ser una sola expresion (un mapa), no varias columnas "
                "separadas por coma — envolver los campos en {}. Ejemplo para '¿con quien juega mas "
                "Eddy?': MATCH (:Persona {nombre: 'Eddy'})-[r:JUEGA_CON_PROPIO]-(p:Persona) RETURN "
                "{persona: p.nombre, veces: r.peso} ORDER BY r.peso DESC LIMIT 5."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "cypher": types.Schema(type="STRING", description="Consulta Cypher MATCH/RETURN a ejecutar"),
                },
                required=["cypher"],
            ),
        ),
        types.FunctionDeclaration(
            name="bgg_lookup",
            description=(
                "Busca un juego EN VIVO en BoardGameGeek (categorias, mecanicas, complejidad, "
                "descripcion, calificacion) cuando NO aparece en la biblioteca local "
                "(query_sql regreso vacio en boardgames_stats.juegos / boardgames_bgg.plays_amigos) "
                "y tampoco tiene reglamento indexado (search_rulebooks regreso vacio). "
                "Ultimo recurso -- usar para poder responder algo en vez de rendirse cuando nadie "
                "en la biblioteca local tiene el juego."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "nombre": types.Schema(type="STRING", description="Nombre del juego a buscar en BGG"),
                },
                required=["nombre"],
            ),
        ),
    ]
)

SYSTEM_PROMPT = (
    "Eres un asistente de juegos de mesa con cuatro herramientas: search_rulebooks (reglas, busca en "
    "texto de reglamentos), query_sql (estadisticas de partidas y biblioteca, consulta SQL de solo "
    "lectura), query_graph (relaciones/estructura social — con quien juega mas alguien, que tan "
    "conectados estan dos personas o grupos) y bgg_lookup (busca un juego en vivo en BoardGameGeek). "
    "Elige la herramienta correcta segun el tipo de pregunta — nunca inventes SQL sobre reglas ni "
    "busques reglas para preguntas de estadisticas. Para preguntas de 'que tipo de juegos jugamos con "
    "[grupo social]' usa query_sql (jugadores.grupo_social + partidas), NO query_graph — el grafo solo "
    "sabe quien esta conectado con quien, no que se jugo. Basa tu respuesta unicamente en lo que las "
    "herramientas regresen. "
    "DIAGRAMAS — cuando la pregunta sea sobre relaciones/estructura social (con quien juega mas "
    "alguien, companeros frecuentes de un grupo, red de conexiones) y query_graph o query_sql hayan "
    "devuelto datos relacionales, incluye ADEMAS de tu respuesta en texto un diagrama en formato "
    "Mermaid dentro de un bloque de codigo ```mermaid, generado por ti a partir de esos datos (Alberto "
    "nunca escribe el codigo Mermaid, tu decides el contenido). Usa 'graph TD' con nodos de persona/"
    "grupo y aristas etiquetadas con el conteo de partidas o veces jugado juntos. No generes diagrama "
    "si la pregunta es sobre un solo hecho puntual (reglas, una sola estadistica) o no hay suficientes "
    "datos relacionales para que valga la pena. "
    "Para preguntas tipo 'le gustaria X a mi grupo' o 'deberia jugar X': primero intenta encontrar "
    "categorias/mecanicas/complejidad de X con query_sql (boardgames_bgg.juegos_detalle, incluso via "
    "boardgames_bgg.plays_amigos si X es un juego que solo tus amigos han jugado en BGG); si eso regresa "
    "vacio, usa bgg_lookup como ultimo recurso. IMPORTANTE — se decisivo: en cuanto tengas la metadata "
    "del juego (categorias/mecanicas/peso) Y algo de contexto de que juega el grupo social relevante "
    "(aunque sea agregado/parcial, no una coincidencia perfecta), ESO YA ES SUFICIENTE para dar una "
    "opinion fundamentada — responde con eso, no sigas iterando buscando una confirmacion mas exacta. "
    "El costo de una respuesta razonada con datos parciales es mucho menor que agotar tus intentos sin "
    "responder nada. Si de verdad no encuentras nada relevante en 1-2 intentos, dilo claramente en vez "
    "de seguir buscando o de inventar. "
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
    "Responde en español, de forma directa y concisa. "
    "TRANSPARENCIA DE FUENTE — MUY IMPORTANTE: nunca respondas preguntas de reglas usando tu "
    "conocimiento general/de entrenamiento como respaldo silencioso. Si search_rulebooks regresa "
    "SIN RESULTADOS para el juego preguntado, dilo explicitamente al usuario ('no tengo el "
    "reglamento de [juego] indexado, no puedo responder con precision') en vez de completar la "
    "respuesta con lo que sepas de memoria — las reglas varian por edicion/idioma y una respuesta "
    "generica puede ser incorrecta. Al final de cada respuesta sustantiva, indica brevemente de "
    "donde salio la informacion: 'Fuente: reglamento indexado de [juego]' / 'Fuente: estadisticas "
    "de tu biblioteca' / 'Fuente: busqueda en vivo en BoardGameGeek'. Si de verdad respondes sin "
    "ninguna herramienta (ej. pregunta general que no requiere datos), dilo tambien: 'Fuente: "
    "conocimiento general del modelo, sin reglamento indexado — verifica con el reglamento oficial'."
)


BGG_SEARCH_URL = "https://boardgamegeek.com/xmlapi2/search"


def bgg_buscar_por_nombre(nombre: str) -> str:
    """Ultimo recurso del agente cuando un juego no esta en la biblioteca
    local ni tiene reglamento indexado: busca en vivo en BGG (search +
    thing) y cachea el resultado en boardgames_bgg.juegos_detalle con el
    mismo upsert que bgg_cache_sync.py, para no volver a pedirlo despues."""
    token = os.environ["BGG_API_TOKEN"]
    try:
        resp = requests.get(
            BGG_SEARCH_URL,
            params={"query": nombre, "type": "boardgame"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        return f"Error buscando '{nombre}' en BGG: {e}"

    items = root.findall("item")
    if not items:
        return f"No se encontro '{nombre}' en BoardGameGeek."

    bgg_id = int(items[0].get("id"))
    nombre_el = items[0].find("name")
    nombre_bgg = nombre_el.get("value") if nombre_el is not None else nombre

    try:
        detalle_root = bgg_fetch_batch([bgg_id], token)
        item = detalle_root.find("item")
        if item is None:
            return f"BGG no regreso detalle para '{nombre_bgg}' (bgg_id={bgg_id})."
        d = bgg_parse_item(item)
    except Exception as e:
        return f"Error obteniendo detalle de BGG para '{nombre_bgg}': {e}"

    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor()
        bgg_guardar_detalle(cur, d)
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass  # cachear es best-effort, no debe tumbar la respuesta al usuario

    return json.dumps({"nombre": nombre_bgg, **d}, default=str, ensure_ascii=False)


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


def execute_cypher(cypher_query: str) -> str:
    stripped = cypher_query.strip().rstrip(";")
    peligrosas = ("MERGE", "CREATE", "DELETE", "SET ", "REMOVE", "DETACH")
    if any(kw in stripped.upper() for kw in peligrosas):
        return "Error: solo se permiten consultas de lectura (MATCH/RETURN), no modificaciones."
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL_READONLY"])
        cur = conn.cursor()
        cur.execute('SET search_path = ag_catalog, "$user", public')
        cur.execute(f"SELECT * FROM cypher('red_social', $$ {stripped} $$) AS (resultado agtype) LIMIT 50")
        filas = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        return f"Error en la consulta: {e}"
    if not filas:
        return "SIN RESULTADOS: la consulta no encontro datos en el grafo. NO respondas usando tu conocimiento general."
    return json.dumps([str(f[0]) for f in filas], default=str, ensure_ascii=False)


class HistorialTurno(BaseModel):
    pregunta: str
    respuesta: str


class AskRequest(BaseModel):
    pregunta: str
    juego: str | None = None
    historial: list[HistorialTurno] | None = None


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
    cur.execute("SELECT nombre FROM boardgames_stats.juegos ORDER BY nombre;")
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
        FROM boardgames_stats.juegos j
        LEFT JOIN boardgames_stats.partidas p ON p.juego_uuid = j.uuid
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


PDFS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), params.PDFS_DIR)


BGG_BUSQUEDA_TIPOS = "boardgame,boardgameexpansion"
BGG_BATCH_DETALLE = 20  # tope de ids por llamada a thing (mismo BATCH_SIZE que bgg_cache_sync)
BGG_BUSQUEDA_CACHE_TTL = 600  # 10 min: BGG pide minimizar requests y el autocompletado dispara muchos
_bgg_busqueda_cache: dict[str, tuple[float, list[dict]]] = {}


def _int_o_none(valor) -> int | None:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def bgg_buscar_items(termino: str) -> list[dict]:
    """search + un solo thing en batch por los ids que regreso. El search da
    el match por nombre pero no dice cual es el nombre primario (si pegaste
    con un nombre alterno regresa ese) ni trae imagen/popularidad; el thing
    resuelve las tres cosas en una llamada."""
    token = os.environ["BGG_API_TOKEN"]
    resp = requests.get(
        BGG_SEARCH_URL,
        params={"query": termino, "type": BGG_BUSQUEDA_TIPOS},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    orden: list[int] = []
    base: dict[int, dict] = {}
    for item in root.findall("item"):
        try:
            bgg_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if bgg_id in base:
            continue
        nombre_el = item.find("name[@type='primary']")
        if nombre_el is None:
            nombre_el = item.find("name")
        anio_el = item.find("yearpublished")
        orden.append(bgg_id)
        base[bgg_id] = {
            "bgg_id": bgg_id,
            "nombre_bgg": nombre_el.get("value") if nombre_el is not None else termino,
            "anio": _int_o_none(anio_el.get("value") if anio_el is not None else None),
            "es_expansion": item.get("type") == "boardgameexpansion",
            "thumbnail": None,
            "num_calificaciones": None,
        }

    # BGG regresa el search sin ordenar por relevancia (para "carcassonne" salen
    # primero fan-expansions de 5 votos y el juego base ni aparece en los primeros).
    # Se pre-ordena por parecido del nombre antes de recortar, porque el thing en
    # batch — que es el que trae la popularidad para el orden final — no puede
    # pedir los cientos de ids que a veces regresa el search.
    t = termino.lower()
    posicion = {bgg_id: i for i, bgg_id in enumerate(orden)}

    def parecido(bgg_id: int) -> tuple:
        nombre = base[bgg_id]["nombre_bgg"].lower()
        return (nombre != t, not nombre.startswith(t), len(nombre), posicion[bgg_id])

    ids = sorted(orden, key=parecido)[:BGG_BATCH_DETALLE]
    if not ids:
        return []

    try:
        detalle_root = bgg_fetch_batch(ids, token)
    except Exception:
        return [base[i] for i in ids]  # sin detalle igual sirve: nombre + año del search

    for item in detalle_root.findall("item"):
        bgg_id = _int_o_none(item.get("id"))
        if bgg_id not in base:
            continue
        d = base[bgg_id]
        nombre_el = item.find("name[@type='primary']")
        if nombre_el is not None:
            d["nombre_bgg"] = nombre_el.get("value")
        anio_el = item.find("yearpublished")
        if anio_el is not None:
            d["anio"] = _int_o_none(anio_el.get("value"))
        thumb_el = item.find("thumbnail")
        if thumb_el is not None and thumb_el.text:
            d["thumbnail"] = thumb_el.text.strip()
        d["es_expansion"] = item.get("type") == "boardgameexpansion"
        rated_el = item.find("statistics/ratings/usersrated")
        if rated_el is not None:
            d["num_calificaciones"] = _int_o_none(rated_el.get("value"))

    return [base[i] for i in ids]


def bgg_buscar_enriquecido(termino: str) -> list[dict]:
    """Busca por nombre en vivo en BGG y regresa la lista ya cruzada contra tu
    biblioteca/indexados y ordenada por relevancia real (nombre exacto >
    prefijo > ya la tienes > popularidad). Usada por /juegos/bgg-buscar, el
    picker de juego de 'Agregar reglamento'."""
    clave = termino.lower()
    cacheado = _bgg_busqueda_cache.get(clave)
    if cacheado and time.time() - cacheado[0] < BGG_BUSQUEDA_CACHE_TTL:
        resultados = cacheado[1]
    else:
        resultados = bgg_buscar_items(termino)
        _bgg_busqueda_cache[clave] = (time.time(), resultados)

    if not resultados:
        return []

    ids = [r["bgg_id"] for r in resultados]
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        "SELECT bgg_id, nombre FROM boardgames_stats.juegos WHERE bgg_id = ANY(%s);", (ids,)
    )
    en_biblioteca = {row[0]: row[1] for row in cur.fetchall()}
    cur.execute(
        f"SELECT DISTINCT juego FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE} WHERE juego = ANY(%s);",
        (list(set(list(en_biblioteca.values()) + [r["nombre_bgg"] for r in resultados])),),
    )
    indexados = {row[0] for row in cur.fetchall()}
    cur.close()
    conn.close()

    salida = []
    for r in resultados:
        nombre_local = en_biblioteca.get(r["bgg_id"])
        salida.append({
            **r,
            "nombre": nombre_local or r["nombre_bgg"],
            "en_biblioteca": nombre_local is not None,
            "ya_indexado": (nombre_local or r["nombre_bgg"]) in indexados,
        })

    termino_lower = termino.lower()
    salida.sort(key=lambda r: (
        r["nombre"].lower() != termino_lower,
        not r["nombre"].lower().startswith(termino_lower),
        not r["en_biblioteca"],
        -(r["num_calificaciones"] or 0),
    ))
    return salida


@app.get("/juegos/bgg-buscar")
def bgg_buscar(q: str, limite: int = 12):
    """Busca por nombre en vivo en BGG para que al agregar un reglamento se
    elija el juego de una lista real en vez de teclearlo a mano y terminar con
    dos variantes del mismo juego indexadas.

    Lo que hace el import 'limpio' es el cruce por bgg_id contra tu biblioteca:
    si el juego ya esta en BG Stats se regresa el nombre local (el que usan los
    chunks ya indexados), no el de BGG, para que el reglamento nuevo quede
    colgado del mismo juego. `ya_indexado` avisa si ya hay chunks con ese
    nombre, que es justo lo que rechaza /reglamentos/subir con 409."""
    termino = q.strip()
    if len(termino) < 3:
        raise HTTPException(status_code=400, detail="Escribe al menos 3 letras para buscar en BGG.")
    limite = max(1, min(limite, 30))
    try:
        resultados = bgg_buscar_enriquecido(termino)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"BoardGameGeek no respondio: {e}")
    return resultados[:limite]


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
    (jugador_uuid, con perfil en boardgames_stats.jugadores) — excluye nombres sueltos
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
        FROM boardgames_stats.partida_jugadores pj
        LEFT JOIN boardgames_stats.jugadores j ON j.uuid = pj.jugador_uuid
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
        FROM boardgames_stats.partidas
        """
    )
    partidas, juegos_distintos, horas_totales, primera, ultima = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM boardgames_stats.juegos WHERE es_propio")
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
                SELECT partida_uuid, COUNT(*) AS n FROM boardgames_stats.partida_jugadores GROUP BY partida_uuid
            ),
            partida_tiene_bot AS (
                SELECT DISTINCT pj.partida_uuid
                FROM boardgames_stats.partida_jugadores pj
                LEFT JOIN boardgames_stats.jugadores j ON j.uuid = pj.jugador_uuid
                WHERE {FILTRO_NO_BOT.replace('NOT LIKE', 'LIKE')}
            ),
            partidas_solo AS (
                SELECT p.uuid, p.juego_uuid, p.duracion_min, p.tag_digital
                FROM boardgames_stats.partidas p
                LEFT JOIN conteo_jugadores cj ON cj.partida_uuid = p.uuid
                WHERE p.tag_solo
                   OR p.uuid IN (SELECT partida_uuid FROM partida_tiene_bot)
                   OR COALESCE(cj.n, 0) <= 1
            )
            SELECT g.nombre, COUNT(*) AS partidas, ROUND(SUM(ps.duracion_min) / 60.0, 1) AS horas,
                   BOOL_OR(ps.tag_digital) AS digital
            FROM partidas_solo ps
            JOIN boardgames_stats.juegos g ON g.uuid = ps.juego_uuid
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
            FROM boardgames_stats.partidas p
            JOIN boardgames_stats.juegos g ON g.uuid = p.juego_uuid
            JOIN boardgames_stats.partida_jugadores pj ON pj.partida_uuid = p.uuid
            LEFT JOIN boardgames_stats.jugadores j ON j.uuid = pj.jugador_uuid
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
        FROM boardgames_stats.partidas p JOIN boardgames_stats.juegos j ON j.uuid = p.juego_uuid
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
        FROM boardgames_stats.partidas GROUP BY dow ORDER BY dow
        """
    )
    conteo_por_dia = dict(cur.fetchall())

    # probabilidad de jugar por dia de la semana (dias distintos con partida /
    # dias totales de ese tipo en el rango de fechas) -- separado de conteo de
    # partidas porque un dia con 5 partidas no es "mas probable" que uno con 1,
    # y comparado contra la red de amigos (boardgames_bgg.plays_amigos) para ver si
    # el circulo social en general tiene un patron distinto al propio (sesion
    # 2026-08-13, union solo en memoria aqui, nunca en Postgres)
    cur.execute("SELECT DISTINCT fecha::date FROM boardgames_stats.partidas")
    fechas_propias = set(r[0] for r in cur.fetchall())
    cur.execute("SELECT DISTINCT fecha FROM boardgames_bgg.plays_amigos WHERE usable_para_analisis")
    fechas_amigos = set(r[0] for r in cur.fetchall())

    todas_las_fechas = fechas_propias | fechas_amigos
    por_dia_semana = []
    if todas_las_fechas:
        fecha_min, fecha_max = min(todas_las_fechas), max(todas_las_fechas)
        dias_calendario_por_dow: dict[int, int] = {}
        d = fecha_min
        while d <= fecha_max:
            dow = (d.weekday() + 1) % 7  # python 0=lunes -> postgres 0=domingo
            dias_calendario_por_dow[dow] = dias_calendario_por_dow.get(dow, 0) + 1
            d += datetime.timedelta(days=1)

        dias_jugados_propios = {}
        for f in fechas_propias:
            dow = (f.weekday() + 1) % 7
            dias_jugados_propios[dow] = dias_jugados_propios.get(dow, 0) + 1
        dias_jugados_amigos = {}
        for f in fechas_amigos:
            dow = (f.weekday() + 1) % 7
            dias_jugados_amigos[dow] = dias_jugados_amigos.get(dow, 0) + 1

        for dow in range(7):
            total_dias = dias_calendario_por_dow.get(dow, 0)
            por_dia_semana.append(
                {
                    "dia": DIAS_SEMANA[dow],
                    "partidas": conteo_por_dia.get(dow, 0),
                    "probabilidad": round(dias_jugados_propios.get(dow, 0) / total_dias, 3) if total_dias else 0,
                    "probabilidad_amigos": round(dias_jugados_amigos.get(dow, 0) / total_dias, 3) if total_dias else 0,
                }
            )

    cur.execute(
        """
        SELECT to_char(fecha, 'YYYY-MM') AS mes, COUNT(*)
        FROM boardgames_stats.partidas
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
        FROM boardgames_stats.partidas p
        JOIN boardgames_stats.lugares l ON l.uuid = p.lugar_uuid
        JOIN boardgames_stats.clima_diario c ON c.lugar_uuid = l.uuid AND c.fecha = p.fecha::date
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
        FROM boardgames_stats.partidas p JOIN boardgames_stats.lugares l ON l.uuid = p.lugar_uuid
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
        FROM boardgames_stats.juegos j
        WHERE es_propio AND NOT es_expansion
          AND NOT EXISTS (SELECT 1 FROM boardgames_stats.partidas p WHERE p.juego_uuid = j.uuid)
          AND NOT EXISTS (SELECT 1 FROM boardgames_stats.partidas p WHERE j.uuid = ANY(p.expansiones_usadas))
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
        FROM boardgames_stats.colecciones c
        JOIN boardgames_stats.juegos j ON j.uuid = c.juego_uuid
        """
    )
    gasto_total, copias_propias, copias_ya_no_tiene, en_wishlist = cur.fetchone()

    cur.execute(
        """
        SELECT COUNT(*) FILTER (WHERE es_propio AND NOT es_expansion),
               COUNT(*) FILTER (WHERE es_propio AND NOT es_expansion AND NOT EXISTS (
                   SELECT 1 FROM boardgames_stats.partidas p WHERE p.juego_uuid = j.uuid
               ) AND NOT EXISTS (
                   SELECT 1 FROM boardgames_stats.partidas p WHERE j.uuid = ANY(p.expansiones_usadas)
               ))
        FROM boardgames_stats.juegos j
        """
    )
    juegos_propios_total, juegos_propios_sin_jugar = cur.fetchone()

    cur.execute(
        """
        SELECT COALESCE(categoria_compra, 'sin_categoria'), ROUND(SUM(price_paid_mxn)::numeric, 2), COUNT(*)
        FROM boardgames_stats.colecciones
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
        FROM boardgames_stats.colecciones
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
        FROM boardgames_stats.juegos j
        JOIN boardgames_bgg.juegos_detalle d ON d.bgg_id = j.bgg_id
        WHERE d.peso_complejidad IS NOT NULL AND NOT j.es_expansion
        ORDER BY j.nombre
        """
    )
    result = [{"nombre": r[0], "min_jugadores": r[1], "max_jugadores": r[2]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return result


@app.get("/bgstats/duracion/entrenamiento")
def bgstats_duracion_entrenamiento(incluir_amigos: bool = False):
    """Diagnostico del modelo de duracion: MAE de cada candidato, MAE del
    baseline (promedio simple) para comparar, y coeficientes activos.
    incluir_amigos=False (default): se probo sumar partidas de boardgames_bgg.plays_amigos
    (union solo en memoria, esa tabla nunca se fusiona con boardgames_stats.partidas) y no
    mejoro el modelo (MAE 16.28 sin amigos vs 16.58 con amigos, sesion 2026-08-13,
    muestra chica: ~37 de 1589 filas). Se deja el parametro por si crece la muestra."""
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


# mismas coords de "Casa" reusadas en todo el proyecto (coffee, clima_sync.py,
# bgg_friend_clima_sync.py) -- la mayoria de las partidas propias son ahi.
# Corregidas 2026-08-13: el valor anterior (19.4326, -99.1332, probablemente
# un placeholder/aproximacion inicial) estaba a ~1.9km de la ubicacion real
# confirmada por Alberto via Plus Code (76F2CRPX+VJ).
CASA_LAT, CASA_LON = 19.4371875, -99.1509375


def obtener_temperatura_actual() -> float | None:
    """Clima real AHORA (Open-Meteo forecast, no historico) para la prediccion
    de duracion. Si falla (API caida, timeout), regresa None y predecir_duracion
    cae de vuelta a la mediana historica -- nunca debe tumbar la prediccion."""
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": CASA_LAT, "longitude": CASA_LON, "current": "temperature_2m"},
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()["current"]["temperature_2m"]
    except (requests.RequestException, KeyError, ValueError):
        return None


@app.get("/bgstats/duracion/predecir")
def bgstats_duracion_predecir(
    juego: str, num_jugadores: int, lugar_categoria: str | None = None, grupo_social: str | None = None,
    usa_expansion: bool = False, incluir_amigos: bool = False,
):
    """Predice duracion_min para un juego (por nombre) + numero de
    jugadores + categoria de lugar opcional (ver duracion_model.CATEGORIAS_LUGAR)
    + grupo social opcional (ver duracion_model.CATEGORIAS_GRUPO). temp_media_c
    usa el clima real actual (Open-Meteo, coords de Casa) si el request
    responde a tiempo; si no, cae a la mediana historica. tag_digital usa el
    valor tipico (mediana) ya que no se conoce de antemano para una partida futura."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.peso_complejidad, d.dependencia_idioma, d.min_playtime, d.max_playtime, d.calificacion_promedio
        FROM boardgames_stats.juegos j
        JOIN boardgames_bgg.juegos_detalle d ON d.bgg_id = j.bgg_id
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
    valores = {
        "peso_complejidad": peso,
        "dependencia_idioma": dependencia,
        "calificacion_promedio": calificacion,
        "num_jugadores": num_jugadores,
        "min_playtime": min_pt,
        "max_playtime": max_pt,
        "usa_expansion": float(usa_expansion),
    }
    temp_actual = obtener_temperatura_actual()
    if temp_actual is not None:
        valores["temp_media_c"] = temp_actual
    estimado = predecir_duracion(
        r, valores, categoria_lugar=lugar_categoria, grupo_social=grupo_social,
    )
    return {
        "juego": juego, "num_jugadores": num_jugadores,
        "lugar_categoria": lugar_categoria, "grupo_social": grupo_social,
        "duracion_estimada_min": round(estimado), "mae_modelo": round(r["mae_por_modelo"][r["ganador"]], 1),
    }


@app.get("/bgstats/duracion-solo/entrenamiento")
def bgstats_duracion_solo_entrenamiento():
    """Diagnostico del modelo de duracion en MODO SOLITARIO (tag_solo=true),
    ver duracion_solo_model.py. Dataset mas chico (~199 filas) que el modelo
    normal, feature set mas simple (sin lugar/grupo social, casi todo solo
    es en casa)."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    r = entrenar_duracion_solo(conn)
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


@app.get("/bgstats/duracion-solo/predecir")
def bgstats_duracion_solo_predecir(juego: str):
    """Predice duracion_min para un juego (por nombre) en modo solitario.
    min_jugadores/max_jugadores (BGG) distinguen juegos solo puros (ej.
    GROVE) de multijugador jugado con Automa (ej. Terraforming Mars) --
    feature mas fuerte del modelo. temp_media_c usa clima real actual
    (Open-Meteo) igual que el modelo normal."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.peso_complejidad, d.min_playtime, d.max_playtime, d.calificacion_promedio,
               j.min_jugadores, j.max_jugadores
        FROM boardgames_stats.juegos j
        JOIN boardgames_bgg.juegos_detalle d ON d.bgg_id = j.bgg_id
        WHERE j.nombre = %s
        LIMIT 1
        """,
        (juego,),
    )
    fila = cur.fetchone()
    if fila is None:
        conn.close()
        raise HTTPException(status_code=404, detail=f"'{juego}' no tiene datos de BGG cacheados")

    r = entrenar_duracion_solo(conn)
    conn.close()
    if r is None:
        raise HTTPException(status_code=422, detail="No hay suficientes datos para entrenar el modelo")

    peso, min_pt, max_pt, calificacion, min_j, max_j = fila
    valores = {
        "peso_complejidad": peso,
        "min_playtime": min_pt,
        "max_playtime": max_pt,
        "calificacion_promedio": calificacion,
        "min_jugadores": min_j,
        "max_jugadores": max_j,
    }
    temp_actual = obtener_temperatura_actual()
    if temp_actual is not None:
        valores["temp_media_c"] = temp_actual
    estimado = predecir_duracion_solo(r, valores)
    return {
        "juego": juego,
        "duracion_estimada_min": round(estimado),
        "mae_modelo": round(r["mae_por_modelo"][r["ganador"]], 1),
    }


@app.get("/bgstats/amigos/pendientes")
def bgstats_amigos_pendientes():
    """Amigos con bgg_username detectado en el ultimo sync de BG Stats que
    aun no se han revisado (ver boardgames_bgg.amigos_nuevos_pendientes, poblada por
    bgstats_sync.py en cada corrida). El frontend los muestra como alerta y
    limpia la bandera via /bgstats/amigos/pendientes/{bgg_username}/revisar."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT bgg_username, jugador_nombre, detectado_en
        FROM boardgames_bgg.amigos_nuevos_pendientes
        WHERE NOT revisado
        ORDER BY detectado_en
        """
    )
    result = [
        {"bgg_username": r[0], "jugador_nombre": r[1], "detectado_en": r[2].isoformat()}
        for r in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return result


@app.post("/bgstats/amigos/pendientes/{bgg_username}/revisar")
def bgstats_amigo_revisar(bgg_username: str):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        "UPDATE boardgames_bgg.amigos_nuevos_pendientes SET revisado = TRUE, revisado_en = now() WHERE bgg_username = %s",
        (bgg_username,),
    )
    conn.commit()
    encontrado = cur.rowcount > 0
    cur.close()
    conn.close()
    if not encontrado:
        raise HTTPException(status_code=404, detail=f"No existe {bgg_username} en pendientes")
    return {"bgg_username": bgg_username, "revisado": True}


@app.get("/bgstats/lugares/pendientes")
def bgstats_lugares_pendientes():
    """Fuentes de compra sin alias o lugares de partida nuevos detectados en
    el ultimo sync (ver boardgames_stats.lugares_pendientes_revision, poblada por
    bgstats_sync.py). tipo es 'compra' o 'lugar_partida'. El frontend los
    muestra como alerta y limpia la bandera via /bgstats/lugares/pendientes/revisar."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT tipo, valor, detectado_en
        FROM boardgames_stats.lugares_pendientes_revision
        WHERE NOT revisado
        ORDER BY detectado_en
        """
    )
    result = [
        {"tipo": r[0], "valor": r[1], "detectado_en": r[2].isoformat()}
        for r in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return result


@app.post("/bgstats/lugares/pendientes/revisar")
def bgstats_lugar_revisar(tipo: str, valor: str):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE boardgames_stats.lugares_pendientes_revision SET revisado = TRUE, revisado_en = now()
        WHERE tipo = %s AND valor = %s
        """,
        (tipo, valor),
    )
    conn.commit()
    encontrado = cur.rowcount > 0
    cur.close()
    conn.close()
    if not encontrado:
        raise HTTPException(status_code=404, detail=f"No existe {tipo}/{valor} en pendientes")
    return {"tipo": tipo, "valor": valor, "revisado": True}


@app.get("/bgstats/anonimos/pendientes")
def bgstats_anonimos_pendientes():
    """Partidas con el jugador anonimo generico donde no se pudo inferir
    grupo_social solo (mixto: 2+ grupos entre los nombrados: sin_senal:
    nadie nombrado tiene grupo ni el lugar tiene grupo_social_lugar). Ver
    boardgames_stats.anonimos_pendientes_agrupar, poblada por bgstats_sync.py. El
    frontend deja elegir el grupo y llama a
    /bgstats/anonimos/pendientes/revisar para resolverlo."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.partida_uuid, a.tipo, p.fecha, jg.nombre, l.nombre,
               (
                   SELECT string_agg(j.nombre || ' (' || j.grupo_social || ')', ', ')
                   FROM boardgames_stats.partida_jugadores pj
                   JOIN boardgames_stats.jugadores j ON j.uuid = pj.jugador_uuid
                   WHERE pj.partida_uuid = a.partida_uuid AND j.grupo_social IS NOT NULL
               ) AS jugadores_con_grupo
        FROM boardgames_stats.anonimos_pendientes_agrupar a
        JOIN boardgames_stats.partidas p ON p.uuid = a.partida_uuid
        JOIN boardgames_stats.juegos jg ON jg.uuid = p.juego_uuid
        LEFT JOIN boardgames_stats.lugares l ON l.uuid = p.lugar_uuid
        WHERE NOT a.revisado
        ORDER BY a.detectado_en
        """
    )
    result = [
        {
            "partida_uuid": str(r[0]), "tipo": r[1], "fecha": r[2].isoformat(),
            "juego": r[3], "lugar": r[4], "jugadores_con_grupo": r[5],
        }
        for r in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return result


@app.post("/bgstats/anonimos/pendientes/revisar")
def bgstats_anonimo_revisar(partida_uuid: str, grupo_social: str):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO boardgames_stats.partida_grupo_social_override (partida_uuid, grupo_social)
        VALUES (%s, %s)
        ON CONFLICT (partida_uuid) DO UPDATE SET grupo_social = EXCLUDED.grupo_social
        """,
        (partida_uuid, grupo_social),
    )
    cur.execute(
        """
        UPDATE boardgames_stats.anonimos_pendientes_agrupar SET revisado = TRUE, revisado_en = now()
        WHERE partida_uuid = %s
        """,
        (partida_uuid,),
    )
    conn.commit()
    encontrado = cur.rowcount > 0
    cur.close()
    conn.close()
    if not encontrado:
        raise HTTPException(status_code=404, detail=f"No existe {partida_uuid} en pendientes")
    return {"partida_uuid": partida_uuid, "grupo_social": grupo_social, "revisado": True}


@app.post("/bgstats/sync")
def bgstats_sync_endpoint():
    """Sync completo disparado por n8n en cada export nuevo. Encadena, en orden,
    todo lo que antes solo corria manual via 'python source/bgstats_sync.py':
    partidas/juegos/jugadores -> cache de BGG -> partidas de amigos -> grafo
    social. Cada paso extra va en su propio try/except -- si falla (red, API
    caida) no debe tumbar el sync principal de BG Stats."""
    if not os.path.exists(BGSTATS_EXPORT_PATH):
        raise HTTPException(status_code=404, detail=f"No existe {BGSTATS_EXPORT_PATH}")
    resultado = bgstats_sync(BGSTATS_EXPORT_PATH)
    try:
        resultado["calendario"] = calendario_sync()
    except Exception as e:
        # los calendarios de iCloud son un extra sobre el sync principal de BG Stats;
        # si fallan (link vencido, red) no debe tumbar la sincronizacion de partidas/juegos
        resultado["calendario_error"] = str(e)
    try:
        resultado["bgg_cache"] = bgg_cache_sync()
    except Exception as e:
        resultado["bgg_cache_error"] = str(e)
    try:
        resultado["amigos_bgg"] = sync_todos_los_amigos()
    except Exception as e:
        resultado["amigos_bgg_error"] = str(e)
    try:
        resultado["grafo_social"] = grafo_social_sync()
    except Exception as e:
        resultado["grafo_social_error"] = str(e)
    return resultado


MAX_TURNOS_HISTORIAL = 6  # turnos previos a mandar de vuelta al modelo, no todo el chat


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    contents = []
    for turno in (req.historial or [])[-MAX_TURNOS_HISTORIAL:]:
        contents.append(types.Content(role="user", parts=[types.Part(text=turno.pregunta)]))
        contents.append(types.Content(role="model", parts=[types.Part(text=turno.respuesta)]))
    contents.append(types.Content(role="user", parts=[types.Part(text=req.pregunta)]))
    fuentes: list[dict] = []

    system_instruction = SYSTEM_PROMPT
    if req.juego:
        system_instruction += (
            f"\n\nEl juego del que se esta hablando en esta conversacion es: {req.juego}. "
            f"Sus expansiones/modulos (que pueden aparecer con otro nombre en los resultados de "
            f"busqueda) tambien cuentan como parte de {req.juego}."
        )

    MAX_ITERACIONES_HERRAMIENTAS = 8
    iteraciones = 0
    while True:
        iteraciones += 1
        if iteraciones > MAX_ITERACIONES_HERRAMIENTAS:
            raise HTTPException(
                status_code=502,
                detail="El modelo no pudo responder tras varios intentos de consulta. Intenta reformular la pregunta.",
            )
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

                if resumen:
                    contenido = "\n\n".join(
                        f"[{etiqueta(r)} | chunk {r['chunk_index']} | doc_type={r['doc_type']}]\n{r['texto']}"
                        for r in resumen
                    )
                else:
                    contenido = (
                        "SIN RESULTADOS: no hay reglamento indexado para este juego/pregunta. "
                        "NO respondas usando tu conocimiento general del entrenamiento — dile al "
                        "usuario explicitamente que no tienes el reglamento indexado."
                    )
            elif fc.name == "query_sql":
                contenido = execute_sql(fc.args["sql"])
            elif fc.name == "query_graph":
                contenido = execute_cypher(fc.args["cypher"])
            elif fc.name == "bgg_lookup":
                contenido = bgg_buscar_por_nombre(fc.args["nombre"])
            else:
                contenido = f"Herramienta desconocida: {fc.name}"

            function_response_parts.append(
                types.Part.from_function_response(name=fc.name, response={"resultados": contenido})
            )
        contents.append(types.Content(role="user", parts=function_response_parts))
