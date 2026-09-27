from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.ai.types import EmbeddingResult, TitleCardOCRResult, TranscriptionResult, VisualDescriptionResult


class AIProviderError(RuntimeError):
    pass


class AIProvider(ABC):
    @abstractmethod
    def transcribe_audio(self, file_path: str, language: str = "en") -> TranscriptionResult:
        raise NotImplementedError

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        raise NotImplementedError

    def describe_images(self, image_paths: list[str], prompt: str) -> VisualDescriptionResult:
        raise AIProviderError(f"{self.__class__.__name__} does not support image description generation")

    def extract_title_card(
        self,
        image_path: str,
        prompt: str,
        *,
        detail: str = "high",
        max_tokens: int = 400,
    ) -> TitleCardOCRResult:
        raise AIProviderError(f"{self.__class__.__name__} does not support title-card OCR extraction")
