"""
Query de prueba: busca los chunks mas relevantes para una pregunta.

Uso:
    python source/query_test.py --pregunta "Como se calcula el puntaje final?"
    python source/query_test.py --pregunta "..." --juego "Wingspan"
"""

import argparse
import os

import psycopg2
from dotenv import load_dotenv
from pgvector import Vector
from pgvector.psycopg2 import register_vector

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import params
from source.embeddings_client import embed

load_dotenv()


def search(pregunta: str, juego: str | None, idioma: str | None, top_k: int = 5):
    query_embedding = Vector(embed([pregunta])[0])

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    register_vector(conn)
    cur = conn.cursor()

    sql = f"""
        SELECT juego, source_pdf, chunk_index, chunk_text, idioma, doc_type,
               1 - (embedding <=> %s) AS similitud
        FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE}
    """
    args = [query_embedding]
    filtros = []
    if juego:
        filtros.append("(juego = %s OR juego_base = %s)")
        args.extend([juego, juego])
    if idioma:
        filtros.append("idioma = %s")
        args.append(idioma)
    if filtros:
        sql += " WHERE " + " AND ".join(filtros)
    sql += " ORDER BY embedding <=> %s LIMIT %s"
    args += [query_embedding, top_k]

    cur.execute(sql, args)
    results = cur.fetchall()
    cur.close()
    conn.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pregunta", required=True)
    parser.add_argument("--juego", default=None)
    parser.add_argument("--idioma", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    for juego, pdf, idx, texto, idioma, doc_type, sim in search(args.pregunta, args.juego, args.idioma, args.top_k):
        print(f"\n[{juego} | {pdf} | chunk {idx} | idioma={idioma} | doc_type={doc_type} | sim={sim:.3f}]")
        print(texto[:400])
