import asyncio
from concurrent.futures import ThreadPoolExecutor

from langchain_core.tools import tool

from app.rag.memory_retriever import search_memories


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@tool
def search_long_term_memory(query: str) -> str:
    """Search your long-term memory of facts learned in past conversations.

    Use this when the user refers to something they told you before, asks what
    you remember, or when prior context about them, their company, or their
    projects would help answer.
    """
    results = _run_async(search_memories(query, k=5))
    if not results:
        return "No relevant memories found."
    return "\n\n".join(f"- {r}" for r in results)