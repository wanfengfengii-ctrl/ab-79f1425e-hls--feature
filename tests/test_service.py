import unittest

from app.errors import ApiError
from app.service import MAX_PAYLOAD_BYTES, MAX_SEGMENTS, normalize_request


def segment(mpegts, local="00:00:00.000", cues=(), regions=()):
    lines = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:{local},MPEGTS:{mpegts}", ""]
    for region in regions:
        lines += ["REGION"] + [f"{key}:{value}" for key, value in region] + [""]
    for start, end, text in cues:
        lines += [f"{start} --> {end}", text, ""]
    return "\n".join(lines)


def region_segment(mpegts, region_settings, cues=(), local="00:00:00.000", inline=False):
    lines = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:{local},MPEGTS:{mpegts}", ""]
    if inline:
        lines += ["REGION " + " ".join(f"{k}:{v}" for k, v in region_settings), ""]
    else:
        lines += ["REGION"] + [f"{k}:{v}" for k, v in region_settings] + [""]
    for start, end, text, *rest in cues:
        extra = f" region:{rest[0]}" if rest else ""
        lines += [f"{start} --> {end}{extra}", text, ""]
    lines.append("")  # terminate the final block with a blank line
    return "\n".join(lines)


def request(segments, anchor=0, interval=900000, region_policy=None):
    body = {
        "anchorTicks": anchor,
        "maxAnchorIntervalTicks": interval,
        "segments": [{"sequence": seq, "content": content} for seq, content in segments],
    }
    if region_policy is not None:
        body["regionPolicy"] = region_policy
    return body


def expect_code(testcase, code, body):
    with testcase.assertRaises(ApiError) as ctx:
        normalize_request(body)
    testcase.assertEqual(ctx.exception.code, code)
    return ctx.exception


SETTINGS_TOP = [
    ("id", "top"), ("width", "40%"), ("lines", "2"),
    ("regionanchor", "10%,20%"), ("viewportanchor", "30%,40%"), ("scroll", "up"),
]


class RequestValidationTest(unittest.TestCase):
    def test_body_must_be_an_object(self):
        expect_code(self, "INVALID_REQUEST", [1, 2, 3])

    def test_anchor_must_be_an_integer(self):
        expect_code(self, "INVALID_REQUEST", request([(0, segment(0))], anchor="0"))
        expect_code(self, "INVALID_REQUEST", request([(0, segment(0))], anchor=True))
        expect_code(self, "INVALID_REQUEST", request([(0, segment(0))], anchor=-1))

    def test_interval_must_be_an_integer(self):
        expect_code(self, "INVALID_REQUEST", request([(0, segment(0))], interval=1.5))

    def test_segments_must_be_a_list(self):
        expect_code(self, "INVALID_REQUEST",
                    {"anchorTicks": 0, "maxAnchorIntervalTicks": 1, "segments": {}})

    def test_segment_count_bounds(self):
        expect_code(self, "SEGMENT_COUNT_OUT_OF_RANGE", request([]))
        many = [(i, segment(i)) for i in range(MAX_SEGMENTS + 1)]
        expect_code(self, "SEGMENT_COUNT_OUT_OF_RANGE", request(many, anchor=0, interval=10**12))

    def test_sequences_must_be_consecutive(self):
        err = expect_code(self, "SEGMENTS_NOT_CONSECUTIVE",
                          request([(3, segment(0)), (5, segment(0))]))
        self.assertEqual(err.segment, 5)

    def test_duplicate_sequences_are_rejected(self):
        expect_code(self, "SEGMENTS_NOT_CONSECUTIVE",
                    request([(3, segment(0)), (3, segment(0))]))

    def test_content_must_be_a_string(self):
        expect_code(self, "INVALID_REQUEST",
                    {"anchorTicks": 0, "maxAnchorIntervalTicks": 1,
                     "segments": [{"sequence": 0, "content": 42}]})

    def test_payload_limit(self):
        huge = segment(0, cues=[("00:00:00.000", "00:00:01.000", "x" * MAX_PAYLOAD_BYTES)])
        expect_code(self, "PAYLOAD_TOO_LARGE", request([(0, huge)]))

    def test_segment_error_carries_sequence(self):
        err = expect_code(self, "WEBVTT_HEADER_INVALID", request([(7, "garbage")]))
        self.assertEqual(err.segment, 7)

    def test_invalid_region_policy(self):
        expect_code(self, "INVALID_REQUEST",
                    request([(0, segment(0))], region_policy="keep"))
        expect_code(self, "INVALID_REQUEST",
                    request([(0, segment(0))], region_policy=1))


class NormalizeRequestTest(unittest.TestCase):
    def test_happy_path_response_shape(self):
        body = request([
            (10, segment(0, cues=[("00:00:00.000", "00:00:01.000", "hello")])),
            (11, segment(90000, cues=[("00:00:00.000", "00:00:00.500", "world")])),
        ])
        result = normalize_request(body)
        self.assertEqual(len(result["cues"]), 2)
        first, second = result["cues"]
        self.assertEqual(first, {"segment": 10, "index": 0,
                                 "startTicks": 0, "endTicks": 90000, "text": "hello"})
        self.assertEqual(second, {"segment": 11, "index": 0,
                                  "startTicks": 90000, "endTicks": 135000, "text": "world"})
        for cue in result["cues"]:
            self.assertIsInstance(cue["startTicks"], int)
            self.assertIsInstance(cue["endTicks"], int)
        self.assertNotIn("regions", result)

    def test_wraparound_end_to_end(self):
        body = request(
            [
                (0, segment(8589930000, cues=[("00:00:00.000", "00:00:00.400", "before wrap")])),
                (1, segment(3000, cues=[("00:00:00.500", "00:00:01.500", "across wrap")])),
            ],
            anchor=8589930000,
        )
        result = normalize_request(body)
        self.assertEqual([c["text"] for c in result["cues"]], ["before wrap", "across wrap"])
        self.assertEqual(result["cues"][1]["startTicks"], 8589982592)
        self.assertGreater(result["cues"][1]["startTicks"], 1 << 33)

    def test_anchor_incompatible_with_first_segment(self):
        err = expect_code(self, "ANCHOR_INCOMPATIBLE",
                          request([(0, segment(900000))], anchor=12345))
        self.assertEqual(err.segment, 0)

    def test_gap_too_large(self):
        err = expect_code(self, "ANCHOR_INCOMPATIBLE",
                          request([(0, segment(0)), (1, segment(900000))], interval=90000))
        self.assertEqual(err.segment, 1)

    def test_ambiguous_unwrap(self):
        err = expect_code(self, "UNWRAP_NOT_UNIQUE",
                          request([(0, segment(0)), (1, segment(1 << 32))],
                                  anchor=1 << 33, interval=1 << 32))
        self.assertEqual(err.segment, 1)


class RegionResolveTest(unittest.TestCase):
    def test_region_response_shape_and_defaults(self):
        content = region_segment(
            0, [("id", "top"), ("width", "40%")],
            cues=[("00:00:01.000", "00:00:02.000", "hello", "top")],
            inline=True,
        )
        result = normalize_request(request([(0, content)], region_policy="resolve"))
        self.assertEqual(result["regions"], [
            {"id": "top", "width": 40, "lines": 3,
             "regionAnchor": {"x": 0, "y": 100},
             "viewportAnchor": {"x": 0, "y": 100},
             "scroll": None},
        ])
        cue = result["cues"][0]
        self.assertEqual(cue["regionId"], "top")

    def test_cue_without_region_has_null_region_id(self):
        content = region_segment(0, [("id", "top")]) + (
            "00:00:01.000 --> 00:00:02.000\nfree\n"
        )
        result = normalize_request(request([(0, content)], region_policy="resolve"))
        self.assertEqual(result["cues"][0]["regionId"], None)

    def test_full_region_settings_round_trip(self):
        content = region_segment(
            0, SETTINGS_TOP,
            cues=[("00:00:01.000", "00:00:02.000", "hello", "top")],
        )
        result = normalize_request(request([(0, content)], region_policy="resolve"))
        self.assertEqual(result["regions"], [
            {"id": "top", "width": 40, "lines": 2,
             "regionAnchor": {"x": 10, "y": 20},
             "viewportAnchor": {"x": 30, "y": 40},
             "scroll": "up"},
        ])

    def test_identical_cross_segment_declarations_are_merged(self):
        seg0 = region_segment(
            8589930000, SETTINGS_TOP,
            cues=[("00:00:00.000", "00:00:00.400", "before", "top")],
        )
        seg1 = region_segment(
            3000, SETTINGS_TOP,
            cues=[("00:00:00.500", "00:00:01.500", "across", "top")],
        )
        body = request([(0, seg0), (1, seg1)], anchor=8589930000, region_policy="resolve")
        result = normalize_request(body)
        self.assertEqual([r["id"] for r in result["regions"]], ["top"])
        self.assertEqual([c["regionId"] for c in result["cues"]], ["top", "top"])
        # Absolute-time ordering stays stable across the wrap.
        self.assertEqual([c["text"] for c in result["cues"]], ["before", "across"])
        self.assertGreater(result["cues"][1]["startTicks"], 1 << 33)

    def test_regions_returned_in_first_declaration_order(self):
        # The "second" region is declared first in segment 0; both it and a
        # new region appear in segment 1 -- first-declaration order must win.
        seg0 = region_segment(0, [("id", "second"), ("width", "20%")])
        seg1 = "\n".join([
            "WEBVTT", "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:90000", "",
            "REGION", "id:first", "width:10%", "",
            "REGION", "id:second", "width:20%", "",
            "",
        ])
        body = request([(0, seg0), (1, seg1)], interval=180000, region_policy="resolve")
        result = normalize_request(body)
        self.assertEqual([r["id"] for r in result["regions"]], ["second", "first"])

    def test_definition_conflict_is_rejected(self):
        seg0 = region_segment(0, [("id", "top"), ("width", "40%")])
        seg1 = region_segment(90000, [("id", "top"), ("width", "50%")])
        err = expect_code(
            self, "REGION_CONFLICT",
            request([(0, seg0), (1, seg1)], interval=180000, region_policy="resolve"),
        )
        self.assertEqual(err.segment, 1)

    def test_conflict_on_non_width_field(self):
        seg0 = region_segment(0, [("id", "top"), ("scroll", "up")])
        seg1 = region_segment(90000, [("id", "top")])
        err = expect_code(
            self, "REGION_CONFLICT",
            request([(0, seg0), (1, seg1)], interval=180000, region_policy="resolve"),
        )
        self.assertEqual(err.segment, 1)

    def test_duplicate_id_within_one_segment_is_rejected(self):
        content = (
            region_segment(0, [("id", "top")])
            + "REGION\nid:top\nwidth:50%\n\n"
        )
        err = expect_code(self, "REGION_DUPLICATE_ID",
                          request([(0, content)], region_policy="resolve"))
        self.assertEqual(err.segment, 0)

    def test_unknown_region_reference_is_rejected(self):
        content = region_segment(
            0, [("id", "top")],
            cues=[("00:00:01.000", "00:00:02.000", "x", "missing")],
        )
        err = expect_code(self, "REGION_REFERENCE_UNKNOWN",
                          request([(0, content)], region_policy="resolve"))
        self.assertEqual(err.segment, 0)

    def test_unknown_reference_in_later_segment(self):
        seg0 = region_segment(0, [("id", "top")])
        seg1 = region_segment(
            90000, [("id", "top")],
            cues=[("00:00:01.000", "00:00:02.000", "x", "elsewhere")],
        )
        err = expect_code(
            self, "REGION_REFERENCE_UNKNOWN",
            request([(0, seg0), (1, seg1)], interval=180000, region_policy="resolve"),
        )
        self.assertEqual(err.segment, 1)

    def test_invalid_region_field_carries_segment(self):
        content = region_segment(3, [("id", "top"), ("width", "wide")])
        err = expect_code(self, "REGION_FIELD_INVALID",
                          request([(3, content)], region_policy="resolve"))
        self.assertEqual(err.segment, 3)

    def test_default_policy_ignores_region_blocks(self):
        # Even malformed-looking region material is untouched without opt-in.
        content = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            "REGION\nid:top width:40%\n\n"
            "00:00:01.000 --> 00:00:02.000\nx\n"
        )
        result = normalize_request(request([(0, content)]))
        self.assertNotIn("regions", result)
        self.assertNotIn("regionId", result["cues"][0])


if __name__ == "__main__":
    unittest.main()
