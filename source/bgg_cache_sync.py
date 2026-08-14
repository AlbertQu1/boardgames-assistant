"""
Cachea datos de BoardGameGeek (peso/complejidad, categorias, mecanicas,
descripcion, playtime, edad minima) para todos los bgg_id distintos en
boardgames_stats.juegos + los bgg_game_id que aparecen en boardgames_bgg.plays_amigos (juegos
que juega el grupo de amigos aunque Alberto nunca los haya jugado el mismo --
es metadata publica de BGG, no personal, asi que cachearla junta no rompe la
regla de no fusionar boardgames_stats.* con los datos de amigos), usando el endpoint
'thing' del XML API2 con Authorization Bearer token (registro obligatorio, ver
https://boardgamegeek.com/using_the_xml_api).

BGG pide minimizar requests: se piden varios ids por llamada (batch) en vez
de uno por juego, y el resultado se guarda en Postgres (boardgames_bgg.juegos_detalle)
para no volver a pedirlo — estos datos casi no cambian, no hace falta correr
esto seguido.

Uso:
    python source/bgg_cache_sync.py
"""

import json
import os
import time
import xml.etree.ElementTree as ET

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

THING_URL = "https://boardgamegeek.com/xmlapi2/thing"
BATCH_SIZE = 20


def fetch_batch(ids: list[int], token: str) -> ET.Element:
    resp = requests.get(
        THING_URL,
        params={"id": ",".join(str(i) for i in ids), "stats": 1},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    # el XML API2 a veces regresa 202 mientras arma la respuesta en background
    if resp.status_code == 202:
        time.sleep(3)
        return fetch_batch(ids, token)
    resp.raise_for_status()
    return ET.fromstring(resp.content)


def parse_suggested_numplayers(item: ET.Element) -> dict | None:
    poll = item.find("poll[@name='suggested_numplayers']")
    if poll is None or int(poll.get("totalvotes", "0")) == 0:
        return None
    out = {}
    for results in poll.findall("results"):
        numplayers = results.get("numplayers")
        votos = {r.get("value"): int(r.get("numvotes")) for r in results.findall("result")}
        out[numplayers] = {
            "best": votos.get("Best", 0),
            "recommended": votos.get("Recommended", 0),
            "not_recommended": votos.get("Not Recommended", 0),
        }
    return out or None


def parse_language_dependence(item: ET.Element) -> float | None:
    poll = item.find("poll[@name='language_dependence']")
    if poll is None or int(poll.get("totalvotes", "0")) == 0:
        return None
    total_votos = 0
    suma_ponderada = 0
    for r in poll.findall("results/result"):
        nivel = int(r.get("level"))
        votos = int(r.get("numvotes"))
        suma_ponderada += nivel * votos
        total_votos += votos
    return round(suma_ponderada / total_votos, 3) if total_votos else None


def parse_item(item: ET.Element) -> dict:
    def attr(tag, key="value", default=None):
        el = item.find(tag)
        return el.get(key) if el is not None else default

    categorias = [l.get("value") for l in item.findall("link[@type='boardgamecategory']")]
    mecanicas = [l.get("value") for l in item.findall("link[@type='boardgamemechanic']")]
    peso_el = item.find("statistics/ratings/averageweight")
    peso = float(peso_el.get("value")) if peso_el is not None and peso_el.get("value") else None
    desc_el = item.find("description")
    descripcion = desc_el.text if desc_el is not None else None

    def rating_attr(tag):
        el = item.find(f"statistics/ratings/{tag}")
        val = el.get("value") if el is not None else None
        return float(val) if val not in (None, "") else None

    return {
        "bgg_id": int(item.get("id")),
        "descripcion": descripcion,
        "categorias": categorias or None,
        "mecanicas": mecanicas or None,
        "peso_complejidad": peso,
        "min_playtime": int(attr("minplaytime")) if attr("minplaytime") else None,
        "max_playtime": int(attr("maxplaytime")) if attr("maxplaytime") else None,
        "min_age": int(attr("minage")) if attr("minage") else None,
        "imagen_url": attr("image", key=None),
        "calificacion_promedio": rating_attr("average"),
        "calificacion_bayes": rating_attr("bayesaverage"),
        "num_calificaciones": int(rating_attr("usersrated")) if rating_attr("usersrated") is not None else None,
        "numero_jugadores_sugerido": parse_suggested_numplayers(item),
        "dependencia_idioma": parse_language_dependence(item),
    }


def guardar_detalle(cur, d: dict) -> None:
    """Upsert de un juego ya parseado (parse_item) en boardgames_bgg.juegos_detalle.
    Compartido entre el sync masivo y el lookup on-demand del agente (api.py)."""
    cur.execute(
        """
        INSERT INTO boardgames_bgg.juegos_detalle
            (bgg_id, descripcion, categorias, mecanicas, peso_complejidad,
             min_playtime, max_playtime, min_age, imagen_url,
             numero_jugadores_sugerido, dependencia_idioma,
             calificacion_promedio, calificacion_bayes, num_calificaciones, sincronizado_en)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, now())
        ON CONFLICT (bgg_id) DO UPDATE SET
            descripcion = EXCLUDED.descripcion, categorias = EXCLUDED.categorias,
            mecanicas = EXCLUDED.mecanicas, peso_complejidad = EXCLUDED.peso_complejidad,
            min_playtime = EXCLUDED.min_playtime, max_playtime = EXCLUDED.max_playtime,
            min_age = EXCLUDED.min_age, imagen_url = EXCLUDED.imagen_url,
            numero_jugadores_sugerido = EXCLUDED.numero_jugadores_sugerido,
            dependencia_idioma = EXCLUDED.dependencia_idioma,
            calificacion_promedio = EXCLUDED.calificacion_promedio,
            calificacion_bayes = EXCLUDED.calificacion_bayes,
            num_calificaciones = EXCLUDED.num_calificaciones,
            sincronizado_en = EXCLUDED.sincronizado_en
        """,
        (
            d["bgg_id"], d["descripcion"], d["categorias"], d["mecanicas"],
            d["peso_complejidad"], d["min_playtime"], d["max_playtime"],
            d["min_age"], d["imagen_url"],
            json.dumps(d["numero_jugadores_sugerido"]) if d["numero_jugadores_sugerido"] else None,
            d["dependencia_idioma"], d["calificacion_promedio"],
            d["calificacion_bayes"], d["num_calificaciones"],
        ),
    )


def sync() -> dict:
    token = os.environ["BGG_API_TOKEN"]
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute(
        """
        SELECT DISTINCT bgg_id FROM boardgames_stats.juegos WHERE bgg_id IS NOT NULL
        UNION
        SELECT DISTINCT bgg_game_id FROM boardgames_bgg.plays_amigos WHERE bgg_game_id IS NOT NULL
        """
    )
    ids = [r[0] for r in cur.fetchall()]

    guardados = 0
    for i in range(0, len(ids), BATCH_SIZE):
        lote = ids[i : i + BATCH_SIZE]
        root = fetch_batch(lote, token)
        for item in root.findall("item"):
            d = parse_item(item)
            guardar_detalle(cur, d)
            guardados += 1
        conn.commit()
        print(f"  lote {i // BATCH_SIZE + 1}: {len(lote)} juegos")
        time.sleep(2)

    cur.close()
    conn.close()
    return {"juegos_distintos": len(ids), "guardados": guardados}


if __name__ == "__main__":
    resultado = sync()
    print(f"\nSincronizado: {resultado}")
