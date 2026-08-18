import requests

import params


def embed(textos: list[str]) -> list[list[float]]:
    resp = requests.post(f"{params.EMBEDDINGS_SERVICE_URL}/embed", json={"textos": textos}, timeout=30)
    resp.raise_for_status()
    return resp.json()["embeddings"]
