"""Evaluación de recuperación: Recall@k y MRR del retriever, sobre huellas conocidas.

Mide el RETRIEVER PURO (search directo), sin el agente/LLM de por medio. Necesita
la BD con los chunks cargados y Ollama vivo (search llama a embed_query). Por eso
NO va en la suite pytest determinista (Parte 15: nada de Ollama en CI); se corre a
mano contra la BD real:

    docker compose exec api python -m app.eval.retrieval
    # o con otro dataset:
    docker compose exec api python -m app.eval.retrieval app/eval/datasets/otro.yaml
"""
import asyncio
import json
import sys
from pathlib import Path

import yaml

from app.rag.retriever import search

K_VALUES = [1, 3, 5, 8]          # el agente usa 8; medimos varios para ver el margen
MAX_K = max(K_VALUES)

DEFAULT_DATASET = "app/eval/datasets/retrieval_proyectos.yaml"


def load_cases(dataset_path: str) -> list[dict]:
    """Lee el dataset: JSON (sintético, stdlib) o YAML (proyectos reales).

    El generador sintético (P19) nace en JSON, stdlib, cero deps; los proyectos
    reales siguen en YAML. El harness es agnóstico al formato según extensión.
    """
    p = Path(dataset_path)
    raw = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        return json.loads(raw)
    return yaml.safe_load(raw)


async def evaluate_cases(cases: list[dict]):
    """Mide Recall@k y MRR sobre una lista de casos ya cargada en memoria."""
    # ranks[caso] = posición (1-based) del primer chunk con la huella, o None
    ranks = []
    for case in cases:
        chunks = await search(case["pregunta"], k=MAX_K)
        rank = None
        for i, chunk in enumerate(chunks, start=1):
            if case["huella"] in chunk:          # acierto = huella exacta presente
                rank = i
                break
        ranks.append((case, rank))

    # Recall@k = fracción de casos cuyo chunk correcto está en el top-k
    recall = {
        k: sum(1 for _, r in ranks if r is not None and r <= k) / len(ranks)
        for k in K_VALUES
    }
    # MRR = media de 1/rank (0 si no se encontró en absoluto)
    mrr = sum((1.0 / r) if r else 0.0 for _, r in ranks) / len(ranks)
    return recall, mrr, ranks


async def evaluate(dataset_path: str):
    return await evaluate_cases(load_cases(dataset_path))


def _format(recall, mrr, ranks) -> str:
    lines = [f"Casos: {len(ranks)}", ""]
    for k in K_VALUES:
        lines.append(f"  Recall@{k}: {recall[k]:.3f}")
    lines.append(f"  MRR:       {mrr:.3f}")
    lines.append("")
    lines.append("Por caso (rank del chunk correcto; '-' = no en top-8):")
    for case, rank in ranks:
        mark = str(rank) if rank else "-"
        lines.append(f"  [{mark}] {case['proyecto']:10} {case['pregunta'][:50]}")
    return "\n".join(lines)


if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATASET
    recall, mrr, ranks = asyncio.run(evaluate(ds))
    print(_format(recall, mrr, ranks))
