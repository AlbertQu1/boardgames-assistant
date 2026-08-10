"""
Trae eventos de los calendarios personales de iCloud (Vacaciones/Visitas,
publicados como ICS) para poder correlacionar partidas con vacaciones fuera
de casa o con visitas de personas especificas. Liga automaticamente un
evento de "visita" a un jugador existente en bgstats.jugadores si el nombre
coincide (ej. "Paul"). Idempotente (upsert por tipo+nombre+rango de fechas).

Uso:
    python source/calendario_sync.py
"""

import os
import re

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()


def fetch_ical_events(url: str) -> list[dict]:
    """Descarga un calendario ICS publicado y regresa sus VEVENT como dicts
    {nombre, fecha_inicio, fecha_fin}. No expande RRULE: este calendario
    personal solo tiene eventos unicos, uno por visita/vacacion."""
    if not url:
        return []
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    contenido = resp.text.replace("\r\n ", "").replace("\n ", "")

    eventos = []
    for bloque in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", contenido, re.DOTALL):
        m_nombre = re.search(r"SUMMARY:(.*)", bloque)
        m_inicio = re.search(r"DTSTART[^:]*:(\d{8})", bloque)
        m_fin = re.search(r"DTEND[^:]*:(\d{8})", bloque)
        if not (m_nombre and m_inicio):
            continue
        fecha_inicio = m_inicio.group(1)
        fecha_fin = m_fin.group(1) if m_fin else fecha_inicio
        eventos.append({
            "nombre": m_nombre.group(1).strip(),
            "fecha_inicio": fecha_inicio,
            "fecha_fin": fecha_fin,
        })
    return eventos


def sync() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT nombre, uuid FROM bgstats.jugadores WHERE NOT es_anonimo")
    jugador_por_nombre = {nombre.strip().lower(): uuid for nombre, uuid in cur.fetchall()}

    counts = {"visita": 0, "vacacion": 0}
    fuentes = [
        ("visita", os.environ.get("VISITS_CALENDAR_URL", "")),
        ("vacacion", os.environ.get("VACATION_CALENDAR_URL", "")),
    ]
    for tipo, url in fuentes:
        if not url:
            print(f"  aviso: no hay URL configurada para calendario '{tipo}' (revisa .env)")
            continue
        eventos = fetch_ical_events(url)
        for ev in eventos:
            # DTEND en ICS es exclusivo (el dia despues del ultimo dia real);
            # se guarda el rango tal cual, la resta se hace al consultar.
            jugador_uuid = jugador_por_nombre.get(ev["nombre"].strip().lower()) if tipo == "visita" else None
            cur.execute(
                """
                INSERT INTO bgstats.calendario_eventos (tipo, nombre, fecha_inicio, fecha_fin, jugador_uuid)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tipo, nombre, fecha_inicio, fecha_fin) DO UPDATE SET
                    jugador_uuid = EXCLUDED.jugador_uuid
                """,
                (tipo, ev["nombre"], ev["fecha_inicio"], ev["fecha_fin"], jugador_uuid),
            )
            counts[tipo] += 1

    conn.commit()
    cur.close()
    conn.close()
    return counts


if __name__ == "__main__":
    counts = sync()
    print(f"Sincronizado: {counts}")
