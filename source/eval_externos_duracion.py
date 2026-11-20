"""Evalua si sumar partidas externas/amigos mejora la prediccion de duracion de las
partidas PROPIAS de Alberto. MAE medido solo sobre filas propias (KFold externo);
historial por juego recalculado dentro de cada fold (sin fuga de target)."""
import os, sys, warnings
sys.path.insert(0, os.path.expanduser('~/proyectos/boardgames-assistant/source'))
from dotenv import load_dotenv; load_dotenv(os.path.expanduser('~/proyectos/boardgames-assistant/.env'))
import numpy as np, pandas as pd, psycopg2
from sklearn.model_selection import KFold
from sklearn.metrics import mean_absolute_error
import duracion_model as dm
warnings.filterwarnings('ignore')
conn = psycopg2.connect(os.environ['DATABASE_URL'])
BOT = "NOT (LEFT(jug->>'nombre',2)='B_' OR LOWER(jug->>'nombre')='automa' OR LOWER(jug->>'nombre') LIKE 'bot %%')"
own = pd.read_sql("""
SELECT p.duracion_min y, j.bgg_id, d.peso_complejidad, d.dependencia_idioma, c.temp_media_c, d.calificacion_promedio,
  (SELECT count(*) FROM boardgames_stats.partida_jugadores pj WHERE pj.partida_uuid=p.uuid) num_jugadores,
  d.min_playtime, d.max_playtime, p.tag_digital::int tag_digital,
  (p.expansiones_usadas IS NOT NULL AND array_length(p.expansiones_usadas,1)>0)::int usa_expansion, p.tag_solo::int tag_solo,
  l.categoria_lugar, COALESCE(gso.grupo_social,(SELECT mode() WITHIN GROUP (ORDER BY jg.grupo_social) FROM boardgames_stats.partida_jugadores pj2
     JOIN boardgames_stats.jugadores jg ON jg.uuid=pj2.jugador_uuid WHERE pj2.partida_uuid=p.uuid AND jg.grupo_social IS NOT NULL)) grupo_social,
  EXTRACT(DOW FROM p.fecha)::int dia_semana
FROM boardgames_stats.partidas p JOIN boardgames_stats.juegos j ON j.uuid=p.juego_uuid
JOIN boardgames_bgg.juegos_detalle d ON d.bgg_id=j.bgg_id
LEFT JOIN boardgames_stats.clima_diario c ON c.lugar_uuid=p.lugar_uuid AND c.fecha=p.fecha::date
LEFT JOIN boardgames_stats.lugares l ON l.uuid=p.lugar_uuid
LEFT JOIN boardgames_stats.partida_grupo_social_override gso ON gso.partida_uuid=p.uuid
WHERE p.duracion_min>0 AND d.peso_complejidad IS NOT NULL""", conn)
def externos_sql(tabla, clima_join, grupo):
    return f"""
SELECT pa.duracion_min y, pa.bgg_game_id bgg_id, d.peso_complejidad, d.dependencia_idioma, {clima_join[0]} temp_media_c, d.calificacion_promedio,
  (SELECT count(*) FROM jsonb_array_elements(pa.jugadores) jug WHERE {BOT}) num_jugadores,
  d.min_playtime, d.max_playtime, COALESCE(pa.tag_digital,false)::int tag_digital, 0 usa_expansion, COALESCE(pa.tag_solo,false)::int tag_solo,
  pa.categoria_lugar, {grupo} grupo_social, EXTRACT(DOW FROM pa.fecha)::int dia_semana
FROM {tabla} pa JOIN boardgames_bgg.juegos_detalle d ON d.bgg_id=pa.bgg_game_id {clima_join[1]}
WHERE pa.usable_para_analisis AND pa.duracion_min>0 AND d.peso_complejidad IS NOT NULL"""
ami = pd.read_sql(externos_sql('boardgames_bgg.plays_amigos',
    ('COALESCE(cu.temp_media_c, cc.temp_media_c)', '''LEFT JOIN boardgames_bgg.clima_ubicacion_diario cu ON cu.ubicacion_normalizada=pa.ubicacion_normalizada AND cu.fecha=pa.fecha
     LEFT JOIN boardgames_bgg.clima_cdmx_diario cc ON cc.fecha=pa.fecha'''),
    '''(SELECT ji.grupo_social FROM jsonb_array_elements(pa.jugadores) jug JOIN boardgames_bgg.jugadores_identificados ji
        ON ji.nombre_variante=LOWER(TRIM(jug->>'nombre')) AND (ji.bgg_username IS NULL OR ji.bgg_username=pa.bgg_username)
        ORDER BY ji.bgg_username NULLS LAST LIMIT 1)'''), conn)
ext = pd.read_sql(externos_sql('boardgames_bgg.plays_externos',
    ('ce.temp_media_c', '''JOIN boardgames_bgg.usuarios_externos u USING (bgg_username)
     LEFT JOIN boardgames_bgg.clima_externos_diario ce ON ce.lat=u.lat AND ce.lon=u.lon AND ce.fecha=pa.fecha'''),
    'NULL::text'), conn)
print(f"filas: propias={len(own)} amigos={len(ami)} externos={len(ext)}")
print(f"externos de juegos que tienes: {ext.bgg_id.isin(own.bgg_id).sum()}")
print("duracion mediana: propias %.0f | amigos %.0f | externos %.0f" % (own.y.median(), ami.y.median(), ext.y.median()))

# media por juego de amigos/externos: no son target -> se calcula con todo
media_ami = ami.groupby('bgg_id').y.mean()
media_ext = ext.groupby('bgg_id').y.mean()

def armar(df, train_own, es_own_train, flag, extra_feat):
    g = train_own.groupby('bgg_id').y.agg(['sum', 'count'])
    s = df.bgg_id.map(g['sum']); n = df.bgg_id.map(g['count'])
    if es_own_train:  # leave-one-out
        media = np.where(n > 1, (s - df.y) / (n - 1), np.nan); npart = (n - 1).fillna(0)
    else:
        media = s / n; npart = n.fillna(0)
    base = np.column_stack([df.peso_complejidad, df.dependencia_idioma, df.temp_media_c, df.calificacion_promedio,
        df.num_jugadores, df.min_playtime, df.max_playtime, df.tag_digital, df.usa_expansion, df.tag_solo,
        media, npart, df.bgg_id.map(media_ami)]).astype(float)
    dummies = np.array([dm.fila_con_categoria([], l, g_, d) for l, g_, d in zip(df.categoria_lugar, df.grupo_social, df.dia_semana)], dtype=float)
    cols = [base, dummies]
    if flag is not None: cols.append(np.full((len(df), 1), flag, float))
    if extra_feat: cols.append(df.bgg_id.map(media_ext).to_numpy(float)[:, None])
    return np.hstack(cols)

def evaluar(nombre, extra_df=None, flag=False, extra_feat=False, solo_fallback=False, reps=(7,)):
  res=[]
  for seed in reps:
    kf = KFold(5, shuffle=True, random_state=seed)
    pred = np.zeros(len(own)); usado_fb = np.zeros(len(own), bool)
    for tr, te in kf.split(own):
        otr, ote = own.iloc[tr], own.iloc[te]
        xown = armar(otr, otr, True, 0 if flag else None, extra_feat); yown = otr.y.to_numpy(float)
        xtr, ytr = xown, yown
        if extra_df is not None and len(extra_df):
            xtr = np.vstack([xown, armar(extra_df, otr, False, 1 if flag else None, extra_feat)])
            ytr = np.concatenate([yown, extra_df.y.to_numpy(float)])
        xte = armar(ote, otr, False, 0 if flag else None, extra_feat)
        hist_cols = list(dm.IDX_HISTORIAL)
        if solo_fallback:
            m_full = dm._ajustar(yown, xown, len(yown))
        else:
            m_full = dm._ajustar(ytr, xtr, len(ytr))
        m_fb = dm._ajustar(ytr, np.delete(xtr, hist_cols, 1), len(ytr))
        sin_hist = np.isnan(xte[:, dm.FEATURES.index('media_juego_propia')])
        for m, mask, X in ((m_full, ~sin_hist, xte), (m_fb, sin_hist, np.delete(xte, hist_cols, 1))):
            if mask.any():
                Xm = X[mask].copy(); inds = np.where(np.isnan(Xm)); Xm[inds] = np.take(m['medianas'], inds[1])
                pred[te[mask]] = np.maximum(5, m['modelo'].predict(m['scaler'].transform(Xm)))
        usado_fb[te] = sin_hist
    y = own.y.to_numpy(float)
    res.append((mean_absolute_error(y, pred), mean_absolute_error(y[~usado_fb], pred[~usado_fb]),
         mean_absolute_error(y[usado_fb], pred[usado_fb]), usado_fb.sum()))
  a = np.array(res)
  print("%-52s MAE total %5.2f | con historial %5.2f | juego nuevo %5.2f (n~%d)" % (nombre, *a[:, :3].mean(0), a[:, 3].mean()), flush=True)


R=(7,11,23)
ext_tuyos = ext[ext.bgg_id.isin(own.bgg_id)]
evaluar("A. solo propias (como hoy)", reps=R)
evaluar("I. respaldo + externos (todos), flag", ext, flag=True, solo_fallback=True, reps=R)
evaluar("J. respaldo + externos juegos que tienes, flag", ext_tuyos, flag=True, solo_fallback=True, reps=R)
evaluar("K. respaldo + externos + media_juego_externos, flag", ext, flag=True, extra_feat=True, solo_fallback=True, reps=R)
evaluar("L. respaldo + amigos + externos, flag", pd.concat([ami, ext]), flag=True, solo_fallback=True, reps=R)
