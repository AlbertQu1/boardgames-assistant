"""
Fase 2: sincroniza un export de BG Stats (JSON) hacia bgstats.* en Postgres.
Idempotente: se puede correr con el mismo archivo varias veces sin duplicar
(upsert por uuid en juegos/jugadores/lugares/partidas/colecciones;
partida_jugadores se borra y reinserta por partida en cada corrida).

Ademas de las columnas tipadas (nombre, fechas, etc.), cada tabla guarda el
registro crudo del JSON en `datos_extra` (JSONB) para no perder informacion
que BG Stats agregue en el futuro y que todavia no tiene columna propia.

`acquired_from` es texto libre (typos, mayusculas distintas, variantes del
mismo lugar) escrito en BG Stats. En cada corrida se normaliza contra
bgstats.fuentes_compra_alias (variantes conocidas -> nombre canonico,
opcionalmente ligado a un bgstats.lugares existente) y, si no hay alias
pero el texto coincide con un lugar ya trackeado, se liga automaticamente.
Agregar una fila nueva a fuentes_compra_alias es suficiente para que la
proxima corrida junte una variante nueva sin tocar este script.

Tambien convierte precios en USD/CAD a MXN usando la tasa historica del dia
de adquisicion (api.frankfurter.dev, gratis, sin API key) para poder sumar
y comparar gastos en una sola moneda.

Uso:
    python source/bgstats_sync.py --archivo pdfs_prueba/BGStatsExport.json
"""

import argparse
import json
import os

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()


def parse_bool(v) -> bool:
    return bool(v)


def limpiar_nombre_prefijo(nombre: str) -> str:
    """BG Stats no soporta grupos para jugadores, asi que Alberto usaba una
    letra minuscula al inicio del nombre (ej. "xJairo", "wDave IT") como
    categoria informal sin exponerla en el nombre visible. Se guarda el
    nombre limpio; el original crudo sigue disponible en datos_extra."""
    if nombre and len(nombre) >= 2 and nombre[0].islower() and nombre[1].isupper():
        return nombre[1:]
    return nombre


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

# monedas que no son un codigo ISO real (ej. "otro") y no se pueden convertir
MONEDAS_NO_CONVERTIBLES = {"otro"}


def resolver_tag_ids(data: dict) -> dict:
    """Busca por nombre (no por id, que puede variar entre exports) los tags
    de tipo 'Play' que marcan una partida como Solo o jugada en copia digital."""
    ids = {"solo": None, "digital": None}
    for t in data.get("tags", []):
        if t.get("type") != "Play":
            continue
        if t.get("name") == "Solo":
            ids["solo"] = t["id"]
        elif t.get("name") == "default_play_tag_digital":
            ids["digital"] = t["id"]
    return ids


def tiene_tag(play: dict, tag_id) -> bool:
    if tag_id is None:
        return False
    return any(t.get("tagRefId") == tag_id for t in play.get("tags", []))


def cargar_alias_compra(cur) -> dict:
    cur.execute("SELECT alias, fuente_canonica, lugar_uuid FROM bgstats.fuentes_compra_alias")
    return {alias: (canonica, lugar_uuid) for alias, canonica, lugar_uuid in cur.fetchall()}


def cargar_categoria_compra(cur) -> dict:
    """Clasificacion manual de cada fuente_compra canonica (en_linea/amigos/regalo/
    viaje/tienda_fisica), para poder agrupar el gasto por tipo de compra."""
    cur.execute("SELECT fuente_canonica, categoria FROM bgstats.fuentes_compra_categoria")
    return dict(cur.fetchall())


def cargar_moneda_override(cur) -> dict:
    """Copias donde price_paid_currency del export es ambiguo (ej. "otro") y se
    confirmo manualmente cual era la moneda real. Ver bgstats.colecciones_moneda_override.
    Las claves se normalizan a minusculas: BG Stats exporta uuids en mayusculas
    pero psycopg2 los regresa como uuid.UUID (str() en minusculas)."""
    cur.execute("SELECT copia_uuid, moneda_real FROM bgstats.colecciones_moneda_override")
    return {str(copia_uuid).lower(): moneda for copia_uuid, moneda in cur.fetchall()}


def normalizar_fuente_compra(raw, alias_map: dict, lugares_por_nombre: dict):
    """Devuelve (fuente_canonica, lugar_uuid, reconocido). `reconocido` es False
    solo cuando el texto no coincide con ningun alias ni lugar existente."""
    if not raw:
        return None, None, True
    limpio = raw.strip()
    if not limpio:
        return None, None, True
    if limpio in alias_map:
        canonica, lugar_uuid = alias_map[limpio]
        return canonica, lugar_uuid, True
    lugar_uuid = lugares_por_nombre.get(limpio.lower())
    return limpio, lugar_uuid, lugar_uuid is not None


def obtener_tasa_fx(moneda: str, fecha: str, cache: dict, intentos: int = 3):
    clave = (moneda, fecha)
    if clave in cache:
        return cache[clave]
    tasa = None
    ultimo_error = None
    for intento in range(intentos):
        try:
            resp = requests.get(
                f"https://api.frankfurter.dev/v1/{fecha}", params={"from": moneda, "to": "MXN"}, timeout=20
            )
            resp.raise_for_status()
            tasa = resp.json()["rates"]["MXN"]
            break
        except (requests.RequestException, KeyError, ValueError) as e:
            ultimo_error = e
    if tasa is None:
        print(f"  aviso: no se pudo obtener tasa {moneda}->MXN para {fecha} tras {intentos} intentos ({ultimo_error})")
    cache[clave] = tasa
    return tasa


def convertir_a_mxn(price_paid, moneda: str, fecha, fecha_alterna, cache: dict):
    """Devuelve (price_paid_mxn, tasa_usada, fecha_usada)."""
    if price_paid is None:
        return None, None, None
    moneda_limpia = (moneda or "").strip()
    if moneda_limpia.lower() in MONEDAS_NO_CONVERTIBLES:
        return None, None, None
    if not moneda_limpia or moneda_limpia.upper() == "MXN":
        return price_paid, 1.0, fecha or fecha_alterna
    fecha_para_tasa = fecha or fecha_alterna
    if not fecha_para_tasa:
        return None, None, None
    tasa = obtener_tasa_fx(moneda_limpia.upper(), fecha_para_tasa, cache)
    if tasa is None:
        return None, None, None
    return round(price_paid * tasa, 2), tasa, fecha_para_tasa


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
    alias_compra = cargar_alias_compra(cur)
    moneda_override = cargar_moneda_override(cur)
    categoria_compra_por_fuente = cargar_categoria_compra(cur)
    cur.execute("SELECT nombre, uuid FROM bgstats.lugares")
    lugares_por_nombre = {nombre.strip().lower(): uuid for nombre, uuid in cur.fetchall()}
    fx_cache: dict = {}
    fuentes_sin_normalizar = set()

    cur.execute(
        "DELETE FROM bgstats.colecciones WHERE juego_uuid = ANY(%s::uuid[])",
        (list(juego_id_a_uuid.values()),),
    )
    for g in data["games"]:
        juego_uuid = juego_id_a_uuid[g["id"]]
        for copia in g.get("copies", []):
            md = parse_metadata(copia.get("metaData"))
            extra = {k: v for k, v in md.items() if k not in COPIA_CAMPOS_TIPADOS}

            acquired_from_raw = md.get("AcquiredFrom") or None
            fuente_compra, lugar_compra_uuid, reconocido = normalizar_fuente_compra(
                acquired_from_raw, alias_compra, lugares_por_nombre
            )
            if acquired_from_raw and not reconocido:
                fuentes_sin_normalizar.add(acquired_from_raw.strip())
            categoria_compra = categoria_compra_por_fuente.get(fuente_compra) if fuente_compra else None

            acquisition_date = parse_date(md.get("AcquisitionDate"))
            inventory_date = parse_date(md.get("InventoryDate"))
            price_paid = parse_float(md.get("PricePaid"))
            price_paid_currency = md.get("PricePaidCurrency") or None
            moneda_para_conversion = moneda_override.get(copia["uuid"].lower(), price_paid_currency)
            price_paid_mxn, fx_rate_usada, fx_fecha_usada = convertir_a_mxn(
                price_paid, moneda_para_conversion, acquisition_date, inventory_date, fx_cache
            )

            cur.execute(
                """
                INSERT INTO bgstats.colecciones
                    (uuid, juego_uuid, version_name, year, status_owned, status_prev_owned,
                     status_for_trade, status_want_in_trade, status_want_to_buy, status_want_to_play,
                     status_wishlist, status_preordered, acquired_from, acquisition_date,
                     inventory_location, inventory_date, price_paid, price_paid_currency,
                     current_price, current_price_currency, rating, quantity, metadata_extra,
                     modification_date, fuente_compra, lugar_compra_uuid, price_paid_mxn,
                     fx_rate_usada, fx_fecha_usada, categoria_compra)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
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
                    metadata_extra = EXCLUDED.metadata_extra, modification_date = EXCLUDED.modification_date,
                    fuente_compra = EXCLUDED.fuente_compra, lugar_compra_uuid = EXCLUDED.lugar_compra_uuid,
                    price_paid_mxn = EXCLUDED.price_paid_mxn, fx_rate_usada = EXCLUDED.fx_rate_usada,
                    fx_fecha_usada = EXCLUDED.fx_fecha_usada, categoria_compra = EXCLUDED.categoria_compra
                """,
                (
                    copia["uuid"], juego_uuid, copia.get("versionName"), copia.get("year"),
                    parse_bool(copia.get("statusOwned")), parse_bool(copia.get("statusPrevOwned")),
                    parse_bool(copia.get("statusForTrade")), parse_bool(copia.get("statusWantInTrade")),
                    parse_bool(copia.get("statusWantToBuy")), parse_bool(copia.get("statusWantToPlay")),
                    parse_bool(copia.get("statusWishlist")), parse_bool(copia.get("statusPreordered")),
                    acquired_from_raw, acquisition_date,
                    md.get("InventoryLocation") or None, inventory_date,
                    price_paid, price_paid_currency,
                    parse_float(md.get("CurrentPrice")), md.get("CurrentPriceCurrency") or None,
                    parse_float(md.get("Rating")), parse_int(md.get("Quantity")),
                    json.dumps(extra), copia.get("modificationDate"),
                    fuente_compra, lugar_compra_uuid, price_paid_mxn, fx_rate_usada, fx_fecha_usada,
                    categoria_compra,
                ),
            )

    # override manual de nombre por jugador/lugar (ej. nombres corregidos a mano en Postgres
    # que BG Stats no tiene bien capturados) — sin esto, cada sync pisaria la correccion con
    # el nombre crudo del export
    cur.execute("SELECT jugador_uuid, nombre_real FROM bgstats.jugadores_nombre_override")
    nombre_override_jugadores = {str(uuid).lower(): nombre for uuid, nombre in cur.fetchall()}
    cur.execute("SELECT lugar_uuid, nombre_real FROM bgstats.lugares_nombre_override")
    nombre_override_lugares = {str(uuid).lower(): nombre for uuid, nombre in cur.fetchall()}

    # jugadores — BG Stats permite etiquetar jugadores con tags tipo "Player"
    # (ej. "Reformers", "Cartoneros", "Cul", "Cdmx") que Alberto ya usa para
    # agrupar por grupo social/ciudad de origen; se guarda el primero como
    # grupo_social, feature del modelo de duracion (correlacion mas fuerte
    # que la de lugar: rango 18.7-50.8 min segun grupo, sesion 2026-08-11).
    tags_player = {t["id"]: t["name"] for t in data.get("tags", []) if t.get("type") == "Player"}
    for p in data["players"]:
        nombre_final = nombre_override_jugadores.get(
            p["uuid"].lower(), limpiar_nombre_prefijo(p["name"])
        )
        tags_p = [tags_player[t["tagRefId"]] for t in p.get("tags", []) if t.get("tagRefId") in tags_player]
        grupo_social = tags_p[0] if tags_p else None
        cur.execute(
            """
            INSERT INTO bgstats.jugadores (uuid, bg_stats_id, nombre, es_anonimo, bgg_username,
                                            modification_date, datos_extra, grupo_social)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (uuid) DO UPDATE SET
                nombre = EXCLUDED.nombre, es_anonimo = EXCLUDED.es_anonimo,
                bgg_username = EXCLUDED.bgg_username, modification_date = EXCLUDED.modification_date,
                datos_extra = EXCLUDED.datos_extra, grupo_social = EXCLUDED.grupo_social
            """,
            (
                p["uuid"], p["id"], nombre_final, parse_bool(p.get("isAnonymous")),
                p.get("bggUsername") or None,
                p.get("modificationDate"), json.dumps(p), grupo_social,
            ),
        )
    jugador_id_a_uuid = {p["id"]: p["uuid"] for p in data["players"]}

    # lugares — BG Stats permite etiquetar lugares con tags tipo "Location"
    # (ej. "Cafe", "Fuera", "Vacaciones") que Alberto ya usa para categorizar
    # donde juega; se guardan tal cual para usarse como feature del modelo
    # de duracion (jugar en cafe vs. en casa vs. de viaje).
    tags_location = {t["id"]: t["name"] for t in data.get("tags", []) if t.get("type") == "Location"}
    for l in data["locations"]:
        nombre_final = nombre_override_lugares.get(l["uuid"].lower(), l["name"])
        tags_l = [
            tags_location[t["tagRefId"]] for t in l.get("tags", []) if t.get("tagRefId") in tags_location
        ] or None
        cur.execute(
            """
            INSERT INTO bgstats.lugares (uuid, bg_stats_id, nombre, modification_date, datos_extra, tags_ubicacion)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (uuid) DO UPDATE SET
                nombre = EXCLUDED.nombre, modification_date = EXCLUDED.modification_date,
                datos_extra = EXCLUDED.datos_extra, tags_ubicacion = EXCLUDED.tags_ubicacion
            """,
            (l["uuid"], l["id"], nombre_final, l.get("modificationDate"), json.dumps(l), tags_l),
        )
    lugar_id_a_uuid = {l["id"]: l["uuid"] for l in data["locations"]}

    # override manual de lugar por partida (ej. partidas etiquetadas "Vacaciones" en BG Stats
    # que en realidad fueron en un lugar especifico segun el comentario) — sin esto, cada sync
    # pisaria la reasignacion con el locationRefId generico del export crudo
    cur.execute("SELECT partida_uuid, lugar_uuid FROM bgstats.partida_lugar_override")
    lugar_override = {str(partida_uuid).lower(): lugar_uuid for partida_uuid, lugar_uuid in cur.fetchall()}
    tag_ids = resolver_tag_ids(data)

    # partidas + partida_jugadores
    for play in data["plays"]:
        expansiones = [
            juego_id_a_uuid[e["gameRefId"]]
            for e in play.get("expansionPlays", [])
            if e.get("gameRefId") in juego_id_a_uuid
        ]
        lugar_uuid_final = lugar_override.get(
            play["uuid"].lower(), lugar_id_a_uuid.get(play.get("locationRefId"))
        )
        cur.execute(
            """
            INSERT INTO bgstats.partidas
                (uuid, juego_uuid, lugar_uuid, fecha, duracion_min, comentarios, usa_equipos,
                 expansiones_usadas, modification_date, datos_extra, tag_solo, tag_digital)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s::jsonb, %s, %s)
            ON CONFLICT (uuid) DO UPDATE SET
                juego_uuid = EXCLUDED.juego_uuid, lugar_uuid = EXCLUDED.lugar_uuid, fecha = EXCLUDED.fecha,
                duracion_min = EXCLUDED.duracion_min, comentarios = EXCLUDED.comentarios,
                usa_equipos = EXCLUDED.usa_equipos, expansiones_usadas = EXCLUDED.expansiones_usadas,
                modification_date = EXCLUDED.modification_date, datos_extra = EXCLUDED.datos_extra,
                tag_solo = EXCLUDED.tag_solo, tag_digital = EXCLUDED.tag_digital
            """,
            (
                play["uuid"], juego_id_a_uuid.get(play["gameRefId"]),
                lugar_uuid_final, play["playDate"], play.get("durationMin"),
                play.get("comments"), play.get("usesTeams"), expansiones or None, play.get("modificationDate"),
                json.dumps(play), tiene_tag(play, tag_ids["solo"]), tiene_tag(play, tag_ids["digital"]),
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
                    play["uuid"], jugador_id_a_uuid.get(ps.get("playerRefId")),
                    limpiar_nombre_prefijo(ps["anonymousName"]) if ps.get("anonymousName") else None,
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
        "fuentes_compra_sin_normalizar": sorted(fuentes_sin_normalizar),
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
    if counts["fuentes_compra_sin_normalizar"]:
        print(
            "Fuentes de compra sin alias ni lugar (se guardaron tal cual, "
            "agrega una fila a bgstats.fuentes_compra_alias si son variantes de algo existente):"
        )
        for f in counts["fuentes_compra_sin_normalizar"]:
            print(f"  - {f}")

    print("\nCacheando datos de BGG para juegos nuevos...")
    from bgg_cache_sync import sync as sync_bgg

    print(f"Sincronizado (BGG): {sync_bgg()}")
