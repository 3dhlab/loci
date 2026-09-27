import logging
from threading import Lock

from app.core.config import settings
from app.services.ai.base import AIProvider, AIProviderError
from app.services.ai.local_provider import LocalEmbeddingProvider, get_local_embedding_provider
from app.services.ai.openai_provider import OpenAIProvider, get_openai_provider
from app.services.ai.types import EmbeddingResult, TitleCardOCRResult, TranscriptionResult, TranscriptionSegmentResult, VisualDescriptionResult

logger = logging.getLogger(__name__)
_warmup_lock = Lock()
_warmed_model_name: str | None = None
_warmup_attempted = False


def get_embedding_provider() -> AIProvider:
    if not settings.embedding_enabled:
        raise AIProviderError("Embedding retrieval is disabled by configuration.")
    provider_preference = settings.embedding_provider.strip().lower()
    if provider_preference == "openai":
        return get_openai_provider()
    if provider_preference in {"", "auto", "local"}:
        return get_local_embedding_provider()
    raise AIProviderError(f"Unsupported embedding provider: {settings.embedding_provider}")


def get_vision_provider() -> AIProvider:
    return get_openai_provider()


def warm_embedding_provider() -> str | None:
    global _warmup_attempted, _warmed_model_name

    if not settings.embedding_enabled or not settings.embedding_warmup_enabled:
        return None

    with _warmup_lock:
        if _warmup_attempted:
            return _warmed_model_name

        provider = get_embedding_provider()
        provider_name = getattr(provider, "provider_name", "")
        if provider_name != "local":
            _warmup_attempted = True
            logger.info("Skipping embedding warmup for non-local provider: %s", provider_name or "unknown")
            return None

        provider.embed_texts(["semantic search warmup"])
        _warmed_model_name = getattr(provider, "model_name", None)
        _warmup_attempted = True
        logger.info("Local embedding model warmed: %s", _warmed_model_name or "unknown")
        return _warmed_model_name

__all__ = [
    "AIProvider",
    "AIProviderError",
    "LocalEmbeddingProvider",
    "OpenAIProvider",
    "TitleCardOCRResult",
    "get_embedding_provider",
    "get_local_embedding_provider",
    "get_openai_provider",
    "get_vision_provider",
    "warm_embedding_provider",
    "EmbeddingResult",
    "TranscriptionResult",
    "TranscriptionSegmentResult",
    "VisualDescriptionResult",
]
