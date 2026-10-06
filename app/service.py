"""Request validation and orchestration for the normalize endpoint."""
from __future__ import annotations

from .errors import ApiError
from .normalize import SegmentInput, normalize_segments
from .webvtt import parse_segment

MAX_SEGMENTS = 64
MAX_PAYLOAD_BYTES = 1 << 20  # 1 MiB of segment text per request
MAX_INT64 = (1 << 63) - 1


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError("INVALID_REQUEST", f"{name} must be an integer")
    if not 0 <= value <= MAX_INT64:
        raise ApiError("INVALID_REQUEST", f"{name} must be between 0 and {MAX_INT64}")
    return value


def normalize_request(body: object) -> dict:
    if not isinstance(body, dict):
        raise ApiError("INVALID_REQUEST", "request body must be a JSON object")

    anchor = _require_int(body.get("anchorTicks"), "anchorTicks")
    max_interval = _require_int(body.get("maxAnchorIntervalTicks"), "maxAnchorIntervalTicks")

    raw_segments = body.get("segments")
    if not isinstance(raw_segments, list):
        raise ApiError("INVALID_REQUEST", "segments must be an array")
    if not 1 <= len(raw_segments) <= MAX_SEGMENTS:
        raise ApiError(
            "SEGMENT_COUNT_OUT_OF_RANGE",
            f"expected 1..{MAX_SEGMENTS} segments, got {len(raw_segments)}",
        )

    items: list[tuple[int, str]] = []
    total_bytes = 0
    for position, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise ApiError("INVALID_REQUEST", f"segments[{position}] must be an object")
        sequence = _require_int(raw.get("sequence"), f"segments[{position}].sequence")
        content = raw.get("content")
        if not isinstance(content, str):
            raise ApiError(
                "INVALID_REQUEST",
                f"segments[{position}].content must be a string",
                segment=sequence,
            )
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise ApiError(
                "INVALID_REQUEST",
                "segment content must be valid UTF-8 text",
                segment=sequence,
            )
        total_bytes += len(encoded)
        items.append((sequence, content))

    items.sort(key=lambda item: item[0])
    for (prev_seq, _), (seq, _) in zip(items, items[1:]):
        if seq != prev_seq + 1:
            raise ApiError(
                "SEGMENTS_NOT_CONSECUTIVE",
                f"segment sequence {seq} does not follow {prev_seq}",
                segment=seq,
            )

    if total_bytes > MAX_PAYLOAD_BYTES:
        raise ApiError(
            "PAYLOAD_TOO_LARGE",
            f"segment text totals {total_bytes} bytes, limit is {MAX_PAYLOAD_BYTES}",
        )

    segments: list[SegmentInput] = []
    for sequence, content in items:
        try:
            parsed = parse_segment(content)
        except ApiError as exc:
            raise exc.with_segment(sequence)
        segments.append(SegmentInput(sequence=sequence, parsed=parsed))

    cues = normalize_segments(anchor, max_interval, segments)
    return {
        "cues": [
            {
                "segment": cue.segment,
                "index": cue.index,
                "startTicks": cue.start_ticks,
                "endTicks": cue.end_ticks,
                "text": cue.text,
            }
            for cue in cues
        ]
    }
