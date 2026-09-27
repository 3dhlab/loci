from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

from app.core.config import settings
from app.models.entities import TitleCardObservation, Video

VALID_TITLE_CARD_CONFIDENCE_VALUES = {"high", "medium", "low", "none"}
VALID_TITLE_CARD_STATUSES = {"pending", "ready", "failed"}


@dataclass
class TitleCardObservationDraft:
    timestamp_ms: int
    payload: dict[str, Any]
    status: str = "ready"
    prompt_version: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    detail: str | None = None
    source_kind: str | None = None
    source_version: str | None = None


@dataclass
class TitleCardImportBatch:
    prompt_version: str | None
    model_provider: str | None
    model_name: str | None
    detail: str | None
    source_kind: str | None
    source_version: str | None
    observations: list[TitleCardObservationDraft]


def build_title_card_ocr_prompt() -> str:
    return settings.title_card_prompt_template.strip()


def title_card_sample_timestamps(duration_ms: int | None, cadence_seconds: int | None = None) -> list[int]:
    cadence_ms = max(1, int(cadence_seconds or settings.title_card_sample_cadence_seconds)) * 1000
    normalized_duration_ms = max(0, int(duration_ms or 0))
    if normalized_duration_ms <= 0:
        return [0]
    return list(range(0, normalized_duration_ms, cadence_ms)) or [0]


def load_title_card_import_batch(path: str | Path) -> TitleCardImportBatch:
    """Read a fixture file and parse it into an import batch."""
    return load_title_card_import_batch_from_bytes(Path(path).read_bytes())


def load_title_card_import_batch_from_bytes(data: bytes) -> TitleCardImportBatch:
    """Parse an import batch from already-read bytes.

    This lets the guarded import hash and parse the SAME bytes (no reopen), so
    the bytes that were SHA-verified are exactly the bytes imported — closing
    the time-of-check/time-of-use gap between hashing and parsing.
    """
    payload = json.loads(data.decode("utf-8"))
    if isinstance(payload, list):
        defaults: dict[str, Any] = {}
        rows = payload
    elif isinstance(payload, dict):
        defaults = payload
        rows = payload.get("observations")
        if rows is None and "timestamp_ms" in payload:
            rows = [payload]
    else:
        raise ValueError("Title-card OCR fixture must be a JSON object or array")

    if not isinstance(rows, list) or not rows:
        raise ValueError("Title-card OCR fixture must include a non-empty observations array")

    observations = [_parse_observation_draft(row, defaults=defaults) for row in rows]
    return TitleCardImportBatch(
        prompt_version=_resolve_prompt_version(defaults.get("prompt_version")),
        model_provider=_resolve_model_provider(defaults.get("model_provider")),
        model_name=_resolve_model_name(defaults.get("model_name")),
        detail=_normalize_optional_text(defaults.get("detail")) or settings.title_card_ocr_detail,
        source_kind=_resolve_source_kind(defaults.get("source_kind")),
        source_version=_resolve_source_version(defaults.get("source_version")),
        observations=observations,
    )


def normalize_title_card_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Title-card OCR payload must be a JSON object")

    object_block = payload.get("object") if isinstance(payload.get("object"), dict) else {}
    presenter_block = payload.get("presenter") if isinstance(payload.get("presenter"), dict) else {}
    confidence_block = payload.get("confidence") if isinstance(payload.get("confidence"), dict) else {}

    title_card_visible = payload.get("title_card_visible")
    if not isinstance(title_card_visible, bool):
        raise ValueError("title_card_visible must be a boolean")

    raw_text_observed = payload.get("raw_text_observed")
    if not isinstance(raw_text_observed, str) or not raw_text_observed.strip():
        raise ValueError("raw_text_observed must be a non-empty string")

    session_date_text = _normalize_verbatim_text(payload.get("session_date_text") or payload.get("session_date"))

    return {
        "title_card_visible": title_card_visible,
        "object_name": _normalize_optional_text(object_block.get("name")),
        "object_accession_number": _normalize_optional_text(object_block.get("accession_number")),
        "object_holding_institution": _normalize_optional_text(object_block.get("holding_institution")),
        "object_city": _normalize_optional_text(object_block.get("city")),
        "object_region": _normalize_optional_text(object_block.get("region")),
        "object_country": _normalize_optional_text(object_block.get("country")),
        "presenter_name": _normalize_optional_text(presenter_block.get("name")),
        "presenter_age": _normalize_optional_int(presenter_block.get("age")),
        "presenter_city": _normalize_optional_text(presenter_block.get("city")),
        "presenter_region": _normalize_optional_text(presenter_block.get("region")),
        "presenter_country": _normalize_optional_text(presenter_block.get("country")),
        "public_speaker_label": _normalize_optional_text(payload.get("public_speaker_label")),
        "session_date": _parse_session_date(session_date_text),
        "session_date_text": session_date_text,
        "additional_text": _normalize_optional_text(payload.get("additional_text")),
        "raw_text_observed": raw_text_observed,
        "raw_payload_json": json.loads(json.dumps(payload)),
        "confidence_object": _normalize_confidence(confidence_block.get("object")),
        "confidence_presenter": _normalize_confidence(confidence_block.get("presenter")),
        "confidence_session_date": _normalize_confidence(confidence_block.get("session_date")),
    }


def import_title_card_observation_batch(
    db,
    *,
    video: Video,
    batch: TitleCardImportBatch,
    model_run_id: UUID | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Import title-card observations for one video.

    When ``replace_existing`` is enabled, replacement stays intentionally narrow:
    stale rows are deleted only when their prompt version, source kind, and
    source version match the imported batch. Other observation sources for the
    same video are preserved.
    """
    if not batch.observations:
        return {
            "observations_written": 0,
            "inserted": 0,
            "updated": 0,
            "removed": 0,
            "prompt_versions": [],
        }

    replacement_dimensions = {
        (
            _resolve_prompt_version(draft.prompt_version or batch.prompt_version),
            _resolve_source_kind(draft.source_kind or batch.source_kind),
            _resolve_source_version(draft.source_version or batch.source_version),
        )
        for draft in batch.observations
    }
    existing_rows = db.execute(select(TitleCardObservation).where(TitleCardObservation.video_id == video.id)).scalars().all()
    existing_by_key = {
        (row.timestamp_ms, row.prompt_version, row.source_kind, row.source_version): row
        for row in existing_rows
    }

    inserted = 0
    updated = 0
    seen_keys: set[tuple[int, str, str, str]] = set()

    for draft in batch.observations:
        normalized = normalize_title_card_payload(draft.payload)
        prompt_version = _resolve_prompt_version(draft.prompt_version or batch.prompt_version)
        source_kind = _resolve_source_kind(draft.source_kind or batch.source_kind)
        source_version = _resolve_source_version(draft.source_version or batch.source_version)
        key = (draft.timestamp_ms, prompt_version, source_kind, source_version)
        row = existing_by_key.get(key)
        if row is None:
            row = TitleCardObservation(
                video_id=video.id,
                timestamp_ms=draft.timestamp_ms,
                title_card_visible=normalized["title_card_visible"],
                raw_text_observed=normalized["raw_text_observed"],
                prompt_version=prompt_version,
                model_provider=_resolve_model_provider(draft.model_provider or batch.model_provider),
                model_name=_resolve_model_name(draft.model_name or batch.model_name),
                detail=_normalize_optional_text(draft.detail or batch.detail) or settings.title_card_ocr_detail,
                source_kind=source_kind,
                source_version=source_version,
                status=_normalize_status(draft.status),
            )
            inserted += 1
        else:
            updated += 1

        row.video_id = video.id
        row.timestamp_ms = draft.timestamp_ms
        row.prompt_version = prompt_version
        row.model_provider = _resolve_model_provider(draft.model_provider or batch.model_provider)
        row.model_name = _resolve_model_name(draft.model_name or batch.model_name)
        row.detail = _normalize_optional_text(draft.detail or batch.detail) or settings.title_card_ocr_detail
        row.source_kind = source_kind
        row.source_version = source_version
        row.status = _normalize_status(draft.status)
        row.model_run_id = model_run_id if model_run_id is not None else row.model_run_id

        for field_name, value in normalized.items():
            setattr(row, field_name, value)

        db.add(row)
        seen_keys.add(key)

    removed = 0
    if replace_existing:
        # Preserve other observation sources for the video unless a caller asks
        # for a broader delete path explicitly outside this import helper.
        stale_ids = [
            row.id
            for key, row in existing_by_key.items()
            if key not in seen_keys
            and (row.prompt_version, row.source_kind, row.source_version) in replacement_dimensions
        ]
        if stale_ids:
            db.execute(delete(TitleCardObservation).where(TitleCardObservation.id.in_(stale_ids)))
            removed = len(stale_ids)

    return {
        "observations_written": len(batch.observations),
        "inserted": inserted,
        "updated": updated,
        "removed": removed,
        "prompt_versions": sorted({dimension[0] for dimension in replacement_dimensions}),
    }


def _parse_observation_draft(row: Any, *, defaults: dict[str, Any]) -> TitleCardObservationDraft:
    if not isinstance(row, dict):
        raise ValueError("Each title-card OCR observation must be a JSON object")

    if "timestamp_ms" not in row:
        raise ValueError("Each title-card OCR observation must include timestamp_ms")

    timestamp_ms = max(0, int(row["timestamp_ms"]))
    payload = _extract_payload(row)
    return TitleCardObservationDraft(
        timestamp_ms=timestamp_ms,
        payload=payload,
        status=_normalize_status(row.get("status") or defaults.get("status") or "ready"),
        prompt_version=_resolve_prompt_version(row.get("prompt_version") or defaults.get("prompt_version")),
        model_provider=_resolve_model_provider(row.get("model_provider") or defaults.get("model_provider")),
        model_name=_resolve_model_name(row.get("model_name") or defaults.get("model_name")),
        detail=_normalize_optional_text(row.get("detail") or defaults.get("detail")) or settings.title_card_ocr_detail,
        source_kind=_resolve_source_kind(row.get("source_kind") or defaults.get("source_kind")),
        source_version=_resolve_source_version(row.get("source_version") or defaults.get("source_version")),
    )


def _extract_payload(row: dict[str, Any]) -> dict[str, Any]:
    nested_payload = row.get("payload")
    if isinstance(nested_payload, dict):
        return nested_payload

    direct_payload_keys = {"title_card_visible", "object", "presenter", "session_date", "session_date_text", "raw_text_observed"}
    if direct_payload_keys.intersection(row.keys()):
        return row

    raise ValueError("Each title-card OCR observation must include a payload object or direct OCR fields")


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text.strip() or None


def _normalize_verbatim_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value.strip() else None


def _normalize_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    normalized = int(value)
    return normalized if normalized >= 0 else None


def _parse_session_date(value: str | None) -> date | None:
    normalized = _normalize_verbatim_text(value)
    if normalized is None:
        return None

    for parser in (date.fromisoformat, _parse_long_month_date, _parse_short_month_date):
        try:
            return parser(normalized)
        except ValueError:
            continue
    return None


def _parse_long_month_date(value: str) -> date:
    return datetime.strptime(value, "%B %d, %Y").date()


def _parse_short_month_date(value: str) -> date:
    return datetime.strptime(value, "%b %d, %Y").date()


def _normalize_confidence(value: Any) -> str | None:
    normalized = str(value or "none").strip().lower()
    if normalized not in VALID_TITLE_CARD_CONFIDENCE_VALUES:
        raise ValueError(f"Unsupported title-card OCR confidence value: {value}")
    return normalized


def _normalize_status(value: Any) -> str:
    normalized = str(value or "pending").strip().lower()
    if normalized not in VALID_TITLE_CARD_STATUSES:
        raise ValueError(f"Unsupported title-card OCR status: {value}")
    return normalized


def _resolve_prompt_version(value: Any) -> str:
    normalized = _normalize_optional_text(value)
    return normalized or settings.title_card_prompt_version


def _resolve_model_provider(value: Any) -> str:
    normalized = _normalize_optional_text(value)
    return normalized or "fixture"


def _resolve_model_name(value: Any) -> str:
    normalized = _normalize_optional_text(value)
    return normalized or "fixture-import"


def _resolve_source_kind(value: Any) -> str:
    normalized = _normalize_optional_text(value)
    return normalized or "fixture_import"


def _resolve_source_version(value: Any) -> str:
    normalized = _normalize_optional_text(value)
    return normalized or "structured-json-v1"