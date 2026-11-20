"""
Descarga TODAS las partidas que un amigo registro directo en BGG (no en BG
Stats) via el endpoint 'plays' del XML API2, y las guarda tal cual en
boardgames_bgg.plays_amigos -- staging propio, NUNCA se fusiona con boardgames_stats.partidas
(decision explicita de Alberto, sesion 2026-08-13). Sirve solo para analizarlo
por su cuenta.

En cada corrida:
1. Busca en boardgames_stats.jugadores quien tiene bgg_username registrado (columna que
   ya llena bgstats_sync.py) -- no hace falta mantener una lista aparte.
2. Descarga/actualiza sus partidas (upsert por bgg_play_id, idempotente).
3. Normaliza ubicacion contra boardgames_bgg.ubicaciones_amigos_alias (mismo patron
   que boardgames_stats.fuentes_compra_alias) -- lo que no matchea queda "Otros" si
   tenia texto, o NULL si no tenia ubicacion capturada del lado de BGG.
4. Marca es_partida_propia (Alberto aparece como jugador -- variantes de
   nombre conocidas, ver NOMBRES_ALBERTO) y usable_para_analisis (ni
   incompleta ni propia) -- no borra nada, solo marca para que un query
   futuro filtre facil con WHERE usable_para_analisis.

Usuarios EXTERNOS (desconocidos, sin grupo_social -- 2026-11-20): misma descarga, pero a
boardgames_bgg.plays_externos, con la lista en boardgames_bgg.usuarios_externos. Se separan a
proposito de plays_amigos para no meter desconocidos al grafo red_social, al clima de amigos
ni al feature media_juego_amigos. Sirven solo para enriquecer stats/ML.

Uso:
    python source/bgg_friend_plays_sync.py --username VINICIO   # uno solo
    python source/bgg_friend_plays_sync.py --todos               # todos los amigos con bgg_username
    python source/bgg_friend_plays_sync.py --agregar-externo user1 user2   # da de alta y sincroniza
    python source/bgg_friend_plays_sync.py --externos            # re-sincroniza todos los externos activos
"""

import argparse
import json
import os
import time
import xml.etree.ElementTree as ET

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

PLAYS_URL = "https://boardgamegeek.com/xmlapi2/plays"

# variantes de nombre encontradas hasta ahora en logs de amigos (sesion
# 2026-08-13) -- si aparece una nueva variante, agregarla aqui
MI_NOMBRE = "Alberto Qu"
NOMBRES_ALBERTO = {"Albert Qu", "Albert Qu (Kinky)", MI_NOMBRE, "HL Albert Q", "Alberto ET"}


def parse_play(play: ET.Element) -> dict:
    item = play.find("item")
    jugadores = [
        {
            "nombre": p.get("name"),
            "username": p.get("username") or None,
            "score": p.get("score"),
            "gano": p.get("win") == "1",
            "nuevo": p.get("new") == "1",
            "posicion_inicial": p.get("startposition") or None,
        }
        for p in (play.find("players").findall("player") if play.find("players") is not None else [])
    ]
    comentarios_el = play.find("comments")

    return {
        "bgg_play_id": int(play.get("id")),
        "fecha": play.get("date"),
        "juego": item.get("name") if item is not None else None,
        "bgg_game_id": int(item.get("objectid")) if item is not None and item.get("objectid") else None,
        "ubicacion": (play.get("location") or "").strip() or None,
        "comentarios": comentarios_el.text if comentarios_el is not None else None,
        "duracion_min": int(play.get("length")) if play.get("length") not in (None, "") else None,
        "cantidad": int(play.get("quantity")) if play.get("quantity") not in (None, "") else None,
        "incompleta": play.get("incomplete") == "1",
        "jugadores": jugadores,
        "datos_extra": {k: v for k, v in play.attrib.items()},
    }


def fetch_all_plays(username: str, token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    todas = []
    page = 1
    while True:
        for intento in range(6):
            resp = requests.get(PLAYS_URL, params={"username": username, "page": page}, headers=headers, timeout=20)
            # 429 = rate limit de BGG, 202 = respuesta en cola: esperar y reintentar con backoff
            if resp.status_code not in (429, 202, 503):
                break
            time.sleep(5 * 2 ** intento)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        plays = root.findall("play")
        if not plays:
            break
        todas.extend(parse_play(p) for p in plays)
        if len(plays) < 100:
            break
        page += 1
        time.sleep(2)
    return todas


def cargar_alias_ubicaciones(cur) -> dict:
    cur.execute("SELECT ubicacion_raw, ubicacion_normalizada, categoria_lugar FROM boardgames_bgg.ubicaciones_amigos_alias")
    return {raw: (norm, cat) for raw, norm, cat in cur.fetchall()}


def normalizar_ubicacion(ubicacion: str | None, alias: dict) -> tuple[str | None, str | None]:
    if not ubicacion:
        return None, None
    if ubicacion in alias:
        return alias[ubicacion]
    return "Otros", "otros"


def es_partida_propia(jugadores: list[dict]) -> bool:
    return any(j["nombre"] in NOMBRES_ALBERTO for j in jugadores)


# patrones de nombre de bots/Automa encontrados en logs de amigos (sesion 2026-08-13):
# "B_<algo>" (bots de modo solitario tipo Automa con prefijo por juego, ej. "B_FormulaD Am2"),
# "Automa" a secas, y "Bot <algo>" (ej. "Bot T-800"). Si aparece alguno, es partida en modo
# solitario contra la IA del juego, no multijugador real -- afecta duracion_min y num_jugadores.
def es_partida_solo(jugadores: list[dict]) -> bool:
    return any(
        j["nombre"].startswith("B_") or j["nombre"].lower() == "automa" or j["nombre"].lower().startswith("bot ")
        for j in jugadores
    )


# plataformas digitales encontradas como ubicacion en logs de amigos (sesion 2026-08-13):
# "BGA" = Board Game Arena. Si aparece una nueva variante agregarla aqui.
UBICACIONES_DIGITALES = {"bga", "board game arena", "tabletopia", "tabletop simulator"}


def es_partida_digital(ubicacion: str | None) -> bool:
    return bool(ubicacion) and ubicacion.strip().lower() in UBICACIONES_DIGITALES


TABLAS_DESTINO = {"amigos": "boardgames_bgg.plays_amigos", "externos": "boardgames_bgg.plays_externos"}


def sync(username: str, destino: str = "amigos") -> dict:
    tabla = TABLAS_DESTINO[destino]
    token = os.environ["BGG_API_TOKEN"]
    plays = fetch_all_plays(username, token)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    alias = cargar_alias_ubicaciones(cur)

    for p in plays:
        ubic_norm, categoria = normalizar_ubicacion(p["ubicacion"], alias)
        propia = es_partida_propia(p["jugadores"])
        solo = es_partida_solo(p["jugadores"])
        digital = es_partida_digital(p["ubicacion"])
        usable = (not p["incompleta"]) and (not propia)
        cur.execute(
            f"""
            INSERT INTO {tabla}
                (bgg_play_id, bgg_username, fecha, juego, bgg_game_id, ubicacion,
                 comentarios, duracion_min, cantidad, incompleta, jugadores, datos_extra,
                 ubicacion_normalizada, categoria_lugar, es_partida_propia, usable_para_analisis,
                 tag_solo, tag_digital)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (bgg_play_id) DO UPDATE SET
                fecha = EXCLUDED.fecha, juego = EXCLUDED.juego, bgg_game_id = EXCLUDED.bgg_game_id,
                ubicacion = EXCLUDED.ubicacion, comentarios = EXCLUDED.comentarios,
                duracion_min = EXCLUDED.duracion_min, cantidad = EXCLUDED.cantidad,
                incompleta = EXCLUDED.incompleta, jugadores = EXCLUDED.jugadores,
                datos_extra = EXCLUDED.datos_extra, sincronizado_en = now(),
                ubicacion_normalizada = EXCLUDED.ubicacion_normalizada,
                categoria_lugar = EXCLUDED.categoria_lugar,
                es_partida_propia = EXCLUDED.es_partida_propia,
                usable_para_analisis = EXCLUDED.usable_para_analisis,
                tag_solo = EXCLUDED.tag_solo,
                tag_digital = EXCLUDED.tag_digital
            """,
            (
                p["bgg_play_id"], username, p["fecha"], p["juego"], p["bgg_game_id"], p["ubicacion"],
                p["comentarios"], p["duracion_min"], p["cantidad"], p["incompleta"],
                json.dumps(p["jugadores"]), json.dumps(p["datos_extra"]),
                ubic_norm, categoria, propia, usable, solo, digital,
            ),
        )
    if destino == "externos":
        cur.execute(
            """UPDATE boardgames_bgg.usuarios_externos
               SET ultimo_sync = now(), partidas_ultimo_sync = %s WHERE bgg_username = %s""",
            (len(plays), username),
        )
    conn.commit()
    cur.close()
    conn.close()
    return {"username": username, "partidas_descargadas": len(plays)}


def sync_todos_los_amigos() -> dict:
    """Busca en boardgames_stats.jugadores quien tiene bgg_username y sincroniza a
    todos -- se usa en cada corrida de bgstats_sync.py para detectar amigos
    nuevos sin mantener una lista aparte."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT bgg_username FROM boardgames_stats.jugadores
        WHERE bgg_username IS NOT NULL AND TRIM(bgg_username) != '' AND nombre != %s
        """,
        (MI_NOMBRE,),  # excluye el propio usuario, esta tabla es solo de amigos
    )
    usuarios = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    resultados = {}
    for u in usuarios:
        try:
            r = sync(u)
            resultados[u] = r["partidas_descargadas"]
        except Exception as e:
            resultados[u] = f"error: {e}"
        time.sleep(1)
    return resultados


def perfil_usuario(username: str) -> dict | None:
    """Perfil publico de BGG (None si no existe). BGG solo expone pais y estado, no ciudad."""
    for intento in range(6):
        resp = requests.get(
            "https://boardgamegeek.com/xmlapi2/user", params={"name": username},
            headers={"Authorization": f"Bearer {os.environ['BGG_API_TOKEN']}"}, timeout=20,
        )
        # mismo backoff que fetch_all_plays: 429 = rate limit, 202 = en cola
        if resp.status_code not in (429, 202, 503):
            break
        time.sleep(5 * 2 ** intento)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    if not root.get("id"):
        return None

    def valor(tag):
        el = root.find(tag)
        return ((el.get("value") if el is not None else "") or "").strip() or None

    return {"pais": valor("country"), "estado": valor("stateorprovince")}


def usuario_existe(username: str) -> bool:
    return perfil_usuario(username) is not None


_GEOCODE_CACHE: dict = {}

# BGG no expone ciudad, solo estado (texto libre). Decision de Alberto 2026-11-20: usar la
# CAPITAL del estado como punto para clima/temporada (mas realista que el centroide, que
# puede caer en sierra/desierto). Llaves normalizadas (minusculas, sin acentos).
CAPITALES_MX = {
    "aguascalientes": "Aguascalientes", "baja california": "Mexicali",
    "baja california sur": "La Paz", "campeche": "San Francisco de Campeche",
    "chiapas": "Tuxtla Gutiérrez", "chihuahua": "Chihuahua",
    "ciudad de mexico": "Ciudad de México", "mexico city": "Ciudad de México", "cdmx": "Ciudad de México",
    "distrito federal": "Ciudad de México", "coahuila": "Saltillo", "colima": "Colima",
    "durango": "Victoria de Durango", "estado de mexico": "Toluca de Lerdo", "mexico": "Toluca de Lerdo",
    "edomex": "Toluca de Lerdo", "edo. mex": "Toluca de Lerdo", "guanajuato": "Guanajuato",
    "guerrero": "Chilpancingo de los Bravo", "hidalgo": "Pachuca de Soto", "jalisco": "Guadalajara",
    "michoacan": "Morelia", "morelos": "Cuernavaca", "nayarit": "Tepic", "nuevo leon": "Monterrey",
    "oaxaca": "Oaxaca de Juárez", "puebla": "Puebla", "queretaro": "Santiago de Querétaro",
    "quintana roo": "Chetumal", "san luis potosi": "San Luis Potosí", "sinaloa": "Culiacán",
    "sonora": "Hermosillo", "tabasco": "Villahermosa", "tamaulipas": "Ciudad Victoria",
    "tlaxcala": "Tlaxcala", "veracruz": "Xalapa", "yucatan": "Mérida", "zacatecas": "Zacatecas",
}


def _normalizar(texto: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower().strip()
    return t.removesuffix(" state").strip()


def _nominatim(params: dict) -> tuple[float | None, float | None]:
    llave = json.dumps(params, sort_keys=True)
    if llave not in _GEOCODE_CACHE:
        time.sleep(1.2)  # politica de uso de Nominatim: max 1 req/seg
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={**params, "format": "json", "limit": 1},
            headers={"User-Agent": "boardgames-assistant-homelab/1.0"}, timeout=20,
        )
        resp.raise_for_status()
        r = resp.json()
        _GEOCODE_CACHE[llave] = (float(r[0]["lat"]), float(r[0]["lon"])) if r else (None, None)
    return _GEOCODE_CACHE[llave]


def geocodificar_estado(estado: str | None, pais: str | None) -> tuple[float | None, float | None]:
    """Coordenadas de la capital del estado del perfil BGG. Mexico: tabla CAPITALES_MX.
    Otros paises: Nominatim con el estado (centroide) como respaldo. Sin estado -> NULL
    (el centroide de un pais grande, ej. EE.UU. -> Kansas, no sirve para clima)."""
    if not estado:
        return None, None
    if _normalizar(pais or "mexico") == "mexico":
        capital = CAPITALES_MX.get(_normalizar(estado))
        if capital:
            return _nominatim({"city": capital, "country": "Mexico"})
    return _nominatim({"q": ", ".join(x for x in (estado, pais) if x)})


def actualizar_ubicacion_externo(cur, username: str, perfil: dict) -> None:
    lat, lon = geocodificar_estado(perfil["estado"], perfil["pais"])
    cur.execute(
        """UPDATE boardgames_bgg.usuarios_externos SET pais = %s, estado = %s, lat = %s, lon = %s
           WHERE bgg_username = %s""",
        (perfil["pais"], perfil["estado"], lat, lon, username),
    )


def agregar_externos(usuarios: list[str]) -> dict:
    """Valida contra BGG, rechaza amigos ya conocidos, da de alta y sincroniza."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        "SELECT LOWER(bgg_username) FROM boardgames_stats.jugadores WHERE bgg_username IS NOT NULL"
    )
    conocidos = {r[0] for r in cur.fetchall()}
    resultados = {}
    for u in usuarios:
        u = u.strip()
        if u.lower() in conocidos:
            resultados[u] = "omitido: ya es amigo/jugador en BG Stats (va a plays_amigos)"
            continue
        perfil = perfil_usuario(u)
        if perfil is None:
            resultados[u] = "omitido: no existe en BGG"
            continue
        cur.execute(
            "INSERT INTO boardgames_bgg.usuarios_externos (bgg_username) VALUES (%s) ON CONFLICT DO NOTHING",
            (u,),
        )
        actualizar_ubicacion_externo(cur, u, perfil)
        conn.commit()
        try:
            resultados[u] = sync(u, "externos")["partidas_descargadas"]
        except Exception as e:
            resultados[u] = f"error: {e}"
        time.sleep(1)
    cur.close()
    conn.close()
    return resultados


def sync_todos_los_externos() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT bgg_username FROM boardgames_bgg.usuarios_externos WHERE activo ORDER BY 1")
    usuarios = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    resultados = {}
    for u in usuarios:
        try:
            resultados[u] = sync(u, "externos")["partidas_descargadas"]
        except Exception as e:
            resultados[u] = f"error: {e}"
        time.sleep(1)
    return resultados


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--username", help="Usuario de BGG de un amigo especifico")
    grupo.add_argument("--todos", action="store_true", help="Todos los amigos con bgg_username en boardgames_stats.jugadores")
    grupo.add_argument("--agregar-externo", nargs="+", metavar="USER", help="Alta + sync de usuarios BGG ajenos")
    grupo.add_argument("--externos", action="store_true", help="Re-sincroniza todos los usuarios externos activos")
    args = parser.parse_args()

    if args.agregar_externo:
        print(f"Externos agregados: {agregar_externos(args.agregar_externo)}")
    elif args.externos:
        print(f"Externos sincronizados: {sync_todos_los_externos()}")
    elif args.todos:
        print(f"Sincronizado: {sync_todos_los_amigos()}")
    else:
        print(f"Sincronizado: {sync(args.username)}")
