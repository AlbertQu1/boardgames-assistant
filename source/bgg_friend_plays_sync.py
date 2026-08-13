"""
Descarga TODAS las partidas que un amigo registro directo en BGG (no en BG
Stats) via el endpoint 'plays' del XML API2, y las guarda tal cual en
bgg_data.plays_amigos -- staging puro, no se fusiona nada con bgstats.partidas
todavia. Sirve para decidir despues, con los datos completos a la vista,
que partidas son nuevas de verdad (el amigo jugo sin Alberto y nunca se
registro del lado de BG Stats) vs. las que ya estan de este lado.

Uso:
    python source/bgg_friend_plays_sync.py --username VINICIO
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


def sync(username: str) -> dict:
    token = os.environ["BGG_API_TOKEN"]
    plays = fetch_all_plays(username, token)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    for p in plays:
        cur.execute(
            """
            INSERT INTO bgg_data.plays_amigos
                (bgg_play_id, bgg_username, fecha, juego, bgg_game_id, ubicacion,
                 comentarios, duracion_min, cantidad, incompleta, jugadores, datos_extra)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (bgg_play_id) DO UPDATE SET
                fecha = EXCLUDED.fecha, juego = EXCLUDED.juego, bgg_game_id = EXCLUDED.bgg_game_id,
                ubicacion = EXCLUDED.ubicacion, comentarios = EXCLUDED.comentarios,
                duracion_min = EXCLUDED.duracion_min, cantidad = EXCLUDED.cantidad,
                incompleta = EXCLUDED.incompleta, jugadores = EXCLUDED.jugadores,
                datos_extra = EXCLUDED.datos_extra, sincronizado_en = now()
            """,
            (
                p["bgg_play_id"], username, p["fecha"], p["juego"], p["bgg_game_id"], p["ubicacion"],
                p["comentarios"], p["duracion_min"], p["cantidad"], p["incompleta"],
                json.dumps(p["jugadores"]), json.dumps(p["datos_extra"]),
            ),
        )
    conn.commit()
    cur.close()
    conn.close()
    return {"username": username, "partidas_descargadas": len(plays)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True, help="Usuario de BGG del amigo")
    args = parser.parse_args()

    resultado = sync(args.username)
    print(f"Sincronizado: {resultado}")
