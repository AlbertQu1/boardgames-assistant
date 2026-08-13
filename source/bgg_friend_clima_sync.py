"""
Clima diario para las partidas de amigos en bgg_data.plays_amigos, en dos capas:

1. sync_por_ubicacion(): clima exacto por lugar, para las ubicaciones de
   bgg_data.ubicaciones_amigos_alias que ya tienen lat/lon (heredadas de
   bgstats.lugares cuando el nombre coincide con un lugar que Alberto tambien
   visita, o geocodificadas a mano via Plus Code, ej. Roll Games). Cubre el
   rango de fechas real de las partidas en cada lugar. Guarda en
   bgg_data.clima_ubicacion_diario.
2. sync_proxy_generico(): clima generico de CDMX (mismas coords de "Casa" que
   usa el resto del proyecto, 19.4326/-99.1332) como respaldo para las
   ubicaciones sin lat/lon todavia (casas de amigos sin geolocalizar, ej.
   "Casa Gus"). Guarda en bgg_data.clima_cdmx_diario.

duracion_model.py usa el clima por ubicacion cuando existe y cae al proxy
generico si no. Ninguna de las dos tablas se fusiona con bgstats.clima_diario
(la del lado personal).

Uso:
    python source/bgg_friend_clima_sync.py
"""

import os
import time

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
LAT, LON = 19.4326, -99.1332  # mismas coords que Casa (reusadas en todo el proyecto)


def _fetch_clima(lat: float, lon: float, fecha_min, fecha_max) -> dict:
    resp = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": lat, "longitude": lon,
            "start_date": fecha_min.isoformat(), "end_date": fecha_max.isoformat(),
            "daily": "temperature_2m_mean,precipitation_sum",
            "timezone": "America/Mexico_City",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["daily"]


def sync_por_ubicacion() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ubicacion_normalizada, lat, lon
        FROM bgg_data.ubicaciones_amigos_alias
        WHERE lat IS NOT NULL
        GROUP BY ubicacion_normalizada, lat, lon
        """
    )
    ubicaciones = cur.fetchall()

    resultados = {}
    for ubicacion_normalizada, lat, lon in ubicaciones:
        cur.execute(
            """
            SELECT MIN(fecha), MAX(fecha) FROM bgg_data.plays_amigos
            WHERE ubicacion_normalizada = %s AND fecha IS NOT NULL
            """,
            (ubicacion_normalizada,),
        )
        fecha_min, fecha_max = cur.fetchone()
        if fecha_min is None:
            continue

        data = _fetch_clima(lat, lon, fecha_min, fecha_max)
        for fecha, temp, precip in zip(data["time"], data["temperature_2m_mean"], data["precipitation_sum"]):
            cur.execute(
                """
                INSERT INTO bgg_data.clima_ubicacion_diario (ubicacion_normalizada, fecha, temp_media_c, precipitacion_mm)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (ubicacion_normalizada, fecha) DO UPDATE SET
                    temp_media_c = EXCLUDED.temp_media_c, precipitacion_mm = EXCLUDED.precipitacion_mm
                """,
                (ubicacion_normalizada, fecha, temp, precip),
            )
        conn.commit()
        resultados[ubicacion_normalizada] = len(data["time"])
        time.sleep(1)  # cortesia con la API, igual criterio que bgg_friend_plays_sync

    cur.close()
    conn.close()
    return resultados


def sync_proxy_generico() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT MIN(fecha), MAX(fecha) FROM bgg_data.plays_amigos WHERE fecha IS NOT NULL")
    fecha_min, fecha_max = cur.fetchone()
    if fecha_min is None:
        cur.close()
        conn.close()
        return {"dias": 0}

    data = _fetch_clima(LAT, LON, fecha_min, fecha_max)
    for fecha, temp, precip in zip(data["time"], data["temperature_2m_mean"], data["precipitation_sum"]):
        cur.execute(
            """
            INSERT INTO bgg_data.clima_cdmx_diario (fecha, temp_media_c, precipitacion_mm)
            VALUES (%s, %s, %s)
            ON CONFLICT (fecha) DO UPDATE SET temp_media_c = EXCLUDED.temp_media_c,
                precipitacion_mm = EXCLUDED.precipitacion_mm
            """,
            (fecha, temp, precip),
        )
    conn.commit()
    dias = len(data["time"])
    cur.close()
    conn.close()
    return {"dias": dias, "desde": fecha_min.isoformat(), "hasta": fecha_max.isoformat()}


if __name__ == "__main__":
    print(f"Sincronizado (por ubicacion exacta): {sync_por_ubicacion()}")
    print(f"Sincronizado (proxy generico CDMX, respaldo): {sync_proxy_generico()}")
