"""
Clima diario generico de CDMX (mismas coordenadas de "Casa" que usa el resto
del proyecto, 19.4326/-99.1332) para las partidas de amigos en
bgg_data.plays_amigos -- no se tienen coordenadas exactas de sus lugares
("Entreturnos", "Mojo Dojo", etc no estan geolocalizados de este lado, y
esta tabla nunca se fusiona con bgstats.lugares), pero el grupo "Reformers"
juega casi siempre en CDMX en general, asi que un proxy por fecha+ciudad es
razonable.

Cubre todo el rango de fechas presente en bgg_data.plays_amigos de una sola
vez (no por partida), igual criterio que source/clima_sync.py.

Uso:
    python source/bgg_friend_clima_sync.py
"""

import os

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
LAT, LON = 19.4326, -99.1332  # mismas coords que Casa (reusadas en todo el proyecto)


def sync() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT MIN(fecha), MAX(fecha) FROM bgg_data.plays_amigos WHERE fecha IS NOT NULL")
    fecha_min, fecha_max = cur.fetchone()
    if fecha_min is None:
        cur.close()
        conn.close()
        return {"dias": 0}

    resp = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": LAT, "longitude": LON,
            "start_date": fecha_min.isoformat(), "end_date": fecha_max.isoformat(),
            "daily": "temperature_2m_mean,precipitation_sum",
            "timezone": "America/Mexico_City",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["daily"]

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
    print(f"Sincronizado: {sync()}")
