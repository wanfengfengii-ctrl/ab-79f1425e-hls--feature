import unittest

from app.errors import ApiError
from app.service import MAX_PAYLOAD_BYTES, MAX_SEGMENTS, normalize_request


def segment(mpegts, local="00:00:00.000", cues=()):
    lines = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:{local},MPEGTS:{mpegts}", ""]
    for start, end, text in cues:
        lines += [f"{start} --> {end}", text, ""]
    return "\n".join(lines)


def request(segments, anchor=0, interval=900000):
    return {
        "anchorTicks": anchor,
        "maxAnchorIntervalTicks": interval,
        "segments": [{"sequence": seq, "content": content} for seq, content in segments],
    }


def expect_code(testcase, code, body):
    with testcase.assertRaises(ApiError) as ctx:
        normalize_request(body)
    testcase.assertEqual(ctx.exception.code, code)
    return ctx.exception


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


if __name__ == "__main__":
    unittest.main()
