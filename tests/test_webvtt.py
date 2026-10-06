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


def expect_code(testcase, code, fn, *args):
    with testcase.assertRaises(ApiError) as ctx:
        fn(*args)
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
            "\ufeffWEBVTT - archived stream\r\n"
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


if __name__ == "__main__":
    unittest.main()
