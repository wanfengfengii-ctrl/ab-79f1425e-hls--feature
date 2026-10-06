"""One-shot verification pipeline.

Runs after the application container reports healthy and performs, in
order:

1. build check -- every shipped source file byte-compiles;
2. unit tests  -- the unittest suite under ``tests/``;
3. API smoke   -- live HTTP calls against the running app, including an
   MPEG-TS wraparound sample and the stable error codes.

Exits 0 when every step passes, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
MODULUS = 1 << 33


# ---------------------------------------------------------------- steps

def build_check() -> str:
    sources = sorted(ROOT.glob("app/*.py")) + sorted(ROOT.glob("tests/*.py"))
    if not sources:
        raise RuntimeError("no source files found")
    for path in sources:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    return f"{len(sources)} source files compile"


def unit_tests() -> str:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError(f"{len(result.failures)} failures, {len(result.errors)} errors")
    return f"{result.testsRun} unit tests passed"


def smoke_health() -> str:
    last_error: object = "no attempt made"
    for _ in range(30):
        try:
            status, body = _request("GET", "/healthz")
            if status == 200 and body.get("status") == "ok":
                return "GET /healthz -> 200 ok"
            last_error = f"unexpected response {status} {body}"
        except OSError as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"health check failed: {last_error}")


def smoke_wraparound() -> str:
    """Two segments straddling the 33-bit MPEG-TS wrap must stay continuous."""
    payload = {
        "anchorTicks": 8589930000,
        "maxAnchorIntervalTicks": 900000,
        "segments": [
            {"sequence": 0, "content": _segment("00:00:00.000", 8589930000,
                                                [("00:00:00.000", "00:00:00.400", "before wrap")])},
            {"sequence": 1, "content": _segment("00:00:00.000", 3000,
                                                [("00:00:00.500", "00:00:01.500", "across wrap")])},
        ],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 200, f"expected 200, got {status}: {body}")
    cues = body["cues"]
    _assert([c["text"] for c in cues] == ["before wrap", "across wrap"], f"bad order: {cues}")
    _assert(cues[0]["startTicks"] == 8589930000 and cues[0]["endTicks"] == 8589966000, cues[0])
    _assert(cues[1]["startTicks"] == 8589982592 and cues[1]["endTicks"] == 8590072592, cues[1])
    _assert(cues[1]["startTicks"] > MODULUS, "second segment must unwrap past the 33-bit boundary")
    _assert(cues[0]["endTicks"] < cues[1]["startTicks"], "cues must stay continuous across the wrap")
    return "wraparound sample keeps cues ordered and continuous past 2**33"


def smoke_invalid_header() -> str:
    payload = {
        "anchorTicks": 0,
        "maxAnchorIntervalTicks": 90000,
        "segments": [{"sequence": 4, "content": "NOTVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n"}],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 400, f"expected 400, got {status}: {body}")
    error = body["error"]
    _assert(error["code"] == "WEBVTT_HEADER_INVALID", error)
    _assert(error["segment"] == 4, error)
    return "format error returns WEBVTT_HEADER_INVALID with the segment sequence"


def smoke_anchor_incompatible() -> str:
    payload = {
        "anchorTicks": 12345,
        "maxAnchorIntervalTicks": 90000,
        "segments": [{"sequence": 0, "content": _segment("00:00:00.000", 900000, [])}],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 400, f"expected 400, got {status}: {body}")
    error = body["error"]
    _assert(error["code"] == "ANCHOR_INCOMPATIBLE" and error["segment"] == 0, error)
    return "anchor mismatch returns ANCHOR_INCOMPATIBLE"


def smoke_ambiguous_unwrap() -> str:
    payload = {
        "anchorTicks": MODULUS,
        "maxAnchorIntervalTicks": MODULUS // 2,
        "segments": [
            {"sequence": 0, "content": _segment("00:00:00.000", 0, [])},
            {"sequence": 1, "content": _segment("00:00:00.000", MODULUS // 2, [])},
        ],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 400, f"expected 400, got {status}: {body}")
    error = body["error"]
    _assert(error["code"] == "UNWRAP_NOT_UNIQUE" and error["segment"] == 1, error)
    return "ambiguous unwrap returns UNWRAP_NOT_UNIQUE"


# ------------------------------------------------------- region policy smoke

def _region_block(rid: str, *, width: str = "50%", lines: int = 3,
                  regionanchor: str = "0%,100%", viewportanchor: str = "0%,100%",
                  scroll: str = "up") -> str:
    return (
        "REGION\n"
        f"id:{rid}\n"
        f"width:{width}\n"
        f"lines:{lines}\n"
        f"regionanchor:{regionanchor}\n"
        f"viewportanchor:{viewportanchor}\n"
        f"scroll:{scroll}\n"
    )


def _region_segment(local: str, mpegts: int, region_blocks: list[str],
                    cues: list[tuple[str, str, str, str | None]]) -> str:
    lines = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:{local},MPEGTS:{mpegts}", ""]
    lines += region_blocks
    if region_blocks:
        lines.append("")
    for start, end, text, ref in cues:
        timing = f"{start} --> {end}"
        if ref is not None:
            timing += f" region:{ref}"
        lines += [timing, text, ""]
    return "\n".join(lines)


def smoke_regions_default_policy_unchanged() -> str:
    """Without regionPolicy the request/response shape stays region-free."""
    content = _region_segment(
        "00:00:00.000", 900000, [_region_block("top")],
        [("00:00:01.000", "00:00:02.000", "hello", "top")],
    )
    payload = {"anchorTicks": 900000, "maxAnchorIntervalTicks": 90000,
               "segments": [{"sequence": 0, "content": content}]}
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 200, f"expected 200, got {status}: {body}")
    _assert("regions" not in body and "regionId" not in body["cues"][0], body)
    _assert(set(body["cues"][0]) == {"segment", "index", "startTicks", "endTicks", "text"}, body)
    return "omitting regionPolicy leaves input, response and ordering unchanged"


def smoke_regions_resolve_success() -> str:
    """REGION blocks survive normalization, merge across segments and keep
    multilingual cues anchored past the 2**33 wrap."""
    top = _region_block("top")
    seg0 = _region_segment(
        "00:00:00.000", 8589930000, [top],
        [("00:00:00.000", "00:00:00.400", "before wrap", "top"),
         ("00:00:00.100", "00:00:00.200", "default cue", None)],
    )
    seg1 = _region_segment(
        "00:00:00.000", 3000,
        [top, _region_block("bottom", width="100%", lines=4,
                            regionanchor="0%,0%", viewportanchor="0%,0%", scroll="up")],
        [("00:00:00.500", "00:00:01.500", "across wrap", "bottom")],
    )
    payload = {
        "anchorTicks": 8589930000,
        "maxAnchorIntervalTicks": 900000,
        "regionPolicy": "resolve",
        "segments": [{"sequence": 0, "content": seg0}, {"sequence": 1, "content": seg1}],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 200, f"expected 200, got {status}: {body}")
    regions = body["regions"]
    _assert([r["id"] for r in regions] == ["top", "bottom"], regions)
    _assert(regions[0] == {"id": "top", "width": "50%", "lines": 3,
                           "regionanchor": "0%,100%", "viewportanchor": "0%,100%",
                           "scroll": "up"}, regions[0])
    _assert(regions[1] == {"id": "bottom", "width": "100%", "lines": 4,
                           "regionanchor": "0%,0%", "viewportanchor": "0%,0%",
                           "scroll": "up"}, regions[1])
    cues = body["cues"]
    _assert([c["text"] for c in cues] == ["before wrap", "default cue", "across wrap"], cues)
    _assert([c["regionId"] for c in cues] == ["top", None, "bottom"], cues)
    _assert(cues[0]["startTicks"] < cues[1]["startTicks"] < cues[2]["startTicks"], cues)
    _assert(cues[2]["startTicks"] > MODULUS, "region cues remain ordered across the wrap")
    return "resolve: regions merge in first-declaration order and cues carry regionId"


def smoke_regions_conflict() -> str:
    seg0 = _region_segment("00:00:00.000", 0, [_region_block("top", width="50%")], [])
    seg1 = _region_segment("00:00:00.000", 0, [_region_block("top", width="60%")], [])
    payload = {
        "anchorTicks": 0,
        "maxAnchorIntervalTicks": 0,
        "regionPolicy": "resolve",
        "segments": [{"sequence": 0, "content": seg0}, {"sequence": 1, "content": seg1}],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 400, f"expected 400, got {status}: {body}")
    error = body["error"]
    _assert(error["code"] == "REGION_CONFLICT" and error["segment"] == 1, error)
    _assert("regions" not in body and "cues" not in body, "no partial result on failure")
    return "conflicting region definitions return REGION_CONFLICT with the segment"


def smoke_regions_unknown_reference() -> str:
    content = _region_segment(
        "00:00:00.000", 0, [],
        [("00:00:01.000", "00:00:02.000", "ghost cue", "ghost")],
    )
    payload = {
        "anchorTicks": 0,
        "maxAnchorIntervalTicks": 90000,
        "regionPolicy": "resolve",
        "segments": [{"sequence": 2, "content": content}],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 400, f"expected 400, got {status}: {body}")
    error = body["error"]
    _assert(error["code"] == "REGION_REFERENCE_UNKNOWN" and error["segment"] == 2, error)
    return "an unknown region reference returns REGION_REFERENCE_UNKNOWN"


def smoke_regions_invalid_setting() -> str:
    content = _region_segment("00:00:00.000", 0, [_region_block("top", width="120%")], [])
    payload = {
        "anchorTicks": 0,
        "maxAnchorIntervalTicks": 90000,
        "regionPolicy": "resolve",
        "segments": [{"sequence": 5, "content": content}],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 400, f"expected 400, got {status}: {body}")
    error = body["error"]
    _assert(error["code"] == "REGION_SETTING_INVALID" and error["segment"] == 5, error)
    return "an illegal REGION field returns REGION_SETTING_INVALID"


def smoke_region_policy_must_be_known() -> str:
    payload = {
        "anchorTicks": 0,
        "maxAnchorIntervalTicks": 90000,
        "regionPolicy": "keep",
        "segments": [{"sequence": 0, "content": _segment("00:00:00.000", 0, [])}],
    }
    status, body = _request("POST", "/api/subtitles/normalize", payload)
    _assert(status == 400, f"expected 400, got {status}: {body}")
    _assert(body["error"]["code"] == "INVALID_REQUEST", body)
    return "an unknown regionPolicy returns INVALID_REQUEST"


# ---------------------------------------------------------------- helpers

def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _segment(local: str, mpegts: int, cues: list[tuple[str, str, str]]) -> str:
    lines = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:{local},MPEGTS:{mpegts}", ""]
    for start, end, text in cues:
        lines += [f"{start} --> {end}", text, ""]
    return "\n".join(lines)


def _assert(condition: bool, detail: object) -> None:
    if not condition:
        raise RuntimeError(f"assertion failed: {detail}")


def main() -> int:
    steps = [
        ("build check", build_check),
        ("unit tests", unit_tests),
        ("smoke: health", smoke_health),
        ("smoke: wraparound", smoke_wraparound),
        ("smoke: invalid header", smoke_invalid_header),
        ("smoke: anchor incompatible", smoke_anchor_incompatible),
        ("smoke: ambiguous unwrap", smoke_ambiguous_unwrap),
        ("smoke: regions default unchanged", smoke_regions_default_policy_unchanged),
        ("smoke: regions resolve success", smoke_regions_resolve_success),
        ("smoke: regions conflict", smoke_regions_conflict),
        ("smoke: regions unknown reference", smoke_regions_unknown_reference),
        ("smoke: regions invalid setting", smoke_regions_invalid_setting),
        ("smoke: region policy value", smoke_region_policy_must_be_known),
    ]
    print(f"verify: targeting app at {BASE_URL}", flush=True)
    failures = 0
    for name, step in steps:
        try:
            detail = step()
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {name}: {exc}", flush=True)
        else:
            print(f"[PASS] {name}: {detail}", flush=True)
    if failures:
        print(f"verify: {failures} of {len(steps)} step(s) failed", flush=True)
        return 1
    print(f"verify: all {len(steps)} steps passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
