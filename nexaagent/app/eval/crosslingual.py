"""Diagnóstico CROSS-LINGUAL del retriever (sesión P29, NO implementa nada).

Problema (visto en P28 case d): una pregunta en ESPAÑOL sobre un documento en
INGLÉS recupera el doc equivocado. Este harness MIDE qué palanca barata lo
resuelve, sin tocar producción (igual que el reranking se midió en P18/P19).

Mide variantes EN PARALELO sobre los MISMOS pares, reproduciendo la lógica del
retriever de producción pero parametrizada:

  - lex_config: 'es_simple' (producción) vs 'simple'.  OJO: la columna almacenada
    content_tsv es GENERATED con 'es_simple'. Para medir la palanca 'simple' de
    forma HONESTA computamos to_tsvector(cfg, content) AL VUELO en ambos lados
    (query y doc) — si comparáramos una query 'simple' contra la columna 'es_simple'
    mediríamos un desajuste de lexemas, no la palanca. Corpus chico -> seq scan OK.
  - expand: añade a la query su traducción al inglés (es + en), para vector y léxico.
    Más robusto que traducir (el Drive es bilingüe), como pidió el diseño.

Para CADA config reporta dos cosas por par:
  - retrieve_rank: posición del chunk correcto en el top-20 del RETRIEVER (la firma
    k=20 de P19: ¿se recupera y se ordena mal, o NO se recupera?).
  - rerank_rank: posición tras el cross-encoder (top-8) -> Recall@1/@5 que ve el agente.
El rerank usa SIEMPRE la query española original (aislar el efecto del POOL; el
reranker bge-v2-m3 es multilingüe).

Uso:
    docker compose exec api python -m app.eval.crosslingual
    docker compose exec api python -m app.eval.crosslingual app/eval/datasets/crosslingual.json
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.db.worker_db import worker_session
from app.rag.embeddings import get_embeddings
from app.rag.reranker import rerank

RRF_K = 60
FETCH_N = 20          # top-20 del retriever (la firma de diagnóstico)
RERANK_K = 8          # lo que el agente usa
POOL = FETCH_N * 5    # candidatos por rama antes de fusionar (= producción: k*5)

DEFAULT_DATASET = "app/eval/datasets/crosslingual.json"

# (nombre, usa_query_expandida, lex_config)
CONFIGS = [
    ("baseline",   False, "es_simple"),
    ("lex_simple", False, "simple"),
    ("expand",     True,  "es_simple"),
    ("combo",      True,  "simple"),
]


def _translate_to_english(spanish: str) -> str:
    """Traduce la query al inglés con el propio LLM (qwen2.5). Determinístico
    (temp 0). Se imprime para auditar la traducción."""
    prompt = (
        "Translate the following search query to English. "
        "Output ONLY the translation, no quotes, no explanation.\n\n"
        f"Query: {spanish}"
    )
    resp = httpx.post(
        f"{settings.ollama_base_url}/api/chat",
        json={
            "model": settings.ollama_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


async def _retrieve_ids(vec_query: str, lex_query: str, lex_config: str,
                        k: int = FETCH_N) -> list[dict]:
    """Híbrido RRF como producción, pero: (1) devuelve id+document_id+content,
    (2) lex_config parametrizado y computado al vuelo en ambos lados, (3) vec y
    lex pueden usar queries distintas (para la expansión)."""
    qvec = get_embeddings().embed_query(vec_query)
    sql = f"""
        WITH vec AS (
            SELECT id, document_id, content,
                   row_number() OVER (ORDER BY embedding <=> :qvec) AS rnk
            FROM document_chunks
            ORDER BY embedding <=> :qvec
            LIMIT :pool
        ),
        lex AS (
            SELECT id, document_id, content,
                   row_number() OVER (
                       ORDER BY ts_rank(to_tsvector('{lex_config}', content),
                                        plainto_tsquery('{lex_config}', :qtext)) DESC
                   ) AS rnk
            FROM document_chunks
            WHERE to_tsvector('{lex_config}', content)
                  @@ plainto_tsquery('{lex_config}', :qtext)
            LIMIT :pool
        ),
        fused AS (
            SELECT id, document_id, content, SUM(1.0 / (:rrf_k + rnk)) AS score
            FROM (
                SELECT id, document_id, content, rnk FROM vec
                UNION ALL
                SELECT id, document_id, content, rnk FROM lex
            ) t
            GROUP BY id, document_id, content
        )
        SELECT id, document_id, content FROM fused
        ORDER BY score DESC
        LIMIT :k
    """
    async with worker_session() as session:
        rows = await session.execute(
            text(sql),
            {"qvec": str(qvec), "qtext": lex_query, "pool": POOL, "rrf_k": RRF_K, "k": k},
        )
        return [{"id": r[0], "document_id": r[1], "content": r[2]} for r in rows.fetchall()]


def _is_correct(cand: dict, case: dict) -> bool:
    if "doc_id" in case:
        return cand["document_id"] == case["doc_id"]
    return case["huella"] in cand["content"]


def _rank_in(cands: list[dict], case: dict) -> int | None:
    for i, c in enumerate(cands, start=1):
        if _is_correct(c, case):
            return i
    return None


async def evaluate(dataset_path: str):
    cases = json.loads(Path(dataset_path).read_text(encoding="utf-8"))

    # Traducción al inglés (una vez por caso); se imprime para auditar.
    print("Traducciones LLM (es -> en):")
    for case in cases:
        en = _translate_to_english(case["pregunta"])
        case["_en"] = en
        print(f"  {case['pregunta'][:46]!r:50} -> {en!r}")
    print()

    # results[config][case_idx] = (retrieve_rank, rerank_rank)
    results: dict[str, list[tuple]] = {name: [] for name, _, _ in CONFIGS}

    for case in cases:
        es = case["pregunta"]
        expanded = f"{es} {case['_en']}"
        for name, use_expand, lex_cfg in CONFIGS:
            vec_q = expanded if use_expand else es
            lex_q = expanded if use_expand else es
            cands = await _retrieve_ids(vec_q, lex_q, lex_cfg, k=FETCH_N)
            retrieve_rank = _rank_in(cands, case)
            # rerank con la query ESPAÑOLA original (aísla el efecto del pool)
            reranked_contents = await asyncio.to_thread(
                rerank, es, [c["content"] for c in cands], RERANK_K
            )
            reranked = [{"document_id": None, "content": c} for c in reranked_contents]
            # para matchear por doc_id necesitamos el document_id del contenido reranked
            content2doc = {c["content"]: c["document_id"] for c in cands}
            for rc in reranked:
                rc["document_id"] = content2doc.get(rc["content"])
            rerank_rank = _rank_in(reranked, case)
            results[name].append((retrieve_rank, rerank_rank))

    return cases, results


def _recall(ranks: list[int | None], k: int) -> float:
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def _format(cases: list[dict], results: dict) -> str:
    n = len(cases)
    out = [f"\n{'='*70}", f"PARES: {n}", "="*70]

    # Tabla agregada: Recall@1/@5 (post-rerank) + Recall@20 (retriever) por config
    out.append("\nAGREGADO (post-rerank salvo R@20-retr):")
    out.append(f"  {'config':12} {'R@1':>6} {'R@5':>6} {'R@20(retr)':>11}")
    for name, _, _ in CONFIGS:
        rr = [rk for _, rk in results[name]]      # rerank ranks
        tr = [rt for rt, _ in results[name]]      # retrieve ranks (top-20)
        out.append(f"  {name:12} {_recall(rr,1):6.3f} {_recall(rr,5):6.3f} "
                   f"{_recall(tr,20):11.3f}")

    # Por caso: retrieve_rank / rerank_rank en cada config (la firma k=20)
    out.append("\nPOR CASO  (formato 'retr|rrnk'; '-' = ausente):")
    header = f"  {'caso':26}" + "".join(f"{name[:9]:>11}" for name, _, _ in CONFIGS)
    out.append(header)
    for i, case in enumerate(cases):
        label = (case.get("etiqueta") or case["pregunta"])[:25]
        cells = ""
        for name, _, _ in CONFIGS:
            rt, rk = results[name][i]
            cells += f"{(str(rt) if rt else '-')+'|'+(str(rk) if rk else '-'):>11}"
        out.append(f"  {label:26}{cells}")
    return "\n".join(out)


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATASET
    cases, results = asyncio.run(evaluate(ds))
    print(_format(cases, results))
