"""Tests for the ADI text parser, including golden fixtures of firmware output.

The parser must never raise and must never turn a bad line into a good one.
"""

import math
import os
import unittest

from bms_monitor.model import (
    CellRecord,
    EventRecord,
    StatusRecord,
    UnparsedRecord,
    looks_disconnected,
)
from bms_monitor.parsing.adi_text import parse_line
from bms_monitor.parsing.lines import LineAssembler

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def parse_all(raw):
    out = []
    assembler = LineAssembler()
    for text, truncated in assembler.feed(raw):
        out.append(parse_line(text, truncated=truncated))
    return out


def cells16(values, ccount=63, pec=0, ic=1):
    body = ",".join("C%d=%.6fV" % (i + 1, v) for i, v in enumerate(values))
    return "IC%d:%s,CCount:%d,PECError:%d" % (ic, body, ccount, pec)


class TestGoldenCellLine(unittest.TestCase):
    def test_canonical_16_cell_line(self):
        line = cells16([3.700 + 0.001 * i for i in range(16)])
        rec = parse_line(line)
        self.assertIsInstance(rec, CellRecord)
        self.assertEqual(rec.ic, 1)
        self.assertEqual(rec.kind, "cell")
        self.assertTrue(rec.complete)
        self.assertEqual(rec.ccount, 63)
        self.assertEqual(rec.pec_error, 0)
        self.assertTrue(rec.pec_ok)
        self.assertEqual(len(rec.values), 16)
        self.assertAlmostEqual(rec.values[0], 3.700)
        self.assertAlmostEqual(rec.values[15], 3.715)

    def test_pec_error_1_is_carried_through(self):
        rec = parse_line(cells16([3.7] * 16, pec=1))
        self.assertTrue(rec.complete)          # complete AND bad: both true
        self.assertFalse(rec.pec_ok)
        self.assertEqual(rec.pec_error, 1)

    def test_negative_voltage_is_valid(self):
        # c_codes is int16_t, so getVoltage() spans -3.4155 .. +6.4150 V.
        rec = parse_line(cells16([-1.234] + [3.7] * 15))
        self.assertTrue(rec.complete)
        self.assertAlmostEqual(rec.values[0], -1.234)

    def test_full_negative_and_positive_extremes(self):
        rec = parse_line(cells16([-3.4152] + [6.4150] * 15))
        self.assertTrue(rec.complete)

    def test_out_of_range_voltage_is_rejected(self):
        rec = parse_line(cells16([99.0] + [3.7] * 15))
        self.assertFalse(rec.complete)
        self.assertIsNone(rec.values[0])

    def test_high_ic_index(self):
        rec = parse_line(cells16([3.7] * 16, ic=8))
        self.assertEqual(rec.ic, 8)


class TestMalformedCellLines(unittest.TestCase):
    def test_missing_ccount(self):
        body = ",".join("C%d=3.700000V" % (i + 1) for i in range(16))
        rec = parse_line("IC1:%s,PECError:0" % body)
        self.assertIsInstance(rec, CellRecord)
        self.assertFalse(rec.complete)
        self.assertIsNone(rec.ccount)

    def test_missing_pecerror(self):
        body = ",".join("C%d=3.700000V" % (i + 1) for i in range(16))
        rec = parse_line("IC1:%s,CCount:5" % body)
        self.assertFalse(rec.complete)

    def test_truncated_mid_number(self):
        rec = parse_line("IC1:C1=3.712000V,C2=3.70")
        self.assertIsInstance(rec, CellRecord)
        self.assertFalse(rec.complete)
        self.assertAlmostEqual(rec.values[0], 3.712)
        self.assertIsNone(rec.values[1])

    def test_truncated_before_first_equals(self):
        rec = parse_line("IC1:C1")
        self.assertIsInstance(rec, UnparsedRecord)

    def test_partial_cell_set(self):
        rec = parse_line("IC1:C1=3.7V,C2=3.7V,CCount:1,PECError:0")
        self.assertFalse(rec.complete)          # 16 expected, 2 present

    def test_nan_is_rejected(self):
        # float("nan") parses happily and would poison every min/max.
        rec = parse_line("IC1:C1=nanV,C2=3.700000V,CCount:1,PECError:0")
        self.assertFalse(rec.complete)
        self.assertIsNone(rec.values[0])

    def test_inf_is_rejected(self):
        for token in ("inf", "-inf", "Infinity", "1e400"):
            rec = parse_line("IC1:C1=%sV,CCount:1,PECError:0" % token)
            self.assertFalse(rec.complete)
            self.assertIsNone(rec.values[0], token)

    def test_underscore_numeric_literal_is_rejected(self):
        rec = parse_line("IC1:C1=3_7V,CCount:1,PECError:0")
        self.assertIsNone(rec.values[0])

    def test_garbage_prefix_still_parses(self):
        line = "\x1b[2Kgarbage " + cells16([3.7] * 16)
        rec = parse_line(line)
        self.assertIsInstance(rec, CellRecord)
        self.assertTrue(rec.complete)

    def test_out_of_range_pec_value_is_dropped(self):
        rec = parse_line(cells16([3.7] * 16, pec=7))
        self.assertIsNone(rec.pec_error)
        self.assertFalse(rec.complete)

    def test_channel_index_out_of_range_is_ignored(self):
        rec = parse_line("IC1:C99=3.7V,C1=3.7V,CCount:1,PECError:0")
        self.assertIsNone(rec.values[15])
        self.assertAlmostEqual(rec.values[0], 3.7)


class TestDisconnectedSignatures(unittest.TestCase):
    """What the firmware prints with no chip connected: all-zero codes read
    1.500000 V and all-0xFF codes read 1.499850 V."""

    def test_all_zero_code_prints_1_500000(self):
        rec = parse_line(cells16([1.500000] * 16, pec=1))
        self.assertTrue(rec.complete)
        self.assertFalse(rec.pec_ok)
        self.assertTrue(looks_disconnected(rec.values))

    def test_all_ff_code_prints_1_499850(self):
        rec = parse_line(cells16([1.499850] * 16, pec=1))
        self.assertTrue(looks_disconnected(rec.values))

    def test_real_pack_is_not_flagged_as_disconnected(self):
        self.assertFalse(looks_disconnected([3.7] * 16))

    def test_almost_but_not_quite_is_not_disconnected(self):
        values = [1.500000] * 15 + [1.501000]
        self.assertFalse(looks_disconnected(values))

    def test_mixed_signatures_are_not_disconnected(self):
        self.assertFalse(looks_disconnected([1.500000] * 8 + [1.499850] * 8))


class TestOtherChannels(unittest.TestCase):
    def test_avg_cell(self):
        body = ",".join("AC%d=3.700000V" % (i + 1) for i in range(16))
        rec = parse_line("IC1:%s,CCount:3,PECError:0" % body)
        self.assertEqual(rec.kind, "acell")
        self.assertTrue(rec.complete)

    def test_fcell_and_scell(self):
        for prefix, kind in (("FC", "fcell"), ("S", "scell")):
            body = ",".join("%s%d=3.700000V" % (prefix, i + 1) for i in range(16))
            rec = parse_line("IC1:%s,CCount:3,PECError:0" % body)
            self.assertEqual(rec.kind, kind)
            self.assertTrue(rec.complete)

    def test_aux_with_vmv_and_vplus(self):
        body = ",".join("AUX%d=1.500000V" % (i + 1) for i in range(10))
        line = "IC1:%s,VMV:30.000000V,V+:30.000000V,CCount:4,PECError:0" % body
        rec = parse_line(line)
        self.assertEqual(rec.kind, "aux")
        self.assertEqual(len(rec.values), 12)
        self.assertTrue(rec.complete)

    def test_raux(self):
        body = ",".join("RAUX%d=1.500000V" % (i + 1) for i in range(10))
        rec = parse_line("IC1:%s,CCount:4,PECError:0" % body)
        self.assertEqual(rec.kind, "raux")
        self.assertTrue(rec.complete)


class TestNonMeasurementLines(unittest.TestCase):
    def test_ack(self):
        rec = parse_line("Enter cmd:4")
        self.assertIsInstance(rec, EventRecord)
        self.assertEqual(rec.kind, "ack")
        self.assertEqual(rec.value, 4)

    def test_ic_header_on_its_own_line(self):
        # printStatus() newlines after the IC header; printVoltages() does
        # not. A parser assuming a uniform layout breaks on one of them.
        rec = parse_line("IC1:")
        self.assertIsInstance(rec, EventRecord)
        self.assertEqual(rec.kind, "ic_header")
        self.assertEqual(rec.value, 1)

    def test_conversion_done(self):
        for text in ("Cell conversion completed", "Aux voltage conversion completed",
                     "RAux voltage conversion completed", "S-Voltage conversion completed",
                     "Avg Cell voltage conversion completed", "F Cell voltage conversion completed"):
            self.assertEqual(parse_line(text).kind, "conversion_done", text)

    def test_registers_cleared_is_surfaced(self):
        # We never send 17-20, so this means something else is on the port.
        rec = parse_line("Cell Registers Cleared")
        self.assertEqual(rec.kind, "registers_cleared")

    def test_incorrect_option(self):
        self.assertEqual(parse_line("Incorrect Option").kind, "firmware_error")

    def test_menu_lines(self):
        for text in ("List of ADBMS6830 Command:", "Read Cell Voltages: 4",
                     "Start S-Voltage Conversion: 5", "Print '0' for menu",
                     "Please enter command:"):
            self.assertEqual(parse_line(text).kind, "menu", text)

    def test_adc_time(self):
        self.assertEqual(parse_line("Adc Conversion Time = 8.000000ms").kind, "adc_time")

    def test_blank(self):
        self.assertEqual(parse_line("").kind, "blank")
        self.assertEqual(parse_line("   ").kind, "blank")

    def test_none_input(self):
        self.assertIsInstance(parse_line(None), UnparsedRecord)

    def test_unknown_text(self):
        self.assertIsInstance(parse_line("who knows"), UnparsedRecord)


class TestStatusLines(unittest.TestCase):
    def test_status_header(self):
        rec = parse_line("Status A:")
        self.assertIsInstance(rec, StatusRecord)
        self.assertEqual(rec.group, "A")

    def test_status_a_payload_utf8_degree(self):
        rec = parse_line("VREF2:3.000000V, VREF3:3.000000V, ITMP:31.000000°C")
        self.assertIsInstance(rec, StatusRecord)
        self.assertAlmostEqual(rec.fields["VREF2"], 3.0)
        self.assertAlmostEqual(rec.fields["ITMP"], 31.0)

    def test_itmp_as_latin1_bytes_through_the_assembler(self):
        records = parse_all(b"ITMP:31.000000\xb0C\n")
        self.assertAlmostEqual(records[0].fields["ITMP"], 31.0)

    def test_itmp_as_utf8_bytes_through_the_assembler(self):
        records = parse_all("ITMP:31.000000°C\n".encode("utf-8"))
        self.assertAlmostEqual(records[0].fields["ITMP"], 31.0)

    def test_status_c_flags(self):
        rec = parse_line("OSCCHK:0x0, TMODCHK:0x0, THSD:0x1, SLEEP:0x0, "
                         "SPIFLT:0x0, COMP:0x0, VDEL:0x0, VDE:0x0")
        self.assertEqual(rec.fields["THSD"], 1)
        self.assertEqual(rec.fields["SPIFLT"], 0)

    def test_status_d_per_cell_flags(self):
        line = ", ".join("C%dUV:0x%d" % (i + 1, 1 if i == 3 else 0) for i in range(16))
        rec = parse_line(line)
        self.assertEqual(rec.fields["C4UV"], 1)
        self.assertEqual(rec.fields["C1UV"], 0)

    def test_status_b_supplies(self):
        rec = parse_line("VA:5.000000V, VD:3.300000V, VR4K:3.000000V")
        self.assertAlmostEqual(rec.fields["VA"], 5.0)
        self.assertAlmostEqual(rec.fields["VD"], 3.3)

    def test_absurd_temperature_is_rejected(self):
        rec = parse_line("ITMP:99999.000000°C")
        self.assertNotIn("ITMP", getattr(rec, "fields", {}))


class TestFixtures(unittest.TestCase):
    def test_menu_fixture(self):
        records = parse_all(load("menu.txt"))
        kinds = {getattr(r, "kind", None) for r in records}
        self.assertIn("menu", kinds)
        self.assertTrue(all(not isinstance(r, UnparsedRecord) for r in records),
                        [r for r in records if isinstance(r, UnparsedRecord)])

    def test_cells_normal_fixture(self):
        records = parse_all(load("cells_normal.txt"))
        cells = [r for r in records if isinstance(r, CellRecord)]
        self.assertEqual(len(cells), 3)
        for rec in cells:
            self.assertTrue(rec.complete)
            self.assertEqual(len(rec.values), 16)
            for v in rec.values:
                self.assertTrue(3.5 < v < 3.9, v)

    def test_cells_disconnected_fixture(self):
        records = parse_all(load("cells_disconnected.txt"))
        cells = [r for r in records if isinstance(r, CellRecord)]
        self.assertTrue(cells)
        for rec in cells:
            self.assertEqual(rec.pec_error, 1)
            self.assertTrue(looks_disconnected(rec.values))

    def test_status_fixture(self):
        records = parse_all(load("status.txt"))
        merged = {}
        for rec in records:
            if isinstance(rec, StatusRecord):
                merged.update(rec.fields)
        for key in ("VREF2", "ITMP", "VA", "VD", "THSD", "SPIFLT", "CSFLT",
                    "OSCCHK", "C1UV", "C16OV"):
            self.assertIn(key, merged)

    def test_aux_fixture(self):
        records = parse_all(load("aux_voltages.txt"))
        kinds = [r.kind for r in records if isinstance(r, CellRecord)]
        self.assertIn("aux", kinds)
        self.assertIn("raux", kinds)

    def test_config_fixture_produces_no_cell_records(self):
        records = parse_all(load("config.txt"))
        self.assertFalse([r for r in records if isinstance(r, CellRecord)])

    def test_conversion_fixture(self):
        records = parse_all(load("conversion.txt"))
        done = [r for r in records if getattr(r, "kind", "") == "conversion_done"]
        self.assertEqual(len(done), 6)

    def test_adversarial_fixture_never_raises_and_yields_no_bad_data(self):
        records = parse_all(load("adversarial.txt"))
        self.assertTrue(records)
        for rec in records:
            if isinstance(rec, CellRecord):
                for v in rec.values:
                    if v is not None:
                        self.assertTrue(math.isfinite(v))
                if rec.complete:
                    self.assertEqual(len(rec.values), 16)


class TestNeverRaises(unittest.TestCase):
    def test_random_text(self):
        import random
        rng = random.Random(11)
        alphabet = "0123456789.,:=VIC+-ABCDEFabcdef \t\x1b°nifty"
        for _ in range(3000):
            n = rng.randrange(0, 90)
            parse_line("".join(rng.choice(alphabet) for _ in range(n)))

    def test_mutated_golden_lines(self):
        import random
        rng = random.Random(12)
        base = cells16([3.7] * 16)
        for _ in range(3000):
            chars = list(base)
            for _ in range(rng.randrange(1, 6)):
                at = rng.randrange(len(chars))
                op = rng.randrange(3)
                if op == 0:
                    chars[at] = rng.choice("0123456789.,:=V ")
                elif op == 1:
                    del chars[at]
                else:
                    chars.insert(at, rng.choice("0123456789.,:=V "))
            rec = parse_line("".join(chars))
            if isinstance(rec, CellRecord) and rec.complete:
                for v in rec.values:
                    self.assertTrue(math.isfinite(v))


if __name__ == "__main__":
    unittest.main()
