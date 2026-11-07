"""Tests for the chart maths, run against the Python copy in chart_twin.py.

The last class checks that charts.js still declares the same functions.
"""

import math
import os
import re
import unittest

from . import chart_twin as M

JS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bms_monitor", "web", "static", "charts.js",
)


class TestScale(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(M.scale_linear(0.5, 0, 1, 0, 100), 50.0)
        self.assertAlmostEqual(M.scale_linear(3.7, 3.6, 3.8, 200, 0), 100.0)

    def test_zero_width_domain_does_not_blow_up(self):
        # A pack sitting perfectly still gives min == max. Must not be
        # NaN/inf, which would blank the whole canvas.
        y = M.scale_linear(3.7, 3.7, 3.7, 0, 100)
        self.assertTrue(math.isfinite(y))
        self.assertEqual(y, 50.0)

    def test_invert_round_trips(self):
        for v in (3.0, 3.7, 4.2):
            p = M.scale_linear(v, 3.0, 4.2, 20, 300)
            self.assertAlmostEqual(M.scale_invert(p, 3.0, 4.2, 20, 300), v, places=9)

    def test_invert_zero_width_range(self):
        self.assertEqual(M.scale_invert(50, 3.0, 4.2, 10, 10), 3.0)


class TestNiceTicks(unittest.TestCase):
    def test_step_is_1_2_5(self):
        for raw, want in [(0.0009, 0.001), (0.0011, 0.002), (0.003, 0.005),
                          (0.006, 0.01), (1.0, 1.0), (23.0, 50.0)]:
            self.assertAlmostEqual(M.nice_step(raw), want, places=12,
                                   msg="nice_step(%r)" % raw)

    def test_nonpositive_step(self):
        self.assertEqual(M.nice_step(0), 1)
        self.assertEqual(M.nice_step(-5), 1)
        self.assertEqual(M.nice_step(float("nan")), 1)

    def test_four_millivolt_span_has_no_duplicate_labels(self):
        # A 4 mV window at 3.7 V would label every tick "3.700" at three
        # decimals.
        ticks = M.nice_ticks(3.698, 3.702, 5)
        labels = [t["label"] for t in ticks]
        self.assertGreaterEqual(len(labels), 3)
        self.assertEqual(len(set(labels)), len(labels), labels)

    def test_sub_millivolt_span(self):
        ticks = M.nice_ticks(3.70000, 3.70040, 5)
        labels = [t["label"] for t in ticks]
        self.assertEqual(len(set(labels)), len(labels), labels)

    def test_min_equals_max(self):
        ticks = M.nice_ticks(3.7, 3.7, 5)
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0]["v"], 3.7)

    def test_reversed_bounds(self):
        self.assertEqual([t["label"] for t in M.nice_ticks(4.2, 3.0, 4)],
                         [t["label"] for t in M.nice_ticks(3.0, 4.2, 4)])

    def test_ticks_lie_inside_domain(self):
        ticks = M.nice_ticks(3.0, 4.2, 5)
        for t in ticks:
            self.assertGreaterEqual(t["v"], 3.0 - 1e-9)
            self.assertLessEqual(t["v"], 4.2 + 1e-9)

    def test_no_negative_zero_label(self):
        labels = [t["label"] for t in M.nice_ticks(-0.5, 0.5, 4)]
        self.assertNotIn("-0.00", labels)
        self.assertNotIn("-0.0", labels)

    def test_non_finite_bounds(self):
        self.assertEqual(M.nice_ticks(float("nan"), 1.0), [])
        self.assertEqual(M.nice_ticks(0.0, float("inf")), [])

    def test_terminates_on_pathological_input(self):
        ticks = M.nice_ticks(0.0, 1e12, 5)
        self.assertLessEqual(len(ticks), 200)


class TestAutoscale(unittest.TestCase):
    def test_all_equal_gives_non_degenerate_domain(self):
        lo, hi = M.autoscale([3.7] * 16)
        self.assertGreater(hi, lo)
        self.assertAlmostEqual((lo + hi) / 2, 3.7, places=9)

    def test_single_value(self):
        lo, hi = M.autoscale([3.7])
        self.assertGreater(hi, lo)

    def test_empty_uses_fallback(self):
        self.assertEqual(M.autoscale([]), [3.0, 4.2])
        self.assertEqual(M.autoscale([], fallback=(0.0, 1.0)), [0.0, 1.0])

    def test_ignores_nan_and_inf(self):
        # A NaN reaching autoscale would make lo/hi NaN and blank the chart.
        lo, hi = M.autoscale([3.6, float("nan"), 3.8, float("inf")])
        self.assertTrue(math.isfinite(lo) and math.isfinite(hi))
        self.assertLess(lo, 3.6)
        self.assertGreater(hi, 3.8)

    def test_all_nan_uses_fallback(self):
        self.assertEqual(M.autoscale([float("nan"), float("inf")]), [3.0, 4.2])

    def test_padding_applied(self):
        lo, hi = M.autoscale([3.0, 4.0], pad=0.1, min_span=0.001)
        self.assertAlmostEqual(lo, 2.9, places=9)
        self.assertAlmostEqual(hi, 4.1, places=9)


class TestSegmentPaths(unittest.TestCase):
    @staticmethod
    def pts(flags):
        return [{"x": float(i), "y": float(i), "ok": f} for i, f in enumerate(flags)]

    def test_all_good_is_one_segment(self):
        segs = M.segment_paths(self.pts([True] * 5))
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(segs[0]), 5)

    def test_all_bad_is_no_segments(self):
        self.assertEqual(M.segment_paths(self.pts([False] * 5)), [])

    def test_empty(self):
        self.assertEqual(M.segment_paths([]), [])

    def test_leading_bad(self):
        segs = M.segment_paths(self.pts([False, False, True, True]))
        self.assertEqual(len(segs), 1)
        self.assertEqual([p["x"] for p in segs[0]], [2.0, 3.0])

    def test_trailing_bad(self):
        segs = M.segment_paths(self.pts([True, True, False, False]))
        self.assertEqual(len(segs), 1)
        self.assertEqual([p["x"] for p in segs[0]], [0.0, 1.0])

    def test_gap_in_the_middle_breaks_the_path(self):
        # No interpolating across a PEC-bad frame.
        segs = M.segment_paths(self.pts([True, True, False, True, True]))
        self.assertEqual(len(segs), 2)
        self.assertEqual([p["x"] for p in segs[0]], [0.0, 1.0])
        self.assertEqual([p["x"] for p in segs[1]], [3.0, 4.0])

    def test_single_good_point_between_two_bad_survives(self):
        # Must render as an isolated dot, not vanish.
        segs = M.segment_paths(self.pts([False, True, False]))
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(segs[0]), 1)
        self.assertEqual(segs[0][0]["x"], 1.0)

    def test_alternating(self):
        segs = M.segment_paths(self.pts([True, False] * 4))
        self.assertEqual(len(segs), 4)
        self.assertTrue(all(len(s) == 1 for s in segs))

    def test_non_finite_coordinate_is_treated_as_bad(self):
        pts = [{"x": 0.0, "y": 0.0, "ok": True},
               {"x": 1.0, "y": float("nan"), "ok": True},
               {"x": 2.0, "y": 2.0, "ok": True}]
        segs = M.segment_paths(pts)
        self.assertEqual(len(segs), 2)

    def test_missing_ok_flag_is_bad(self):
        self.assertEqual(M.segment_paths([{"x": 0.0, "y": 0.0}]), [])


class TestWindowSlice(unittest.TestCase):
    @staticmethod
    def samples(ts):
        return [{"t": float(t)} for t in ts]

    def test_basic(self):
        s = self.samples(range(11))
        got = M.window_slice(s, 10.0, 3.0)
        self.assertEqual([x["t"] for x in got], [7.0, 8.0, 9.0, 10.0])

    def test_window_larger_than_data(self):
        s = self.samples([0, 1, 2])
        self.assertEqual(len(M.window_slice(s, 2.0, 1000.0)), 3)

    def test_empty(self):
        self.assertEqual(M.window_slice([], 5.0, 30.0), [])

    def test_excludes_future_samples(self):
        s = self.samples([0, 1, 2, 3])
        self.assertEqual([x["t"] for x in M.window_slice(s, 2.0, 10.0)], [0.0, 1.0, 2.0])


class TestNearestIndex(unittest.TestCase):
    @staticmethod
    def samples(ts):
        return [{"t": float(t)} for t in ts]

    def test_empty_returns_minus_one(self):
        self.assertEqual(M.nearest_index([], 1.0), -1)

    def test_exact(self):
        self.assertEqual(M.nearest_index(self.samples([0, 1, 2]), 1.0), 1)

    def test_rounds_to_closer_neighbour(self):
        s = self.samples([0, 1, 2])
        self.assertEqual(M.nearest_index(s, 1.4), 1)
        self.assertEqual(M.nearest_index(s, 1.6), 2)

    def test_clamps_outside_range(self):
        s = self.samples([10, 11, 12])
        self.assertEqual(M.nearest_index(s, -100.0), 0)
        self.assertEqual(M.nearest_index(s, 1000.0), 2)

    def test_single_sample(self):
        self.assertEqual(M.nearest_index(self.samples([5]), 0.0), 0)


class TestCellViewLayout(unittest.TestCase):
    """The drill-down's panel layout.

    The car is 5 boards x 2 chips = 10 ICs. A cell shown under the wrong
    board sends someone to the wrong part of the pack.
    """

    def test_segment_of_maps_the_real_car(self):
        # IC1,IC2 -> S1;  IC3,IC4 -> S2;  ...  IC9,IC10 -> S5
        got = [M.segment_of(ic, 2) for ic in range(1, 11)]
        self.assertEqual(got, [1, 1, 2, 2, 3, 3, 4, 4, 5, 5])

    def test_segment_count(self):
        self.assertEqual(M.segment_count(10, 2), 5)
        self.assertEqual(M.segment_count(2, 2), 1)
        self.assertEqual(M.segment_count(1, 2), 1)
        self.assertEqual(M.segment_count(0, 2), 0)

    def test_a_half_populated_last_board_returns_the_chips_that_exist(self):
        # 9 ICs on 5 boards: the last board has one chip, not two.
        self.assertEqual(M.segment_ics(5, 9, 2), [9])
        self.assertEqual(M.segment_ics(4, 9, 2), [7, 8])

    def test_pack_level_is_one_panel_per_board(self):
        panels = M.cell_view_panels(10, 2, None)
        self.assertEqual(len(panels), 5)
        self.assertEqual([p["label"] for p in panels],
                         ["S1", "S2", "S3", "S4", "S5"])
        self.assertEqual(panels[2]["ics"], [5, 6])
        self.assertEqual(panels[2]["sub"], "IC5\u2013IC6")

    def test_drilling_in_is_one_panel_per_chip_on_that_board(self):
        panels = M.cell_view_panels(10, 2, 3)
        self.assertEqual([p["label"] for p in panels], ["IC5", "IC6"])
        self.assertEqual([p["ics"] for p in panels], [[5], [6]])
        self.assertEqual(panels[0]["sub"], "S3")

    def test_a_single_board_chain_skips_the_pack_level(self):
        # The 2-IC bench setup: drilling into the only board would be a
        # no-op click, so the per-chip panels are the top level.
        panels = M.cell_view_panels(2, 2, None)
        self.assertEqual([p["label"] for p in panels], ["IC1", "IC2"])
        self.assertEqual(panels[0]["sub"], "", "no board tag with one board")

    def test_one_ic_chain(self):
        panels = M.cell_view_panels(1, 2, None)
        self.assertEqual([p["label"] for p in panels], ["IC1"])

    def test_an_out_of_range_focus_falls_back_to_the_pack(self):
        # Focus is persisted in localStorage, so a saved S5 can outlive the
        # chain that had five boards. It must not render an empty chart.
        for bad in (0, 6, 99, -1):
            panels = M.cell_view_panels(10, 2, bad)
            self.assertEqual(len(panels), 5, "focus=%r" % bad)

    def test_every_cell_appears_exactly_once_at_each_level(self):
        for focus in [None, 1, 2, 3, 4, 5]:
            panels = M.cell_view_panels(10, 2, focus)
            seen = [ic for p in panels for ic in p["ics"]]
            self.assertEqual(len(seen), len(set(seen)), "duplicate chip")
            if focus is None:
                self.assertEqual(sorted(seen), list(range(1, 11)))
            else:
                self.assertEqual(sorted(seen), M.segment_ics(focus, 10, 2))

    def test_five_boards_fit_on_one_row(self):
        # The pack level should fit all five boards in one row.
        self.assertEqual(M.panel_columns(5), 5)
        self.assertEqual(M.cell_view_height(5), 300)

    def test_height_grows_when_panels_wrap(self):
        self.assertGreater(M.cell_view_height(10), M.cell_view_height(5))


class TestTwinStaysInSync(unittest.TestCase):
    """chart_twin.py and charts.js must define the same functions."""

    JS_TO_PY = {
        "scaleLinear": "scale_linear",
        "scaleInvert": "scale_invert",
        "niceStep": "nice_step",
        "decimalsFor": "decimals_for",
        "niceTicks": "nice_ticks",
        "autoscale": "autoscale",
        "segmentPaths": "segment_paths",
        "windowSlice": "window_slice",
        "nearestIndex": "nearest_index",
        "formatFixed": "format_fixed",
        "segmentOf": "segment_of",
        "segmentCount": "segment_count",
        "segmentIcs": "segment_ics",
        "cellViewPanels": "cell_view_panels",
        "panelColumns": "panel_columns",
        "cellViewHeight": "cell_view_height",
    }

    @staticmethod
    def _js_source():
        with open(JS_PATH, encoding="utf-8") as handle:
            return handle.read()

    def test_charts_js_declares_every_mirrored_function(self):
        src = self._js_source()
        declared = set(re.findall(r"^function\s+(\w+)\s*\(", src, re.M))
        for js_name in self.JS_TO_PY:
            self.assertIn(js_name, declared,
                          "charts.js no longer defines %s()" % js_name)

    def test_python_twin_declares_every_mirrored_function(self):
        for py_name in self.JS_TO_PY.values():
            self.assertTrue(callable(getattr(M, py_name, None)),
                            "chart_twin.py is missing %s()" % py_name)

    def test_charts_js_exports_them(self):
        src = self._js_source()
        export = src[src.index("window.Charts"):]
        for js_name in self.JS_TO_PY:
            self.assertIn(js_name, export)


if __name__ == "__main__":
    unittest.main()
