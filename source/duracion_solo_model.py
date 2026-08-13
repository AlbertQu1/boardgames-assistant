"""
Predice duracion de una partida en MODO SOLITARIO (tag_solo=true en
bgstats.partidas) usando los mismos datos de BGG que duracion_model.py
(peso_complejidad, playtime, rating) mas clima del dia y min/max_jugadores
del juego (BGG) -- esto ultimo distingue juegos solo puros (min_jugadores=1,
ej. GROVE) de juegos multijugador adaptados con Automa (min_jugadores>1,
ej. Terraforming Mars), que suelen tomar tiempos distintos. Se omiten
categoria_lugar/grupo_social/dependencia_idioma — casi todo el modo solo
es en casa y sin otros jugadores, no aportan senal. Dataset mucho mas chico
(~199 filas) que el modelo multijugador (~1550), por eso el feature set
es mas simple para no sobreajustar.

Uso (solo para pruebas manuales):
    python source/duracion_solo_model.py
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

FEATURES = [
    "peso_complejidad", "min_playtime", "max_playtime", "calificacion_promedio",
    "temp_media_c", "min_jugadores", "max_jugadores",
]


def cargar_datos(conn) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.duracion_min, d.peso_complejidad, d.min_playtime, d.max_playtime, d.calificacion_promedio,
               c.temp_media_c, j.min_jugadores, j.max_jugadores
        FROM bgstats.partidas p
        JOIN bgstats.juegos j ON j.uuid = p.juego_uuid
        JOIN bgg_data.juegos_detalle d ON d.bgg_id = j.bgg_id
        LEFT JOIN bgstats.clima_diario c ON c.lugar_uuid = p.lugar_uuid AND c.fecha = p.fecha::date
        WHERE p.tag_solo AND p.duracion_min > 0 AND d.peso_complejidad IS NOT NULL
        """
    )
    return cur.fetchall()


def entrenar(conn):
    filas = cargar_datos(conn)
    if len(filas) < 20:
        return None

    y = np.array([float(f[0]) for f in filas])
    x_raw = np.array([[f[1], f[2], f[3], f[4], f[5], f[6], f[7]] for f in filas], dtype=float)

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


def predecir(resultado_entrenamiento: dict, valores: dict) -> float:
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
