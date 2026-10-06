import unittest

from app.errors import ApiError
from app.webvtt import MPEGTS_MAX, parse_segment, parse_timestamp_ms

VALID = (
    "WEBVTT\n"
    "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n"
    "\n"
    "00:00:01.000 --> 00:00:04.000\n"
    "Hello world\n"
)


def expect_code(testcase, code, fn, *args, **kwargs):
    with testcase.assertRaises(ApiError) as ctx:
        fn(*args, **kwargs)
    testcase.assertEqual(ctx.exception.code, code)
    return ctx.exception


class TimestampTest(unittest.TestCase):
    def test_millisecond_forms(self):
        self.assertEqual(parse_timestamp_ms("00:00.500"), 500)
        self.assertEqual(parse_timestamp_ms("01:02.003"), 62_003)
        self.assertEqual(parse_timestamp_ms("01:00:00.000"), 3_600_000)
        self.assertEqual(parse_timestamp_ms("100:00:00.000"), 360_000_000)

    def test_rejects_non_millisecond_precision(self):
        expect_code(self, "TIMESTAMP_INVALID", parse_timestamp_ms, "00:00:01.00")
        expect_code(self, "TIMESTAMP_INVALID", parse_timestamp_ms, "00:00:01.0000")

    def test_rejects_out_of_range_components(self):
        expect_code(self, "TIMESTAMP_INVALID", parse_timestamp_ms, "00:00:60.000")
        expect_code(self, "TIMESTAMP_INVALID", parse_timestamp_ms, "00:60:00.000")
        expect_code(self, "TIMESTAMP_INVALID", parse_timestamp_ms, "1:00:00.000")


class ParseSegmentTest(unittest.TestCase):
    def test_valid_segment(self):
        parsed = parse_segment(VALID)
        self.assertEqual(parsed.local_map_ms, 0)
        self.assertEqual(parsed.mpegts, 900000)
        self.assertEqual(len(parsed.cues), 1)
        self.assertEqual((parsed.cues[0].start_ms, parsed.cues[0].end_ms), (1000, 4000))
        self.assertEqual(parsed.cues[0].text, "Hello world")

    def test_header_text_crlf_and_bom(self):
        content = (
            "﻿WEBVTT - archived stream\r\n"
            "X-TIMESTAMP-MAP=LOCAL:00:00:10.000,MPEGTS:900\r\n"
            "\r\n"
            "00:00:10.000 --> 00:00:11.000\r\n"
            "hi\r\n"
        )
        parsed = parse_segment(content)
        self.assertEqual(parsed.local_map_ms, 10_000)
        self.assertEqual(parsed.mpegts, 900)
        self.assertEqual(len(parsed.cues), 1)

    def test_cue_identifier_settings_and_multiline_text(self):
        content = (
            "WEBVTT\n"
            "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n"
            "\n"
            "cue-17\n"
            "00:00:01.000 --> 00:00:02.500 align:start position:0%\n"
            "line one\n"
            "line two\n"
        )
        parsed = parse_segment(content)
        self.assertEqual(len(parsed.cues), 1)
        self.assertEqual(parsed.cues[0].end_ms, 2500)
        self.assertEqual(parsed.cues[0].text, "line one\nline two")

    def test_note_blocks_are_skipped(self):
        content = (
            "WEBVTT\n"
            "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n"
            "\n"
            "NOTE this is a comment\n"
            "spanning two lines\n"
            "\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "real cue\n"
        )
        parsed = parse_segment(content)
        self.assertEqual(len(parsed.cues), 1)
        self.assertEqual(parsed.cues[0].text, "real cue")

    def test_missing_webvtt_header(self):
        expect_code(self, "WEBVTT_HEADER_INVALID", parse_segment, "NOTVTT\n")
        expect_code(self, "WEBVTT_HEADER_INVALID", parse_segment, "")

    def test_missing_timestamp_map(self):
        expect_code(self, "TIMESTAMP_MAP_MISSING", parse_segment, "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nx\n")

    def test_duplicate_timestamp_map(self):
        content = (
            "WEBVTT\n"
            "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n"
            "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:1\n"
            "\n"
        )
        expect_code(self, "TIMESTAMP_MAP_DUPLICATE", parse_segment, content)

    def test_timestamp_map_outside_header_block(self):
        content = (
            "WEBVTT\n"
            "\n"
            "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n"
            "\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "x\n"
        )
        expect_code(self, "TIMESTAMP_MAP_INVALID", parse_segment, content)

    def test_malformed_timestamp_map(self):
        expect_code(self, "TIMESTAMP_MAP_INVALID", parse_segment,
                    "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000\n\n")
        expect_code(self, "TIMESTAMP_MAP_INVALID", parse_segment,
                    "WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:0,LOCAL:00:00:00.000\n\n")

    def test_bad_local_timestamp(self):
        expect_code(self, "TIMESTAMP_INVALID", parse_segment,
                    "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:0:00,MPEGTS:0\n\n")

    def test_mpegts_must_be_33_bit(self):
        expect_code(self, "MPEGTS_OUT_OF_RANGE", parse_segment,
                    f"WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{MPEGTS_MAX + 1}\n\n")
        parsed = parse_segment(f"WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{MPEGTS_MAX}\n\n")
        self.assertEqual(parsed.mpegts, MPEGTS_MAX)

    def test_bad_cue_timestamp(self):
        content = VALID.replace("00:00:01.000", "00:00:01.00")
        expect_code(self, "TIMESTAMP_INVALID", parse_segment, content)

    def test_cue_interval_must_be_positive(self):
        content = VALID.replace("00:00:04.000", "00:00:01.000")
        expect_code(self, "CUE_INTERVAL_INVALID", parse_segment, content)

    def test_block_without_timing_line(self):
        content = "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\njust text\n"
        expect_code(self, "CUE_TIMING_INVALID", parse_segment, content)


_REGION_SEGMENT = (
    "WEBVTT\n"
    "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n"
    "\n"
    "REGION\n"
    "id:top\n"
    "width:40%\n"
    "lines:2\n"
    "regionanchor:10%,20%\n"
    "viewportanchor:30%,40%\n"
    "scroll:up\n"
    "\n"
    "00:00:01.000 --> 00:00:02.000 region:top align:left\n"
    "hello\n"
)


class RegionParsingTest(unittest.TestCase):
    def test_regions_ignored_without_opt_in(self):
        parsed = parse_segment(_REGION_SEGMENT)
        self.assertEqual(parsed.regions, [])
        self.assertIsNone(parsed.cues[0].region_id)

    def test_legacy_path_matches_old_semantics(self):
        # A bare REGION block is skipped exactly as before the feature.
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION\nid:top\n\n"
            "00:00:01.000 --> 00:00:02.000\nx\n"
        )
        parsed = parse_segment(content)
        self.assertEqual(len(parsed.cues), 1)
        # A "REGION ..." line (not a bare block) used to be treated as a cue
        # identifier line and fail timing validation -- that is unchanged.
        content2 = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:top\n\n"
        )
        expect_code(self, "CUE_TIMING_INVALID", parse_segment, content2)

    def test_region_with_all_settings(self):
        parsed = parse_segment(_REGION_SEGMENT, resolve_regions=True)
        self.assertEqual(len(parsed.regions), 1)
        region = parsed.regions[0]
        self.assertEqual(region.id, "top")
        self.assertEqual(region.width, 40)
        self.assertEqual(region.lines, 2)
        self.assertEqual(region.region_anchor, (10, 20))
        self.assertEqual(region.viewport_anchor, (30, 40))
        self.assertEqual(region.scroll, "up")
        self.assertEqual(parsed.cues[0].region_id, "top")

    def test_region_defaults(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:bottom\n"
            "\n"
            "00:00:01.000 --> 00:00:02.000 region:bottom\n"
            "x\n"
        )
        region = parse_segment(content, resolve_regions=True).regions[0]
        self.assertEqual(region.id, "bottom")
        self.assertEqual(region.width, 100)
        self.assertEqual(region.lines, 3)
        self.assertEqual(region.region_anchor, (0, 100))
        self.assertEqual(region.viewport_anchor, (0, 100))
        self.assertIsNone(region.scroll)

    def test_settings_on_region_line_and_following_lines(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:a width:10% lines:5\n"
            "regionanchor:0%,0%\n"
            "\n"
        )
        region = parse_segment(content, resolve_regions=True).regions[0]
        self.assertEqual((region.id, region.width, region.lines, region.region_anchor),
                         ("a", 10, 5, (0, 0)))

    def test_cue_without_region_has_null_reference(self):
        parsed = parse_segment(VALID, resolve_regions=True)
        self.assertEqual(parsed.regions, [])
        self.assertIsNone(parsed.cues[0].region_id)

    def test_multiple_regions_preserve_order(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:second\n"
            "\n"
            "REGION id:first\n"
            "\n"
        )
        regions = parse_segment(content, resolve_regions=True).regions
        self.assertEqual([r.id for r in regions], ["second", "first"])

    def test_region_in_header_block_is_rejected(self):
        content = (
            "WEBVTT\n"
            "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n"
            "REGION id:x\n"
            "\n"
        )
        err = expect_code(self, "REGION_BLOCK_INVALID", parse_segment, content, resolve_regions=True)
        self.assertIsNone(err.segment)

    def test_region_requires_id(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION\nwidth:50%\n\n"
        )
        expect_code(self, "REGION_FIELD_INVALID", parse_segment, content, resolve_regions=True)

    def test_duplicate_setting_in_one_block(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:a width:10%\nwidth:20%\n\n"
        )
        expect_code(self, "REGION_SETTING_DUPLICATE", parse_segment, content, resolve_regions=True)
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:a id:b\n\n"
        )
        expect_code(self, "REGION_SETTING_DUPLICATE", parse_segment, content, resolve_regions=True)

    def test_unknown_region_field(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:a bogus:1\n\n"
        )
        expect_code(self, "REGION_FIELD_INVALID", parse_segment, content, resolve_regions=True)

    def test_bad_width_and_anchor_and_scroll(self):
        base = "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\nREGION id:a {}\n\n"
        expect_code(self, "REGION_FIELD_INVALID", parse_segment,
                    base.format("width:10"), resolve_regions=True)
        expect_code(self, "REGION_FIELD_INVALID", parse_segment,
                    base.format("width:101%"), resolve_regions=True)
        expect_code(self, "REGION_FIELD_INVALID", parse_segment,
                    base.format("lines:0"), resolve_regions=True)
        expect_code(self, "REGION_FIELD_INVALID", parse_segment,
                    base.format("regionanchor:10%,120%"), resolve_regions=True)
        expect_code(self, "REGION_FIELD_INVALID", parse_segment,
                    base.format("viewportanchor:5%-5%"), resolve_regions=True)
        expect_code(self, "REGION_FIELD_INVALID", parse_segment,
                    base.format("scroll:sideways"), resolve_regions=True)

    def test_malformed_region_setting_line(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION\nid:a width:50%\n\n"
        )
        expect_code(self, "REGION_BLOCK_INVALID", parse_segment, content, resolve_regions=True)

    def test_duplicate_cue_region_setting(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:a\n\n"
            "REGION id:b\n\n"
            "00:00:01.000 --> 00:00:02.000 region:a region:b\n"
            "x\n"
        )
        expect_code(self, "REGION_SETTING_DUPLICATE", parse_segment, content, resolve_regions=True)

    def test_unknown_cue_settings_still_accepted(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "00:00:01.000 --> 00:00:02.000 align:left future:value\n"
            "x\n"
        )
        parsed = parse_segment(content, resolve_regions=True)
        self.assertIsNone(parsed.cues[0].region_id)

    def test_empty_and_malformed_cue_region_setting(self):
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION id:a\n\n"
            "00:00:01.000 --> 00:00:02.000 region:\n"
            "x\n"
        )
        expect_code(self, "REGION_FIELD_INVALID", parse_segment, content, resolve_regions=True)
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "00:00:01.000 --> 00:00:02.000 region\n"
            "x\n"
        )
        expect_code(self, "CUE_TIMING_INVALID", parse_segment, content, resolve_regions=True)

    def test_region_blocks_with_crlf(self):
        content = (
            "WEBVTT\r\n"
            "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\r\n\r\n"
            "REGION\r\nid:top\r\nwidth:40%\r\n\r\n"
            "00:00:01.000 --> 00:00:02.000 region:top\r\nx\r\n"
        )
        parsed = parse_segment(content, resolve_regions=True)
        self.assertEqual(parsed.regions[0].id, "top")
        self.assertEqual(parsed.regions[0].width, 40)
        self.assertEqual(parsed.cues[0].region_id, "top")


if __name__ == "__main__":
    unittest.main()
