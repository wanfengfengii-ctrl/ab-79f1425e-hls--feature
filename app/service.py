"""Request validation and orchestration for the normalize endpoint."""
from __future__ import annotations

from .errors import ApiError
from .normalize import SegmentInput, normalize_segments
from .webvtt import RegionDefinition, parse_segment

MAX_SEGMENTS = 64
MAX_PAYLOAD_BYTES = 1 << 20  # 1 MiB of segment text per request
MAX_INT64 = (1 << 63) - 1

REGION_POLICIES = frozenset({"resolve"})


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError("INVALID_REQUEST", f"{name} must be an integer")
    if not 0 <= value <= MAX_INT64:
        raise ApiError("INVALID_REQUEST", f"{name} must be between 0 and {MAX_INT64}")
    return value


def normalize_request(body: object) -> dict:
    if not isinstance(body, dict):
        raise ApiError("INVALID_REQUEST", "request body must be a JSON object")

    resolve_regions = False
    if "regionPolicy" in body:
        policy = body["regionPolicy"]
        if not isinstance(policy, str) or policy not in REGION_POLICIES:
            raise ApiError(
                "INVALID_REQUEST",
                f"regionPolicy must be one of {sorted(REGION_POLICIES)!r}",
            )
        resolve_regions = policy == "resolve"

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
            parsed = parse_segment(content, resolve_regions=resolve_regions)
        except ApiError as exc:
            raise exc.with_segment(sequence)
        segments.append(SegmentInput(sequence=sequence, parsed=parsed))

    regions: list[RegionDefinition] = []
    if resolve_regions:
        regions = _merge_regions(segments)
        known = {region.id for region in regions}
        for seg in segments:
            for cue in seg.parsed.cues:
                if cue.region_id is not None and cue.region_id not in known:
                    raise ApiError(
                        "REGION_REFERENCE_UNKNOWN",
                        f"cue references undeclared region {cue.region_id!r}",
                        segment=seg.sequence,
                    )

    cues = normalize_segments(anchor, max_interval, segments)

    response_cues = [
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
    response = {"cues": response_cues}

    if resolve_regions:
        response = {"regions": [_region_to_json(region) for region in regions], "cues": response_cues}

    return response


def _merge_regions(segments: list[SegmentInput]) -> list[RegionDefinition]:
    """Merge REGION declarations across segments.

    Declarations carrying the same id and the same definition are merged;
    regions are returned in first-declaration order (segment sequence, then
    in-segment order).  A re-declared id with a differing definition is a
    conflict attributed to the segment that re-declares it.
    """
    merged: dict[str, RegionDefinition] = {}
    order: list[str] = []
    for seg in segments:
        for region in seg.parsed.regions:
            existing = merged.get(region.id)
            if existing is None:
                merged[region.id] = region
                order.append(region.id)
            elif existing != region:
                raise ApiError(
                    "REGION_CONFLICT",
                    f"region {region.id!r} is re-declared with a different definition",
                    segment=seg.sequence,
                )
    return [merged[region_id] for region_id in order]


def _region_to_json(region: RegionDefinition) -> dict:
    data: dict = {"id": region.id}
    if region.width is not None:
        data["width"] = f"{region.width}%"
    if region.lines is not None:
        data["lines"] = region.lines
    if region.region_anchor is not None:
        data["regionanchor"] = f"{region.region_anchor.x_percent}%,{region.region_anchor.y_percent}%"
    if region.viewport_anchor is not None:
        data["viewportanchor"] = (
            f"{region.viewport_anchor.x_percent}%,{region.viewport_anchor.y_percent}%"
        )
    if region.scroll is not None:
        data["scroll"] = region.scroll
    return data
