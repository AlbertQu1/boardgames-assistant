"""
Trae eventos de visitas (dos fuentes: calendario personal de iCloud
publicado como ICS, y tabla home_visits de la app Vacaciones -- misma DB
que vacation_trips, permite registrar una visita directo en la app sin
depender del calendario) y de vacaciones (tabla vacation_trips) para poder
correlacionar partidas con vacaciones fuera de casa o con visitas de
personas especificas. Liga automaticamente un evento de "visita" a un
jugador existente en boardgames_stats.jugadores si el nombre coincide (ej.
"Paul"). Idempotente (upsert por tipo+nombre+rango de fechas).

Uso:
    python source/calendario_sync.py
"""

import os
import re
from datetime import date, timedelta

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()


def fetch_ical_events(url: str) -> list[dict]:
    """Descarga un calendario ICS publicado y regresa sus VEVENT como dicts
    {nombre, fecha_inicio, fecha_fin}. No expande RRULE: este calendario
    personal solo tiene eventos unicos, uno por visita."""
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


def fetch_home_visits(hoy: date) -> list[dict]:
    """Lee home_visits de la DB de la app Vacaciones (misma DB que
    vacation_trips, tabla nueva) -- segunda fuente de eventos tipo='visita'
    ademas del calendario de iCloud, para poder registrar una visita desde
    la app directamente sin depender de tener el evento en el calendario."""
    conn = psycopg2.connect(os.environ.get("VACATION_DB_URL", "postgresql://albertqu@/vacaciones"))
    cur = conn.cursor()
    cur.execute("SELECT visitor_name, arrival_date, departure_date FROM home_visits WHERE arrival_date <= %s", (hoy,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            "nombre": nombre,
            "fecha_inicio": llegada.strftime("%Y%m%d"),
            "fecha_fin": (salida + timedelta(days=1)).strftime("%Y%m%d"),
        }
        for nombre, llegada, salida in rows
    ]


def fetch_vacation_trips(hoy: date) -> list[dict]:
    """Lee vacation_trips de la DB de la app Vacaciones (DB separada, mismo
    cluster local) y la regresa en el mismo formato {nombre, fecha_inicio,
    fecha_fin} que fetch_ical_events, con fecha_fin ya convertida a
    EXCLUSIVA (+1 dia) para respetar el contrato existente de la tabla
    calendario_eventos, documentado en source/api.py."""
    conn = psycopg2.connect(os.environ.get("VACATION_DB_URL", "postgresql://albertqu@/vacaciones"))
    cur = conn.cursor()
    cur.execute("SELECT destination, departure_date, return_date FROM vacation_trips WHERE departure_date <= %s", (hoy,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return [
        {
            "nombre": destino,
            "fecha_inicio": salida.strftime("%Y%m%d"),
            "fecha_fin": (regreso + timedelta(days=1)).strftime("%Y%m%d"),
        }
        for destino, salida, regreso in rows
    ]


def sync() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT nombre, uuid FROM boardgames_stats.jugadores WHERE NOT es_anonimo")
    jugador_por_nombre = {nombre.strip().lower(): uuid for nombre, uuid in cur.fetchall()}

    # alias manuales para nombres de visita que no coinciden exacto con el
    # jugador real (ej. "Angel ulises" -> "Angel U") -- se checan primero,
    # asi sobreviven a re-syncs en vez de tener que reaplicarse a mano cada vez
    cur.execute("SELECT alias, jugador_uuid FROM boardgames_stats.visita_nombre_alias")
    alias_por_nombre = {alias.strip().lower(): uuid for alias, uuid in cur.fetchall()}

    hoy_date = date.today()
    hoy = hoy_date.strftime("%Y%m%d")  # mismo formato que fecha_inicio (YYYYMMDD, sin guiones)

    counts = {"visita": 0, "vacacion": 0}
    fuentes = [
        ("visita", fetch_ical_events(os.environ.get("VISITS_CALENDAR_URL", ""))),
        ("visita", fetch_home_visits(hoy_date)),
        ("vacacion", fetch_vacation_trips(hoy_date)),
    ]
    for tipo, eventos in fuentes:
        for ev in eventos:
            if ev["fecha_inicio"] > hoy:
                # planes a futuro: se ignoran hasta que ya hayan empezado, para no
                # guardar en el historial algo que todavia puede cambiar/cancelarse
                continue
            # fecha_fin es exclusiva (el dia despues del ultimo dia real);
            # se guarda el rango tal cual, la resta se hace al consultar.
            nombre_lower = ev["nombre"].strip().lower()
            jugador_uuid = (
                alias_por_nombre.get(nombre_lower) or jugador_por_nombre.get(nombre_lower)
                if tipo == "visita" else None
            )
            cur.execute(
                """
                INSERT INTO boardgames_stats.calendario_eventos (tipo, nombre, fecha_inicio, fecha_fin, jugador_uuid)
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
