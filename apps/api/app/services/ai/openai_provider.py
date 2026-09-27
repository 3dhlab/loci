from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx

from app.core.config import settings
from app.services.ai.base import AIProvider, AIProviderError
from app.services.ai.types import EmbeddingResult, TitleCardOCRResult, TranscriptionResult, TranscriptionSegmentResult, VisualDescriptionResult


class OpenAIProvider(AIProvider):
    def __init__(
        self,
        api_key: str,
        embedding_model: str,
        embedding_dimensions: int,
        transcription_model: str,
        vision_model: str,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        if not api_key:
            raise AIProviderError("OPENAI_API_KEY is not configured")

        self.api_key = api_key
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.transcription_model = transcription_model
        self.vision_model = vision_model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _post_with_retry(self, url: str, *, headers: dict, json: dict | None = None, data: dict | None = None, files=None):
        max_retries = max(0, int(settings.openai_max_retries))
        backoff_seconds = max(0.0, float(settings.openai_retry_backoff_seconds))

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(url, headers=headers, json=json, data=data, files=files)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                retryable = status_code in {408, 409, 425, 429} or (status_code is not None and status_code >= 500)
            except httpx.TransportError as exc:
                last_error = exc
                retryable = True

            if attempt >= max_retries or not retryable:
                break

            if backoff_seconds > 0:
                time.sleep(backoff_seconds * (2**attempt))

        if last_error is not None:
            raise last_error
        raise AIProviderError("OpenAI request failed without an exception")

    def transcribe_audio(self, file_path: str, language: str = "en") -> TranscriptionResult:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise AIProviderError(f"Audio source does not exist: {file_path}")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        data = {
            "model": self.transcription_model,
            "language": language,
            "response_format": "verbose_json",
        }

        with path.open("rb") as audio_stream:
            files = {
                "file": (path.name, audio_stream, "application/octet-stream"),
            }
            try:
                response = self._post_with_retry(
                    f"{self.base_url}/audio/transcriptions",
                    headers=headers,
                    data=data,
                    files=files,
                )
            except httpx.HTTPError as exc:
                raise AIProviderError("OpenAI transcription request failed") from exc

        payload = response.json()
        text = (payload.get("text") or "").strip()
        if not text:
            raise AIProviderError("OpenAI transcription response did not include text")

        segments: list[TranscriptionSegmentResult] = []
        for item in payload.get("segments", []) or []:
            segment_text = (item.get("text") or "").strip()
            if not segment_text:
                continue

            start_ms = int(float(item.get("start", 0)) * 1000)
            end_ms = int(float(item.get("end", 0)) * 1000)
            if end_ms < start_ms:
                end_ms = start_ms

            segments.append(
                TranscriptionSegmentResult(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=segment_text,
                )
            )

        return TranscriptionResult(
            model=payload.get("model") or self.transcription_model,
            text=text,
            segments=segments,
        )

    def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        cleaned = [text.strip() for text in texts if text.strip()]
        if not cleaned:
            raise AIProviderError("No texts provided for embedding")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.embedding_model,
            "input": cleaned,
        }
        if self.embedding_model.startswith("text-embedding-3"):
            payload["dimensions"] = self.embedding_dimensions

        try:
            response = self._post_with_retry(
                f"{self.base_url}/embeddings",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise AIProviderError("OpenAI embeddings request failed") from exc

        body = response.json()
        data = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in data]

        if not vectors or any(not isinstance(vec, list) for vec in vectors):
            raise AIProviderError("OpenAI embeddings response was missing vectors")

        usage = body.get("usage") or {}
        return EmbeddingResult(
            model=body.get("model") or self.embedding_model,
            vectors=vectors,
            total_tokens=usage.get("total_tokens"),
        )

    def describe_images(self, image_paths: list[str], prompt: str) -> VisualDescriptionResult:
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            raise AIProviderError("No prompt provided for image description")

        content = [{"type": "text", "text": cleaned_prompt}]
        normalized_paths: list[Path] = []
        for image_path in image_paths:
            path = Path(image_path)
            if not path.exists() or not path.is_file():
                raise AIProviderError(f"Image source does not exist: {image_path}")
            normalized_paths.append(path)
            mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                }
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.vision_model,
            "temperature": settings.openai_vision_temperature,
            "max_tokens": settings.visual_description_max_tokens,
            "messages": [{"role": "user", "content": content}],
        }

        try:
            response = self._post_with_retry(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise AIProviderError("OpenAI image description request failed") from exc

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise AIProviderError("OpenAI image description response did not include choices")

        message = choices[0].get("message") or {}
        description_text = (message.get("content") or "").strip()
        if not description_text:
            raise AIProviderError("OpenAI image description response did not include text")

        usage = body.get("usage") or {}
        return VisualDescriptionResult(
            model=body.get("model") or self.vision_model,
            description_text=description_text,
            image_count=len(normalized_paths),
            total_tokens=usage.get("total_tokens"),
        )

    def extract_title_card(
        self,
        image_path: str,
        prompt: str,
        *,
        detail: str = "high",
        max_tokens: int = 400,
    ) -> TitleCardOCRResult:
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            raise AIProviderError("No prompt provided for title-card OCR extraction")

        path = Path(image_path)
        if not path.exists() or not path.is_file():
            raise AIProviderError(f"Image source does not exist: {image_path}")

        cleaned_detail = detail.strip().lower() or "high"
        mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.vision_model,
            "temperature": settings.openai_vision_temperature,
            "max_tokens": max(1, int(max_tokens)),
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": cleaned_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{encoded}",
                                "detail": cleaned_detail,
                            },
                        },
                    ],
                }
            ],
        }

        try:
            response = self._post_with_retry(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise AIProviderError("OpenAI title-card OCR request failed") from exc

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise AIProviderError("OpenAI title-card OCR response did not include choices")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            payload_text = content.strip()
        elif isinstance(content, list):
            payload_text = "\n".join(
                str(item.get("text") or "").strip()
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        else:
            payload_text = ""

        if not payload_text:
            raise AIProviderError("OpenAI title-card OCR response did not include JSON content")

        try:
            parsed_payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise AIProviderError("OpenAI title-card OCR response was not valid JSON") from exc

        if not isinstance(parsed_payload, dict):
            raise AIProviderError("OpenAI title-card OCR response JSON must be an object")

        usage = body.get("usage") or {}
        return TitleCardOCRResult(
            model=body.get("model") or self.vision_model,
            payload=parsed_payload,
            prompt_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )


def get_openai_provider() -> OpenAIProvider:
    return OpenAIProvider(
        api_key=settings.openai_api_key,
        embedding_model=settings.openai_embedding_model,
        embedding_dimensions=settings.embedding_vector_dimensions,
        transcription_model=settings.openai_transcription_model,
        vision_model=settings.openai_vision_model,
        base_url=settings.openai_api_base,
        timeout_seconds=settings.openai_timeout_seconds,
    )
