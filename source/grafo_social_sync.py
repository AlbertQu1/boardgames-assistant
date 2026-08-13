"""
Puebla el grafo 'red_social' (Apache AGE, extension sobre la misma Postgres
'casa') con jugadores y relaciones "jugo con" -- separado en dos tipos de
relacion para no mezclar tu red propia con la de amigos (BGG), mismo
principio de nunca fusionar bgstats.* con bgg_data.* a nivel de storage:

- JUEGA_CON_PROPIO: coocurrencia en bgstats.partida_jugadores
- JUEGA_CON_AMIGOS: coocurrencia en bgg_data.plays_amigos.jugadores,
  resuelto via bgg_data.jugadores_identificados donde aplica

Se conecta con psycopg2 normal (DATABASE_URL) + LOAD 'age' -- no usa el
paquete pip "age" (no instalado, y su forma de pasar parametros via
cypher(..., params) no funciono con esta version 1.7.0~rc0: "third
argument of cypher function must be a parameter"). En vez de eso los
valores se embeben como literales Cypher con escape manual
(cypher_str) -- mismo patron validado a mano antes de escribir esto.

Idempotente via recalculo completo (DELETE de todo el grafo + re-insert),
mas simple que diffs incrementales con un dataset de este tamano. Correr
de nuevo despues de cada sync para mantener el grafo actualizado.

Uso:
    python source/grafo_social_sync.py
"""

import os
from itertools import combinations

import psycopg2
from dotenv import load_dotenv

load_dotenv()

GRAFO = "red_social"


def cypher_str(valor) -> str:
    """Literal de cadena para Cypher, con escape de backslash y comilla simple."""
    if valor is None:
        return "null"
    return "'" + str(valor).replace("\\", "\\\\").replace("'", "\\'") + "'"


def construir_edges_propio(cur):
    cur.execute(
        """
        SELECT pj.partida_uuid, j.nombre, j.grupo_social
        FROM bgstats.partida_jugadores pj
        JOIN bgstats.jugadores j ON j.uuid = pj.jugador_uuid
        WHERE j.nombre NOT LIKE '%🤖%'
        """
    )
    partidas: dict[str, list[tuple[str, str | None]]] = {}
    for partida_uuid, nombre, grupo in cur.fetchall():
        partidas.setdefault(str(partida_uuid), []).append((nombre, grupo))

    nodos: dict[str, str | None] = {}
    edges: dict[tuple[str, str], int] = {}
    for jugadores in partidas.values():
        for nombre, grupo in jugadores:
            nodos[nombre] = grupo
        for (n1, _), (n2, _) in combinations(jugadores, 2):
            if n1 == n2:
                continue
            clave = tuple(sorted((n1, n2)))
            edges[clave] = edges.get(clave, 0) + 1
    return nodos, edges


def construir_edges_amigos(cur):
    cur.execute("SELECT nombre_variante, persona_real, grupo_social FROM bgg_data.jugadores_identificados")
    identificados = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    cur.execute("SELECT jugadores FROM bgg_data.plays_amigos WHERE usable_para_analisis")
    nodos: dict[str, str | None] = {}
    edges: dict[tuple[str, str], int] = {}
    for (jugadores,) in cur.fetchall():
        resueltos = set()
        for j in jugadores:
            nombre_crudo = (j.get("nombre") or "").strip()
            if not nombre_crudo:
                continue
            clave = nombre_crudo.lower()
            nombre_final, grupo = identificados.get(clave, (nombre_crudo, None))
            resueltos.add(nombre_final)
            nodos[nombre_final] = grupo
        for n1, n2 in combinations(sorted(resueltos), 2):
            edges[(n1, n2)] = edges.get((n1, n2), 0) + 1
    return nodos, edges


def sync() -> dict:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("LOAD 'age'")
    cur.execute('SET search_path = ag_catalog, "$user", public')

    nodos_propio, edges_propio = construir_edges_propio(cur)
    nodos_amigos, edges_amigos = construir_edges_amigos(cur)

    todos_los_nodos = dict(nodos_propio)
    for n, g in nodos_amigos.items():
        todos_los_nodos.setdefault(n, g)

    cur.execute(f"SELECT * FROM cypher('{GRAFO}', $$ MATCH (n) DETACH DELETE n $$) AS (v agtype)")

    for nombre, grupo in todos_los_nodos.items():
        cur.execute(
            f"""
            SELECT * FROM cypher('{GRAFO}', $$
                MERGE (p:Persona {{nombre: {cypher_str(nombre)}}})
                SET p.grupo_social = {cypher_str(grupo)}
            $$) AS (v agtype)
            """
        )

    for (n1, n2), peso in edges_propio.items():
        cur.execute(
            f"""
            SELECT * FROM cypher('{GRAFO}', $$
                MATCH (a:Persona {{nombre: {cypher_str(n1)}}}), (b:Persona {{nombre: {cypher_str(n2)}}})
                MERGE (a)-[r:JUEGA_CON_PROPIO]-(b)
                SET r.peso = {peso}
            $$) AS (v agtype)
            """
        )

    for (n1, n2), peso in edges_amigos.items():
        cur.execute(
            f"""
            SELECT * FROM cypher('{GRAFO}', $$
                MATCH (a:Persona {{nombre: {cypher_str(n1)}}}), (b:Persona {{nombre: {cypher_str(n2)}}})
                MERGE (a)-[r:JUEGA_CON_AMIGOS]-(b)
                SET r.peso = {peso}
            $$) AS (v agtype)
            """
        )

    cur.close()
    conn.close()
    return {
        "nodos": len(todos_los_nodos),
        "edges_propio": len(edges_propio),
        "edges_amigos": len(edges_amigos),
    }


if __name__ == "__main__":
    print(f"Sincronizado: {sync()}")
