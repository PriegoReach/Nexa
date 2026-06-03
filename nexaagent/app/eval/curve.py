"""Curva de recuperación vs. escala del corpus (P19) — versión automatizada.

Equivalente de un comando al protocolo manual con gates. Mide cómo se degrada el
retriever (Recall@1/3/5/8 + MRR) a medida que crece el número de proyectos de
PLANTILLA IDÉNTICA (Opción B: un proyecto = un chunk). Hipótesis: a más falsos
amigos léxicos, más cae Recall@1 — y si cae, ESO es el hallazgo, no un fallo: es
el techo real del re-ranking a escala. La plantilla idéntica es el peor caso a
propósito.

Protocolo de AISLAMIENTO por punto N:
  1. limpiar()           — arranca limpio (marcador synthetic_eval_%).
  2. generar_e_insertar  — inserta SOLO los N (embeddings reales, 1 chunk c/u).
  3. GATE                — confirmar chunks==N y con_vector==N ANTES de medir.
                           Si falla, el problema está en la inserción, no en el
                           ranking: paramos aquí, no medimos sobre el vacío.
  4. evaluate_cases      — el harness sobre ese dataset.
  5. limpiar()           — borrar los N antes del siguiente punto.

Los 4 proyectos reales (RB-/ESM-/AM-/ON-) quedan como ruido inocuo: sus huellas no
se solapan con SYN-*, así que nunca aciertan una pregunta sintética.

Necesita BD con pgvector + Ollama vivos. Como el harness, NO va en CI:

    docker compose exec api python -m app.eval.curve
"""
import asyncio
import sys

from sqlalchemy import text

from app.db.worker_db import worker_session
from app.eval.generate_synthetic import (
    MARKER,
    MAX_ATRIBUTO,
    SIZES,
    generar_e_insertar,
    limpiar,
)
from app.eval.retrieval import K_VALUES, evaluate_cases


async def _gate() -> tuple[int, int]:
    """Cuenta chunks sintéticos y cuántos tienen vector. (chunks, con_vector)."""
    async with worker_session() as session:
        row = (await session.execute(
            text(
                "SELECT count(c.id) AS chunks, count(c.embedding) AS con_vector "
                "FROM documents d "
                "LEFT JOIN document_chunks c ON c.document_id = d.id "
                "WHERE d.filename LIKE :p"
            ),
            {"p": f"{MARKER}%"},
        )).one()
        return row.chunks, row.con_vector


async def run_curve(sizes: list[int], variante: str = "numero") -> list[dict]:
    puntos = []
    for n in sizes:
        await limpiar()                              # arranca limpio cada punto
        _, n_chunks, ds = await generar_e_insertar(n, variante)

        chunks, con_vector = await _gate()
        if chunks != n or con_vector != chunks:
            raise SystemExit(
                f"GATE FALLÓ n={n}: chunks={chunks}, con_vector={con_vector} "
                f"(esperado {n}/{n}). La inserción falló; no mido sobre el vacío."
            )

        recall, mrr, _ = await evaluate_cases(ds)
        puntos.append({"n": n, "casos": len(ds), "chunks": n_chunks,
                       "recall": recall, "mrr": mrr})
        print(f"  n={n:>3}: {len(ds)} casos, {n_chunks} chunks  "
              f"R@1={recall[1]:.3f} MRR={mrr:.3f}")
    await limpiar()                                  # no dejar rastro al terminar
    return puntos


def _format_curva(puntos: list[dict], variante: str) -> str:
    head = ["N".rjust(4)] + [f"R@{k}".rjust(7) for k in K_VALUES] + ["MRR".rjust(7)]
    lines = ["", f"Curva ({variante}, corpus aislado por punto, 1 chunk/proyecto):",
             "  ".join(head)]
    for p in puntos:
        row = [str(p["n"]).rjust(4)]
        row += [f"{p['recall'][k]:.3f}".rjust(7) for k in K_VALUES]
        row += [f"{p['mrr']:.3f}".rjust(7)]
        lines.append("  ".join(row))
    return "\n".join(lines)


if __name__ == "__main__":
    # python -m app.eval.curve [variante]   (variante: numero | atributo)
    variante = sys.argv[1] if len(sys.argv) > 1 else "numero"
    # 'atributo' solo tiene responsables únicos hasta MAX_ATRIBUTO.
    sizes = [n for n in SIZES if n <= MAX_ATRIBUTO] if variante == "atributo" else SIZES
    puntos = asyncio.run(run_curve(sizes, variante))
    print(_format_curva(puntos, variante))
