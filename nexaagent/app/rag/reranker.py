"""Re-ranking con cross-encoder: reordena los candidatos del retriever por relevancia fina.

Carga bge-reranker-v2-m3 una vez (singleton) en GPU y fp16. El import de
sentence_transformers/torch es lazy dentro del singleton para que el worker (que
nunca rerankea) no arrastre torch.
"""
import logging

logger = logging.getLogger("nexa.reranker")

_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder  # lazy: torch solo si se usa

        logger.info("loading reranker", extra={"event": "reranker_load", "model": _MODEL_NAME})
        model = CrossEncoder(_MODEL_NAME, device="cuda", max_length=1024)
        model.model.half()
        _model = model
        logger.info("reranker loaded", extra={"event": "reranker_ready"})
    return _model


def rerank(query: str, chunks: list[str], top_k: int) -> list[str]:
    """Reordena `chunks` por relevancia a `query`; devuelve los top_k mejores.

    Es SYNC (CrossEncoder.predict bloquea y usa GPU). El caller async lo invoca
    con asyncio.to_thread para no bloquear el event loop.
    """
    if not chunks:
        return []
    scores = _get_model().predict([(query, c) for c in chunks])  # ve query+chunk JUNTOS
    ranked = [c for _, c in sorted(zip(scores, chunks), key=lambda pair: pair[0], reverse=True)]
    return ranked[:top_k]
