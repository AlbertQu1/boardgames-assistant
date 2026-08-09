# boardgames-assistant

RAG sobre reglamentos de juegos de mesa (PDF/DOCX → chunks → embeddings → pgvector) +
(a futuro) stats de partidas (BG Stats). Consumido por la app
[Boardgames Assistant](https://github.com/AlbertQu1/Loggers/tree/master/Boardgames%20Assistant)
(Next.js, mismo monorepo que Coffee Logger / Soda Stream Logger).

Roadmap completo en la memoria del proyecto (Claude Code).

**Estado actual:**
- ✅ Fase 0 — pipeline de indexado (extraer → trocear → embeber → guardar en pgvector), validado con varios juegos reales, multilenguaje (es/en) y expansiones ligadas al juego base.
- ✅ Fase 3 (version inicial) — backend `/ask` que conecta Gemini (tool-use) con el retrieval, sintetiza respuesta en lenguaje natural y cita fuentes.
- ⏳ Fase 1 (automatizar ingesta vía n8n) y Fase 2 (BG Stats) — no empezadas.

**Limitacion conocida:** el backend usa el tier gratuito de la API de Gemini, que tiene
un limite duro de 20 requests/dia por modelo — cada pregunta consume 2-3 requests
(ciclo de busqueda), asi que en la practica el limite real es ~7-10 preguntas/dia.
Suficiente para probar, no para una noche de juego real. Subir a billing de pago
(~$0.01-0.02 USD/pregunta) resuelve esto sin tocar codigo — pendiente de decidir.

## Setup

```bash
conda env create -f environment.yml
conda activate boardgames
cp .env.example .env  # ajusta DATABASE_URL y agrega GEMINI_API_KEY (aistudio.google.com/apikey)
```

## Uso

```bash
# Indexar un reglamento (PDF o DOCX)
python source/pdf_pipeline.py --juego "Wingspan" --pdf pdfs_prueba/wingspan.pdf \
    --idioma es --doc-type reglamento

# Indexar una expansion (queda ligada al juego base en la busqueda)
python source/pdf_pipeline.py --juego "Wingspan: European Expansion" \
    --pdf pdfs_prueba/wingspan-europa.pdf --juego-base "Wingspan"

# Probar el retrieval crudo (sin LLM, solo pgvector)
python source/query_test.py --pregunta "Como se calcula el puntaje final?" --juego "Wingspan"

# Levantar el backend de consumo (Fase 3)
uvicorn source.api:app --host 0.0.0.0 --port 8000 --reload

# Probar /ask con salida legible (en otra terminal, con el backend arriba)
python source/ask_cli.py --pregunta "Como se juega para 2 jugadores?" --juego "Wingspan"
```

Los reglamentos (PDF/DOCX) no se versionan (`.gitignore`) por derechos de autor —
van en `pdfs_prueba/` localmente, solo en el servidor.

## Schema (`boardgames.rulebook_chunks`)

Cada chunk guarda: `juego`, `juego_base` (si es expansion), `idioma`, `doc_type`
(`reglamento`/`errata`/`faq`), `source_pdf`, `chunk_text`, `embedding` (384-dim,
`paraphrase-multilingual-MiniLM-L12-v2`).
