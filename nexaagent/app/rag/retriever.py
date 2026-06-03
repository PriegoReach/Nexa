"""Recuperación híbrida RRF (vectorial + léxico) + re-ranking con cross-encoder (P18).

Pipeline: `_retrieve` trae FETCH_N candidatos con la lógica híbrida de siempre, y
el cross-encoder (`rerank`) los reordena por relevancia fina, devolviendo el top-k.
Recuperar más de los que usa el agente (FETCH_N > k) es lo que le da material al
re-ranker para mejorar el ranking fino (Recall@1/@3, que iban flojos).

DUAL cross-lingual (P30, opcional vía dual=True): P29 diagnosticó por capa que una
pregunta en ES sobre un doc en EN no se RECUPERA al pool (nomic v1 sesgado a inglés
hunde el doc inglés al último rank; la rama léxica 'es_simple' no lo matchea), aunque
el reranker bge-v2-m3 (multilingüe) lo pone #1 si lo ve. La cura medida NO es mezclar
idiomas en una query (el español arrastra el embedding) ni traducir-y-reemplazar (rompe
el español), sino DOS búsquedas vectoriales separadas — es y la traducción en — fusionadas
por RRF junto a la rama léxica es. El reranker, que ya está después, ordena el pool combinado.
Fail-safe: si la traducción LLM falla, se degrada a solo-es (no rompe la búsqueda).
"""
import asyncio
import logging

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.db.worker_db import worker_session
from app.rag.embeddings import get_embeddings
from app.rag.reranker import rerank

logger = logging.getLogger("nexa.retriever")

RRF_K = 60      # constante estándar del algoritmo Reciprocal Rank Fusion
FETCH_N = 20    # candidatos que el retriever pasa al re-ranker (path monolingüe es-only)
# P30: el pool dual fusiona TRES ramas (vec_es + vec_en + lex) y queda más apretado
# que el monolingüe de dos; el doc inglés entra al pool por la cola (TF #22 en rank ~21)
# y la rama vec_en empuja al doc español hacia abajo (proy-ámbar ~21). Medido 30 vs 40:
# 40 ~duplica la holgura de los candidatos al borde (proy-ámbar 9->19 slots, TF a media
# tabla) sin tocar Recall@1/@5; el +10 pares cuesta ~55 ms de reranker (marginal).
DUAL_FETCH_N = 40

# Traducción para la rama inglesa de la dual. Prompt mínimo (P30): el 7B traduce
# una query corta en ~1-2s. Timeout acotado; si falla, se degrada a solo-es.
_TRANSLATE_TIMEOUT = 30
_TRANSLATE_PROMPT = (
    "Translate the following search query to English. "
    "Output ONLY the translation, no quotes, no explanation.\n\nQuery: {q}"
)


async def _translate_to_english(query: str) -> str | None:
    """Traduce la query al inglés con el LLM (qwen2.5 vía Ollama). Devuelve None
    ante cualquier fallo (timeout/red/respuesta vacía) -> la dual degrada a solo-es,
    fail-safe como la degradación de Redis del proyecto. Cliente httpx efímero por
    llamada (cross-loop P2/P24: corre en el loop del ThreadPoolExecutor de la tool)."""
    try:
        async with httpx.AsyncClient(timeout=_TRANSLATE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": [{"role": "user", "content": _TRANSLATE_PROMPT.format(q=query)}],
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            resp.raise_for_status()
        out = (resp.json().get("message", {}).get("content") or "").strip()
        if not out:
            return None
        logger.info("dual: query translated", extra={"event": "dual_translate"})
        return out
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning(
            "dual: translation failed, degrading to es-only",
            extra={"event": "dual_degraded", "exc_type": type(exc).__name__},
        )
        return None


async def _retrieve_ids(query: str, k: int, dual: bool = False) -> list[tuple[int, str]]:
    """Búsqueda híbrida RRF; devuelve [(document_id, content), ...] (los ids permiten
    medir Recall por doc en el harness; producción usa solo el content vía _retrieve).

    Ramas fusionadas por RRF:
      - vec_es : ranking vectorial con la query original (siempre).
      - lex    : ranking léxico 'es_simple' (siempre; no ayuda cross-lingual pero
                 mantiene sano el español).
      - vec_en : ranking vectorial con la traducción inglesa (SOLO si dual y la
                 traducción no falló). Es la rama que recupera el doc inglés al pool.
    """
    embedder = get_embeddings()
    qvec_es = embedder.embed_query(query)
    pool = k * 5  # candidatos por rama antes de fusionar

    params = {
        "qvec_es": str(qvec_es),
        "qtext": query,
        "pool": pool,
        "rrf_k": RRF_K,
        "k": k,
    }

    # Rama inglesa (dual): traducir + embeddear. Fail-safe -> si no hay traducción,
    # se omite la rama y la dual se comporta como la búsqueda es de siempre.
    vec_en_cte = ""
    vec_en_union = ""
    if dual:
        en_query = await _translate_to_english(query)
        if en_query:
            qvec_en = embedder.embed_query(en_query)
            params["qvec_en"] = str(qvec_en)
            vec_en_cte = """,
                vec_en AS (
                    SELECT id, document_id, content,
                           row_number() OVER (ORDER BY embedding <=> :qvec_en) AS rnk
                    FROM document_chunks
                    ORDER BY embedding <=> :qvec_en
                    LIMIT :pool
                )"""
            vec_en_union = "UNION ALL SELECT id, document_id, content, rnk FROM vec_en"

    sql = f"""
        WITH vec AS (
            SELECT id, document_id, content,
                   row_number() OVER (ORDER BY embedding <=> :qvec_es) AS rnk
            FROM document_chunks
            ORDER BY embedding <=> :qvec_es
            LIMIT :pool
        ),
        lex AS (
            SELECT id, document_id, content,
                   row_number() OVER (
                       ORDER BY ts_rank(content_tsv,
                                        plainto_tsquery('es_simple', :qtext)) DESC
                   ) AS rnk
            FROM document_chunks
            WHERE content_tsv @@ plainto_tsquery('es_simple', :qtext)
            LIMIT :pool
        ){vec_en_cte},
        fused AS (
            SELECT id, document_id, content, SUM(1.0 / (:rrf_k + rnk)) AS score
            FROM (
                SELECT id, document_id, content, rnk FROM vec
                UNION ALL
                SELECT id, document_id, content, rnk FROM lex
                {vec_en_union}
            ) t
            GROUP BY id, document_id, content
        )
        SELECT document_id, content FROM fused
        ORDER BY score DESC
        LIMIT :k
    """
    async with worker_session() as session:
        rows = await session.execute(text(sql), params)
        return [(r[0], r[1]) for r in rows.fetchall()]


async def _retrieve(query: str, k: int, dual: bool = False) -> list[str]:
    """Igual que _retrieve_ids pero devuelve solo el content (lo que usa producción)."""
    return [c for _, c in await _retrieve_ids(query, k, dual=dual)]


async def search(query: str, k: int = 8, dual: bool = False) -> list[str]:
    """Recupera FETCH_N candidatos híbridos y el cross-encoder los reordena al top-k.

    El re-ranking es sync (GPU); se delega a un hilo con asyncio.to_thread para no
    bloquear el event loop del caller. `dual=True` añade la rama vectorial inglesa
    (P30) y amplía el fetch a DUAL_FETCH_N (el pool de 3 ramas necesita más holgura);
    el reranker (multilingüe) ordena el pool combinado con la query original.
    """
    fetch_n = DUAL_FETCH_N if dual else FETCH_N
    candidates = await _retrieve(query, k=fetch_n, dual=dual)
    if not candidates:
        return []
    return await asyncio.to_thread(rerank, query, candidates, k)
