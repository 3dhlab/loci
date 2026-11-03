"""Canonical public-only citation attribution snapshots.

This accepts the output of the existing public attribution policy. It never
accepts raw title cards, OCR, presenter records, provenance or internal paths.
"""
from datetime import date
import re

MAX_ATTRIBUTION_BANDS = 10_000
MAX_ATTRIBUTION_TEXT = 255
MAX_ATTRIBUTION_TIME_MS = 2**53 - 1
PAYLOAD_FIELDS = {"speaker_label", "session_date", "session_date_text",
                  "session_date_precision", "attribution_mode"}


class PublicAttributionError(ValueError):
    pass


def _text(value):
    if value is None:
        return None
    if (not isinstance(value, str) or not value or len(value) > MAX_ATTRIBUTION_TEXT
            or value.strip() != value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise PublicAttributionError("Public attribution text is invalid.")
    return value


def _date(value):
    if value is None:
        return None
    if type(value) is date:
        return value.isoformat()
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise PublicAttributionError("Public attribution date is invalid.")
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError:
        raise PublicAttributionError("Public attribution date is invalid.") from None
    if normalized != value:
        raise PublicAttributionError("Public attribution date is not canonical.")
    return normalized


def validate_public_attribution_bands(value):
    """Return a fresh JSON-safe snapshot or fail without exposing input data.

    Empty means intentionally no public attribution. NULL is invalid for a
    generation, so readers cannot silently consult stale local title cards.
    Existing public-policy time segmentation/order is preserved.
    """
    if not isinstance(value, list) or len(value) > MAX_ATTRIBUTION_BANDS:
        raise PublicAttributionError("Public attribution snapshot is invalid.")
    result = []
    previous_start = -1
    for index, band in enumerate(value):
        if not isinstance(band, dict) or set(band) != {"start_ms", "end_ms", "payload"}:
            raise PublicAttributionError("Public attribution band contains unsupported fields.")
        start, end = band["start_ms"], band["end_ms"]
        if (type(start) is not int or not 0 <= start <= MAX_ATTRIBUTION_TIME_MS
                or start < previous_start or (index == 0 and start != 0)
                or (end is None and index != len(value) - 1)
                or (end is not None and (type(end) is not int or not start < end <= MAX_ATTRIBUTION_TIME_MS))):
            raise PublicAttributionError("Public attribution band timing is invalid.")
        payload = band["payload"]
        if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS:
            raise PublicAttributionError("Public attribution payload contains unsupported fields.")
        speaker = _text(payload["speaker_label"])
        exact_date = _date(payload["session_date"])
        date_text = _text(payload["session_date_text"])
        precision = "day" if exact_date is not None else "text" if date_text is not None else "unknown"
        mode = "named" if speaker is not None else "unavailable"
        if (payload["session_date_precision"] != precision or payload["attribution_mode"] != mode
                or (speaker is not None and exact_date is None and date_text is None)):
            raise PublicAttributionError("Public attribution policy flags are inconsistent.")
        result.append({"start_ms": start, "end_ms": end, "payload": {
            "speaker_label": speaker, "session_date": exact_date,
            "session_date_text": date_text, "session_date_precision": precision,
            "attribution_mode": mode}})
        previous_start = start
    return result
