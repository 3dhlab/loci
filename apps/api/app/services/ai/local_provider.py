from __future__ import annotations

from functools import lru_cache

from fastembed import TextEmbedding

from app.core.config import settings
from app.services.ai.base import AIProvider, AIProviderError
from app.services.ai.types import EmbeddingResult, TranscriptionResult


class LocalEmbeddingProvider(AIProvider):
    provider_name = "local"

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.target_dimension = settings.embedding_vector_dimensions

    def transcribe_audio(self, file_path: str, language: str = "en") -> TranscriptionResult:
        raise AIProviderError("Local embedding provider does not support audio transcription")

    def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        cleaned = [text.strip() for text in texts if text.strip()]
        if not cleaned:
            raise AIProviderError("No texts provided for embedding")

        try:
            raw_vectors = list(_text_embedding_model(self.model_name).embed(cleaned))
        except Exception as exc:
            raise AIProviderError("Local embedding generation failed") from exc

        vectors = [self._validate_vector(vector) for vector in raw_vectors]
        return EmbeddingResult(model=f"local:{self.model_name}", vectors=vectors, total_tokens=None)

    def _validate_vector(self, vector) -> list[float]:
        values = [float(value) for value in vector]
        current_dimension = len(values)
        if current_dimension != self.target_dimension:
            raise AIProviderError(
                f"Local embedding dimension mismatch: model produced {current_dimension}, expected {self.target_dimension}"
            )
        return values


@lru_cache(maxsize=4)
def _text_embedding_model(model_name: str) -> TextEmbedding:
    return TextEmbedding(model_name=model_name)


@lru_cache(maxsize=1)
def get_local_embedding_provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider(model_name=settings.local_embedding_model)