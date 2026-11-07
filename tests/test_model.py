"""Model invariants: the voltage maths and the Sample contract."""

import math
import unittest

from bms_monitor import model as M


class TestVoltageMaths(unittest.TestCase):
    def test_matches_the_firmware_formula(self):
        # getVoltage(data) = ((data + 10000) * 0.000150)
        for code in (-32768, -10000, -1, 0, 1, 1000, 32767):
            self.assertAlmostEqual(M.code_to_volts(code),
                                   (code + 10000) * 0.000150, places=12)

    def test_zero_code_is_1_5_volts(self):
        self.assertAlmostEqual(M.code_to_volts(0), 1.5, places=9)

    def test_minus_one_is_the_all_ff_signature(self):
        self.assertAlmostEqual(M.code_to_volts(-1), 1.49985, places=9)

    def test_int16_span(self):
        self.assertAlmostEqual(M.VOLTAGE_MIN, -3.4152, places=4)
        self.assertAlmostEqual(M.VOLTAGE_MAX, 6.41505, places=5)

    def test_round_trip(self):
        for code in range(-32768, 32768, 977):
            self.assertEqual(M.volts_to_code(M.code_to_volts(code)), code)

    def test_volts_to_code_clamps(self):
        self.assertEqual(M.volts_to_code(1e9), 32767)
        self.assertEqual(M.volts_to_code(-1e9), -32768)


class TestPlausibility(unittest.TestCase):
    def test_rejects_nan_and_inf(self):
        for bad in (float("nan"), float("inf"), float("-inf"), None):
            self.assertFalse(M.is_plausible_voltage(bad), bad)

    def test_accepts_the_full_printable_range(self):
        self.assertTrue(M.is_plausible_voltage(-3.4152))
        self.assertTrue(M.is_plausible_voltage(6.41505))
        self.assertTrue(M.is_plausible_voltage(0.0))

    def test_rejects_outside(self):
        self.assertFalse(M.is_plausible_voltage(20.0))
        self.assertFalse(M.is_plausible_voltage(-10.0))


class TestSample(unittest.TestCase):
    def make(self, cells=None, pec_ok=True, stale=False):
        return M.Sample(
            seq=0, t_mono=1.0, t_wall=2.0, ic=1,
            cells=tuple(cells if cells is not None else
                        [3.70 + 0.001 * i for i in range(16)]),
            ccount=7, pec_ok=pec_ok, stale=stale,
        )

    def test_derived_values(self):
        s = self.make()
        self.assertAlmostEqual(s.pack_v, sum(s.cells))
        self.assertAlmostEqual(s.min_v, 3.700)
        self.assertAlmostEqual(s.max_v, 3.715)
        self.assertEqual(s.min_index, 0)
        self.assertEqual(s.max_index, 15)
        self.assertAlmostEqual(s.spread_mv, 15.0, places=6)

    def test_usable_requires_pec_ok_and_not_stale(self):
        self.assertTrue(self.make().usable)
        self.assertFalse(self.make(pec_ok=False).usable)
        self.assertFalse(self.make(stale=True).usable)
        self.assertFalse(self.make(pec_ok=False, stale=True).usable)

    def test_to_json_is_serialisable_and_keeps_the_flags(self):
        import json
        payload = json.loads(json.dumps(self.make(pec_ok=False, stale=True).to_json()))
        self.assertFalse(payload["pec_ok"])
        self.assertTrue(payload["stale"])
        self.assertEqual(len(payload["cells"]), 16)

    def test_sample_is_immutable(self):
        s = self.make()
        with self.assertRaises(Exception):
            s.cells = ()

    def test_all_equal_cells(self):
        s = self.make([3.7] * 16)
        self.assertEqual(s.spread_mv, 0.0)
        self.assertEqual(s.min_index, 0)


class TestCcountDelta(unittest.TestCase):
    def test_wrap(self):
        self.assertEqual(M.ccount_delta(63, 0), 1)
        self.assertEqual(M.ccount_delta(62, 7), 9)

    def test_no_movement(self):
        self.assertEqual(M.ccount_delta(5, 5), 0)

    def test_unknown(self):
        self.assertIsNone(M.ccount_delta(None, None))


class TestDisconnectDetection(unittest.TestCase):
    def test_exact_signatures_only(self):
        self.assertTrue(M.looks_disconnected([1.500000] * 16))
        self.assertTrue(M.looks_disconnected([1.499850] * 16))
        self.assertFalse(M.looks_disconnected([]))
        self.assertFalse(M.looks_disconnected([1.5] * 15 + [1.49985]))
        # The adjacent code on the 150 uV grid, which is the closest a
        # real frame can get to the all-zero signature.
        self.assertFalse(M.looks_disconnected([1.500150] * 16))
        self.assertFalse(M.looks_disconnected([1.499700] * 16))


if __name__ == "__main__":
    unittest.main()


class TestNtcConversion(unittest.TestCase):
    """Thermistor maths.

    Beta is a CLI parameter, not a constant, because it varies by part. A
    hardcoded beta would give wrong temperatures with a different thermistor.
    """

    def setUp(self):
        self.cfg = M.NtcConfig()

    def test_ten_k_over_ten_k_reads_25c_at_half_vref(self):
        self.assertAlmostEqual(M.ntc_resistance(1.5, self.cfg), 10000.0, places=3)
        self.assertAlmostEqual(M.ntc_celsius(1.5, self.cfg), 25.0, places=6)

    def test_warmer_thermistor_pulls_the_node_down(self):
        hot = M.ntc_celsius(1.2, self.cfg)
        cold = M.ntc_celsius(1.8, self.cfg)
        self.assertGreater(hot, 25.0)
        self.assertLess(cold, 25.0)
        self.assertGreater(hot, cold)

    def test_round_trip_across_a_realistic_band(self):
        for celsius in (-20.0, 0.0, 25.0, 45.0, 60.0, 80.0):
            r = self.cfg.r25 * math.exp(
                self.cfg.beta * (1.0 / (celsius + 273.15) - 1.0 / 298.15))
            volts = self.cfg.vref * r / (self.cfg.series + r)
            self.assertAlmostEqual(M.ntc_celsius(volts, self.cfg), celsius, places=4)

    def test_beta_actually_changes_the_answer(self):
        a = M.ntc_celsius(1.2, M.NtcConfig(beta=3435.0))
        b = M.ntc_celsius(1.2, M.NtcConfig(beta=4000.0))
        self.assertNotAlmostEqual(a, b, places=2)

    def test_series_resistor_actually_changes_the_answer(self):
        a = M.ntc_celsius(1.2, M.NtcConfig(series=10000.0))
        b = M.ntc_celsius(1.2, M.NtcConfig(series=4700.0))
        self.assertNotAlmostEqual(a, b, places=2)

    def test_pulldown_topology(self):
        cfg = M.NtcConfig(pullup=False)
        self.assertAlmostEqual(M.ntc_resistance(1.5, cfg), 10000.0, places=3)
        # Inverted: a warmer thermistor now pushes the node up.
        self.assertGreater(M.ntc_celsius(1.8, cfg), M.ntc_celsius(1.2, cfg))

    def test_rail_voltages_mean_no_sensor_not_a_temperature(self):
        for volts in (0.0, 3.0, -0.5, 3.5):
            self.assertIsNone(M.ntc_resistance(volts, self.cfg), volts)
            self.assertIsNone(M.ntc_celsius(volts, self.cfg), volts)

    def test_absurd_results_are_refused(self):
        # Very close to a rail: mathematically finite, physically nonsense.
        self.assertIsNone(M.ntc_celsius(2.999, self.cfg))
        self.assertIsNone(M.ntc_celsius(0.001, self.cfg))

    def test_nan_and_none(self):
        self.assertIsNone(M.ntc_celsius(float("nan"), self.cfg))
        self.assertIsNone(M.ntc_celsius(None, self.cfg))

    def test_degenerate_config_is_refused_rather_than_dividing_by_zero(self):
        self.assertIsNone(M.ntc_celsius(1.5, M.NtcConfig(vref=0.0)))
        self.assertIsNone(M.ntc_celsius(1.5, M.NtcConfig(series=0.0)))
        self.assertIsNone(M.ntc_celsius(1.5, M.NtcConfig(beta=0.0)))
        self.assertIsNone(M.ntc_celsius(1.5, M.NtcConfig(r25=0.0)))


class TestPackSample(unittest.TestCase):
    """Pack totals and min/max must be withheld for partial, PEC-bad or
    stale cycles."""

    @staticmethod
    def mk(ics=10, present=None, cells_per_ic=9, pec_ok=True, stale=False):
        from tests.packs import pack
        return pack(seq=0, ics=ics, cells_per_ic=cells_per_ic,
                    present=present, pec_ok=pec_ok, stale=stale)

    def test_complete_chain_aggregates(self):
        p = self.mk()
        self.assertTrue(p.complete)
        self.assertFalse(p.partial)
        self.assertEqual(p.cell_count, 90)
        self.assertIsNotNone(p.pack_v)
        self.assertGreater(p.pack_v, 300.0)
        self.assertEqual(p.min_cell[:2], (1, 1))
        self.assertEqual(p.max_cell[:2], (10, 9))

    def test_partial_chain_refuses_to_aggregate(self):
        p = self.mk(present=[1, 2, 3])
        self.assertTrue(p.partial)
        self.assertEqual(p.missing_ics, (4, 5, 6, 7, 8, 9, 10))
        for value in (p.pack_v, p.mean_v, p.spread_mv, p.min_cell, p.max_cell):
            self.assertIsNone(value)

    def test_pec_bad_chain_refuses_to_aggregate(self):
        p = self.mk(pec_ok=False)
        self.assertTrue(p.complete)
        self.assertFalse(p.pec_ok)
        self.assertIsNone(p.pack_v)
        self.assertEqual(len(p.bad_ics), 10)

    def test_warmup_refuses_to_aggregate(self):
        self.assertIsNone(self.mk(stale=True).pack_v)

    def test_masking_is_applied_to_every_view(self):
        p = self.mk(cells_per_ic=9)
        self.assertEqual(len(p.masked(p.readings[0])), 9)
        self.assertEqual(len(p.cell_list()), 90)
        self.assertEqual(len(p.readings[0].cells), M.CELL_COUNT)

    def test_json_is_serialisable_and_carries_the_flags(self):
        import json
        p = self.mk(present=[1, 2], pec_ok=False)
        payload = json.loads(json.dumps(p.to_json()))
        self.assertFalse(payload["complete"])
        self.assertTrue(payload["partial"])
        self.assertFalse(payload["pec_ok"])
        self.assertEqual(payload["missing"], [3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(len(payload["ics"]), 2)
        self.assertEqual(len(payload["ics"][0]["cells"]), 9)

    def test_empty_cycle(self):
        p = self.mk(present=[])
        self.assertFalse(p.complete)
        self.assertFalse(p.partial)
        self.assertFalse(p.pec_ok)
        self.assertIsNone(p.pack_v)
        self.assertEqual(p.cell_count, 0)
