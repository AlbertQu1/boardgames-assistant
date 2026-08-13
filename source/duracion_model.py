"""
Predice duracion real de una partida (duracion_min) usando datos de BGG
(peso_complejidad, dependencia_idioma, min/max_playtime) + clima del
lugar/dia + numero de jugadores real. Feature set validado a mano contra la
base real antes de escribir este modulo (sesion 2026-08-11) — quedaron
fuera por correlacion nula/redundante: hora del dia, festivos oficiales de
Mexico, suggested_numplayers (poll de BGG), temporada de lluvias/secas
(redundante con temp_media_c).

Dataset chico (~1500 filas), entrena en menos de 1 segundo — se entrena en
cada llamada al endpoint, no hace falta cachear el modelo entre requests.

Uso (solo para pruebas manuales):
    python source/duracion_model.py
"""

import os

import numpy as np
import psycopg2
from dotenv import load_dotenv
from sklearn.linear_model import ElasticNetCV, LassoCV, RidgeCV
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import KFold, cross_val_predict
from sklearn.preprocessing import StandardScaler

load_dotenv()

FEATURES_BASE = [
    "peso_complejidad", "dependencia_idioma", "temp_media_c", "calificacion_promedio",
    "num_jugadores", "min_playtime", "max_playtime", "tag_digital", "usa_expansion",
]
# categoria_lugar: diccionario armado a mano con Alberto (sesion 2026-08-11)
# cruzando tags "Location" de BG Stats donde existian + contexto que el sabe
# de cada lugar. "sin_clasificar" es la categoria base (todo en 0), no lleva
# columna propia. Mejora el MAE solo un poco (~0.1 min) una vez combinado
# con las demas features, pero la direccion de los coeficientes coincide
# con las diferencias reales de duracion promedio por categoria.
CATEGORIAS_LUGAR = [
    "casa_propia", "cafe", "fuera", "evento", "amigos", "expareja", "pareja",
    "otros",  # casas del circulo social de un amigo (ej. Vinicio via BGG), no necesariamente el de Alberto
]
# grupo_social: tags "Player" que ya existian en BG Stats (Reformers, Cartoneros,
# GEM, Cul/Cdmx/Gdl por ciudad, etc). Senal aun mas fuerte que categoria_lugar
# en la validacion (rango 18.7-50.8 min segun grupo vs. 19-47.8 de lugar).
# Se calcula con JOIN en vivo a bgstats.jugadores.grupo_social, no queda
# congelado por partida — si alguien se vuelve anonimo en BG Stats (perfil
# se fusiona al generico compartido, sin tag), sus partidas historicas
# pierden la senal salvo que haya una fila en partida_grupo_social_override
# (ver bgstats.partida_grupo_social_override, poblada a mano caso por caso).
CATEGORIAS_GRUPO = [
    "Reformers", "Solo", "Pup", "Cartoneros", "GEM", "Cdmx", "Otros", "Extra", "Cul", "Entreturnos",
    "Cun", "Gdl",  # grupos chicos hoy (13/5 partidas) pero reales, pueden crecer
]
FEATURES = FEATURES_BASE + [f"lugar_{c}" for c in CATEGORIAS_LUGAR] + [f"grupo_{g}" for g in CATEGORIAS_GRUPO]


def cargar_datos(conn, incluir_amigos: bool = False) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.duracion_min, d.peso_complejidad, d.dependencia_idioma,
               c.temp_media_c, d.calificacion_promedio,
               (SELECT count(*) FROM bgstats.partida_jugadores pj WHERE pj.partida_uuid = p.uuid) AS num_jugadores,
               d.min_playtime, d.max_playtime, p.tag_digital,
               (p.expansiones_usadas IS NOT NULL AND array_length(p.expansiones_usadas, 1) > 0) AS usa_expansion,
               l.categoria_lugar,
               COALESCE(
                   gso.grupo_social,
                   (
                       SELECT mode() WITHIN GROUP (ORDER BY jg.grupo_social)
                       FROM bgstats.partida_jugadores pj2
                       JOIN bgstats.jugadores jg ON jg.uuid = pj2.jugador_uuid
                       WHERE pj2.partida_uuid = p.uuid AND jg.grupo_social IS NOT NULL
                   )
               ) AS grupo_social
        FROM bgstats.partidas p
        JOIN bgstats.juegos j ON j.uuid = p.juego_uuid
        JOIN bgg_data.juegos_detalle d ON d.bgg_id = j.bgg_id
        LEFT JOIN bgstats.clima_diario c ON c.lugar_uuid = p.lugar_uuid AND c.fecha = p.fecha::date
        LEFT JOIN bgstats.lugares l ON l.uuid = p.lugar_uuid
        LEFT JOIN bgstats.partida_grupo_social_override gso ON gso.partida_uuid = p.uuid
        WHERE p.duracion_min > 0 AND d.peso_complejidad IS NOT NULL
        """
    )
    filas = cur.fetchall()

    if incluir_amigos:
        # bgg_data.plays_amigos (partidas de amigos registradas directo en BGG,
        # NUNCA se fusiona con bgstats.partidas en Postgres — union solo en
        # memoria, aqui, para entrenar un modelo mas robusto). Clima es un
        # proxy general de CDMX por fecha (bgg_data.clima_cdmx_diario, no por
        # lugar exacto -- no tenemos coordenadas de "Entreturnos"/"Mojo Dojo"
        # de este lado). grupo_social queda NULL (no son jugadores tuyos con
        # perfil); el entrenamiento rellena con la mediana como cualquier
        # otro dato faltante.
        cur.execute(
            """
            SELECT pa.duracion_min, d.peso_complejidad, d.dependencia_idioma,
                   cc.temp_media_c, d.calificacion_promedio,
                   jsonb_array_length(pa.jugadores) AS num_jugadores,
                   d.min_playtime, d.max_playtime, FALSE AS tag_digital, FALSE AS usa_expansion,
                   pa.categoria_lugar, NULL::text AS grupo_social
            FROM bgg_data.plays_amigos pa
            JOIN bgg_data.juegos_detalle d ON d.bgg_id = pa.bgg_game_id
            LEFT JOIN bgg_data.clima_cdmx_diario cc ON cc.fecha = pa.fecha
            WHERE pa.usable_para_analisis AND pa.duracion_min > 0 AND d.peso_complejidad IS NOT NULL
            """
        )
        filas += cur.fetchall()

    return filas


def fila_con_categoria(base: list, categoria_lugar: str | None, grupo_social: str | None) -> list:
    dummies_lugar = [1.0 if categoria_lugar == c else 0.0 for c in CATEGORIAS_LUGAR]
    dummies_grupo = [1.0 if grupo_social == g else 0.0 for g in CATEGORIAS_GRUPO]
    return base + dummies_lugar + dummies_grupo


def entrenar(conn, incluir_amigos: bool = False):
    filas = cargar_datos(conn, incluir_amigos=incluir_amigos)
    if len(filas) < 20:
        return None

    y = np.array([float(f[0]) for f in filas])
    x_raw = np.array(
        [
            fila_con_categoria(
                [f[1], f[2], f[3], f[4], f[5], f[6], f[7], float(bool(f[8])), float(bool(f[9]))], f[10], f[11]
            )
            for f in filas
        ],
        dtype=float,
    )
    # temp_media_c puede venir NULL (lugar sin geolocalizar o sin clima
    # sincronizado) — se rellena con la mediana en vez de tirar la fila,
    # mismo criterio que ya usa el proyecto de Coffee para huecos de clima
    medianas = np.nanmedian(x_raw, axis=0)
    inds = np.where(np.isnan(x_raw))
    x_raw[inds] = np.take(medianas, inds[1])

    scaler = StandardScaler()
    x = scaler.fit_transform(x_raw)

    cv = KFold(n_splits=min(5, len(filas)), shuffle=True, random_state=42)
    candidatos = {
        "Lasso": LassoCV(cv=cv, random_state=42, max_iter=10000),
        "Ridge": RidgeCV(cv=cv),
        "ElasticNet": ElasticNetCV(cv=cv, random_state=42, max_iter=10000),
    }

    resultados = {}
    for nombre, modelo in candidatos.items():
        pred = cross_val_predict(modelo, x, y, cv=cv)
        resultados[nombre] = mean_absolute_error(y, pred)

    ganador = min(resultados, key=resultados.get)
    modelo_final = candidatos[ganador]
    modelo_final.fit(x, y)

    baseline_mae = mean_absolute_error(y, np.full_like(y, y.mean()))

    return {
        "modelo": modelo_final,
        "scaler": scaler,
        "medianas": medianas,
        "ganador": ganador,
        "mae_por_modelo": resultados,
        "mae_baseline": baseline_mae,
        "n": len(filas),
        "coeficientes": dict(zip(FEATURES, getattr(modelo_final, "coef_", [None] * len(FEATURES)))),
    }


def predecir(
    resultado_entrenamiento: dict,
    valores: dict,
    categoria_lugar: str | None = None,
    grupo_social: str | None = None,
) -> float:
    for c in CATEGORIAS_LUGAR:
        valores.setdefault(f"lugar_{c}", 1.0 if categoria_lugar == c else 0.0)
    for g in CATEGORIAS_GRUPO:
        valores.setdefault(f"grupo_{g}", 1.0 if grupo_social == g else 0.0)
    fila = np.array([[valores.get(f, np.nan) for f in FEATURES]], dtype=float)
    inds = np.where(np.isnan(fila))
    fila[inds] = np.take(resultado_entrenamiento["medianas"], inds[1])
    x = resultado_entrenamiento["scaler"].transform(fila)
    return float(resultado_entrenamiento["modelo"].predict(x)[0])


if __name__ == "__main__":
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    r = entrenar(conn)
    if r is None:
        print("No hay suficientes datos para entrenar.")
    else:
        print(f"Ganador: {r['ganador']}  (n={r['n']})")
        print(f"MAE por modelo: {r['mae_por_modelo']}")
        print(f"MAE baseline (promedio simple): {r['mae_baseline']:.1f}")
        print("Coeficientes:")
        for f, c in r["coeficientes"].items():
            print(f"  {f}: {c:+.2f}" if c is not None else f"  {f}: n/a")
    conn.close()
