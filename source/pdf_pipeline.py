"""
Indexa un PDF de reglamento: extrae texto, lo trocea, genera embeddings
y los inserta en boardgames.rulebook_chunks.

Uso:
    python source/pdf_pipeline.py --juego "Wingspan" --pdf pdfs_prueba/wingspan.pdf
"""

import argparse
import hashlib
import os

import psycopg2
import pytesseract
from dotenv import load_dotenv
from docx import Document as DocxDocument
from pdf2image import convert_from_path
from pgvector import Vector
from pgvector.psycopg2 import register_vector
from pypdf import PdfReader

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import params
from source.embeddings_client import embed

load_dotenv()

OCR_MIN_WORDS = 20  # por debajo de esto, se asume que el PDF no tiene texto real (delineado/escaneado)


def extract_text_ocr(pdf_path: str) -> str:
    paginas = convert_from_path(pdf_path, dpi=200)
    return "\n".join(pytesseract.image_to_string(pagina) for pagina in paginas)


def extract_text(pdf_path: str) -> str:
    ext = os.path.splitext(pdf_path)[1].lower()
    if ext == ".pdf":
        reader = PdfReader(pdf_path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(text.split()) < OCR_MIN_WORDS:
            print("Poco o ningun texto extraible, usando OCR (puede tardar varios minutos)...")
            text = extract_text_ocr(pdf_path)
        return text
    elif ext == ".docx":
        doc = DocxDocument(pdf_path)
        return "\n".join(p.text for p in doc.paragraphs)
    else:
        raise ValueError(f"Formato no soportado: {ext} (solo .pdf y .docx)")


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


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
    file_hash = hash_file(pdf_path)
    source_pdf = os.path.basename(pdf_path)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    register_vector(conn)
    cur = conn.cursor()

    cur.execute(
        f"""
        SELECT DISTINCT juego, source_pdf FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE}
        WHERE file_hash = %s AND NOT (juego = %s AND source_pdf = %s)
        """,
        (file_hash, juego, source_pdf),
    )
    duplicados = cur.fetchall()
    if duplicados:
        cur.close()
        conn.close()
        existentes = ", ".join(f"'{j}' ({p})" for j, p in duplicados)
        raise ValueError(f"Este archivo ya esta indexado como: {existentes}")

    text = extract_text(pdf_path)
    chunks = chunk_text(text, params.CHUNK_SIZE, params.CHUNK_OVERLAP)

    embeddings = embed(chunks)

    cur.execute(
        f"""
        DELETE FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE}
        WHERE juego = %s AND source_pdf = %s
        """,
        (juego, source_pdf),
    )
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        cur.execute(
            f"""
            INSERT INTO {params.DB_SCHEMA}.{params.CHUNKS_TABLE}
                (juego, source_pdf, chunk_index, chunk_text, embedding, idioma, doc_type, juego_base, file_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (juego, source_pdf, i, chunk, Vector(embedding), idioma, doc_type, juego_base, file_hash),
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
        "--doc-type", default="reglamento", choices=["reglamento", "errata", "faq", "automa"]
    )
    parser.add_argument(
        "--juego-base", default=None, help="Nombre del juego base (solo si esto es una expansion)"
    )
    args = parser.parse_args()

    n = index_pdf(args.juego, args.pdf, args.idioma, args.doc_type, args.juego_base)
    print(f"Indexados {n} chunks de '{args.juego}' ({args.pdf}, idioma={args.idioma}, doc_type={args.doc_type}, juego_base={args.juego_base})")
