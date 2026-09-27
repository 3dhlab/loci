from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.entities import TranscriptFormat

TIMECODE_PATTERN = re.compile(r"\s*([^\s]+)\s*-->\s*([^\s]+)")
PLAIN_RANGE_PATTERN = re.compile(r"\s*([0-9:.,]+)\s*(?:-->|-|–|—)\s*([0-9:.,]+)\s*")
TAG_PATTERN = re.compile(r"<[^>]+>")


@dataclass
class ParsedSegment:
    position: int
    start_ms: int
    end_ms: int
    text: str


def parse_transcript(raw_text: str, transcript_format: TranscriptFormat) -> list[ParsedSegment]:
    if not raw_text.strip():
        raise ValueError("Transcript text cannot be empty")

    if transcript_format == TranscriptFormat.VTT:
        segments = _parse_timed_blocks(raw_text, time_delimiter=".")
    elif transcript_format == TranscriptFormat.SRT:
        segments = _parse_timed_blocks(raw_text, time_delimiter=",")
    else:
        segments = _parse_plain_blocks(raw_text)

    if not segments:
        raise ValueError("No transcript segments could be parsed")

    return segments


def _parse_timed_blocks(raw_text: str, time_delimiter: str) -> list[ParsedSegment]:
    cleaned = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block.strip() for block in re.split(r"\n\s*\n", cleaned) if block.strip()]

    parsed: list[ParsedSegment] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        if lines[0].upper().startswith("WEBVTT"):
            continue

        if len(lines) >= 2 and lines[0].isdigit() and "-->" in lines[1]:
            timing_line = lines[1]
            text_lines = lines[2:]
        elif "-->" in lines[0]:
            timing_line = lines[0]
            text_lines = lines[1:]
        else:
            continue

        match = TIMECODE_PATTERN.match(timing_line)
        if not match:
            continue

        start_ms = _time_to_ms(match.group(1), time_delimiter=time_delimiter)
        end_ms = _time_to_ms(match.group(2), time_delimiter=time_delimiter)
        if end_ms < start_ms:
            continue

        text = _clean_segment_text(" ".join(text_lines))
        if not text:
            continue

        parsed.append(
            ParsedSegment(
                position=len(parsed),
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
        )

    return parsed


def _parse_plain_blocks(raw_text: str) -> list[ParsedSegment]:
    cleaned = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [block.strip() for block in re.split(r"\n\s*\n", cleaned) if block.strip()]
    if not blocks:
        blocks = [cleaned]

    timed_segments: list[ParsedSegment] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 2:
            continue

        match = PLAIN_RANGE_PATTERN.match(lines[0])
        if not match:
            continue

        try:
            start_ms = _flex_time_to_ms(match.group(1))
            end_ms = _flex_time_to_ms(match.group(2))
        except ValueError:
            continue

        if end_ms <= start_ms:
            continue

        text = _clean_segment_text(" ".join(lines[1:]))
        if not text:
            continue

        timed_segments.append(
            ParsedSegment(
                position=len(timed_segments),
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
        )

    if timed_segments:
        return timed_segments

    parsed: list[ParsedSegment] = []
    cursor_ms = 0
    for block in blocks:
        text = _clean_segment_text(block)
        if not text:
            continue

        duration_ms = max(1000, min(15000, len(text) * 45))
        parsed.append(
            ParsedSegment(
                position=len(parsed),
                start_ms=cursor_ms,
                end_ms=cursor_ms + duration_ms,
                text=text,
            )
        )
        cursor_ms += duration_ms

    return parsed


def _clean_segment_text(text: str) -> str:
    no_tags = TAG_PATTERN.sub("", text)
    collapsed = re.sub(r"\s+", " ", no_tags)
    return collapsed.strip()


def _time_to_ms(value: str, time_delimiter: str) -> int:
    normalized = value.split(" ")[0]

    if time_delimiter == ",":
        normalized = normalized.replace(",", ".")

    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes = int(parts[0])
        seconds = float(parts[1])
    elif len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    else:
        raise ValueError(f"Invalid timecode: {value}")

    total_ms = int(((hours * 3600) + (minutes * 60) + seconds) * 1000)
    return max(total_ms, 0)


def _flex_time_to_ms(value: str) -> int:
    normalized = value.strip().replace(",", ".")

    # Supports HH:MM:SS.mmm, MM:SS.mmm, and HH:MM:SS:ff style timecodes.
    if normalized.count(":") == 3 and "." not in normalized:
        hours_s, minutes_s, seconds_s, fraction_s = normalized.split(":", 3)
        hours = int(hours_s)
        minutes = int(minutes_s)
        seconds = int(seconds_s)
        if len(fraction_s) >= 3:
            millis = int(fraction_s[:3])
        else:
            # Two-digit tail is commonly frame/centisecond-like; map to 10ms precision.
            millis = int(fraction_s) * 10
        total_ms = ((hours * 3600) + (minutes * 60) + seconds) * 1000 + millis
        return max(total_ms, 0)

    return _time_to_ms(normalized, time_delimiter=".")
