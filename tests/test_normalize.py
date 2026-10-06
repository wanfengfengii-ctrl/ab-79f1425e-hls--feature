import unittest

from app.errors import ApiError
from app.normalize import SegmentInput, normalize_segments, unwrap_map_positions
from app.webvtt import MPEGTS_MODULUS as MOD, parse_segment


def make_segment(sequence, mpegts, local="00:00:00.000", cues=()):
    lines = ["WEBVTT", f"X-TIMESTAMP-MAP=LOCAL:{local},MPEGTS:{mpegts}", ""]
    for start, end, text in cues:
        lines += [f"{start} --> {end}", text, ""]
    return SegmentInput(sequence=sequence, parsed=parse_segment("\n".join(lines)))


def expect_code(testcase, code, fn, *args):
    with testcase.assertRaises(ApiError) as ctx:
        fn(*args)
    testcase.assertEqual(ctx.exception.code, code)
    return ctx.exception


class UnwrapTest(unittest.TestCase):
    def test_no_wrap(self):
        segments = [make_segment(0, 0), make_segment(1, 90000)]
        self.assertEqual(unwrap_map_positions(0, 180000, segments), [0, 90000])

    def test_wraps_past_33_bit_boundary(self):
        segments = [make_segment(0, 8589930000), make_segment(1, 3000)]
        positions = unwrap_map_positions(8589930000, 900000, segments)
        self.assertEqual(positions, [8589930000, MOD + 3000])

    def test_anchor_must_match_first_segment_mpegts(self):
        err = expect_code(self, "ANCHOR_INCOMPATIBLE", unwrap_map_positions,
                          12345, 90000, [make_segment(7, 900000)])
        self.assertEqual(err.segment, 7)

    def test_gap_beyond_max_interval_is_incompatible(self):
        segments = [make_segment(0, 0), make_segment(5, 900000)]
        err = expect_code(self, "ANCHOR_INCOMPATIBLE", unwrap_map_positions,
                          0, 90000, segments)
        self.assertEqual(err.segment, 5)

    def test_ambiguous_unwrap_is_rejected(self):
        # max interval of 2**32 makes both k and k+1 land inside the window.
        segments = [make_segment(0, 0), make_segment(1, MOD // 2)]
        err = expect_code(self, "UNWRAP_NOT_UNIQUE", unwrap_map_positions,
                          MOD, MOD // 2, segments)
        self.assertEqual(err.segment, 1)

    def test_negative_absolute_position_is_not_a_candidate(self):
        # k = -1 would place the map point at -5 ticks; that is not allowed.
        segments = [make_segment(0, 100), make_segment(1, MOD - 5)]
        expect_code(self, "ANCHOR_INCOMPATIBLE", unwrap_map_positions,
                    100, 1000, segments)


class NormalizeTest(unittest.TestCase):
    def test_cue_ticks_use_local_map_offset(self):
        segments = [make_segment(0, 0, local="00:00:10.000",
                                 cues=[("00:00:12.000", "00:00:13.500", "hi")])]
        cues = normalize_segments(0, 90000, segments)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].start_ticks, 2000 * 90)
        self.assertEqual(cues[0].end_ticks, 3500 * 90)
        self.assertEqual(cues[0].text, "hi")

    def test_cues_stay_continuous_across_wrap(self):
        segments = [
            make_segment(0, 8589930000, cues=[("00:00:00.000", "00:00:00.400", "before wrap")]),
            make_segment(1, 3000, cues=[("00:00:00.500", "00:00:01.500", "across wrap")]),
        ]
        cues = normalize_segments(8589930000, 900000, segments)
        self.assertEqual([c.text for c in cues], ["before wrap", "across wrap"])
        self.assertEqual((cues[0].start_ticks, cues[0].end_ticks), (8589930000, 8589966000))
        self.assertEqual((cues[1].start_ticks, cues[1].end_ticks), (8589982592, 8590072592))
        self.assertLess(cues[0].end_ticks, cues[1].start_ticks)

    def test_sorted_by_start_then_segment_then_index(self):
        segments = [
            make_segment(5, 0, cues=[("00:00:00.000", "00:00:01.000", "a"),
                                     ("00:00:02.000", "00:00:03.000", "c")]),
            make_segment(6, 45000, cues=[("00:00:00.000", "00:00:00.400", "b")]),
        ]
        cues = normalize_segments(0, 90000, segments)
        self.assertEqual([(c.segment, c.index) for c in cues], [(5, 0), (6, 0), (5, 1)])
        self.assertEqual([c.text for c in cues], ["a", "b", "c"])

    def test_equal_start_ticks_order_is_stable(self):
        segments = [
            make_segment(2, 0, cues=[("00:00:00.000", "00:00:01.000", "first")]),
            make_segment(3, 0, cues=[("00:00:00.000", "00:00:01.000", "second")]),
        ]
        # max interval 0: identical map points are allowed.
        cues = normalize_segments(0, 0, segments)
        self.assertEqual([c.text for c in cues], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
