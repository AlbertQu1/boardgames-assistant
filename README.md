# boardgames-assistant

RAG sobre reglamentos de juegos de mesa (PDF → chunks → embeddings → pgvector) +
stats de partidas (BG Stats), pensado para consultarse por WhatsApp/Claude en la mesa.

Roadmap completo en la memoria del proyecto (Claude Code). Este repo cubre por ahora
la Fase 0: indexar reglamentos de prueba y confirmar que el RAG responde bien.

## Setup

```bash
conda env create -f environment.yml
conda activate boardgames
cp .env.example .env  # ya trae la conexion local por defecto
```

## Uso

```bash
# Indexar un reglamento
python source/pdf_pipeline.py --juego "Wingspan" --pdf pdfs_prueba/wingspan.pdf

# Probar el RAG
python source/query_test.py --pregunta "Como se calcula el puntaje final?"
```

Los PDFs de reglamentos no se versionan (`.gitignore`) por derechos de autor —
van en `pdfs_prueba/` localmente.
