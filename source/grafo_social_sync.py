"""
Puebla el grafo 'red_social' (Apache AGE, extension sobre la misma Postgres
'casa') con jugadores y relaciones "jugo con" -- separado en dos tipos de
relacion para no mezclar tu red propia con la de amigos (BGG), mismo
principio de nunca fusionar boardgames_stats.* con boardgames_bgg.* a nivel de storage:

- JUEGA_CON_PROPIO: coocurrencia en boardgames_stats.partida_jugadores
- JUEGA_CON_AMIGOS: coocurrencia en boardgames_bgg.plays_amigos.jugadores,
  resuelto via boardgames_bgg.jugadores_identificados donde aplica

Se conecta con psycopg2 normal (DATABASE_URL) + LOAD 'age' -- no usa el
paquete pip "age" (no instalado, y su forma de pasar parametros via
cypher(..., params) no funciono con esta version 1.7.0~rc0: "third
argument of cypher function must be a parameter"). En vez de eso los
valores se embeben como literales Cypher con escape manual
(cypher_str) -- mismo patron validado a mano antes de escribir esto.

Idempotente via recalculo: se borran solo las relaciones que este script
controla (JUEGA_CON_PROPIO, JUEGA_CON_AMIGOS, VISITO) y se reconstruyen,
sin tocar nodos Persona/Casa ni otro tipo de relacion (Evento/ASISTIO, que
pertenece a personal-assistant/source/graph_sync.py). Antes (hasta
2026-08-17) hacia un DETACH DELETE de TODO el grafo -- borraba tambien lo
de graph_sync.py y cualquier fusion manual de duplicados, cada vez que
llegaba un export nuevo de BG Stats. Corregido el mismo dia que se
encontro, tras una sesion completa de fusionar duplicados a mano
(Grace/Hilcris/Olga/etc.) que un sync nuevo hubiera deshecho por completo.

Cada nombre crudo de jugador se resuelve contra personal_wiki.personas
(nombre_canonico o alias, case-insensitive) ANTES de crear/tocar el nodo
-- mismo patron ya usado para el problema de "angel ulises" (nombre de
visita que no matcheaba exacto contra el jugador real), aplicado aqui de
forma general para que un nodo corto (ej. "Grace") jamas se vuelva a crear
por separado si ya existe una entrada completa ("Grace Quintero") en el
catalogo. Cache en memoria por corrida (un nombre no cambia a media
ejecucion).

Correr de nuevo despues de cada sync para mantener el grafo actualizado.

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


def resolver_nombre_canonico(cur, cache: dict[str, str], nombre_crudo: str) -> str:
    """Busca nombre_crudo en personal_wiki.personas (nombre_canonico o
    alias). Si hay match, regresa el nombre_canonico completo -- asi
    "Grace" siempre resuelve a "Grace Quintero" y nunca se vuelve a crear
    como nodo aparte. Sin match, regresa el nombre tal cual."""
    clave = nombre_crudo.strip().lower()
    if clave in cache:
        return cache[clave]
    cur.execute(
        """
        SELECT nombre_canonico FROM personal_wiki.personas
        WHERE lower(nombre_canonico) = %s OR %s = ANY(SELECT lower(a) FROM unnest(alias) AS a)
        """,
        (clave, clave),
    )
    row = cur.fetchone()
    resultado = row[0] if row else nombre_crudo
    cache[clave] = resultado
    return resultado


def canonicalizar_nodos(cur, cache: dict[str, str], nodos: dict[str, str | None]) -> dict[str, str | None]:
    resultado: dict[str, str | None] = {}
    for nombre, grupo in nodos.items():
        canon = resolver_nombre_canonico(cur, cache, nombre)
        if grupo or canon not in resultado:
            resultado[canon] = grupo or resultado.get(canon)
    return resultado


def canonicalizar_edges(
    cur, cache: dict[str, str], edges: dict[tuple[str, str], int]
) -> dict[tuple[str, str], int]:
    resultado: dict[tuple[str, str], int] = {}
    for (n1, n2), peso in edges.items():
        c1 = resolver_nombre_canonico(cur, cache, n1)
        c2 = resolver_nombre_canonico(cur, cache, n2)
        if c1 == c2:
            continue
        clave = tuple(sorted((c1, c2)))
        resultado[clave] = resultado.get(clave, 0) + peso
    return resultado


def canonicalizar_visitas(cur, cache: dict[str, str], visitas: dict[str, int]) -> dict[str, int]:
    resultado: dict[str, int] = {}
    for nombre, peso in visitas.items():
        canon = resolver_nombre_canonico(cur, cache, nombre)
        resultado[canon] = resultado.get(canon, 0) + peso
    return resultado


def construir_edges_propio(cur):
    cur.execute(
        """
        SELECT pj.partida_uuid, j.nombre, j.grupo_social
        FROM boardgames_stats.partida_jugadores pj
        JOIN boardgames_stats.jugadores j ON j.uuid = pj.jugador_uuid
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


def construir_visitas(cur):
    """Nodos + relacion VISITO desde calendario_eventos (tipo='visita').
    A proposito NO se limita a personas que ya tienen Persona por jugar --
    alguien puede visitar y afectar patrones de consumo (ej. soda) sin
    necesariamente jugar (o sin que su partida se haya logueado), asi que
    el nombre crudo del evento entra como Persona nueva si hace falta."""
    cur.execute(
        """
        SELECT COALESCE(j.nombre, ce.nombre) AS nombre, j.grupo_social
        FROM boardgames_stats.calendario_eventos ce
        LEFT JOIN boardgames_stats.jugadores j ON j.uuid = ce.jugador_uuid
        WHERE ce.tipo = 'visita'
        """
    )
    nodos: dict[str, str | None] = {}
    visitas: dict[str, int] = {}
    for nombre, grupo in cur.fetchall():
        nodos.setdefault(nombre, grupo)
        visitas[nombre] = visitas.get(nombre, 0) + 1
    return nodos, visitas


def construir_edges_amigos(cur):
    cur.execute("SELECT nombre_variante, persona_real, grupo_social FROM boardgames_bgg.jugadores_identificados")
    identificados = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    cur.execute("SELECT jugadores FROM boardgames_bgg.plays_amigos WHERE usable_para_analisis")
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
    nodos_visitas, visitas = construir_visitas(cur)

    cache_nombres: dict[str, str] = {}
    nodos_propio = canonicalizar_nodos(cur, cache_nombres, nodos_propio)
    edges_propio = canonicalizar_edges(cur, cache_nombres, edges_propio)
    nodos_amigos = canonicalizar_nodos(cur, cache_nombres, nodos_amigos)
    edges_amigos = canonicalizar_edges(cur, cache_nombres, edges_amigos)
    nodos_visitas = canonicalizar_nodos(cur, cache_nombres, nodos_visitas)
    visitas = canonicalizar_visitas(cur, cache_nombres, visitas)

    todos_los_nodos = dict(nodos_propio)
    for n, g in nodos_amigos.items():
        todos_los_nodos.setdefault(n, g)
    for n, g in nodos_visitas.items():
        todos_los_nodos.setdefault(n, g)

    # Borrado quirurgico: solo las relaciones que este script controla, no
    # los nodos (Persona/Casa se preservan con cualquier propiedad que
    # otros scripts les hayan puesto, ej. cumpleanos) ni Evento/ASISTIO
    # (pertenece a personal-assistant/source/graph_sync.py).
    for tipo in ("JUEGA_CON_PROPIO", "JUEGA_CON_AMIGOS", "VISITO"):
        cur.execute(f"SELECT * FROM cypher('{GRAFO}', $$ MATCH ()-[r:{tipo}]-() DELETE r $$) AS (v agtype)")

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

    if visitas:
        cur.execute(f"SELECT * FROM cypher('{GRAFO}', $$ MERGE (c:Casa) $$) AS (v agtype)")
    for nombre, peso in visitas.items():
        cur.execute(
            f"""
            SELECT * FROM cypher('{GRAFO}', $$
                MATCH (p:Persona {{nombre: {cypher_str(nombre)}}}), (c:Casa)
                MERGE (p)-[r:VISITO]->(c)
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
        "edges_visitas": len(visitas),
    }


if __name__ == "__main__":
    print(f"Sincronizado: {sync()}")
