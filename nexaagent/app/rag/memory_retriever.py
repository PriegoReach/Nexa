"""Recuperación de memoria de largo plazo por similitud vectorial."""
from sqlalchemy import text

from app.db.worker_db import worker_session
from app.rag.embeddings import get_embeddings


async def search_memories(query: str, k: int = 4) -> list[str]:
    """Devuelve los k hechos más cercanos a la consulta (distancia coseno).

    Búsqueda solo vectorial: la memoria de largo plazo es texto breve y
    reformulado por el LLM extractor; el match léxico exacto raramente ayuda.
    """
    query_vec = get_embeddings().embed_query(query)

    async with worker_session() as session:
        rows = await session.execute(
            text(
                "SELECT content FROM long_term_memories "
                "ORDER BY embedding <=> :qvec LIMIT :k"
            ),
            {"qvec": str(query_vec), "k": k},
        )
        return [r[0] for r in rows.fetchall()]
