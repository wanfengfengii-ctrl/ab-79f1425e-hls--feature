"""Unwrap 33-bit MPEG-TS timestamps onto an absolute 90 kHz timeline.

Each segment's ``X-TIMESTAMP-MAP`` binds its LOCAL reference time to a
33-bit MPEGTS value.  The absolute position of segment 0's map point is
given by the request anchor; every later segment's map point is placed at
the unique value ``MPEGTS + k * 2**33`` (``k >= 0``) that lies within
``maxAnchorIntervalTicks`` of the previous segment's map point.  When no
such ``k`` exists the anchors are incompatible; when more than one exists
the unwrap is not unique.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ApiError
from .webvtt import MPEGTS_MODULUS, ParsedSegment

TICKS_PER_MS = 90  # 90 000 ticks per second on the MPEG-TS clock


@dataclass
class SegmentInput:
    sequence: int
    parsed: ParsedSegment


@dataclass
class NormalizedCue:
    segment: int
    index: int
    start_ticks: int
    end_ticks: int
    text: str


def unwrap_map_positions(
    anchor_ticks: int,
    max_interval_ticks: int,
    segments: list[SegmentInput],
) -> list[int]:
    """Return the absolute 90 kHz tick of each segment's map point."""
    first = segments[0]
    if anchor_ticks % MPEGTS_MODULUS != first.parsed.mpegts:
        raise ApiError(
            "ANCHOR_INCOMPATIBLE",
            f"anchor {anchor_ticks} is incompatible with the first segment's "
            f"MPEGTS {first.parsed.mpegts} (mod {MPEGTS_MODULUS})",
            segment=first.sequence,
        )

    positions = [anchor_ticks]
    for seg in segments[1:]:
        prev = positions[-1]
        lo = prev - max_interval_ticks
        hi = prev + max_interval_ticks
        m = seg.parsed.mpegts
        # Candidates are m + k*MOD with lo <= candidate <= hi and candidate >= 0.
        k_min = -((m - lo) // MPEGTS_MODULUS)  # ceil((lo - m) / MOD)
        k_max = (hi - m) // MPEGTS_MODULUS     # floor((hi - m) / MOD)
        k_min = max(k_min, 0)                  # absolute ticks never go negative
        if k_max < k_min:
            raise ApiError(
                "ANCHOR_INCOMPATIBLE",
                f"no unwrapped position for MPEGTS {m} within "
                f"{max_interval_ticks} ticks of previous map point {prev}",
                segment=seg.sequence,
            )
        if k_max > k_min:
            raise ApiError(
                "UNWRAP_NOT_UNIQUE",
                f"multiple unwrapped positions for MPEGTS {m} within "
                f"{max_interval_ticks} ticks of previous map point {prev}",
                segment=seg.sequence,
            )
        positions.append(m + k_min * MPEGTS_MODULUS)
    return positions


def normalize_segments(
    anchor_ticks: int,
    max_interval_ticks: int,
    segments: list[SegmentInput],
) -> list[NormalizedCue]:
    """Project every cue onto the absolute timeline and order the result by
    (absolute start, segment sequence, in-segment order)."""
    positions = unwrap_map_positions(anchor_ticks, max_interval_ticks, segments)
    cues: list[NormalizedCue] = []
    for seg, position in zip(segments, positions):
        base = position - seg.parsed.local_map_ms * TICKS_PER_MS
        for index, cue in enumerate(seg.parsed.cues):
            cues.append(
                NormalizedCue(
                    segment=seg.sequence,
                    index=index,
                    start_ticks=base + cue.start_ms * TICKS_PER_MS,
                    end_ticks=base + cue.end_ms * TICKS_PER_MS,
                    text=cue.text,
                )
            )
    cues.sort(key=lambda c: (c.start_ticks, c.segment, c.index))
    return cues
