"""CSV logging and export tests.

PEC-bad rows must still be written, with pec_ok=0 and blank voltages, so a
noisy loom shows up in the log instead of looking clean.
"""

import csv
import os
import tempfile
import unittest

from bms_monitor.logging_csv import (CsvLogger, CsvReader, TempCsvLogger, render_csv,
                                     temps_path_for)
from bms_monitor.model import CELL_COUNT

from .packs import pack


def sample(seq, pec_ok=True, stale=False, ccount=5, jump=False):
    return pack(seq=seq, ics=1, cells_per_ic=CELL_COUNT, pec_ok=pec_ok,
                stale=stale, ccount=ccount, jump=jump)


class CsvTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "log.csv")

    def rows(self):
        """Dict rows, skipping the leading ``# schema`` comment line that
        lets CsvReader refuse a file it does not understand."""
        with open(self.path, newline="", encoding="utf-8") as handle:
            lines = [ln for ln in handle.read().splitlines() if not ln.startswith("#")]
        return list(csv.DictReader(lines))


class TestCsvLogger(CsvTestCase):
    def test_header_and_a_good_row(self):
        log = CsvLogger(self.path)
        log.write(sample(0))
        log.close()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["pec_ok"], "1")
        self.assertEqual(row["stale"], "0")
        self.assertEqual(row["ic1_ccount"], "5")
        self.assertAlmostEqual(float(row["ic1_c1"]), 3.700)
        self.assertAlmostEqual(float(row["ic1_c16"]), 3.715)
        self.assertAlmostEqual(float(row["spread_mv"]), 15.0, places=3)

    def test_pec_bad_rows_are_written_with_blank_values(self):
        log = CsvLogger(self.path)
        log.write(sample(0, pec_ok=True))
        log.write(sample(1, pec_ok=False))
        log.write(sample(2, pec_ok=True))
        log.close()
        rows = self.rows()
        self.assertEqual(len(rows), 3, "a bad frame must be logged, not hidden")
        bad = rows[1]
        self.assertEqual(bad["pec_ok"], "0")
        for i in range(1, CELL_COUNT + 1):
            self.assertEqual(bad["ic1_c%d" % i], "")
        for key in ("pack_v", "min_v", "max_v", "spread_mv"):
            self.assertEqual(bad[key], "")
        # Metadata is still there, so you can see when it happened.
        self.assertNotEqual(bad["t_wall"], "")
        self.assertEqual(bad["seq"], "1")

    def test_stale_rows_are_written_and_flagged(self):
        log = CsvLogger(self.path)
        log.write(sample(0, stale=True))
        log.close()
        self.assertEqual(self.rows()[0]["stale"], "1")

    def test_ccount_jump_column(self):
        log = CsvLogger(self.path)
        log.write(sample(0, jump=True))
        log.close()
        self.assertEqual(self.rows()[0]["ic1_jump"], "1")

    def test_missing_ccount_is_blank_not_none(self):
        log = CsvLogger(self.path)
        log.write(sample(0, ccount=None))
        log.close()
        self.assertEqual(self.rows()[0]["ic1_ccount"], "")

    def test_appending_does_not_duplicate_the_header(self):
        log = CsvLogger(self.path)
        log.write(sample(0))
        log.close()
        log = CsvLogger(self.path)
        log.write(sample(1))
        log.close()
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(text.count("t_wall"), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_creates_missing_directories(self):
        path = os.path.join(self.tmp, "a", "b", "log.csv")
        log = CsvLogger(path)
        log.write(sample(0))
        log.close()
        self.assertTrue(os.path.exists(path))

    def test_row_count_is_tracked(self):
        log = CsvLogger(self.path)
        for i in range(37):
            log.write(sample(i))
        log.close()
        self.assertEqual(log.rows, 37)

    def test_writing_after_close_is_a_no_op_not_a_crash(self):
        log = CsvLogger(self.path)
        log.write(sample(0))
        log.close()
        log.write(sample(1))            # must not raise
        self.assertEqual(len(self.rows()), 1)

    def test_double_close(self):
        log = CsvLogger(self.path)
        log.close()
        log.close()

    def test_row_size_supports_the_readme_size_claim(self):
        """The README says ~8 MB/hour at 10 Hz (36000 rows/hour). This fails
        if a schema change pushes the real size well past that.
        """
        log = CsvLogger(self.path)
        for i in range(100):
            log.write(sample(i))
        log.close()
        per_row = os.path.getsize(self.path) / 100.0
        hourly_mb = per_row * 36000 / 1e6
        self.assertLess(per_row, 300, "row is %.0f bytes" % per_row)
        self.assertLess(hourly_mb, 12.0, "%.1f MB/hour" % hourly_mb)


class TestMultiIcCsvRoundTrip(CsvTestCase):
    """The CSV is the replay format, so it has to survive a round trip,
    including cycles that carry no numbers."""

    def test_ten_ic_round_trip(self):
        log = CsvLogger(self.path)
        written = []
        for i in range(30):
            p = pack(seq=i, ics=10, cells_per_ic=9)
            written.append(p)
            log.write(p)
        log.close()

        reader = CsvReader(self.path)
        back = reader.load()
        self.assertEqual(reader.expected_ics, 10)
        self.assertEqual(reader.cells_per_ic, 9)
        self.assertEqual(len(back), 30)
        self.assertEqual(reader.bad_rows, 0)
        for original, restored in zip(written, back):
            self.assertEqual(restored.present_ics, original.present_ics)
            self.assertEqual(restored.cells_per_ic, 9)
            self.assertAlmostEqual(restored.pack_v, original.pack_v, places=4)
            self.assertAlmostEqual(restored.spread_mv, original.spread_mv, places=2)
            self.assertEqual(restored.min_cell[:2], original.min_cell[:2])
            self.assertEqual(restored.max_cell[:2], original.max_cell[:2])

    def test_partial_cycle_round_trips_as_partial(self):
        log = CsvLogger(self.path)
        log.write(pack(seq=0, ics=10, cells_per_ic=9))
        log.write(pack(seq=1, ics=10, cells_per_ic=9, present=[1, 2, 3]))
        log.close()
        back = CsvReader(self.path).load()
        self.assertFalse(back[0].partial)
        self.assertTrue(back[1].partial)
        self.assertEqual(back[1].missing_ics, (4, 5, 6, 7, 8, 9, 10))
        self.assertIsNone(back[1].pack_v)

    def test_pec_bad_cycle_round_trips_as_unusable(self):
        log = CsvLogger(self.path)
        log.write(pack(seq=0, ics=2, cells_per_ic=9, pec_ok=False))
        log.close()
        back = CsvReader(self.path).load()
        self.assertFalse(back[0].usable)
        self.assertFalse(back[0].pec_ok)
        self.assertIsNone(back[0].pack_v)

    def test_mixed_shape_append_is_refused(self):
        log = CsvLogger(self.path)
        log.write(pack(seq=0, ics=2, cells_per_ic=9))
        log.close()
        second = CsvLogger(self.path)
        with self.assertRaises(ValueError):
            second.write(pack(seq=1, ics=10, cells_per_ic=9))
        second.close()

    def test_reader_rejects_a_foreign_csv(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("time,voltage\n1,2\n")
        with self.assertRaises(ValueError):
            CsvReader(self.path).load()

    def test_reader_rejects_a_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            CsvReader(self.path + ".nope").load()


class TestRenderCsv(CsvTestCase):
    """The Export button in the UI.

    An export and a ``--csv`` log of the same samples must be the same bytes,
    otherwise exported files might not replay.
    """

    def packs(self):
        return [pack(seq=i, ics=2, cells_per_ic=9, pec_ok=(i != 3))
                for i in range(6)]

    def test_export_is_byte_identical_to_a_disk_log(self):
        packs = self.packs()
        log = CsvLogger(self.path)
        for p in packs:
            log.write(p)
        log.close()
        with open(self.path, "rb") as handle:
            on_disk = handle.read()
        self.assertEqual(render_csv(packs).encode("utf-8"), on_disk)

    def test_export_round_trips_through_the_reader(self):
        packs = self.packs()
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(render_csv(packs))
        back = CsvReader(self.path).load()
        self.assertEqual(len(back), len(packs))
        self.assertEqual([p.seq for p in back], [p.seq for p in packs])
        self.assertEqual(back[0].expected_ics, 2)
        self.assertEqual(back[0].cells_per_ic, 9)

    def test_pec_bad_cycles_survive_the_export(self):
        # Same rule as the disk logger: bad cycles are kept and flagged,
        # not dropped.
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(render_csv(self.packs()))
        back = CsvReader(self.path).load()
        self.assertFalse(back[3].pec_ok)
        self.assertTrue(back[0].pec_ok)

    def test_no_samples_renders_empty_not_a_bare_header(self):
        # The web layer turns "" into a 409 with a reason. A header-only
        # file would download as a success and confuse whoever opened it.
        self.assertEqual(render_csv([]), "")

    def test_rows_of_a_different_chain_shape_are_skipped(self):
        # One header describes one column layout, so a session whose chain
        # length changed cannot be one file. Newest shape wins.
        mixed = [pack(seq=0, ics=1, cells_per_ic=9),
                 pack(seq=1, ics=2, cells_per_ic=9),
                 pack(seq=2, ics=2, cells_per_ic=9)]
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(render_csv(mixed))
        back = CsvReader(self.path).load()
        self.assertEqual([p.seq for p in back], [1, 2])


def temps_payload(values, t=12.5):
    """A complete CAN temperature update: RT1.. with their chips."""
    sensors = []
    for n, celsius in enumerate(values, start=1):
        sensors.append({"rt": n, "ic": 1 if n <= 5 else 2, "celsius": celsius})
    return {"source": "can", "complete": True, "t": t, "t_wall": 1700000000.0 + t,
            "sensors": sensors}


class TestTempCsvLogger(CsvTestCase):
    """Thermistors from the controller go to ``<log>_temps.csv``.

    It's a separate file because the sample CSV's columns are its replay
    schema, and adding temperature columns would break reading old logs.
    """

    def test_the_temps_file_sits_next_to_the_sample_log(self):
        self.assertEqual(temps_path_for("/x/run1.csv"), "/x/run1_temps.csv")
        self.assertEqual(temps_path_for("/x/run1"), "/x/run1_temps.csv")

    def test_one_row_per_update_with_a_column_per_thermistor(self):
        log = TempCsvLogger(self.path)
        log.write(temps_payload([21.0 + n for n in range(9)], t=1.0))
        log.write(temps_payload([22.0 + n for n in range(9)], t=1.5))
        log.close()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(list(rows[0])[:2], ["t_wall", "t_mono"])
        self.assertEqual(list(rows[0])[2:], ["rt%d_ic%d_c" % (n, 1 if n <= 5 else 2)
                                             for n in range(1, 10)])
        self.assertEqual(rows[1]["t_mono"], "1.500")
        self.assertEqual(rows[1]["t_wall"], "1700000001.500")
        self.assertEqual(rows[1]["rt9_ic2_c"], "30.0")

    def test_a_probe_with_no_reading_is_blank_not_zero(self):
        log = TempCsvLogger(self.path)
        log.write(temps_payload([21.0, None, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0]))
        log.close()
        self.assertEqual(self.rows()[0]["rt2_ic1_c"], "")

    def test_no_file_until_a_temperature_arrives(self):
        log = TempCsvLogger(self.path)
        log.close()
        self.assertFalse(os.path.exists(self.path),
                         "a serial session with no CAN temps must not leave an empty file")

    def test_a_partial_update_is_not_logged(self):
        log = TempCsvLogger(self.path)
        partial = temps_payload([21.0] * 4)
        partial["complete"] = False
        log.write(partial)
        log.close()
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
