"""
Cachea datos de BoardGameGeek (peso/complejidad, categorias, mecanicas,
descripcion, playtime, edad minima) para todos los bgg_id distintos en
bgstats.juegos, usando el endpoint 'thing' del XML API2 con Authorization
Bearer token (registro obligatorio, ver
https://boardgamegeek.com/using_the_xml_api).

BGG pide minimizar requests: se piden varios ids por llamada (batch) en vez
de uno por juego, y el resultado se guarda en Postgres (bgg_data.juegos_detalle)
para no volver a pedirlo — estos datos casi no cambian, no hace falta correr
esto seguido.

Uso:
    python source/bgg_cache_sync.py
"""

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


def parse_item(item: ET.Element) -> dict:
    def attr(tag, key="value", default=None):
        el = item.find(tag)
        return el.get(key) if el is not None else default

    categorias = [l.get("value") for l in item.findall("link[@type='boardgamecategory']")]
    mecanicas = [l.get("value") for l in item.findall("link[@type='boardgamemechanic']")]
    peso_el = item.find("statistics/ratings/averageweight")
    peso = float(peso_el.get("value")) if peso_el is not None and peso_el.get("value") else None

    return {
        "bgg_id": int(item.get("id")),
        "descripcion": attr("description"),
        "categorias": categorias or None,
        "mecanicas": mecanicas or None,
        "peso_complejidad": peso,
        "min_playtime": int(attr("minplaytime")) if attr("minplaytime") else None,
        "max_playtime": int(attr("maxplaytime")) if attr("maxplaytime") else None,
        "min_age": int(attr("minage")) if attr("minage") else None,
        "imagen_url": attr("image", key=None),
    }


def sync() -> dict:
    token = os.environ["BGG_API_TOKEN"]
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT bgg_id FROM bgstats.juegos WHERE bgg_id IS NOT NULL")
    ids = [r[0] for r in cur.fetchall()]

    guardados = 0
    for i in range(0, len(ids), BATCH_SIZE):
        lote = ids[i : i + BATCH_SIZE]
        root = fetch_batch(lote, token)
        for item in root.findall("item"):
            d = parse_item(item)
            cur.execute(
                """
                INSERT INTO bgg_data.juegos_detalle
                    (bgg_id, descripcion, categorias, mecanicas, peso_complejidad,
                     min_playtime, max_playtime, min_age, imagen_url, sincronizado_en)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (bgg_id) DO UPDATE SET
                    descripcion = EXCLUDED.descripcion, categorias = EXCLUDED.categorias,
                    mecanicas = EXCLUDED.mecanicas, peso_complejidad = EXCLUDED.peso_complejidad,
                    min_playtime = EXCLUDED.min_playtime, max_playtime = EXCLUDED.max_playtime,
                    min_age = EXCLUDED.min_age, imagen_url = EXCLUDED.imagen_url,
                    sincronizado_en = EXCLUDED.sincronizado_en
                """,
                (
                    d["bgg_id"], d["descripcion"], d["categorias"], d["mecanicas"],
                    d["peso_complejidad"], d["min_playtime"], d["max_playtime"],
                    d["min_age"], d["imagen_url"],
                ),
            )
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
