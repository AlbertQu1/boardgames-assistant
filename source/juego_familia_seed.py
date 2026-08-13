"""
Semilla de bgg_data.juego_familia -- agrupa bgg_ids que son la MISMA partida
jugable bajo distintas ediciones/reimpresiones (ej. Everdell vs Everdell:
The Complete Collection), para que al buscar el historial real de un juego
no se pierdan partidas registradas bajo una edicion hermana.

Regla aplicada a mano con Alberto (sesion 2026-08-13): solo se agrupan
bgg_ids que son la MISMA experiencia de juego con distinto empaque/arte
(deluxe, reimpresion, reskin). NO se agrupan variantes de la misma marca
que son juegos jugablemente distintos (ej. Everdell Farshore es otro juego,
Azul: Stained Glass of Sintra es otro juego de la serie Azul, Ticket to
Ride New York/London/etc son mapas de duracion muy distinta -- excepto
Europe + Europa 15th Anniversary, que Alberto confirmo que SI es el mismo).

Este script es un registro de que se corrio, no re-corre nada automatico
en cada sync -- la tabla se edita a mano via Postgres directo cuando
aparece una familia nueva.

Uso (una sola vez, o para volver a aplicar despues de un DROP):
    python source/juego_familia_seed.py
"""

import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

FAMILIAS = [
    # Teotihuacan
    (229853, "Teotihuacan", "Teotihuacan: City of Gods"),
    (381715, "Teotihuacan", "Teotihuacan: City of Gods – Deluxe Master Set"),
    # Everdell (Farshore EXCLUIDO -- es otro juego, confirmado)
    (199792, "Everdell", "Everdell"),
    (332398, "Everdell", "Everdell: The Complete Collection"),
    # Terraforming Mars (familia completa: base + big box + expansiones)
    (167791, "Terraforming Mars", "Terraforming Mars"),
    (311247, "Terraforming Mars", "Terraforming Mars: Big Box"),
    (312319, "Terraforming Mars", "Terraforming Mars: Big Box Promos"),
    (255681, "Terraforming Mars", "Terraforming Mars: Colonies"),
    (247030, "Terraforming Mars", "Terraforming Mars: Prelude"),
    (387809, "Terraforming Mars", "Terraforming Mars: Prelude 2"),
    (218127, "Terraforming Mars", "Terraforming Mars: Hellas & Elysium"),
    (231965, "Terraforming Mars", "Terraforming Mars: Venus Next"),
    (273473, "Terraforming Mars", "Terraforming Mars: Turmoil"),
    (395612, "Terraforming Mars", "Terraforming Mars: Automa"),
    (406001, "Terraforming Mars", "Terraforming Mars: Amazonis & Vastitas"),
    # Viticulture (incluye Wine Crate/Complete Collector's Edition aunque Alberto
    # no la tenga registrada aun, para cuando aparezca)
    (128621, "Viticulture", "Viticulture"),
    (183394, "Viticulture", "Viticulture Essential Edition"),
    (156455, "Viticulture", "Viticulture: Complete Collector's Edition (Wine Crate)"),
    (147101, "Viticulture", "Viticulture: Tuscany"),
    (202174, "Viticulture", "Viticulture: Tuscany Essential Edition"),
    (193823, "Viticulture", "Viticulture: Moor Visitors Expansion"),
    (462369, "Viticulture", "Viticulture: Bordeaux Expansion"),
    (248929, "Viticulture", "Viticulture: Visit from the Rhine Valley"),
    (360226, "Viticulture", "Viticulture World: Cooperative Expansion"),
    (364100, "Viticulture", "Viticulture World: First Game Continent Promo Pack"),
    # reimpresiones/ediciones especiales confirmadas mismo juego
    (822, "Carcassonne", "Carcassonne"),
    (329954, "Carcassonne", "Carcassonne: 20th Anniversary Edition"),
    (232832, "Century", "Century: Golem Edition"),
    (209685, "Century", "Century: Spice Road"),
    (70323, "King of Tokyo", "King of Tokyo"),
    (293141, "King of Tokyo", "King of Tokyo: Dark Edition"),
    (206718, "Ethnos", "Ethnos"),
    (432527, "Ethnos", "Ethnos: 2nd Edition"),
    (271320, "The Castles of Burgundy", "The Castles of Burgundy"),
    (363622, "The Castles of Burgundy", "The Castles of Burgundy: Special Edition"),
    (14996, "Ticket to Ride: Europe", "Ticket to Ride: Europe"),
    (329841, "Ticket to Ride: Europe", "Ticket to Ride: Europa – 15th Anniversary"),
    (39856, "Dixit", "Dixit"),
    (381308, "Dixit", "Dixit: Disney Edition"),
    (92828, "Dixit", "Dixit: Odyssey"),
]


def sync() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bgg_data.juego_familia (
            bgg_id INTEGER PRIMARY KEY,
            familia TEXT NOT NULL,
            nombre TEXT
        )
        """
    )
    for bgg_id, familia, nombre in FAMILIAS:
        cur.execute(
            """
            INSERT INTO bgg_data.juego_familia (bgg_id, familia, nombre)
            VALUES (%s, %s, %s)
            ON CONFLICT (bgg_id) DO UPDATE SET familia = EXCLUDED.familia, nombre = EXCLUDED.nombre
            """,
            (bgg_id, familia, nombre),
        )
    conn.commit()
    cur.close()
    conn.close()
    return {"bgg_ids": len(FAMILIAS)}


if __name__ == "__main__":
    print(f"Sincronizado: {sync()}")
