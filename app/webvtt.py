"""Strict WebVTT parsing for HLS subtitle segments.

Every HLS subtitle segment is a WebVTT file whose header block carries
exactly one ``X-TIMESTAMP-MAP`` line binding local cue times to 33-bit
MPEG-TS timestamps on the 90 kHz clock::

    WEBVTT
    X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000

    00:00:01.000 --> 00:00:04.000
    Hello world

When region resolution is requested (``regionPolicy=resolve``), REGION
blocks in the body are parsed and validated and cue ``region`` settings
are captured as references.  Region parsing is opt-in: without it the
parser behaves exactly as before and REGION blocks are ignored.
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
_CUE_TIMING_RE = re.compile(r"^(\S+)[ \t]+-->[ \t]+(\S+)(?:[ \t]+(.*?))?$")
_SETTING_RE = re.compile(r"^([a-z]+):(.*)$")
_PERCENT_RE = re.compile(r"^([0-9]{1,3})%$")
_ANCHOR_RE = re.compile(r"^([0-9]{1,3})%[ \t]*,[ \t]*([0-9]{1,3})%$")

# Region defaults mandated by the WebVTT specification when a setting is
# omitted from a REGION block.
DEFAULT_REGION_WIDTH = 100
DEFAULT_REGION_LINES = 3
DEFAULT_REGION_ANCHOR = (0, 100)
DEFAULT_VIEWPORT_ANCHOR = (0, 100)

_REGION_SETTING_NAMES = frozenset(
    {"id", "width", "lines", "regionanchor", "viewportanchor", "scroll"}
)


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


@dataclass(frozen=True)
class RegionDefinition:
    """A canonicalised REGION declaration (defaults already resolved)."""

    id: str
    width: int = DEFAULT_REGION_WIDTH
    lines: int = DEFAULT_REGION_LINES
    region_anchor: tuple[int, int] = DEFAULT_REGION_ANCHOR
    viewport_anchor: tuple[int, int] = DEFAULT_VIEWPORT_ANCHOR
    scroll: str | None = None  # None (no scroll) or "up"


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str
    region_id: str | None = None  # cue ``region:`` reference, when resolved


@dataclass
class ParsedSegment:
    local_map_ms: int   # LOCAL side of X-TIMESTAMP-MAP, in milliseconds
    mpegts: int         # MPEGTS side of X-TIMESTAMP-MAP, 33-bit ticks
    cues: list[Cue] = field(default_factory=list)
    regions: list[RegionDefinition] = field(default_factory=list)


def parse_segment(content: str, *, resolve_regions: bool = False) -> ParsedSegment:
    """Parse one WebVTT segment, enforcing the HLS segment invariants."""
    if content.startswith("﻿"):  # strip one optional UTF-8 BOM
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

    if resolve_regions:
        for line in header_lines:
            if _is_region_signature(line):
                raise ApiError(
                    "REGION_BLOCK_INVALID",
                    "REGION blocks belong in the body, not the header block",
                )
        cues, regions = _parse_body_with_regions(lines[body_start:])
    else:
        # Legacy path: region blocks are not interpreted at all, preserving
        # the original semantics (a bare "REGION"/"STYLE" block is ignored;
        # any other block is parsed as a cue).
        cues = _parse_cues(lines[body_start:])
        regions = []

    return ParsedSegment(
        local_map_ms=local_map_ms,
        mpegts=mpegts,
        cues=cues,
        regions=regions,
    )


# ---------------------------------------------------------------- body blocks

def _parse_body_with_regions(body_lines: list[str]) -> tuple[list[Cue], list[RegionDefinition]]:
    cues: list[Cue] = []
    regions: list[RegionDefinition] = []
    block: list[str] = []
    for line in body_lines + [""]:  # sentinel flushes the final block
        if line == "":
            if block:
                if _is_note(block[0]):
                    pass  # comment block
                elif _is_style(block[0]):
                    pass  # style blocks are not part of the normalized output
                elif _is_region_signature(block[0]):
                    regions.append(_parse_region_block(block))
                else:
                    cues.append(_parse_cue_block(block))
                block = []
        else:
            block.append(line)
    return cues, regions


def _is_note(line: str) -> bool:
    return line == "NOTE" or line.startswith("NOTE ") or line.startswith("NOTE\t")


def _is_style(line: str) -> bool:
    return line == "STYLE" or line.startswith("STYLE ") or line.startswith("STYLE\t")


def _is_region_signature(line: str) -> bool:
    return line == "REGION" or line.startswith("REGION ") or line.startswith("REGION\t")


# ---------------------------------------------------------------- REGION blocks

def _valid_region_id(region_id: str) -> bool:
    # Region ids are single whitespace-delimited tokens here; WebVTT only
    # forbids the empty string, line breaks and the cue separator "-->".
    return bool(region_id) and "-->" not in region_id


def _parse_percent(value: str, *, what: str) -> int:
    match = _PERCENT_RE.match(value)
    if match is None:
        raise ApiError("REGION_FIELD_INVALID", f"{what} must be an integer percentage, got {value!r}")
    percent = int(match.group(1))
    if percent > 100:
        raise ApiError("REGION_FIELD_INVALID", f"{what} percentage must be between 0 and 100")
    return percent


def _parse_anchor(value: str, *, what: str) -> tuple[int, int]:
    match = _ANCHOR_RE.match(value)
    if match is None:
        raise ApiError(
            "REGION_FIELD_INVALID",
            f"{what} must be two percentages as 'x%,y%', got {value!r}",
        )
    x, y = int(match.group(1)), int(match.group(2))
    if x > 100 or y > 100:
        raise ApiError("REGION_FIELD_INVALID", f"{what} percentages must be between 0 and 100")
    return x, y


def _parse_region_block(block: list[str]) -> RegionDefinition:
    """Parse one REGION block into a canonicalised definition.

    Settings may sit on the ``REGION`` line itself (whitespace separated)
    or one per following line.  Unknown fields, malformed values and a
    setting repeated inside the same block are rejected.
    """
    raw_settings: list[str] = block[0][len("REGION"):].split()
    for line in block[1:]:
        if re.search(r"\s", line):
            raise ApiError(
                "REGION_BLOCK_INVALID",
                f"region setting lines hold a single 'name:value' token: {line!r}",
            )
        raw_settings.append(line)

    values: dict[str, str] = {}
    for raw in raw_settings:
        match = _SETTING_RE.match(raw)
        if match is None or not match.group(2):
            raise ApiError("REGION_FIELD_INVALID", f"malformed region setting: {raw!r}")
        name, value = match.group(1), match.group(2)
        if name not in _REGION_SETTING_NAMES:
            raise ApiError("REGION_FIELD_INVALID", f"unknown region setting {name!r}")
        if name in values:
            raise ApiError(
                "REGION_SETTING_DUPLICATE",
                f"region setting {name!r} is given more than once",
            )
        values[name] = value

    region_id = values.get("id")
    if region_id is None:
        raise ApiError("REGION_FIELD_INVALID", "REGION block is missing the required 'id' setting")
    if not _valid_region_id(region_id):
        raise ApiError("REGION_FIELD_INVALID", f"invalid region id: {region_id!r}")

    width = (
        _parse_percent(values["width"], what="region width") if "width" in values
        else DEFAULT_REGION_WIDTH
    )
    if "lines" in values:
        if not re.fullmatch(r"[0-9]+", values["lines"]):
            raise ApiError("REGION_FIELD_INVALID", f"region lines must be an integer, got {values['lines']!r}")
        lines = int(values["lines"])
        if lines < 1:
            raise ApiError("REGION_FIELD_INVALID", "region lines must be a positive integer")
    else:
        lines = DEFAULT_REGION_LINES
    region_anchor = (
        _parse_anchor(values["regionanchor"], what="regionanchor")
        if "regionanchor" in values
        else DEFAULT_REGION_ANCHOR
    )
    viewport_anchor = (
        _parse_anchor(values["viewportanchor"], what="viewportanchor")
        if "viewportanchor" in values
        else DEFAULT_VIEWPORT_ANCHOR
    )
    if "scroll" in values:
        if values["scroll"] != "up":
            raise ApiError("REGION_FIELD_INVALID", f"region scroll must be 'up', got {values['scroll']!r}")
        scroll = "up"
    else:
        scroll = None

    return RegionDefinition(
        id=region_id,
        width=width,
        lines=lines,
        region_anchor=region_anchor,
        viewport_anchor=viewport_anchor,
        scroll=scroll,
    )


# ---------------------------------------------------------------- cue blocks

def _parse_cues(body_lines: list[str]) -> list[Cue]:
    """Legacy body parsing: bare NOTE/STYLE/REGION blocks are skipped, every
    other block must be a cue.  Region references are not interpreted."""
    cues: list[Cue] = []
    block: list[str] = []
    for line in body_lines + [""]:  # sentinel flushes the final block
        if line == "":
            if block:
                cue = _parse_legacy_block(block)
                if cue is not None:
                    cues.append(cue)
                block = []
        else:
            block.append(line)
    return cues


def _parse_legacy_block(block: list[str]) -> Cue | None:
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


def _parse_cue_block(block: list[str]) -> Cue:
    first = block[0]
    if "-->" in first:
        timing_index = 0
    elif len(block) > 1 and "-->" in block[1]:
        timing_index = 1  # cue identifier line precedes the timing line
    else:
        raise ApiError("CUE_TIMING_INVALID", "cue block is missing a '-->' timing line")

    timing_line = block[timing_index]
    match = _CUE_TIMING_RE.match(timing_line)
    if match is None:
        raise ApiError("CUE_TIMING_INVALID", f"malformed cue timing line: {timing_line!r}")
    start_ms = parse_timestamp_ms(match.group(1))
    end_ms = parse_timestamp_ms(match.group(2))
    if end_ms <= start_ms:
        raise ApiError("CUE_INTERVAL_INVALID", "cue end time must be greater than its start time")

    region_id: str | None = None
    if match.group(3):
        region_id = _parse_cue_settings(match.group(3))

    return Cue(
        start_ms=start_ms,
        end_ms=end_ms,
        text="\n".join(block[timing_index + 1:]),
        region_id=region_id,
    )


def _parse_cue_settings(settings_text: str) -> str | None:
    """Validate the cue settings trailing the timing line.

    Only the ``region`` reference is captured; other known settings are
    accepted and ignored, as unknown settings are per the WebVTT rules.
    """
    region_id: str | None = None
    for token in settings_text.split():
        match = _SETTING_RE.match(token)
        name = match.group(1) if match is not None else None
        if name == "region":
            value = match.group(2)
            if region_id is not None:
                raise ApiError(
                    "REGION_SETTING_DUPLICATE",
                    "cue carries more than one 'region' setting",
                )
            if not value or not _valid_region_id(value):
                raise ApiError("REGION_FIELD_INVALID", f"invalid cue region reference: {token!r}")
            region_id = value
            continue
        if match is None or not match.group(2):
            raise ApiError("CUE_TIMING_INVALID", f"malformed cue setting: {token!r}")
        # Other known settings are accepted and ignored, as are unknown
        # settings per the WebVTT rules for consumers.
    return region_id
