import unittest

from app.errors import ApiError
from app.normalize import SegmentInput, normalize_segments
from app.service import normalize_request
from app.webvtt import (
    RegionAnchor,
    RegionDefinition,
    parse_region_block,
    parse_segment,
)


def expect_code(testcase, code, fn, *args):
    with testcase.assertRaises(ApiError) as ctx:
        fn(*args)
    testcase.assertEqual(ctx.exception.code, code)
    return ctx.exception


def region_segment(mpegts, regions=(), cues=(), local="00:00:00.000"):
    """Build a segment string with REGION blocks and region-bearing cues."""
    lines = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:{local},MPEGTS:{mpegts}", ""]
    for settings_lines in regions:
        lines.append("REGION")
        lines.extend(settings_lines)
        lines.append("")
    for start, end, text, ref in cues:
        timing = f"{start} --> {end}"
        if ref is not None:
            timing += f" region:{ref}"
        lines += [timing, text, ""]
    return "\n".join(lines)


TOP_LINES = [
    "id:top",
    "width:50%",
    "lines:3",
    "regionanchor:0%,100%",
    "viewportanchor:0%,100%",
    "scroll:up",
]
TOP_DEF = RegionDefinition(
    id="top",
    width=50,
    lines=3,
    region_anchor=RegionAnchor(0, 100),
    viewport_anchor=RegionAnchor(0, 100),
    scroll="up",
)


class ParseRegionBlockTest(unittest.TestCase):
    def test_all_settings(self):
        self.assertEqual(parse_region_block(["REGION"] + TOP_LINES), TOP_DEF)

    def test_settings_on_region_line(self):
        region = parse_region_block(["REGION id:inline width:25% lines:2"])
        self.assertEqual(region, RegionDefinition(id="inline", width=25, lines=2))

    def test_id_only_uses_defaults(self):
        self.assertEqual(parse_region_block(["REGION", "id:bare"]), RegionDefinition(id="bare"))

    def test_settings_may_be_split_across_head_and_body(self):
        region = parse_region_block(["REGION width:25%", "id:split", "lines:2"])
        self.assertEqual(region, RegionDefinition(id="split", width=25, lines=2))

    def test_missing_or_blank_id(self):
        expect_code(self, "REGION_ID_INVALID", parse_region_block, ["REGION", "width:50%"])
        expect_code(self, "REGION_ID_INVALID", parse_region_block, ["REGION", "id:"])

    def test_bad_width(self):
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block, ["REGION", "id:r", "width:50"])
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block, ["REGION", "id:r", "width:101%"])
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block, ["REGION", "id:r", "width:x%"])

    def test_bad_lines(self):
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block, ["REGION", "id:r", "lines:0"])
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block, ["REGION", "id:r", "lines:2%"])
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block, ["REGION", "id:r", "lines:-1"])

    def test_bad_anchors(self):
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block,
                    ["REGION", "id:r", "regionanchor:10%,20"])
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block,
                    ["REGION", "id:r", "regionanchor:x%,20%"])
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block,
                    ["REGION", "id:r", "viewportanchor:10%,200%"])

    def test_bad_scroll(self):
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block,
                    ["REGION", "id:r", "scroll:down"])

    def test_unknown_and_malformed_setting(self):
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block,
                    ["REGION", "id:r", "color:red"])
        expect_code(self, "REGION_SETTING_INVALID", parse_region_block,
                    ["REGION", "id:r", "width"])

    def test_duplicate_setting(self):
        expect_code(self, "REGION_SETTING_DUPLICATE", parse_region_block,
                    ["REGION width:10%", "id:r", "width:20%"])
        expect_code(self, "REGION_SETTING_DUPLICATE", parse_region_block,
                    ["REGION", "id:r", "lines:2", "lines:3"])


class ParseSegmentRegionsTest(unittest.TestCase):
    def test_regions_and_cue_refs_are_parsed_when_enabled(self):
        content = region_segment(
            0,
            regions=[TOP_LINES, ["id:bottom", "width:100%", "lines:4"]],
            cues=[
                ("00:00:01.000", "00:00:02.000", "on top", "top"),
                ("00:00:03.000", "00:00:04.000", "default", None),
            ],
        )
        parsed = parse_segment(content, resolve_regions=True)
        self.assertEqual(parsed.regions, [TOP_DEF, RegionDefinition(id="bottom", width=100, lines=4)])
        self.assertEqual([cue.region_id for cue in parsed.cues], ["top", None])

    def test_regions_are_ignored_by_default(self):
        content = region_segment(
            0,
            regions=[["id:top", "width:120%"]],  # invalid, but ignored without the policy
            cues=[("00:00:01.000", "00:00:02.000", "x", "top")],
        )
        parsed = parse_segment(content)
        self.assertEqual(parsed.regions, [])
        self.assertIsNone(parsed.cues[0].region_id)

    def test_duplicate_cue_region_setting(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION\nid:r\n\n"
            "00:00:01.000 --> 00:00:02.000 region:r region:r\nx\n"
        )
        expect_code(self, "CUE_REGION_DUPLICATE",
                    lambda: parse_segment(content, resolve_regions=True))

    def test_invalid_cue_region_reference(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "00:00:01.000 --> 00:00:02.000 region:\nx\n"
        )
        expect_code(self, "CUE_REGION_INVALID",
                    lambda: parse_segment(content, resolve_regions=True))

    def test_cue_identifier_named_region_is_not_a_region_block(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION\n"
            "00:00:01.000 --> 00:00:02.000 region:r\n"
            "cue text\n"
        )
        parsed = parse_segment(content, resolve_regions=True)
        self.assertEqual(parsed.regions, [])
        self.assertEqual(len(parsed.cues), 1)
        self.assertEqual(parsed.cues[0].region_id, "r")
        self.assertEqual(parsed.cues[0].text, "cue text")


class RegionNormalizeTest(unittest.TestCase):
    def test_region_id_is_projected_onto_normalized_cues(self):
        content = region_segment(0, regions=[["id:top"]],
                                 cues=[("00:00:01.000", "00:00:02.000", "x", "top")])
        parsed = parse_segment(content, resolve_regions=True)
        cues = normalize_segments(0, 90000, [SegmentInput(sequence=0, parsed=parsed)])
        self.assertEqual(cues[0].region_id, "top")


def body(segments, anchor=0, interval=900000, policy=None):
    data = {
        "anchorTicks": anchor,
        "maxAnchorIntervalTicks": interval,
        "segments": [{"sequence": seq, "content": content} for seq, content in segments],
    }
    if policy is not None:
        data["regionPolicy"] = policy
    return data


class RegionServiceTest(unittest.TestCase):
    def test_policy_must_be_known(self):
        content = region_segment(0)
        expect_code(self, "INVALID_REQUEST", normalize_request,
                    body([(0, content)], policy="keep"))
        expect_code(self, "INVALID_REQUEST", normalize_request,
                    body([(0, content)], policy=42))

    def test_default_response_has_no_region_fields(self):
        content = region_segment(0, regions=[["id:top", "width:50%"]],
                                 cues=[("00:00:01.000", "00:00:02.000", "x", "top")])
        result = normalize_request(body([(0, content)]))
        self.assertNotIn("regions", result)
        self.assertNotIn("regionId", result["cues"][0])

    def test_resolve_response_shape_and_merge(self):
        seg0 = region_segment(
            8589930000,
            regions=[TOP_LINES],
            cues=[("00:00:00.000", "00:00:00.400", "before wrap", "top"),
                  ("00:00:00.100", "00:00:00.200", "no region", None)],
        )
        seg1 = region_segment(
            3000,
            regions=[TOP_LINES, ["id:bottom", "width:100%", "lines:4"]],
            cues=[("00:00:00.500", "00:00:01.500", "across wrap", "bottom")],
        )
        result = normalize_request(
            body([(0, seg0), (1, seg1)], anchor=8589930000, policy="resolve")
        )
        self.assertEqual(
            result["regions"],
            [
                {"id": "top", "width": "50%", "lines": 3,
                 "regionanchor": "0%,100%", "viewportanchor": "0%,100%", "scroll": "up"},
                {"id": "bottom", "width": "100%", "lines": 4},
            ],
        )
        cues = result["cues"]
        self.assertEqual([c["text"] for c in cues], ["before wrap", "no region", "across wrap"])
        self.assertEqual([c["regionId"] for c in cues], ["top", None, "bottom"])
        # absolute-time ordering stays stable across the 2**33 wrap
        self.assertLess(cues[0]["startTicks"], cues[1]["startTicks"])
        self.assertLess(cues[1]["startTicks"], cues[2]["startTicks"])
        self.assertGreater(cues[2]["startTicks"], 1 << 33)

    def test_first_declaration_order_is_preserved(self):
        seg0 = region_segment(0, regions=[["id:zeta"], ["id:alpha"]])
        seg1 = region_segment(0, regions=[["id:alpha"], ["id:zeta"]])
        result = normalize_request(body([(0, seg0), (1, seg1)], interval=0, policy="resolve"))
        self.assertEqual([r["id"] for r in result["regions"]], ["zeta", "alpha"])

    def test_conflicting_redeclaration(self):
        seg0 = region_segment(0, regions=[["id:top", "width:50%"]])
        seg1 = region_segment(0, regions=[["id:top", "width:60%"]])
        err = expect_code(self, "REGION_CONFLICT", normalize_request,
                          body([(0, seg0), (1, seg1)], interval=0, policy="resolve"))
        self.assertEqual(err.segment, 1)

    def test_unknown_region_reference(self):
        content = region_segment(
            0, cues=[("00:00:01.000", "00:00:02.000", "x", "ghost")]
        )
        err = expect_code(self, "REGION_REFERENCE_UNKNOWN", normalize_request,
                          body([(0, content)], policy="resolve"))
        self.assertEqual(err.segment, 0)

    def test_invalid_region_setting_carries_segment(self):
        content = region_segment(0, regions=[["id:top", "width:120%"]])
        err = expect_code(self, "REGION_SETTING_INVALID", normalize_request,
                          body([(3, content)], policy="resolve"))
        self.assertEqual(err.segment, 3)

    def test_failure_returns_no_partial_result(self):
        # A conflict in segment 1 must surface even though segment 0 alone
        # would normalize successfully.
        seg0 = region_segment(0, regions=[["id:top", "width:50%"]],
                              cues=[("00:00:01.000", "00:00:02.000", "ok", "top")])
        seg1 = region_segment(0, regions=[["id:top", "width:60%"]])
        with self.assertRaises(ApiError) as ctx:
            normalize_request(body([(0, seg0), (1, seg1)], interval=0, policy="resolve"))
        self.assertIsInstance(ctx.exception, ApiError)
        self.assertEqual(ctx.exception.code, "REGION_CONFLICT")


if __name__ == "__main__":
    unittest.main()
