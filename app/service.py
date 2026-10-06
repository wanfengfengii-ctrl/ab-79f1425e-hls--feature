"""Request validation and orchestration for the normalize endpoint."""
from __future__ import annotations

from .errors import ApiError
from .normalize import SegmentInput, normalize_segments
from .webvtt import RegionDefinition, parse_segment

MAX_SEGMENTS = 64
MAX_PAYLOAD_BYTES = 1 << 20  # 1 MiB of segment text per request
MAX_INT64 = (1 << 63) - 1

REGION_POLICY_RESOLVE = "resolve"


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

    resolve_regions = _parse_region_policy(body.get("regionPolicy"))

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
            parsed = parse_segment(content, resolve_regions=resolve_regions)
        except ApiError as exc:
            raise exc.with_segment(sequence)
        segments.append(SegmentInput(sequence=sequence, parsed=parsed))

    if resolve_regions:
        regions = _resolve_regions(segments)
    else:
        regions = []

    cues = normalize_segments(anchor, max_interval, segments)
    result: dict = {
        "cues": [
            {
                "segment": cue.segment,
                "index": cue.index,
                "startTicks": cue.start_ticks,
                "endTicks": cue.end_ticks,
                "text": cue.text,
                **({"regionId": cue.region_id} if resolve_regions else {}),
            }
            for cue in cues
        ]
    }
    if resolve_regions:
        result["regions"] = [_region_payload(region) for region in regions]
    return result


def _parse_region_policy(value: object) -> bool:
    """``regionPolicy`` is optional; when present it must be ``"resolve"``."""
    if value is None:
        return False
    if not isinstance(value, str) or value != REGION_POLICY_RESOLVE:
        raise ApiError(
            "INVALID_REQUEST",
            f"regionPolicy must be {REGION_POLICY_RESOLVE!r} when provided",
        )
    return True


def _resolve_regions(segments: list[SegmentInput]) -> list[RegionDefinition]:
    """Collect REGION declarations across segments and validate references.

    Declarations with the same id and the same definition are merged; a
    repeated id with a different definition is a conflict.  The returned
    list preserves first-declaration order (segment sequence, then
    in-segment order).  Every check runs before any response is built, so
    a failure never yields a partially normalized result.
    """
    regions: list[RegionDefinition] = []
    by_id: dict[str, RegionDefinition] = {}
    for seg in segments:
        seen_in_segment: set[str] = set()
        for region in seg.parsed.regions:
            if region.id in seen_in_segment:
                raise ApiError(
                    "REGION_DUPLICATE_ID",
                    f"region {region.id!r} is declared more than once in segment {seg.sequence}",
                    segment=seg.sequence,
                )
            seen_in_segment.add(region.id)
            existing = by_id.get(region.id)
            if existing is None:
                by_id[region.id] = region
                regions.append(region)
            elif region != existing:
                raise ApiError(
                    "REGION_CONFLICT",
                    f"region {region.id!r} in segment {seg.sequence} conflicts with "
                    f"its earlier declaration",
                    segment=seg.sequence,
                )

    for seg in segments:
        for cue in seg.parsed.cues:
            if cue.region_id is not None and cue.region_id not in by_id:
                raise ApiError(
                    "REGION_REFERENCE_UNKNOWN",
                    f"cue references unknown region {cue.region_id!r}",
                    segment=seg.sequence,
                )
    return regions


def _region_payload(region: RegionDefinition) -> dict:
    return {
        "id": region.id,
        "width": region.width,
        "lines": region.lines,
        "regionAnchor": {"x": region.region_anchor[0], "y": region.region_anchor[1]},
        "viewportAnchor": {"x": region.viewport_anchor[0], "y": region.viewport_anchor[1]},
        "scroll": region.scroll,  # None unless "up"
    }
