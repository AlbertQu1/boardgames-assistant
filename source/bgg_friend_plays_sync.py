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

Uso:
    python source/bgg_friend_plays_sync.py --username VINICIO   # uno solo
    python source/bgg_friend_plays_sync.py --todos               # todos los amigos con bgg_username
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
        resp = requests.get(PLAYS_URL, params={"username": username, "page": page}, headers=headers, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        plays = root.findall("play")
        if not plays:
            break
        todas.extend(parse_play(p) for p in plays)
        if len(plays) < 100:
            break
        page += 1
        time.sleep(1)
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


def sync(username: str) -> dict:
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
            """
            INSERT INTO boardgames_bgg.plays_amigos
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--username", help="Usuario de BGG de un amigo especifico")
    grupo.add_argument("--todos", action="store_true", help="Todos los amigos con bgg_username en boardgames_stats.jugadores")
    args = parser.parse_args()

    if args.todos:
        print(f"Sincronizado: {sync_todos_los_amigos()}")
    else:
        print(f"Sincronizado: {sync(args.username)}")
