from langchain_ollama import OllamaEmbeddings

from app.core.config import settings

_embeddings: OllamaEmbeddings | None = None


def get_embeddings() -> OllamaEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = OllamaEmbeddings(
            model=settings.ollama_embedding_model,
            base_url=settings.ollama_base_url,
        )
    return _embeddings
