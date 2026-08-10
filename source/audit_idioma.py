"""
Auditoria: detecta el idioma real del texto indexado y lo compara contra la
columna `idioma` guardada, para encontrar documentos mal etiquetados
(ej. Tuscany se encontro marcado como 'en' siendo contenido en espanol).

No corrige nada solo, solo reporta — la correccion se hace a mano (UPDATE)
una vez confirmado el caso real, igual que se hizo con Tuscany.

Uso:
    python source/audit_idioma.py
"""

import os

import psycopg2
from dotenv import load_dotenv
from langdetect import DetectorFactory, detect

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import params

load_dotenv()
DetectorFactory.seed = 0  # resultados deterministas


def audit() -> list[dict]:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT juego, source_pdf, idioma,
               string_agg(chunk_text, ' ' ORDER BY chunk_index) FILTER (WHERE chunk_index < 3) AS muestra
        FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE}
        GROUP BY juego, source_pdf, idioma
        ORDER BY juego;
        """
    )
    filas = cur.fetchall()
    cur.close()
    conn.close()

    problemas = []
    for juego, source_pdf, idioma_guardado, muestra in filas:
        if not muestra or len(muestra.split()) < 10:
            continue
        try:
            detectado = detect(muestra[:3000])
        except Exception:
            continue
        if detectado != idioma_guardado:
            problemas.append(
                {
                    "juego": juego,
                    "source_pdf": source_pdf,
                    "idioma_guardado": idioma_guardado,
                    "idioma_detectado": detectado,
                }
            )
    return problemas


if __name__ == "__main__":
    problemas = audit()
    if not problemas:
        print("Sin problemas detectados — todos los idiomas guardados coinciden con el texto.")
    else:
        print(f"{len(problemas)} posible(s) desajuste(s) de idioma:\n")
        for p in problemas:
            print(
                f"  {p['juego']} ({p['source_pdf']}): guardado='{p['idioma_guardado']}' "
                f"vs detectado='{p['idioma_detectado']}'"
            )
