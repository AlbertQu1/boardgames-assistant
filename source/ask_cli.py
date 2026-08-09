"""
CLI de prueba para /ask: imprime la respuesta y las fuentes de forma legible,
en vez de JSON crudo.

Uso:
    python source/ask_cli.py --pregunta "Como se juega Ticket to Ride para 2 jugadores?"
    python source/ask_cli.py --pregunta "..." --juego "Ticket to Ride"
"""

import argparse

import requests

API_URL = "http://localhost:8000"


def ask(pregunta: str, juego: str | None = None):
    body = {"pregunta": pregunta}
    if juego:
        body["juego"] = juego
    resp = requests.post(f"{API_URL}/ask", json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pregunta", required=True)
    parser.add_argument("--juego", default=None)
    args = parser.parse_args()

    data = ask(args.pregunta, args.juego)

    print("\n" + "=" * 60)
    print(data["respuesta"])
    print("=" * 60)

    print("\nFuentes:")
    vistos = set()
    for f in data["fuentes"]:
        clave = (f["juego"], f["source_pdf"], f["chunk_index"])
        if clave in vistos:
            continue
        vistos.add(clave)
        print(f"  - {f['juego']} ({f['idioma']}) | {f['source_pdf']} | chunk {f['chunk_index']}")
