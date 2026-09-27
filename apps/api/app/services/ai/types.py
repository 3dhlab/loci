from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TranscriptionSegmentResult:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class TranscriptionResult:
    model: str
    text: str
    segments: list[TranscriptionSegmentResult]


@dataclass
class EmbeddingResult:
    model: str
    vectors: list[list[float]]
    total_tokens: int | None = None


@dataclass
class VisualDescriptionResult:
    model: str
    description_text: str
    image_count: int
    total_tokens: int | None = None


@dataclass
class TitleCardOCRResult:
    model: str
    payload: dict
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
