import asyncio
from concurrent.futures import ThreadPoolExecutor

from langchain_core.tools import tool

from app.rag.retriever import search


def _run_async(coro):
    """Ejecuta una corrutina de forma segura, haya o no un event loop activo.

    La herramienta la invoca el agente desde dentro del loop de FastAPI, donde
    asyncio.run() falla. La corremos en un hilo con su propio loop limpio.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@tool
def search_knowledge_base(query: str) -> str:
    """Search the company's ingested documents for relevant context.

    Use this whenever the user asks about internal documents, policies,
    or any information that may live in uploaded files.
    """
    results = _run_async(search(query, k=8, dual=True))
    if not results:
        return "No relevant documents found."
    return "\n\n---\n\n".join(results)