"""
Fase 2: sincroniza un export de BG Stats (JSON) hacia bgstats.* en Postgres.
Idempotente: se puede correr con el mismo archivo varias veces sin duplicar
(upsert por uuid en juegos/jugadores/lugares/partidas/colecciones;
partida_jugadores se borra y reinserta por partida en cada corrida).

Ademas de las columnas tipadas (nombre, fechas, etc.), cada tabla guarda el
registro crudo del JSON en `datos_extra` (JSONB) para no perder informacion
que BG Stats agregue en el futuro y que todavia no tiene columna propia.

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


def parse_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_int(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_date(v):
    return v if v else None


def parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


# claves de metaData de una copia que ya se guardan en columnas tipadas de
# bgstats.colecciones; el resto se guarda tal cual en metadata_extra
COPIA_CAMPOS_TIPADOS = {
    "AcquiredFrom", "AcquisitionDate", "InventoryLocation", "InventoryDate",
    "PricePaid", "PricePaidCurrency", "CurrentPrice", "CurrentPriceCurrency",
    "Rating", "Quantity",
}


def sync(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    # juegos
    for g in data["games"]:
        es_propio = any(copy.get("statusOwned") == 1 for copy in g.get("copies", []))
        cur.execute(
            """
            INSERT INTO bgstats.juegos
                (uuid, bg_stats_id, nombre, bgg_id, bgg_nombre, bgg_year, es_expansion, es_base,
                 designers, min_jugadores, max_jugadores, min_duracion_min, max_duracion_min,
                 cooperativo, rating, veces_jugado_previo, modification_date, es_propio, datos_extra)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (uuid) DO UPDATE SET
                nombre = EXCLUDED.nombre, bgg_id = EXCLUDED.bgg_id, bgg_nombre = EXCLUDED.bgg_nombre,
                bgg_year = EXCLUDED.bgg_year, es_expansion = EXCLUDED.es_expansion, es_base = EXCLUDED.es_base,
                designers = EXCLUDED.designers, min_jugadores = EXCLUDED.min_jugadores,
                max_jugadores = EXCLUDED.max_jugadores, min_duracion_min = EXCLUDED.min_duracion_min,
                max_duracion_min = EXCLUDED.max_duracion_min, cooperativo = EXCLUDED.cooperativo,
                rating = EXCLUDED.rating, veces_jugado_previo = EXCLUDED.veces_jugado_previo,
                modification_date = EXCLUDED.modification_date, es_propio = EXCLUDED.es_propio,
                datos_extra = EXCLUDED.datos_extra
            """,
            (
                g["uuid"], g["id"], g["name"], g.get("bggId"), g.get("bggName"), g.get("bggYear"),
                parse_bool(g.get("isExpansion")), parse_bool(g.get("isBaseGame")), g.get("designers"),
                g.get("minPlayerCount"), g.get("maxPlayerCount"), g.get("minPlayTime"), g.get("maxPlayTime"),
                g.get("cooperative"), g.get("rating"), g.get("previouslyPlayedAmount"), g.get("modificationDate"),
                es_propio, json.dumps(g),
            ),
        )
    juego_id_a_uuid = {g["id"]: g["uuid"] for g in data["games"]}

    # colecciones (una fila por copia fisica de un juego: precio, origen, estado)
    cur.execute(
        "DELETE FROM bgstats.colecciones WHERE juego_uuid = ANY(%s::uuid[])",
        (list(juego_id_a_uuid.values()),),
    )
    for g in data["games"]:
        juego_uuid = juego_id_a_uuid[g["id"]]
        for copia in g.get("copies", []):
            md = parse_metadata(copia.get("metaData"))
            extra = {k: v for k, v in md.items() if k not in COPIA_CAMPOS_TIPADOS}
            cur.execute(
                """
                INSERT INTO bgstats.colecciones
                    (uuid, juego_uuid, version_name, year, status_owned, status_prev_owned,
                     status_for_trade, status_want_in_trade, status_want_to_buy, status_want_to_play,
                     status_wishlist, status_preordered, acquired_from, acquisition_date,
                     inventory_location, inventory_date, price_paid, price_paid_currency,
                     current_price, current_price_currency, rating, quantity, metadata_extra,
                     modification_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb, %s)
                ON CONFLICT (uuid) DO UPDATE SET
                    juego_uuid = EXCLUDED.juego_uuid, version_name = EXCLUDED.version_name,
                    year = EXCLUDED.year, status_owned = EXCLUDED.status_owned,
                    status_prev_owned = EXCLUDED.status_prev_owned, status_for_trade = EXCLUDED.status_for_trade,
                    status_want_in_trade = EXCLUDED.status_want_in_trade,
                    status_want_to_buy = EXCLUDED.status_want_to_buy,
                    status_want_to_play = EXCLUDED.status_want_to_play,
                    status_wishlist = EXCLUDED.status_wishlist, status_preordered = EXCLUDED.status_preordered,
                    acquired_from = EXCLUDED.acquired_from, acquisition_date = EXCLUDED.acquisition_date,
                    inventory_location = EXCLUDED.inventory_location, inventory_date = EXCLUDED.inventory_date,
                    price_paid = EXCLUDED.price_paid, price_paid_currency = EXCLUDED.price_paid_currency,
                    current_price = EXCLUDED.current_price, current_price_currency = EXCLUDED.current_price_currency,
                    rating = EXCLUDED.rating, quantity = EXCLUDED.quantity,
                    metadata_extra = EXCLUDED.metadata_extra, modification_date = EXCLUDED.modification_date
                """,
                (
                    copia["uuid"], juego_uuid, copia.get("versionName"), copia.get("year"),
                    parse_bool(copia.get("statusOwned")), parse_bool(copia.get("statusPrevOwned")),
                    parse_bool(copia.get("statusForTrade")), parse_bool(copia.get("statusWantInTrade")),
                    parse_bool(copia.get("statusWantToBuy")), parse_bool(copia.get("statusWantToPlay")),
                    parse_bool(copia.get("statusWishlist")), parse_bool(copia.get("statusPreordered")),
                    md.get("AcquiredFrom") or None, parse_date(md.get("AcquisitionDate")),
                    md.get("InventoryLocation") or None, parse_date(md.get("InventoryDate")),
                    parse_float(md.get("PricePaid")), md.get("PricePaidCurrency") or None,
                    parse_float(md.get("CurrentPrice")), md.get("CurrentPriceCurrency") or None,
                    parse_float(md.get("Rating")), parse_int(md.get("Quantity")),
                    json.dumps(extra), copia.get("modificationDate"),
                ),
            )

    # jugadores
    for p in data["players"]:
        cur.execute(
            """
            INSERT INTO bgstats.jugadores (uuid, bg_stats_id, nombre, es_anonimo, bgg_username,
                                            modification_date, datos_extra)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (uuid) DO UPDATE SET
                nombre = EXCLUDED.nombre, es_anonimo = EXCLUDED.es_anonimo,
                bgg_username = EXCLUDED.bgg_username, modification_date = EXCLUDED.modification_date,
                datos_extra = EXCLUDED.datos_extra
            """,
            (
                p["uuid"], p["id"], p["name"], parse_bool(p.get("isAnonymous")), p.get("bggUsername") or None,
                p.get("modificationDate"), json.dumps(p),
            ),
        )
    jugador_id_a_uuid = {p["id"]: p["uuid"] for p in data["players"]}

    # lugares
    for l in data["locations"]:
        cur.execute(
            """
            INSERT INTO bgstats.lugares (uuid, bg_stats_id, nombre, modification_date, datos_extra)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (uuid) DO UPDATE SET
                nombre = EXCLUDED.nombre, modification_date = EXCLUDED.modification_date,
                datos_extra = EXCLUDED.datos_extra
            """,
            (l["uuid"], l["id"], l["name"], l.get("modificationDate"), json.dumps(l)),
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
                 expansiones_usadas, modification_date, datos_extra)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s::jsonb)
            ON CONFLICT (uuid) DO UPDATE SET
                juego_uuid = EXCLUDED.juego_uuid, lugar_uuid = EXCLUDED.lugar_uuid, fecha = EXCLUDED.fecha,
                duracion_min = EXCLUDED.duracion_min, comentarios = EXCLUDED.comentarios,
                usa_equipos = EXCLUDED.usa_equipos, expansiones_usadas = EXCLUDED.expansiones_usadas,
                modification_date = EXCLUDED.modification_date, datos_extra = EXCLUDED.datos_extra
            """,
            (
                play["uuid"], juego_id_a_uuid.get(play["gameRefId"]),
                lugar_id_a_uuid.get(play.get("locationRefId")), play["playDate"], play.get("durationMin"),
                play.get("comments"), play.get("usesTeams"), expansiones or None, play.get("modificationDate"),
                json.dumps(play),
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
    cur.execute("SELECT COUNT(*) FROM bgstats.colecciones")
    n_colecciones = cur.fetchone()[0]
    counts = {
        "juegos": len(data["games"]),
        "colecciones": n_colecciones,
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
