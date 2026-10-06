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

import dataclasses
import re
from dataclasses import dataclass, field

from .errors import ApiError

MPEGTS_MODULUS = 1 << 33          # 33-bit MPEG-TS timestamp space
MPEGTS_MAX = MPEGTS_MODULUS - 1   # largest representable MPEGTS value

# mm:ss.mmm or hh:mm:ss.mmm -- exactly three fractional (millisecond) digits.
_TIMESTAMP_RE = re.compile(r"^(?:([0-9]{2,}):)?([0-5][0-9]):([0-5][0-9])\.([0-9]{3})$")
_TIMESTAMP_MAP_RE = re.compile(r"^X-TIMESTAMP-MAP=LOCAL:([^,\s]+),MPEGTS:([0-9]+)$")
_CUE_TIMING_RE = re.compile(r"^(\S+)[ \t]+-->[ \t]+(\S+)(?:[ \t]+(.*))?$")


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
class RegionAnchor:
    """A regionanchor/viewportanchor point in viewport percentages."""

    x_percent: int
    y_percent: int


@dataclass(frozen=True)
class RegionDefinition:
    """The six REGION settings; optional fields are ``None`` when absent."""

    id: str
    width: int | None = None             # percent of the viewport width
    lines: int | None = None
    region_anchor: RegionAnchor | None = None
    viewport_anchor: RegionAnchor | None = None
    scroll: str | None = None            # "up" when scrolling is enabled


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str
    region_id: str | None = None  # cue "region:<id>" setting, when resolved


@dataclass
class ParsedSegment:
    local_map_ms: int   # LOCAL side of X-TIMESTAMP-MAP, in milliseconds
    mpegts: int         # MPEGTS side of X-TIMESTAMP-MAP, 33-bit ticks
    cues: list[Cue] = field(default_factory=list)
    regions: list[RegionDefinition] = field(default_factory=list)


def parse_segment(content: str, *, resolve_regions: bool = False) -> ParsedSegment:
    """Parse one WebVTT segment, enforcing the HLS segment invariants.

    When ``resolve_regions`` is set, REGION blocks are parsed/validated and
    cue ``region:`` references are captured; otherwise they are ignored and
    the result is identical to a region-unaware parse.
    """
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

    regions: list[RegionDefinition] = []
    if resolve_regions:
        regions = _parse_regions(lines[body_start:])

    return ParsedSegment(
        local_map_ms=local_map_ms,
        mpegts=mpegts,
        cues=_parse_cues(lines[body_start:], resolve_regions),
        regions=regions,
    )


# ---------------------------------------------------------------- regions

# REGION ids and cue settings share WebVTT's "no newline, no control char"
# rule; we additionally forbid whitespace so settings lists stay parseable.
_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f]+$")
_SCROLL_VALUES = frozenset({"up"})


def _parse_percent(text: str) -> int:
    """Parse a ``NN%`` percentage into an int in 0..100."""
    if not text.endswith("%"):
        raise ApiError("REGION_SETTING_INVALID", f"percentage must end with '%': {text!r}")
    number = text[:-1]
    if not number or not number.isdigit():
        raise ApiError("REGION_SETTING_INVALID", f"malformed percentage: {text!r}")
    value = int(number)
    if not 0 <= value <= 100:
        raise ApiError("REGION_SETTING_INVALID", f"percentage out of range 0..100: {text!r}")
    return value


def _parse_anchor(text: str, name: str) -> RegionAnchor:
    parts = text.split(",")
    if len(parts) != 2:
        raise ApiError("REGION_SETTING_INVALID", f"{name} must be 'x%,y%': {text!r}")
    try:
        return RegionAnchor(_parse_percent(parts[0]), _parse_percent(parts[1]))
    except ApiError:
        raise ApiError("REGION_SETTING_INVALID", f"malformed {name}: {text!r}")


def parse_region_block(block: list[str]) -> RegionDefinition:
    """Parse a REGION metadata block into a validated region definition.

    Settings may follow ``REGION`` on the same line (space separated) and/or
    occupy the following lines as ``key:value`` pairs.  Duplicated settings
    are rejected; unknown keys and bad values are rejected as well.
    """
    head = block[0]
    values: dict[str, str] = {}

    def add_setting(token: str) -> None:
        if ":" not in token:
            raise ApiError("REGION_SETTING_INVALID", f"malformed REGION setting: {token!r}")
        key, value = token.split(":", 1)
        if key in values:
            raise ApiError("REGION_SETTING_DUPLICATE", f"duplicate REGION setting {key!r}")
        values[key] = value

    for token in head[len("REGION"):].strip().split():
        add_setting(token)
    for line in block[1:]:
        add_setting(line)

    region_id = values.get("id")
    if region_id is None or not _ID_RE.match(region_id):
        raise ApiError("REGION_ID_INVALID", "REGION requires a non-empty 'id' without whitespace")

    definition = RegionDefinition(id=region_id)
    if "width" in values:
        definition = dataclasses.replace(definition, width=_parse_percent(values["width"]))
    if "lines" in values:
        raw = values["lines"]
        if not raw.isdigit() or int(raw) == 0:
            raise ApiError("REGION_SETTING_INVALID", f"lines must be a positive integer: {raw!r}")
        definition = dataclasses.replace(definition, lines=int(raw))
    if "regionanchor" in values:
        definition = dataclasses.replace(
            definition, region_anchor=_parse_anchor(values["regionanchor"], "regionanchor")
        )
    if "viewportanchor" in values:
        definition = dataclasses.replace(
            definition, viewport_anchor=_parse_anchor(values["viewportanchor"], "viewportanchor")
        )
    if "scroll" in values:
        if values["scroll"] not in _SCROLL_VALUES:
            raise ApiError("REGION_SETTING_INVALID", f"scroll must be 'up': {values['scroll']!r}")
        definition = dataclasses.replace(definition, scroll=values["scroll"])

    unknown = values.keys() - {"id", "width", "lines", "regionanchor", "viewportanchor", "scroll"}
    if unknown:
        raise ApiError("REGION_SETTING_INVALID", f"unknown REGION setting(s): {sorted(unknown)!r}")
    return definition


def _is_region_head(line: str) -> bool:
    return line == "REGION" or line.startswith("REGION ") or line.startswith("REGION\t")


def _parse_regions(body_lines: list[str]) -> list[RegionDefinition]:
    """Collect and validate every REGION block in declaration order.

    A block is a REGION block only when it starts with a REGION head and
    carries no ``-->`` timing line; the latter case is a cue block whose
    identifier happens to read ``REGION``.
    """
    regions: list[RegionDefinition] = []
    block: list[str] = []
    for line in body_lines + [""]:  # sentinel flushes the final block
        if line == "":
            if (
                block
                and _is_region_head(block[0])
                and not any("-->" in entry for entry in block)
            ):
                regions.append(parse_region_block(block))
            block = []
        else:
            block.append(line)
    return regions


def _parse_cues(body_lines: list[str], resolve_regions: bool = False) -> list[Cue]:
    cues: list[Cue] = []
    block: list[str] = []
    for line in body_lines + [""]:  # sentinel flushes the final block
        if line == "":
            if block:
                cue = _parse_block(block, resolve_regions)
                if cue is not None:
                    cues.append(cue)
                block = []
        else:
            block.append(line)
    return cues


def _parse_block(block: list[str], resolve_regions: bool = False) -> Cue | None:
    first = block[0]
    has_timing = any("-->" in entry for entry in block[:2])
    if first == "NOTE" or first.startswith("NOTE ") or first.startswith("NOTE\t"):
        return None  # comment block
    if first == "STYLE":
        return None  # style metadata block; not expected in HLS segments
    if _is_region_head(first):
        if not resolve_regions:
            # Legacy behavior is preserved exactly: the bare "REGION" head
            # is ignored; a "REGION ..." cue-identifier line falls through.
            if first == "REGION":
                return None
        elif not has_timing:
            # A real REGION metadata block; parsed/validated by _parse_regions.
            return None

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

    region_id = None
    if resolve_regions:
        settings = match.group(3)
        if settings is not None:
            region_id = _cue_region_setting(settings)

    return Cue(
        start_ms=start_ms,
        end_ms=end_ms,
        text="\n".join(block[timing_index + 1:]),
        region_id=region_id,
    )


def _cue_region_setting(settings: str) -> str | None:
    """Extract the (unique) ``region:<id>`` cue setting from a timing line."""
    region_id: str | None = None
    for token in settings.split():
        if token.startswith("region:"):
            ref = token[len("region:"):]
            if region_id is not None:
                raise ApiError("CUE_REGION_DUPLICATE", "cue declares more than one 'region' setting")
            if not ref or not _ID_RE.match(ref):
                raise ApiError("CUE_REGION_INVALID", f"cue has an invalid region reference: {token!r}")
            region_id = ref
    return region_id
