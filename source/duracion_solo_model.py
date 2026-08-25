"""
Predice duracion de una partida en MODO SOLITARIO (tag_solo=true en
boardgames_stats.partidas) usando los mismos datos de BGG que duracion_model.py
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
    "media_juego_propia", "n_partidas_juego", "media_juego_amigos",
]


def cargar_datos(conn) -> list[tuple]:
    """Ademas de las features de BGG, trae el historial REAL del mismo juego:
    cuanto le ha tomado a Alberto (media_juego_propia) y a sus amigos
    (media_juego_amigos). Sin esto el modelo solo generalizaba por peso/
    playtime de BGG e ignoraba que ya habia jugado ese titulo -- ej. Robinson
    Crusoe predecia 62 min contra un promedio real propio de 46.

    media_juego_propia se calcula LEAVE-ONE-OUT (se resta la fila misma del
    promedio de su juego) para no filtrar el target dentro del feature; sin
    eso el CV se ve artificialmente bueno y el modelo no generaliza. Los
    juegos con una sola partida quedan en NULL (no hay historial previo) y
    caen a la mediana como cualquier otro hueco.

    media_juego_amigos no necesita leave-one-out: viene de
    boardgames_bgg.plays_amigos, que nunca es target de este modelo."""
    cur = conn.cursor()
    cur.execute(
        """
        WITH propias AS (
            SELECT p.juego_uuid, sum(p.duracion_min)::numeric AS suma, count(*) AS n
            FROM boardgames_stats.partidas p
            WHERE p.duracion_min > 0
            GROUP BY p.juego_uuid
        ),
        amigos AS (
            SELECT pa.bgg_game_id, avg(pa.duracion_min)::numeric AS media
            FROM boardgames_bgg.plays_amigos pa
            WHERE pa.usable_para_analisis AND pa.duracion_min > 0
            GROUP BY pa.bgg_game_id
        )
        SELECT p.duracion_min, d.peso_complejidad, d.min_playtime, d.max_playtime, d.calificacion_promedio,
               c.temp_media_c, j.min_jugadores, j.max_jugadores,
               CASE WHEN pr.n > 1 THEN (pr.suma - p.duracion_min) / (pr.n - 1) END AS media_juego_propia,
               pr.n - 1 AS n_partidas_juego,
               am.media AS media_juego_amigos
        FROM boardgames_stats.partidas p
        JOIN boardgames_stats.juegos j ON j.uuid = p.juego_uuid
        JOIN boardgames_bgg.juegos_detalle d ON d.bgg_id = j.bgg_id
        LEFT JOIN boardgames_stats.clima_diario c ON c.lugar_uuid = p.lugar_uuid AND c.fecha = p.fecha::date
        LEFT JOIN propias pr ON pr.juego_uuid = p.juego_uuid
        LEFT JOIN amigos am ON am.bgg_game_id = j.bgg_id
        WHERE p.tag_solo AND p.duracion_min > 0 AND d.peso_complejidad IS NOT NULL
        """
    )
    return cur.fetchall()


def _ajustar(y: np.ndarray, x_raw: np.ndarray, n_filas: int) -> dict:
    """Entrena los 3 candidatos sobre una matriz ya armada y regresa el mejor."""
    medianas = np.nanmedian(x_raw, axis=0)
    inds = np.where(np.isnan(x_raw))
    x_raw = x_raw.copy()
    x_raw[inds] = np.take(medianas, inds[1])

    scaler = StandardScaler()
    x = scaler.fit_transform(x_raw)

    cv = KFold(n_splits=min(5, n_filas), shuffle=True, random_state=42)
    candidatos = {
        "Lasso": LassoCV(cv=cv, random_state=42, max_iter=10000),
        "Ridge": RidgeCV(cv=cv),
        "ElasticNet": ElasticNetCV(cv=cv, random_state=42, max_iter=10000),
    }
    resultados = {}
    for nombre, modelo in candidatos.items():
        resultados[nombre] = mean_absolute_error(y, cross_val_predict(modelo, x, y, cv=cv))

    ganador = min(resultados, key=resultados.get)
    modelo_final = candidatos[ganador]
    modelo_final.fit(x, y)
    return {
        "modelo": modelo_final, "scaler": scaler, "medianas": medianas,
        "ganador": ganador, "mae_por_modelo": resultados, "n": n_filas,
    }


def entrenar(conn):
    """Entrena DOS modelos:

    - el principal usa el historial del mismo juego (media_juego_propia), que
      es la senal mas fuerte, pero solo sirve si ese juego ya se jugo antes;
    - el fallback usa solo las features de BGG (peso/playtime/etc), para
      juegos que nunca se han jugado. Sin el, un juego nuevo caia a la
      mediana de media_juego_propia y terminaba prediciendo "juego promedio"
      sin importar su peso -- justo lo que el modelo viejo si hacia bien.

    predecir() elige uno u otro segun haya historial del juego consultado."""
    filas = cargar_datos(conn)
    if len(filas) < 20:
        return None

    y = np.array([float(f[0]) for f in filas])
    x_completo = np.array(
        [[f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9], f[10]] for f in filas], dtype=float
    )
    r = _ajustar(y, x_completo, len(filas))

    # fallback: mismas filas, sin las 3 features de historial (las ultimas)
    n_base = len(FEATURES) - 3
    r_fallback = _ajustar(y, x_completo[:, :n_base], len(filas))

    r["mae_baseline"] = mean_absolute_error(y, np.full_like(y, y.mean()))
    r["coeficientes"] = dict(zip(FEATURES, getattr(r["modelo"], "coef_", [None] * len(FEATURES))))
    r["fallback"] = r_fallback
    r["fallback"]["coeficientes"] = dict(
        zip(FEATURES[:n_base], getattr(r_fallback["modelo"], "coef_", [None] * n_base))
    )
    return r


def predecir(resultado_entrenamiento: dict, valores: dict) -> float:
    """Usa el modelo con historial si el juego ya se jugo; si no, cae al
    fallback que solo mira las features de BGG (ver entrenar)."""
    sin_historial = valores.get("media_juego_propia") is None
    r = resultado_entrenamiento["fallback"] if sin_historial else resultado_entrenamiento
    features = FEATURES[: len(FEATURES) - 3] if sin_historial else FEATURES

    fila = np.array([[valores.get(f, np.nan) for f in features]], dtype=float)
    inds = np.where(np.isnan(fila))
    fila[inds] = np.take(r["medianas"], inds[1])
    x = r["scaler"].transform(fila)
    # un modelo lineal puede irse a ~0 o negativo en los extremos (ej. un juego
    # muy ligero nunca visto daba 0.6 min); ninguna partida real dura menos de
    # unos minutos, asi que se acota por abajo en vez de regresar un absurdo.
    return max(5.0, float(r["modelo"].predict(x)[0]))


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
