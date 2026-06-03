"""Validación de la DUAL cross-lingual sobre corpus REAL (P30).

Compara BASELINE (producción: vec_es + lex_es + rerank) vs DUAL (+ rama vec_en con
traducción LLM) sobre 9 preguntas ES cuyo doc correcto es conocido, más una
regresión en español (la dual NO debe degradar el monolingüe). Mide end-to-end
(retrieve FETCH_N -> rerank top-k, que es lo que ve el agente) y también el pool
del retriever (firma k=20 de P19: ¿el doc llega al reranker?). Reporta latencia.

    docker compose exec api python -m app.eval.dual_validation
"""
import asyncio
import time

from app.rag.retriever import _retrieve_ids, FETCH_N
from app.rag.reranker import rerank

RERANK_K = 8
K_VALUES = (1, 5)

# (pregunta_ES, doc_id_correcto, etiqueta)
CROSSLINGUAL = [
    ("¿Cuáles son los pilares fundamentales del marco Well-Architected de AWS?", 23, "AWS-pilares"),
    ("¿Qué recomendaciones ofrece AWS para mejorar la excelencia operacional en una arquitectura cloud?", 23, "AWS-opex"),
    ("¿Por qué la arquitectura Transformer elimina la necesidad de redes recurrentes?", 22, "TF-norecurrent"),
    ("¿Qué papel cumple el mecanismo de atención en el modelo Transformer?", 22, "TF-attention"),
    ("¿Cuáles son las funciones principales del marco de ciberseguridad del NIST?", 21, "NIST-funcs"),
    ("¿Qué actividades incluye la función Identify dentro del NIST Cybersecurity Framework?", 21, "NIST-identify"),
    ("¿Cuál es el propósito de una Oficina de Gestión de Proyectos (PMO)?", 25, "PMBOK-pmo1"),
    ("¿Cómo ayuda una PMO a mejorar la gobernanza de proyectos dentro de una organización?", 25, "PMBOK-pmo2"),
    ("¿en qué ciudades hay centros de datos?", 20, "GFN-datacenters"),
]

# Regresión monolingüe ES (Spanish query -> Spanish doc). #19 Django, #8 Proyectos.
SPANISH = [
    ("¿qué son las migraciones de modelos en Django?", 19, "django-migr"),
    ("¿cómo se definen los modelos en Django?", 19, "django-models"),
    ("¿cuál es el código de autorización del Proyecto Rubí?", 8, "proy-rubi"),
    ("¿quién es el responsable del Proyecto Ámbar?", 8, "proy-ambar"),
]


async def _ranks(query: str, target: int, dual: bool) -> tuple[int | None, int | None, float]:
    """Devuelve (retr_rank en top-FETCH_N, rerank_rank en top-RERANK_K, segundos)."""
    t0 = time.perf_counter()
    cands = await _retrieve_ids(query, k=FETCH_N, dual=dual)  # [(doc_id, content)]
    retr_ids = [d for d, _ in cands]
    retr_rank = (retr_ids.index(target) + 1) if target in retr_ids else None

    reranked = await asyncio.to_thread(rerank, query, [c for _, c in cands], RERANK_K)
    c2d = {c: d for d, c in cands}
    rer_ids = [c2d.get(c) for c in reranked]
    rer_rank = (rer_ids.index(target) + 1) if target in rer_ids else None
    elapsed = time.perf_counter() - t0
    return retr_rank, rer_rank, elapsed


def _agg(rer_ranks: list[int | None]) -> dict:
    n = len(rer_ranks)
    out = {f"R@{k}": sum(1 for r in rer_ranks if r and r <= k) / n for k in K_VALUES}
    out["MRR"] = sum((1.0 / r) if r else 0.0 for r in rer_ranks) / n
    return out


async def _run(cases: list[tuple], label: str):
    print(f"\n{'='*78}\n{label}  ({len(cases)} preguntas)\n{'='*78}")
    print(f"{'pregunta':30} {'BASE retr|rrnk':>15} {'DUAL retr|rrnk':>15}")
    base_rer, dual_rer, base_t, dual_t = [], [], [], []
    for q, target, tag in cases:
        b_retr, b_rer, b_s = await _ranks(q, target, dual=False)
        d_retr, d_rer, d_s = await _ranks(q, target, dual=True)
        base_rer.append(b_rer); dual_rer.append(d_rer)
        base_t.append(b_s); dual_t.append(d_s)
        fmt = lambda a, b: f"{(str(a) if a else '-')}|{(str(b) if b else '-')}"
        print(f"{tag:30} {fmt(b_retr,b_rer):>15} {fmt(d_retr,d_rer):>15}")
    ba, da = _agg(base_rer), _agg(dual_rer)
    print(f"\n{'metric':10} {'BASELINE':>12} {'DUAL':>12}")
    for m in ("R@1", "R@5", "MRR"):
        print(f"{m:10} {ba[m]:>12.3f} {da[m]:>12.3f}")
    print(f"{'lat(s)':10} {sum(base_t)/len(base_t):>12.2f} {sum(dual_t)/len(dual_t):>12.2f}")
    return base_rer, dual_rer


async def main():
    # warm-up del reranker (carga el modelo) para no contaminar la latencia.
    await asyncio.to_thread(rerank, "warmup", ["a", "b"], 1)
    await _run(CROSSLINGUAL, "CROSS-LINGUAL (pregunta ES -> doc EN)")
    await _run(SPANISH, "REGRESIÓN ESPAÑOL (pregunta ES -> doc ES)")


if __name__ == "__main__":
    asyncio.run(main())
