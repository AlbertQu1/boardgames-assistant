"""
Fase 2: sincroniza un export de BG Stats (JSON) hacia bgstats.* en Postgres.
Idempotente: se puede correr con el mismo archivo varias veces sin duplicar
(upsert por uuid en juegos/jugadores/lugares/partidas; partida_jugadores se
borra y reinserta por partida en cada corrida).

Uso:
    python source/bgstats_sync.py --archivo pdfs_prueba/BGStatsExport.json
"""

import argparse
import json
import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def parse_bool(v) -> bool:
    return bool(v)


def sync(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    # juegos
    for g in data["games"]:
        cur.execute(
            """
            INSERT INTO bgstats.juegos
                (uuid, bg_stats_id, nombre, bgg_id, bgg_nombre, bgg_year, es_expansion, es_base,
                 designers, min_jugadores, max_jugadores, min_duracion_min, max_duracion_min,
                 cooperativo, rating, veces_jugado_previo, modification_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (uuid) DO UPDATE SET
                nombre = EXCLUDED.nombre, bgg_id = EXCLUDED.bgg_id, bgg_nombre = EXCLUDED.bgg_nombre,
                bgg_year = EXCLUDED.bgg_year, es_expansion = EXCLUDED.es_expansion, es_base = EXCLUDED.es_base,
                designers = EXCLUDED.designers, min_jugadores = EXCLUDED.min_jugadores,
                max_jugadores = EXCLUDED.max_jugadores, min_duracion_min = EXCLUDED.min_duracion_min,
                max_duracion_min = EXCLUDED.max_duracion_min, cooperativo = EXCLUDED.cooperativo,
                rating = EXCLUDED.rating, veces_jugado_previo = EXCLUDED.veces_jugado_previo,
                modification_date = EXCLUDED.modification_date
            """,
            (
                g["uuid"], g["id"], g["name"], g.get("bggId"), g.get("bggName"), g.get("bggYear"),
                parse_bool(g.get("isExpansion")), parse_bool(g.get("isBaseGame")), g.get("designers"),
                g.get("minPlayerCount"), g.get("maxPlayerCount"), g.get("minPlayTime"), g.get("maxPlayTime"),
                g.get("cooperative"), g.get("rating"), g.get("previouslyPlayedAmount"), g.get("modificationDate"),
            ),
        )
    juego_id_a_uuid = {g["id"]: g["uuid"] for g in data["games"]}

    # jugadores
    for p in data["players"]:
        cur.execute(
            """
            INSERT INTO bgstats.jugadores (uuid, bg_stats_id, nombre, es_anonimo, modification_date)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (uuid) DO UPDATE SET
                nombre = EXCLUDED.nombre, es_anonimo = EXCLUDED.es_anonimo,
                modification_date = EXCLUDED.modification_date
            """,
            (p["uuid"], p["id"], p["name"], parse_bool(p.get("isAnonymous")), p.get("modificationDate")),
        )
    jugador_id_a_uuid = {p["id"]: p["uuid"] for p in data["players"]}

    # lugares
    for l in data["locations"]:
        cur.execute(
            """
            INSERT INTO bgstats.lugares (uuid, bg_stats_id, nombre, modification_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (uuid) DO UPDATE SET
                nombre = EXCLUDED.nombre, modification_date = EXCLUDED.modification_date
            """,
            (l["uuid"], l["id"], l["name"], l.get("modificationDate")),
        )
    lugar_id_a_uuid = {l["id"]: l["uuid"] for l in data["locations"]}

    # partidas + partida_jugadores
    for play in data["plays"]:
        expansiones = [
            juego_id_a_uuid[e["gameRefId"]]
            for e in play.get("expansionPlays", [])
            if e.get("gameRefId") in juego_id_a_uuid
        ]
        cur.execute(
            """
            INSERT INTO bgstats.partidas
                (uuid, juego_uuid, lugar_uuid, fecha, duracion_min, comentarios, usa_equipos,
                 expansiones_usadas, modification_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s)
            ON CONFLICT (uuid) DO UPDATE SET
                juego_uuid = EXCLUDED.juego_uuid, lugar_uuid = EXCLUDED.lugar_uuid, fecha = EXCLUDED.fecha,
                duracion_min = EXCLUDED.duracion_min, comentarios = EXCLUDED.comentarios,
                usa_equipos = EXCLUDED.usa_equipos, expansiones_usadas = EXCLUDED.expansiones_usadas,
                modification_date = EXCLUDED.modification_date
            """,
            (
                play["uuid"], juego_id_a_uuid.get(play["gameRefId"]),
                lugar_id_a_uuid.get(play.get("locationRefId")), play["playDate"], play.get("durationMin"),
                play.get("comments"), play.get("usesTeams"), expansiones or None, play.get("modificationDate"),
            ),
        )

        cur.execute("DELETE FROM bgstats.partida_jugadores WHERE partida_uuid = %s", (play["uuid"],))
        for ps in play.get("playerScores", []):
            cur.execute(
                """
                INSERT INTO bgstats.partida_jugadores
                    (partida_uuid, jugador_uuid, nombre_anonimo, puntaje, posicion, gano, orden_asiento)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    play["uuid"], jugador_id_a_uuid.get(ps.get("playerRefId")), ps.get("anonymousName"),
                    ps.get("score"), ps.get("rank"), ps.get("winner"), ps.get("seatOrder"),
                ),
            )

    conn.commit()
    counts = {
        "juegos": len(data["games"]),
        "jugadores": len(data["players"]),
        "lugares": len(data["locations"]),
        "partidas": len(data["plays"]),
    }
    cur.close()
    conn.close()
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archivo", required=True, help="Ruta al BGStatsExport.json")
    args = parser.parse_args()

    counts = sync(args.archivo)
    print(f"Sincronizado: {counts}")
