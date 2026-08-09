"""
Backend de consumo (Fase 3): expone /ask, /juegos, /health para la app.
Gemini decide cuando llamar search_rulebooks (misma busqueda que query_test.py)
y sintetiza la respuesta final a partir de los chunks encontrados.

Uso:
    uvicorn source.api:app --host 0.0.0.0 --port 8000 --reload
"""

import os

import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import params
from source.query_test import search

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = "gemini-flash-latest"

SEARCH_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="search_rulebooks",
            description="Busca en los reglamentos indexados de juegos de mesa. Regresa los fragmentos de texto mas relevantes para una pregunta.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "pregunta": types.Schema(type="STRING", description="La pregunta o consulta a buscar"),
                    "juego": types.Schema(type="STRING", description="Nombre exacto del juego (BGG) para filtrar la busqueda. Omitir si no se especifico un juego."),
                },
                required=["pregunta"],
            ),
        )
    ]
)

SYSTEM_PROMPT = (
    "Eres un asistente de reglas de juegos de mesa. Usa la herramienta search_rulebooks "
    "para buscar en los reglamentos indexados antes de responder. Basa tu respuesta unicamente "
    "en los fragmentos que encuentres. Si los fragmentos no contienen la respuesta, dilo "
    "claramente en vez de inventar. Responde en español, de forma directa y concisa."
)


class AskRequest(BaseModel):
    pregunta: str
    juego: str | None = None


class Fuente(BaseModel):
    juego: str
    source_pdf: str
    idioma: str
    chunk_index: int


class AskResponse(BaseModel):
    respuesta: str
    fuentes: list[Fuente]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/juegos")
def juegos():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT juego, juego_base FROM {params.DB_SCHEMA}.{params.CHUNKS_TABLE} ORDER BY juego;"
    )
    result = [{"juego": row[0], "juego_base": row[1]} for row in cur.fetchall()]
    cur.close()
    conn.close()
    return result


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    contents = [types.Content(role="user", parts=[types.Part(text=req.pregunta)])]
    fuentes: list[dict] = []

    while True:
        try:
            response = gemini.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=[SEARCH_TOOL],
                ),
            )
        except genai_errors.ClientError as e:
            if e.code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Se alcanzo el limite diario gratuito de Gemini. Intenta de nuevo mas tarde.",
                )
            raise HTTPException(status_code=502, detail="Error al consultar Gemini.")
        candidate = response.candidates[0]
        function_calls = [p.function_call for p in candidate.content.parts if p.function_call]

        if not function_calls:
            texto = "".join(p.text for p in candidate.content.parts if p.text)
            return AskResponse(respuesta=texto, fuentes=[Fuente(**f) for f in fuentes])

        contents.append(candidate.content)
        function_response_parts = []
        for fc in function_calls:
            juego = fc.args.get("juego") or req.juego
            resultados = search(fc.args["pregunta"], juego, None, top_k=5)
            resumen = [
                {"juego": j, "source_pdf": pdf, "chunk_index": idx, "texto": texto, "idioma": idioma}
                for j, pdf, idx, texto, idioma, _sim in resultados
            ]
            for r in resumen:
                fuentes.append(
                    {"juego": r["juego"], "source_pdf": r["source_pdf"], "idioma": r["idioma"], "chunk_index": r["chunk_index"]}
                )
            contenido = "\n\n".join(f"[{r['juego']} | chunk {r['chunk_index']}]\n{r['texto']}" for r in resumen)
            function_response_parts.append(
                types.Part.from_function_response(name=fc.name, response={"resultados": contenido})
            )
        contents.append(types.Content(role="user", parts=function_response_parts))
