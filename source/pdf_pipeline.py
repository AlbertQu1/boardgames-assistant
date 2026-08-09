"""
Indexa un PDF de reglamento: extrae texto, lo trocea, genera embeddings
y los inserta en boardgames.rulebook_chunks.

Uso:
    python source/pdf_pipeline.py --juego "Wingspan" --pdf pdfs_prueba/wingspan.pdf
"""

import argparse
import os

import psycopg2
from dotenv import load_dotenv
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import params

load_dotenv()


def extract_text(pdf_path: str) -> str:
    reader = PdfReader(pdf_path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunks.append(" ".join(words[start:end]))
        start = end - overlap
    return chunks


def index_pdf(juego: str, pdf_path: str, idioma: str, doc_type: str, juego_base: str | None) -> int:
    text = extract_text(pdf_path)
    chunks = chunk_text(text, params.CHUNK_SIZE, params.CHUNK_OVERLAP)

    model = SentenceTransformer(params.EMBEDDING_MODEL)
    embeddings = model.encode(chunks, show_progress_bar=True)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        f"""
        DELETE FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE}
        WHERE juego = %s AND source_pdf = %s
        """,
        (juego, os.path.basename(pdf_path)),
    )
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        cur.execute(
            f"""
            INSERT INTO {params.DB_SCHEMA}.{params.CHUNKS_TABLE}
                (juego, source_pdf, chunk_index, chunk_text, embedding, idioma, doc_type, juego_base)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (juego, os.path.basename(pdf_path), i, chunk, embedding.tolist(), idioma, doc_type, juego_base),
        )
    conn.commit()
    cur.close()
    conn.close()
    return len(chunks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--juego", required=True, help="Nombre del juego")
    parser.add_argument("--pdf", required=True, help="Ruta al PDF del reglamento")
    parser.add_argument("--idioma", default="es", help="Idioma del PDF (es, en, ...)")
    parser.add_argument(
        "--doc-type", default="reglamento", choices=["reglamento", "errata", "faq"]
    )
    parser.add_argument(
        "--juego-base", default=None, help="Nombre del juego base (solo si esto es una expansion)"
    )
    args = parser.parse_args()

    n = index_pdf(args.juego, args.pdf, args.idioma, args.doc_type, args.juego_base)
    print(f"Indexados {n} chunks de '{args.juego}' ({args.pdf}, idioma={args.idioma}, doc_type={args.doc_type}, juego_base={args.juego_base})")
