from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings


@dataclass(frozen=True)
class TranscriptWindowDraft:
    window_index: int
    start_position: int
    end_position: int
    start_ms: int
    end_ms: int
    text: str


def build_transcript_windows(
    segments,
    *,
    target_ms: int | None = None,
    overlap_ms: int | None = None,
) -> list[TranscriptWindowDraft]:
    ordered = sorted(
        [segment for segment in segments if getattr(segment, "text", "").strip()],
        key=lambda segment: (int(getattr(segment, "position", 0)), int(getattr(segment, "start_ms", 0))),
    )
    if not ordered:
        return []

    resolved_target_ms = max(5000, int(target_ms or settings.semantic_window_target_ms))
    resolved_overlap_ms = max(0, min(int(overlap_ms or settings.semantic_window_overlap_ms), resolved_target_ms - 1000))
    step_ms = max(1000, resolved_target_ms - resolved_overlap_ms)

    source_start_ms = max(0, int(getattr(ordered[0], "start_ms", 0)))
    source_end_ms = max(source_start_ms, int(getattr(ordered[-1], "end_ms", getattr(ordered[-1], "start_ms", 0))))

    drafts: list[TranscriptWindowDraft] = []
    seen_ranges: set[tuple[int, int]] = set()
    cursor_ms = source_start_ms
    next_window_index = 0

    while cursor_ms <= source_end_ms:
        start_idx = _first_segment_index_for_cursor(ordered, cursor_ms)
        end_idx = _last_segment_index_for_window(ordered, start_idx, cursor_ms + resolved_target_ms)
        next_window_index = _append_window_if_new(ordered, start_idx, end_idx, next_window_index, seen_ranges, drafts)
        if end_idx >= len(ordered) - 1:
            break
        cursor_ms += step_ms

    if drafts and drafts[-1].end_position != int(getattr(ordered[-1], "position", 0)):
        trailing_start_ms = max(source_start_ms, source_end_ms - resolved_target_ms)
        start_idx = _first_segment_index_for_cursor(ordered, trailing_start_ms)
        end_idx = len(ordered) - 1
        _append_window_if_new(ordered, start_idx, end_idx, next_window_index, seen_ranges, drafts)

    return drafts


def _first_segment_index_for_cursor(ordered, cursor_ms: int) -> int:
    for index, segment in enumerate(ordered):
        segment_end = max(int(getattr(segment, "end_ms", 0)), int(getattr(segment, "start_ms", 0)))
        if segment_end > cursor_ms:
            return index
    return len(ordered) - 1


def _last_segment_index_for_window(ordered, start_idx: int, target_end_ms: int) -> int:
    end_idx = start_idx
    while end_idx + 1 < len(ordered) and int(getattr(ordered[end_idx + 1], "start_ms", 0)) < target_end_ms:
        end_idx += 1
    while end_idx + 1 < len(ordered) and max(
        int(getattr(ordered[end_idx], "end_ms", 0)),
        int(getattr(ordered[end_idx], "start_ms", 0)),
    ) < target_end_ms:
        end_idx += 1
    return end_idx


def _append_window_if_new(ordered, start_idx: int, end_idx: int, next_window_index: int, seen_ranges, drafts) -> int:
    start_segment = ordered[start_idx]
    end_segment = ordered[end_idx]
    start_position = int(getattr(start_segment, "position", 0))
    end_position = int(getattr(end_segment, "position", start_position))
    range_key = (start_position, end_position)

    if range_key in seen_ranges:
        return next_window_index

    drafts.append(
        TranscriptWindowDraft(
            window_index=next_window_index,
            start_position=start_position,
            end_position=end_position,
            start_ms=max(0, int(getattr(start_segment, "start_ms", 0))),
            end_ms=max(
                int(getattr(end_segment, "end_ms", getattr(end_segment, "start_ms", 0))),
                int(getattr(start_segment, "start_ms", 0)),
            ),
            text=" ".join(getattr(segment, "text", "").strip() for segment in ordered[start_idx : end_idx + 1]).strip(),
        )
    )
    seen_ranges.add(range_key)
    return next_window_index + 1
