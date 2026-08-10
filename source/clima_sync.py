"""
Trae clima historico (Open-Meteo) para cada lugar con coordenadas, cubriendo
el rango de fechas real de las partidas jugadas ahi. Un solo request por
lugar (no por partida) para no abusar de la API. Idempotente (upsert por
lugar_uuid+fecha).

Uso:
    python source/clima_sync.py
"""

import os
import time

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def sync_clima() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT l.uuid, l.nombre, l.lat, l.lon, min(p.fecha)::date, max(p.fecha)::date
        FROM bgstats.partidas p
        JOIN bgstats.lugares l ON l.uuid = p.lugar_uuid
        WHERE l.lat IS NOT NULL
        GROUP BY l.uuid, l.nombre, l.lat, l.lon
        """
    )
    lugares = cur.fetchall()

    total_dias = 0
    for uuid, nombre, lat, lon, fecha_min, fecha_max in lugares:
        resp = requests.get(
            ARCHIVE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": fecha_min.isoformat(),
                "end_date": fecha_max.isoformat(),
                "daily": "temperature_2m_mean,precipitation_sum",
                "timezone": "auto",
            },
            timeout=30,
        )
        resp.raise_for_status()
        datos = resp.json().get("daily", {})
        fechas = datos.get("time", [])
        temps = datos.get("temperature_2m_mean", [])
        precs = datos.get("precipitation_sum", [])

        for fecha, temp, prec in zip(fechas, temps, precs):
            cur.execute(
                """
                INSERT INTO bgstats.clima_diario (lugar_uuid, fecha, temp_media_c, precipitacion_mm)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (lugar_uuid, fecha) DO UPDATE SET
                    temp_media_c = EXCLUDED.temp_media_c,
                    precipitacion_mm = EXCLUDED.precipitacion_mm
                """,
                (uuid, fecha, temp, prec),
            )
        conn.commit()
        print(f"  {nombre}: {len(fechas)} dias ({fecha_min} a {fecha_max})")
        total_dias += len(fechas)
        time.sleep(1)  # ser buen ciudadano con la API gratuita

    cur.close()
    conn.close()
    return {"lugares": len(lugares), "dias_totales": total_dias}


if __name__ == "__main__":
    resultado = sync_clima()
    print(f"\nSincronizado: {resultado}")
