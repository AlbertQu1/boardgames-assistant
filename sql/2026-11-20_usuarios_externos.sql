-- 2026-11-20 -- Usuarios BGG externos + alias por usuario + clima de externos
-- Aplicado a mano en la base `casa`. Generado desde la base real (pg_dump --schema-only)
-- y limpiado; los GRANT a boardgames_readonly son los que usa la tool query_sql de /ask.

-- 1) jugadores_identificados: alias opcionalmente limitado a los logs de un usuario BGG
ALTER TABLE boardgames_bgg.jugadores_identificados ADD COLUMN bgg_username TEXT;
ALTER TABLE boardgames_bgg.jugadores_identificados DROP CONSTRAINT jugadores_identificados_pkey;
CREATE UNIQUE INDEX jugadores_identificados_uniq
    ON boardgames_bgg.jugadores_identificados (nombre_variante, COALESCE(bgg_username, ''));
COMMENT ON COLUMN boardgames_bgg.jugadores_identificados.bgg_username IS
    'NULL = aplica a los logs de todos los amigos; con valor = solo a los plays_amigos de ese usuario (y gana sobre la fila global)';

CREATE TABLE boardgames_bgg.clima_externos_diario (
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    fecha date NOT NULL,
    temp_media_c double precision,
    precipitacion_mm double precision
);
COMMENT ON TABLE boardgames_bgg.clima_externos_diario IS 'Clima diario (Open-Meteo archive) en la capital del estado de cada usuario de usuarios_externos; join por usuarios_externos.lat/lon + fecha';
CREATE TABLE boardgames_bgg.plays_externos (
    bgg_play_id integer CONSTRAINT plays_amigos_bgg_play_id_not_null NOT NULL,
    bgg_username text CONSTRAINT plays_amigos_bgg_username_not_null NOT NULL,
    fecha date CONSTRAINT plays_amigos_fecha_not_null NOT NULL,
    juego text CONSTRAINT plays_amigos_juego_not_null NOT NULL,
    bgg_game_id integer,
    ubicacion text,
    comentarios text,
    duracion_min integer,
    cantidad integer,
    incompleta boolean,
    jugadores jsonb,
    datos_extra jsonb,
    sincronizado_en timestamp with time zone DEFAULT now(),
    ubicacion_normalizada text,
    categoria_lugar text,
    es_partida_propia boolean,
    usable_para_analisis boolean,
    tag_solo boolean,
    tag_digital boolean
);
COMMENT ON TABLE boardgames_bgg.plays_externos IS 'Partidas de usuarios BGG ajenos (no amigos, sin grupo_social). Solo para stats/ML; NO alimenta red_social ni clima de amigos.';
CREATE TABLE boardgames_bgg.usuarios_externos (
    bgg_username text NOT NULL,
    activo boolean DEFAULT true NOT NULL,
    notas text,
    agregado_en timestamp with time zone DEFAULT now() NOT NULL,
    ultimo_sync timestamp with time zone,
    partidas_ultimo_sync integer,
    pais text,
    estado text,
    lat double precision,
    lon double precision
);
COMMENT ON COLUMN boardgames_bgg.usuarios_externos.lat IS 'Capital del estado del perfil BGG (CAPITALES_MX + Nominatim; otros paises: centroide del estado). NULL si el perfil no trae estado. BGG no expone ciudad';
ALTER TABLE ONLY boardgames_bgg.clima_externos_diario
    ADD CONSTRAINT clima_externos_diario_pkey PRIMARY KEY (lat, lon, fecha);
ALTER TABLE ONLY boardgames_bgg.plays_externos
    ADD CONSTRAINT plays_externos_pkey PRIMARY KEY (bgg_play_id);
ALTER TABLE ONLY boardgames_bgg.usuarios_externos
    ADD CONSTRAINT usuarios_externos_pkey PRIMARY KEY (bgg_username);
CREATE INDEX plays_externos_fecha_idx ON boardgames_bgg.plays_externos USING btree (fecha);
CREATE INDEX plays_externos_game_idx ON boardgames_bgg.plays_externos USING btree (bgg_game_id);
CREATE INDEX plays_externos_username_idx ON boardgames_bgg.plays_externos USING btree (bgg_username);

GRANT SELECT ON boardgames_bgg.usuarios_externos, boardgames_bgg.plays_externos,
    boardgames_bgg.clima_externos_diario TO boardgames_readonly;
