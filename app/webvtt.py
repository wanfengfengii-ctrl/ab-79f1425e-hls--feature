"""Strict WebVTT parsing for HLS subtitle segments.

Every HLS subtitle segment is a WebVTT file whose header block carries
exactly one ``X-TIMESTAMP-MAP`` line binding local cue times to 33-bit
MPEG-TS timestamps on the 90 kHz clock::

    WEBVTT
    X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000

    00:00:01.000 --> 00:00:04.000
    Hello world
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ApiError

MPEGTS_MODULUS = 1 << 33          # 33-bit MPEG-TS timestamp space
MPEGTS_MAX = MPEGTS_MODULUS - 1   # largest representable MPEGTS value

# mm:ss.mmm or hh:mm:ss.mmm -- exactly three fractional (millisecond) digits.
_TIMESTAMP_RE = re.compile(r"^(?:([0-9]{2,}):)?([0-5][0-9]):([0-5][0-9])\.([0-9]{3})$")
_TIMESTAMP_MAP_RE = re.compile(r"^X-TIMESTAMP-MAP=LOCAL:([^,\s]+),MPEGTS:([0-9]+)$")
_CUE_TIMING_RE = re.compile(r"^(\S+)[ \t]+-->[ \t]+(\S+)(?:[ \t]+.*)?$")


def parse_timestamp_ms(text: str) -> int:
    """Parse a WebVTT timestamp into milliseconds, rejecting anything that
    is not a well-formed millisecond-precision timestamp."""
    match = _TIMESTAMP_RE.match(text)
    if match is None:
        raise ApiError("TIMESTAMP_INVALID", f"malformed WebVTT timestamp: {text!r}")
    hours, minutes, seconds, millis = match.groups()
    total = int(minutes) * 60_000 + int(seconds) * 1_000 + int(millis)
    if hours is not None:
        total += int(hours) * 3_600_000
    return total


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class ParsedSegment:
    local_map_ms: int   # LOCAL side of X-TIMESTAMP-MAP, in milliseconds
    mpegts: int         # MPEGTS side of X-TIMESTAMP-MAP, 33-bit ticks
    cues: list[Cue] = field(default_factory=list)


def parse_segment(content: str) -> ParsedSegment:
    """Parse one WebVTT segment, enforcing the HLS segment invariants."""
    if content.startswith("\ufeff"):  # strip one optional UTF-8 BOM
        content = content[1:]
    lines = re.split(r"\r\n|\r|\n", content)

    first = lines[0] if lines else ""
    if not (first == "WEBVTT" or first.startswith("WEBVTT ") or first.startswith("WEBVTT\t")):
        raise ApiError("WEBVTT_HEADER_INVALID", "segment must start with a WEBVTT header line")

    # Header block: the lines between WEBVTT and the first blank line.
    header_lines: list[str] = []
    body_start = len(lines)
    for i in range(1, len(lines)):
        if lines[i] == "":
            body_start = i + 1
            break
        header_lines.append(lines[i])

    map_lines = [line for line in lines if line.startswith("X-TIMESTAMP-MAP")]
    if not map_lines:
        raise ApiError("TIMESTAMP_MAP_MISSING", "segment has no X-TIMESTAMP-MAP header")
    if len(map_lines) > 1:
        raise ApiError("TIMESTAMP_MAP_DUPLICATE", "segment has more than one X-TIMESTAMP-MAP line")
    if not any(line.startswith("X-TIMESTAMP-MAP") for line in header_lines):
        raise ApiError(
            "TIMESTAMP_MAP_INVALID",
            "X-TIMESTAMP-MAP must appear in the header block before the first blank line",
        )

    match = _TIMESTAMP_MAP_RE.match(map_lines[0])
    if match is None:
        raise ApiError("TIMESTAMP_MAP_INVALID", f"malformed X-TIMESTAMP-MAP line: {map_lines[0]!r}")
    local_map_ms = parse_timestamp_ms(match.group(1))
    mpegts = int(match.group(2))
    if mpegts > MPEGTS_MAX:
        raise ApiError(
            "MPEGTS_OUT_OF_RANGE",
            f"MPEGTS value {mpegts} exceeds the 33-bit maximum {MPEGTS_MAX}",
        )

    return ParsedSegment(local_map_ms=local_map_ms, mpegts=mpegts, cues=_parse_cues(lines[body_start:]))


def _parse_cues(body_lines: list[str]) -> list[Cue]:
    cues: list[Cue] = []
    block: list[str] = []
    for line in body_lines + [""]:  # sentinel flushes the final block
        if line == "":
            if block:
                cue = _parse_block(block)
                if cue is not None:
                    cues.append(cue)
                block = []
        else:
            block.append(line)
    return cues


def _parse_block(block: list[str]) -> Cue | None:
    first = block[0]
    if first == "NOTE" or first.startswith("NOTE ") or first.startswith("NOTE\t"):
        return None  # comment block
    if first == "STYLE" or first == "REGION":
        return None  # not expected in HLS segments; ignored

    if "-->" in first:
        timing_index = 0
    elif len(block) > 1 and "-->" in block[1]:
        timing_index = 1  # cue identifier line precedes the timing line
    else:
        raise ApiError("CUE_TIMING_INVALID", "cue block is missing a '-->' timing line")

    match = _CUE_TIMING_RE.match(block[timing_index])
    if match is None:
        raise ApiError("CUE_TIMING_INVALID", f"malformed cue timing line: {block[timing_index]!r}")
    start_ms = parse_timestamp_ms(match.group(1))
    end_ms = parse_timestamp_ms(match.group(2))
    if end_ms <= start_ms:
        raise ApiError("CUE_INTERVAL_INVALID", "cue end time must be greater than its start time")

    return Cue(start_ms=start_ms, end_ms=end_ms, text="\n".join(block[timing_index + 1:]))
