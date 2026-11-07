"""CanSource tests using the frame sequence the F105 controller sends.

``BMS/App/Src/bms_monitor.c`` sends, every 100 ms cycle:

    0x6FF            chain status: state, cycle, invalid-IC bitmap
    0x700 .. 0x704   cells, 4 per frame, 0xFFFF for an invalid chip
    0x720, 0x721     per-IC status, only every BMS_STATUS_EVERY (5) cycles
    0x730 .. 0x734   thermistors, 10 slots per IC, on the same cycles

The BMSCMU test board has 2 x ADBMS6830 with 10 cells each, plus nine
thermistors: RT1-RT5 in IC1's slots 1-5, RT6-RT9 in IC2's slots 1-4.

Frames go straight into ``CanSource._handle``, so no bus or python-can is
needed.
"""

import csv
import os
import struct
import tempfile
import unittest
from types import SimpleNamespace

from bms_monitor.hub import Hub
from bms_monitor.logging_csv import TempCsvLogger
from bms_monitor.sources import can_frames as F
from bms_monitor.sources.can_source import CanSource

ICS = 2
CELLS = 10
STATUS_EVERY = 5

# The firmware's per-IC sentinel for "no die temperature" (INT16_MAX).
F105_TEMP_UNKNOWN = 0x7FFF
# Thermistor slots: INT16_MIN = no probe there, INT16_MAX = probe, no reading.
F105_NO_SENSOR = -0x8000
PROBES_PER_IC = (5, 4)
SLOTS_PER_IC = 10


def probe_celsius(cycle, rt):
    """A value that encodes which cycle and which RT number it came from."""
    return round(20.0 + 0.5 * rt + 0.1 * (cycle % 10), 1)


def temp_frames(cycle, invalid_ics=0):
    slots = []
    rt = 0
    for ic in range(ICS):
        for slot in range(SLOTS_PER_IC):
            if slot >= PROBES_PER_IC[ic]:
                slots.append(F105_NO_SENSOR)
                continue
            rt += 1
            if (invalid_ics >> ic) & 1:
                slots.append(F105_TEMP_UNKNOWN)
            else:
                slots.append(int(round(probe_celsius(cycle, rt) * 10)))
    return [(F.ID_TEMPS_BASE + f, struct.pack("<4h", *slots[4 * f:4 * f + 4]))
            for f in range((len(slots) + 3) // 4)]


def cell_volts(cycle, ic, cell):
    """A value that encodes which cycle, chip and cell it came from."""
    return round(3.0 + 0.01 * (cycle % 50) + 0.0001 * (ic * CELLS + cell), 4)


def ic_frame(index, status_bits=0, cmd_count=7, invalid_run=0, die_raw=250):
    data = struct.pack("<BBBBhH", index, status_bits, cmd_count, invalid_run,
                       die_raw, 0)
    return F.ID_IC_BASE + index, data


def f105_cycle(cycle, invalid_ics=0, ic_bits=None, die_raw=None, status_cycle=None):
    """The frames of one controller cycle, in transmit order."""
    frames = [F.encode_chain_status(F.CHAIN_STATE_RUNNING, cycle & 0xFF, invalid_ics)]
    flat = []
    for ic in range(ICS):
        invalid = (invalid_ics >> ic) & 1
        for cell in range(CELLS):
            flat.append(None if invalid else cell_volts(cycle, ic, cell))
    for f in range((len(flat) + 3) // 4):
        frames.append(F.encode_cells(f, flat[4 * f:4 * f + 4]))
    if status_cycle is None:
        status_cycle = (cycle % STATUS_EVERY) == 0
    if status_cycle:
        for ic in range(ICS):
            bits = (ic_bits or {}).get(ic, 0)
            if (invalid_ics >> ic) & 1:
                bits |= F.IC_PEC_FAIL
            raw = (die_raw or {}).get(ic, 250)
            if bits & F.IC_PEC_FAIL:
                raw = F105_TEMP_UNKNOWN
            frames.append(ic_frame(ic, bits, 0 if bits & F.IC_PEC_FAIL else 7 + cycle % 50,
                                   0, raw))
        frames.extend(temp_frames(cycle, invalid_ics))
    return frames


class F105Harness:
    def __init__(self, temp_csv_logger=None):
        self.source = CanSource(channel="unused", interface="virtual",
                                total_ic=ICS, cells_per_ic=CELLS, warmup=0)
        self.hub = Hub(history=500, temp_csv_logger=temp_csv_logger)
        self.hub.attach(self.source)
        self.packs = []
        self.source.subscribe(lambda kind, payload:
                              self.packs.append(payload) if kind == "sample" else None)

    def feed(self, frames):
        for can_id, data in frames:
            self.source._handle(SimpleNamespace(arbitration_id=can_id, data=data))

    def run(self, first, count, **kwargs):
        for cycle in range(first, first + count):
            self.feed(f105_cycle(cycle, **kwargs))


def cells_of(pack, ic):
    reading = next(r for r in pack.readings if r.ic == ic)
    return list(pack.masked(reading))


class TestF105RoundAssembly(unittest.TestCase):
    def test_each_snapshot_holds_one_cycle_and_only_that_cycle(self):
        h = F105Harness()
        h.run(0, 12)
        # Cycle 0 only opens rounds (the first 0x6FF has nothing to close);
        # each later 0x6FF publishes the cycle before it.
        self.assertEqual(len(h.packs), 11)
        for k, pack in enumerate(h.packs):
            for ic in (1, 2):
                expected = [cell_volts(k, ic - 1, c) for c in range(CELLS)]
                for got, want in zip(cells_of(pack, ic), expected):
                    self.assertAlmostEqual(got, want, places=4,
                                           msg="cycle %d IC%d mixes cycles" % (k, ic))

    def test_twenty_cells_two_chips_and_a_pack_voltage(self):
        h = F105Harness()
        h.run(0, 7)
        pack = h.packs[-1]
        self.assertTrue(pack.usable)
        self.assertEqual(pack.cell_count, 20)
        self.assertEqual([r.ic for r in pack.readings], [1, 2])
        self.assertIsNotNone(pack.pack_v)

    def test_cells_before_the_first_chain_status_are_dropped(self):
        h = F105Harness()
        # Link comes up mid-cycle: the tail of a cycle with no 0x6FF.
        h.feed(f105_cycle(0)[3:])
        h.run(1, 3)
        self.assertEqual(len(h.packs), 2)
        self.assertAlmostEqual(cells_of(h.packs[0], 1)[0], cell_volts(1, 0, 0), places=4)

    def test_a_lost_chain_status_does_not_merge_two_cycles(self):
        h = F105Harness()
        h.run(0, 6)
        before = len(h.packs)
        h.feed(f105_cycle(6)[1:])          # 0x6FF of cycle 6 lost
        h.run(7, 2)
        new = h.packs[before:]
        self.assertGreaterEqual(len(new), 2)
        for pack in new:
            values = cells_of(pack, 1) + cells_of(pack, 2)
            cycles = {round((v - 3.0 - 0.0001 * i) / 0.01) for i, v in enumerate(values)}
            self.assertEqual(len(cycles), 1, "snapshot mixes cycles %s" % sorted(cycles))


class TestF105Validity(unittest.TestCase):
    def test_an_invalid_chip_makes_the_cycle_partial(self):
        h = F105Harness()
        h.run(0, 6)
        h.run(6, 2, invalid_ics=0b10)
        pack = h.packs[-1]
        self.assertTrue(pack.partial)
        self.assertIn(2, pack.missing_ics)
        self.assertIsNone(pack.pack_v)

    def test_a_status_only_pec_failure_keeps_the_good_cells(self):
        h = F105Harness()
        h.run(0, 5)
        # Cycle 5 is a status cycle: IC2's status read fails, cells are fine.
        h.feed(f105_cycle(5, ic_bits={1: F.IC_PEC_FAIL}))
        h.run(6, 2)
        pack = h.packs[-1]
        self.assertTrue(pack.usable, "valid cells were blanked by a status-read failure")
        ic2 = next(r for r in pack.readings if r.ic == 2)
        self.assertTrue(ic2.pec_ok)
        self.assertIs(ic2.status_ok, False)
        self.assertIsNone(ic2.flags)
        self.assertIsNone(ic2.die_temp_c)

    def test_cells_are_shown_before_the_first_per_ic_frame(self):
        h = F105Harness()
        # Cycles 1..4 carry no per-IC frames at all.
        h.run(1, 4)
        self.assertTrue(h.packs)
        reading = h.packs[-1].readings[0]
        self.assertIsNone(reading.status_ok)
        self.assertIsNone(reading.flags, "no report must not read as 'no flags'")


class TestF105Diagnostics(unittest.TestCase):
    def test_per_ic_flags_and_die_temp_reach_the_chain_view(self):
        h = F105Harness()
        h.run(0, 5)
        h.feed(f105_cycle(5, ic_bits={1: F.IC_ANY_UV | F.IC_THSD}, die_raw={0: 312}))
        h.run(6, 1)
        chain = {c["ic"]: c for c in h.hub.stats()["chain"]}
        self.assertEqual(chain[1]["flags"], [])
        self.assertAlmostEqual(chain[1]["die_temp_c"], 31.2)
        self.assertEqual(sorted(chain[2]["flags"]), ["any_uv", "thsd"])
        self.assertIs(chain[2]["status_ok"], True)

    def test_the_f105_unknown_die_temperature_is_not_3276_degrees(self):
        _, data = ic_frame(0, 0, 7, 0, F105_TEMP_UNKNOWN)
        decoded = F.decode(F.ID_IC_BASE, data)
        self.assertIsNone(decoded["die_temp_c"])

    def test_a_real_temperature_never_encodes_as_a_sentinel(self):
        _, data = F.encode_ic_status(0, 0, 0, 0, 5000.0, 0)
        self.assertIsNotNone(F.decode(F.ID_IC_BASE, data)["die_temp_c"])
        _, data = F.encode_ic_status(0, 0, 0, 0, -5000.0, 0)
        self.assertIsNotNone(F.decode(F.ID_IC_BASE, data)["die_temp_c"])


class TestF105Temperatures(unittest.TestCase):
    def sensors(self, h):
        return h.hub.stats()["can_temps"]["sensors"]

    def test_nine_thermistors_arrive_numbered_as_on_the_board(self):
        h = F105Harness()
        h.run(0, 6)
        sensors = self.sensors(h)
        self.assertEqual([s["rt"] for s in sensors], list(range(1, 10)))
        self.assertEqual([s["ic"] for s in sensors], [1] * 5 + [2] * 4)
        for s in sensors:
            self.assertAlmostEqual(s["celsius"], probe_celsius(5, s["rt"]), places=1)

    def test_empty_slots_are_not_listed_as_sensors(self):
        h = F105Harness()
        h.run(0, 6)
        self.assertEqual(len(self.sensors(h)), 9, "the 11 empty slots are not thermistors")

    def test_a_probe_with_no_reading_keeps_its_place(self):
        h = F105Harness()
        h.run(0, 5)
        h.run(5, 1, invalid_ics=0b10)
        sensors = self.sensors(h)
        self.assertEqual([s["rt"] for s in sensors], list(range(1, 10)))
        for s in sensors:
            if s["ic"] == 2:
                self.assertIsNone(s["celsius"], "no reading must not show a number")
            else:
                self.assertIsNotNone(s["celsius"])

    def test_history_holds_one_entry_per_controller_update(self):
        h = F105Harness()
        h.run(0, 16)                       # status cycles 0, 5, 10, 15
        history = h.hub.snapshot()["can_temps_history"]
        self.assertEqual(len(history), 4)
        self.assertEqual(len(history[-1]["celsius"]), 9)
        self.assertAlmostEqual(history[-1]["celsius"][0], probe_celsius(15, 1), places=1)

    def test_each_update_is_one_row_of_the_temps_csv(self):
        path = os.path.join(tempfile.mkdtemp(), "run_temps.csv")
        log = TempCsvLogger(path)
        h = F105Harness(temp_csv_logger=log)
        h.run(0, 16)
        log.close()
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(ln for ln in handle if not ln.startswith("#")))
        self.assertEqual(len(rows), 4)
        self.assertEqual(float(rows[-1]["rt9_ic2_c"]), probe_celsius(15, 9))


class TestLinkError(unittest.TestCase):
    def test_an_old_error_is_cleared_once_frames_flow_again(self):
        h = F105Harness()
        links = []
        h.source.subscribe(lambda kind, payload: links.append(payload) if kind == "link" else None)
        h.source._last_error = "no CAN frames for 15s -- cycling the bus"
        h.source._clear_error()
        self.assertEqual(h.source.status()["last_error"], "")
        self.assertEqual(links[-1]["error"], "")


class TestLegacyLayout(unittest.TestCase):
    """A controller with no 0x6FF: rounds are delimited by 0x700."""

    def test_frame_zero_of_the_next_round_is_not_mixed_in(self):
        h = F105Harness()
        for cycle in range(6):
            frames = f105_cycle(cycle, status_cycle=True)[1:]   # drop 0x6FF
            h.feed(frames)
        self.assertGreaterEqual(len(h.packs), 4)
        for k, pack in enumerate(h.packs):
            self.assertAlmostEqual(cells_of(pack, 1)[0], cell_volts(k, 0, 0), places=4)
            self.assertAlmostEqual(cells_of(pack, 2)[-1], cell_volts(k, 1, CELLS - 1),
                                   places=4)


if __name__ == "__main__":
    unittest.main()
