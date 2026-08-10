# boardgames-assistant

RAG sobre reglamentos de juegos de mesa (PDF/DOCX → chunks → embeddings → pgvector) +
stats de partidas (BG Stats → Postgres). Consumido por la app
[Boardgames Assistant](https://github.com/AlbertQu1/Loggers/tree/master/Boardgames%20Assistant)
(Next.js, mismo monorepo que Coffee Logger / Soda Stream Logger).

Roadmap completo en la memoria del proyecto (Claude Code).

**Estado actual:**
- ✅ Fase 0 — pipeline de indexado (extraer → trocear → embeber → guardar en pgvector), validado con varios juegos reales, multilenguaje (es/en), OCR automatico como fallback para PDFs sin texto real, y expansiones ligadas al juego base.
- ✅ Fase 2 — schema `bgstats.*` (juegos/jugadores/lugares/partidas/partida_jugadores) sincronizado desde export de BG Stats, automatizado end-to-end via Google Drive + n8n (ver `source/bgstats_sync.py` y `POST /bgstats/sync`).
- ✅ Fase 3 (version inicial) — backend `/ask` con 2 herramientas para Gemini: `search_rulebooks` (RAG) y `query_sql` (SQL de solo lectura sobre `bgstats.*`, rol Postgres `boardgames_readonly` sin permisos de escritura). Gemini elige cual usar segun la pregunta.
- ✅ Fase 1 — ingesta de reglamentos sin SSH, validada end-to-end (subida real desde compu y desde telefono, ambas indexadas correctamente). Dos caminos, ambos disponibles en la app:
  1. **Subida directa**: pestaña "Agregar" → eliges archivo del dispositivo → llenas el formulario → indexa al toque (`POST /reglamentos/subir`).
  2. **Buzon asincrono**: subes el PDF/DOCX a una carpeta de Google Drive ("Reglamentos") desde cualquier dispositivo → un workflow de n8n lo baja solo a `pdfs_prueba/pendientes/` y mueve el original a una subcarpeta "Procesados" en Drive → la pestaña "Agregar" muestra un badge con el conteo y una lista de pendientes → completas el formulario despues, desde donde sea, y confirmas (`POST /reglamentos/confirmar`). Pensado para cuando subes el archivo en un momento y quieres poner el nombre/idioma despues, sin notificacion activa (se revisa la app cuando se abre, a proposito mas simple que configurar email/push).
- ⏳ Fase 4/5/6 — no empezadas (BGG API para mecanicas/categorias, clima/geocoding, dashboard de BG Stats en la app, analitica avanzada).

**Nombre del juego al agregar un reglamento:** se autocompleta SOLO si el juego ya esta
en tu biblioteca de BG Stats (busqueda local por `bgg_id` extraido de un link de BGG que
pegues, sin llamar a la API de BGG — `GET /juegos/bgg-lookup`); para juegos nuevos se
escribe a mano. Se aplico a la API oficial de BGG el 2026-08-09
(boardgamegeek.com/using_the_xml_api, requiere aprobacion, "una semana o mas" segun su
propia politica) — mientras se aprueba, el flujo funciona igual, solo sin autocompletado
para juegos nuevos. iCloud Drive se descarto como alternativa a Google Drive para el
buzon asincrono — Apple no ofrece API publica para automatizacion de terceros.

**Limitacion conocida:** el backend usa el tier gratuito de la API de Gemini, que tiene
un limite duro de 20 requests/dia por modelo — cada pregunta consume 2-3 requests
(ciclo de busqueda/consulta), asi que en la practica el limite real es ~7-10 preguntas/dia.
Suficiente para probar, no para una noche de juego real. Subir a billing de pago
(~$0.01-0.02 USD/pregunta) resuelve esto sin tocar codigo — pendiente de decidir.

## Setup

```bash
conda env create -f environment.yml
conda activate boardgames
cp .env.example .env  # ajusta DATABASE_URL, DATABASE_URL_READONLY, GEMINI_API_KEY
```

OCR (para PDFs sin texto real, ej. libros de reglas muy ilustrados) necesita paquetes
del sistema, aparte del conda env:

```bash
sudo apt install -y tesseract-ocr poppler-utils
```

## Uso

```bash
# Indexar un reglamento (PDF o DOCX) — usa OCR automaticamente si el PDF no tiene texto real
python source/pdf_pipeline.py --juego "Wingspan" --pdf pdfs_prueba/wingspan.pdf \
    --idioma es --doc-type reglamento

# Indexar una expansion (queda ligada al juego base en la busqueda)
python source/pdf_pipeline.py --juego "Wingspan: European Expansion" \
    --pdf pdfs_prueba/wingspan-europa.pdf --juego-base "Wingspan"

# Probar el retrieval crudo (sin LLM, solo pgvector)
python source/query_test.py --pregunta "Como se calcula el puntaje final?" --juego "Wingspan"

# Sincronizar un export de BG Stats a Postgres (idempotente, upsert por uuid)
python source/bgstats_sync.py --archivo bgstats_data/BGStatsExport.json

# Levantar el backend de consumo (Fase 3)
uvicorn source.api:app --host 0.0.0.0 --port 8000 --reload

# Probar /ask con salida legible (en otra terminal, con el backend arriba)
python source/ask_cli.py --pregunta "Como se juega para 2 jugadores?" --juego "Wingspan"
python source/ask_cli.py --pregunta "Mejores juegos que tengo para 3 personas?"

# Ver que hay esperando info en pdfs_prueba/pendientes/ (llegan ahi via n8n + Drive)
curl http://localhost:8000/reglamentos/pendientes
```

Los reglamentos (PDF/DOCX) no se versionan (`.gitignore`) por derechos de autor —
van en `pdfs_prueba/` localmente. El export de BG Stats (`bgstats_data/`) tampoco se
versiona — son datos personales.

## Schemas

**`boardgames.rulebook_chunks`** — cada chunk guarda: `juego`, `juego_base` (si es
expansion), `idioma`, `doc_type` (`reglamento`/`errata`/`faq`), `source_pdf`,
`chunk_text`, `embedding` (384-dim, `paraphrase-multilingual-MiniLM-L12-v2`).

**`bgstats.*`** — `juegos`, `jugadores`, `lugares`, `partidas`, `partida_jugadores`,
todo referenciado por `uuid` (mismo uuid que usa BG Stats internamente, permite
upsert idempotente en cada sync). Ver `source/bgstats_sync.py` para el mapeo completo.
