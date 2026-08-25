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
    "num_jugadores", "min_playtime", "max_playtime", "tag_digital", "usa_expansion", "tag_solo",
    # historial real del MISMO juego -- sin esto el modelo generalizaba solo por
    # peso/playtime de BGG e ignoraba lo que ya sabe de ese titulo. En el modelo
    # solo resulto el feature mas fuerte con diferencia (+29.6 vs +1.8 de peso).
    # media_juego_propia es leave-one-out al entrenar (se resta la fila misma)
    # para no filtrar el target; media_juego_amigos viene de plays_amigos, que
    # no es target de este modelo, asi que no necesita LOO.
    "media_juego_propia", "n_partidas_juego", "media_juego_amigos",
]
# categoria_lugar: diccionario armado a mano con Alberto (sesion 2026-08-11)
# cruzando tags "Location" de BG Stats donde existian + contexto que el sabe
# de cada lugar. "sin_clasificar" es la categoria base (todo en 0), no lleva
# columna propia. Mejora el MAE solo un poco (~0.1 min) una vez combinado
# con las demas features, pero la direccion de los coeficientes coincide
# con las diferencias reales de duracion promedio por categoria.
CATEGORIAS_LUGAR = [
    "casa_propia", "cafe", "fuera", "evento", "amigos", "expareja", "pareja",
    "otros",  # catch-all real, sin patron identificable
    "reuniones",  # casa/depa/cueva de alguien del circulo de un amigo (bgg_data), separado de
                  # "otros" para no inflar ese catch-all -- la mayoria de partidas de amigos
                  # eran justo este tipo de reunion informal (sesion 2026-08-13)
]
# grupo_social: tags "Player" que ya existian en BG Stats (Reformers, Cartoneros,
# GEM, Cul/Cdmx/Gdl por ciudad, etc). Senal aun mas fuerte que categoria_lugar
# en la validacion (rango 18.7-50.8 min segun grupo vs. 19-47.8 de lugar).
# Se calcula con JOIN en vivo a boardgames_stats.jugadores.grupo_social, no queda
# congelado por partida — si alguien se vuelve anonimo en BG Stats (perfil
# se fusiona al generico compartido, sin tag), sus partidas historicas
# pierden la senal salvo que haya una fila en partida_grupo_social_override
# (ver boardgames_stats.partida_grupo_social_override, poblada a mano caso por caso).
CATEGORIAS_GRUPO = [
    "Reformers", "Solo", "Pup", "Cartoneros", "GEM", "Cdmx", "Otros", "Extra", "Cul", "Entreturnos",
    "Cun", "Gdl",  # grupos chicos hoy (13/5 partidas) pero reales, pueden crecer
    "Ex",  # gente con la que Alberto ya no tiene contacto (recuperado via partida_grupo_social_override
           # de jugadores anonimizados en BG Stats, ej. Jairo/Frank Munoz). Entrena el modelo pero no
           # debe aparecer como opcion elegible en el picker de prediccion del frontend.
    "Evento", "Mty",  # inferidos via partida_grupo_social_override a partir de partidas mixtas/sin
                       # senal (sesion 2026-08-13): Evento = gente conocida en convenciones/expos
                       # (Mega XP), Mty = circulo de Monterrey.
]
# dia de la semana (0=domingo..6=sabado, igual que EXTRACT(DOW) de Postgres).
# Se probo a mano antes de agregarlo (sesion 2026-08-13): duracion promedio
# weekend vs entre semana identica (35 min ambos) -- se deja como feature de
# todas formas porque con Lasso/ElasticNet un feature sin señal simplemente
# se pondera a ~0, no le hace daño al modelo, y confirma la conclusion desde
# adentro del modelo en vez de solo el promedio crudo.
DIAS_SEMANA = list(range(7))
FEATURES = (
    FEATURES_BASE
    + [f"lugar_{c}" for c in CATEGORIAS_LUGAR]
    + [f"grupo_{g}" for g in CATEGORIAS_GRUPO]
    + [f"dia_{d}" for d in DIAS_SEMANA]
)
# las 3 features de historial viven al final de FEATURES_BASE, o sea a MITAD
# del vector completo (antes de los dummies), no al final -- por eso el
# fallback se arma quitando estos indices y no un slice.
FEATURES_HISTORIAL = ["media_juego_propia", "n_partidas_juego", "media_juego_amigos"]
IDX_HISTORIAL = [FEATURES.index(f) for f in FEATURES_HISTORIAL]
FEATURES_SIN_HISTORIAL = [f for f in FEATURES if f not in FEATURES_HISTORIAL]


CTE_HISTORIAL = """
    WITH propias AS (
        SELECT p2.juego_uuid, sum(p2.duracion_min)::numeric AS suma, count(*) AS n
        FROM boardgames_stats.partidas p2
        WHERE p2.duracion_min > 0
        GROUP BY p2.juego_uuid
    ),
    amigos_juego AS (
        SELECT pa2.bgg_game_id, sum(pa2.duracion_min)::numeric AS suma, count(*) AS n
        FROM boardgames_bgg.plays_amigos pa2
        WHERE pa2.usable_para_analisis AND pa2.duracion_min > 0
        GROUP BY pa2.bgg_game_id
    )
"""


def cargar_datos(conn, incluir_amigos: bool = False) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(
        CTE_HISTORIAL
        + """
        SELECT p.duracion_min, d.peso_complejidad, d.dependencia_idioma,
               c.temp_media_c, d.calificacion_promedio,
               (SELECT count(*) FROM boardgames_stats.partida_jugadores pj WHERE pj.partida_uuid = p.uuid) AS num_jugadores,
               d.min_playtime, d.max_playtime, p.tag_digital,
               (p.expansiones_usadas IS NOT NULL AND array_length(p.expansiones_usadas, 1) > 0) AS usa_expansion,
               p.tag_solo,
               l.categoria_lugar,
               COALESCE(
                   gso.grupo_social,
                   (
                       SELECT mode() WITHIN GROUP (ORDER BY jg.grupo_social)
                       FROM boardgames_stats.partida_jugadores pj2
                       JOIN boardgames_stats.jugadores jg ON jg.uuid = pj2.jugador_uuid
                       WHERE pj2.partida_uuid = p.uuid AND jg.grupo_social IS NOT NULL
                   )
               ) AS grupo_social,
               EXTRACT(DOW FROM p.fecha)::int AS dia_semana,
               CASE WHEN pr.n > 1 THEN (pr.suma - p.duracion_min) / (pr.n - 1) END AS media_juego_propia,
               pr.n - 1 AS n_partidas_juego,
               CASE WHEN am.n > 0 THEN am.suma / am.n END AS media_juego_amigos
        FROM boardgames_stats.partidas p
        JOIN boardgames_stats.juegos j ON j.uuid = p.juego_uuid
        JOIN boardgames_bgg.juegos_detalle d ON d.bgg_id = j.bgg_id
        LEFT JOIN boardgames_stats.clima_diario c ON c.lugar_uuid = p.lugar_uuid AND c.fecha = p.fecha::date
        LEFT JOIN boardgames_stats.lugares l ON l.uuid = p.lugar_uuid
        LEFT JOIN boardgames_stats.partida_grupo_social_override gso ON gso.partida_uuid = p.uuid
        LEFT JOIN propias pr ON pr.juego_uuid = p.juego_uuid
        LEFT JOIN amigos_juego am ON am.bgg_game_id = j.bgg_id
        WHERE p.duracion_min > 0 AND d.peso_complejidad IS NOT NULL
        """
    )
    filas = cur.fetchall()

    if incluir_amigos:
        # boardgames_bgg.plays_amigos (partidas de amigos registradas directo en BGG,
        # NUNCA se fusiona con boardgames_stats.partidas en Postgres — union solo en
        # memoria, aqui, para entrenar un modelo mas robusto). Clima usa el
        # lugar exacto cuando existe (boardgames_bgg.clima_ubicacion_diario, lugares
        # con lat/lon heredado de boardgames_stats.lugares o geocodificado a mano) y
        # cae al proxy general de CDMX (boardgames_bgg.clima_cdmx_diario) cuando el
        # lugar no esta geolocalizado (ej. casas de amigos sin coords).
        # grupo_social sale primero de boardgames_bgg.ubicaciones_amigos_alias
        # .grupo_social_lugar (ej. "Global Excel"/"Trabajo" -> GEM, el lugar ya
        # implica el grupo aunque el jugador no matchee) y si no hay eso, de
        # boardgames_bgg.jugadores_identificados (personas confirmadas a mano como
        # las mismas que ya conoce Alberto, ej. "Pablo"/"Rubens"/"Vinicio" ->
        # Reformers) cruzando cualquier jugador listado; si nada matchea, queda
        # NULL y el entrenamiento rellena con la mediana como dato faltante.
        cur.execute(
            CTE_HISTORIAL
            + """
            SELECT pa.duracion_min, d.peso_complejidad, d.dependencia_idioma,
                   COALESCE(cu.temp_media_c, cc.temp_media_c) AS temp_media_c,
                   d.calificacion_promedio,
                   (
                       SELECT count(*) FROM jsonb_array_elements(pa.jugadores) jug
                       WHERE NOT (
                           LEFT(jug->>'nombre', 2) = 'B_' OR LOWER(jug->>'nombre') = 'automa'
                           OR LOWER(jug->>'nombre') LIKE 'bot %'
                       )
                   ) AS num_jugadores,
                   d.min_playtime, d.max_playtime, COALESCE(pa.tag_digital, FALSE) AS tag_digital,
                   FALSE AS usa_expansion,
                   COALESCE(pa.tag_solo, FALSE) AS tag_solo,
                   pa.categoria_lugar,
                   COALESCE(
                       ua.grupo_social_lugar,
                       (
                           SELECT ji.grupo_social
                           FROM jsonb_array_elements(pa.jugadores) jug
                           JOIN boardgames_bgg.jugadores_identificados ji
                               ON ji.nombre_variante = LOWER(TRIM(jug->>'nombre'))
                           LIMIT 1
                       )
                   ) AS grupo_social,
                   EXTRACT(DOW FROM pa.fecha)::int AS dia_semana,
                   -- para una fila de amigo, "propia" sigue siendo el historial de
                   -- Alberto del mismo juego (no hay fila propia que restar, asi
                   -- que no aplica leave-one-out); el LOO va del lado de amigos.
                   CASE WHEN pr.n > 0 THEN pr.suma / pr.n END AS media_juego_propia,
                   COALESCE(pr.n, 0) AS n_partidas_juego,
                   CASE WHEN am.n > 1 THEN (am.suma - pa.duracion_min) / (am.n - 1) END AS media_juego_amigos
            FROM boardgames_bgg.plays_amigos pa
            JOIN boardgames_bgg.juegos_detalle d ON d.bgg_id = pa.bgg_game_id
            LEFT JOIN boardgames_bgg.ubicaciones_amigos_alias ua ON ua.ubicacion_raw = pa.ubicacion
            LEFT JOIN boardgames_bgg.clima_ubicacion_diario cu
                ON cu.ubicacion_normalizada = pa.ubicacion_normalizada AND cu.fecha = pa.fecha
            LEFT JOIN boardgames_bgg.clima_cdmx_diario cc ON cc.fecha = pa.fecha
            LEFT JOIN boardgames_stats.juegos jp ON jp.bgg_id = pa.bgg_game_id
            LEFT JOIN propias pr ON pr.juego_uuid = jp.uuid
            LEFT JOIN amigos_juego am ON am.bgg_game_id = pa.bgg_game_id
            WHERE pa.usable_para_analisis AND pa.duracion_min > 0 AND d.peso_complejidad IS NOT NULL
            """
        )
        filas += cur.fetchall()

    return filas


def fila_con_categoria(
    base: list, categoria_lugar: str | None, grupo_social: str | None, dia_semana: int | None = None
) -> list:
    dummies_lugar = [1.0 if categoria_lugar == c else 0.0 for c in CATEGORIAS_LUGAR]
    dummies_grupo = [1.0 if grupo_social == g else 0.0 for g in CATEGORIAS_GRUPO]
    dummies_dia = [1.0 if dia_semana == d else 0.0 for d in DIAS_SEMANA]
    return base + dummies_lugar + dummies_grupo + dummies_dia


def _ajustar(y: np.ndarray, x_raw: np.ndarray, n_filas: int) -> dict:
    """Entrena los 3 candidatos sobre una matriz ya armada y regresa el mejor.
    temp_media_c y otras columnas pueden venir NULL (lugar sin geolocalizar o
    sin clima sincronizado) — se rellenan con la mediana en vez de tirar la
    fila, mismo criterio que ya usa el proyecto de Coffee para huecos."""
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


def entrenar(conn, incluir_amigos: bool = False):
    """Igual que el modelo solo: uno principal con el historial del mismo
    juego, y un fallback sin esas features para juegos nunca jugados (ver
    duracion_solo_model.entrenar para el razonamiento completo)."""
    filas = cargar_datos(conn, incluir_amigos=incluir_amigos)
    if len(filas) < 20:
        return None

    y = np.array([float(f[0]) for f in filas])
    x_completo = np.array(
        [
            fila_con_categoria(
                [f[1], f[2], f[3], f[4], f[5], f[6], f[7], float(bool(f[8])), float(bool(f[9])),
                 float(bool(f[10])), f[14], f[15], f[16]],
                f[11], f[12], f[13],
            )
            for f in filas
        ],
        dtype=float,
    )
    r = _ajustar(y, x_completo, len(filas))
    r_fallback = _ajustar(y, np.delete(x_completo, IDX_HISTORIAL, axis=1), len(filas))

    r["mae_baseline"] = mean_absolute_error(y, np.full_like(y, y.mean()))
    r["coeficientes"] = dict(zip(FEATURES, getattr(r["modelo"], "coef_", [None] * len(FEATURES))))
    r["fallback"] = r_fallback
    r["fallback"]["coeficientes"] = dict(
        zip(FEATURES_SIN_HISTORIAL,
            getattr(r_fallback["modelo"], "coef_", [None] * len(FEATURES_SIN_HISTORIAL)))
    )
    return r


def predecir(
    resultado_entrenamiento: dict,
    valores: dict,
    categoria_lugar: str | None = None,
    grupo_social: str | None = None,
    dia_semana: int | None = None,
) -> float:
    for c in CATEGORIAS_LUGAR:
        valores.setdefault(f"lugar_{c}", 1.0 if categoria_lugar == c else 0.0)
    for g in CATEGORIAS_GRUPO:
        valores.setdefault(f"grupo_{g}", 1.0 if grupo_social == g else 0.0)
    if dia_semana is not None:
        for d in DIAS_SEMANA:
            valores.setdefault(f"dia_{d}", 1.0 if dia_semana == d else 0.0)

    sin_historial = valores.get("media_juego_propia") is None
    r = resultado_entrenamiento["fallback"] if sin_historial else resultado_entrenamiento
    features = FEATURES_SIN_HISTORIAL if sin_historial else FEATURES

    fila = np.array([[valores.get(f, np.nan) for f in features]], dtype=float)
    inds = np.where(np.isnan(fila))
    fila[inds] = np.take(r["medianas"], inds[1])
    x = r["scaler"].transform(fila)
    # piso de 5 min: un modelo lineal puede irse a ~0 o negativo en los extremos
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
