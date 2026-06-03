"""Re-ranking con cross-encoder: reordena los candidatos del retriever por relevancia fina.

Carga bge-reranker-v2-m3 una vez (singleton, como el agente) en GPU y en fp16:
halva los pesos (~2.3GB → ~1.1GB), sano en un card de 12GB que comparte VRAM con
qwen2.5(8192)+nomic (verificado: ~5.3GB libres con el chat residente).

El import de sentence_transformers/torch es LAZY dentro del singleton, a propósito:
así el worker (que nunca rerankea) no arrastra torch al importar este módulo, y el
coste de carga del modelo se paga solo en la primera búsqueda real, en api.
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
        model.model.half()  # fp16 en GPU
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
