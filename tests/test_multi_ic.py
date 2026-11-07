"""Daisy-chain assembly tests.

The car has ten ADBMS6830s on one isoSPI chain (five segment boards, 90
cells). ``printVoltages()`` prints one line per IC and the pipeline joins them
into one poll cycle, including when a chip goes silent, fails PEC, or doesn't
match ``--ics``.
"""

import time
import unittest

from bms_monitor.model import CELL_COUNT, PackSample
from bms_monitor.sources.base import IngestPipeline
from bms_monitor.sources.command_gate import encode_command


def line(ic, ccount=9, pec=0, cells=None, count=CELL_COUNT, base=3.70):
    """One printVoltages() line.

    Always 16 channels, because the firmware always prints 16 no matter how
    many are populated. Masking to the populated count is the tool's job.
    """
    cells = cells if cells is not None else [base + 0.001 * i for i in range(count)]
    if len(cells) < CELL_COUNT:
        cells = list(cells) + [cells[-1]] * (CELL_COUNT - len(cells))
    body = ",".join("C%d=%.6fV" % (i + 1, v) for i, v in enumerate(cells))
    return "IC%d:%s,CCount:%d,PECError:%d\n\n" % (ic, body, ccount, pec)


class PipelineHarness:
    def __init__(self, ics=2, cells_per_ic=9, warmup=0, cycle_timeout=0.2):
        self.packs = []
        self.events = []
        self.aux = []
        self.terminators = 0
        self.pipeline = IngestPipeline(
            on_pack=self.packs.append,
            on_line=lambda text: None,
            on_status=lambda fields: None,
            on_event=self.events.append,
            on_aux=self.aux.append,
            on_terminator=self._term,
            warmup=warmup, expected_ics=ics, cells_per_ic=cells_per_ic,
            cycle_timeout=cycle_timeout,
        )

    def _term(self):
        self.terminators += 1

    def feed(self, text):
        self.pipeline.feed_bytes(text.encode())


class TestCycleAssembly(unittest.TestCase):
    def test_two_ic_lines_become_one_cycle(self):
        h = PipelineHarness(ics=2, cells_per_ic=9)
        h.feed("Enter cmd:4\n" + line(1) + line(2))
        self.assertEqual(len(h.packs), 1)
        p = h.packs[0]
        self.assertEqual(p.present_ics, (1, 2))
        self.assertTrue(p.complete)
        self.assertEqual(p.cell_count, 18)
        self.assertTrue(p.usable)

    def test_ten_ic_chain_becomes_one_cycle_of_ninety_cells(self):
        h = PipelineHarness(ics=10, cells_per_ic=9)
        h.feed("Enter cmd:4\n" + "".join(line(ic) for ic in range(1, 11)))
        self.assertEqual(len(h.packs), 1)
        p = h.packs[0]
        self.assertEqual(p.present_ics, tuple(range(1, 11)))
        self.assertEqual(p.cell_count, 90)
        self.assertTrue(p.complete)
        # 90 cells of ~3.7 V. A tenfold or tenth here would mean the chain
        # assembly is wrong.
        self.assertGreater(p.pack_v, 300.0)
        self.assertLess(p.pack_v, 360.0)

    def test_the_pacer_is_not_released_until_the_whole_chain_answers(self):
        """With a chain still printing, the blank line after IC1 must not
        let the next command go out."""
        h = PipelineHarness(ics=3, cells_per_ic=9)
        h.feed("Enter cmd:4\n" + line(1))
        self.assertEqual(h.terminators, 0)
        h.feed(line(2))
        self.assertEqual(h.terminators, 0)
        h.feed(line(3))
        self.assertGreaterEqual(h.terminators, 1)

    def test_single_ic_still_releases_the_pacer_immediately(self):
        h = PipelineHarness(ics=1, cells_per_ic=16)
        h.feed("Enter cmd:4\n" + line(1, count=16))
        self.assertEqual(len(h.packs), 1)
        self.assertGreaterEqual(h.terminators, 1)

    def test_a_repeated_ic_index_starts_a_new_cycle(self):
        h = PipelineHarness(ics=3, cells_per_ic=9)
        h.feed(line(1) + line(2) + line(1) + line(2) + line(3))
        self.assertEqual(len(h.packs), 2)
        self.assertTrue(h.packs[0].partial)
        self.assertEqual(h.packs[0].missing_ics, (3,))
        self.assertTrue(h.packs[1].complete)

    def test_bytes_fed_one_at_a_time_produce_the_same_cycles(self):
        text = "Enter cmd:4\n" + "".join(line(ic) for ic in range(1, 11))
        whole = PipelineHarness(ics=10, cells_per_ic=9)
        whole.feed(text)
        single = PipelineHarness(ics=10, cells_per_ic=9)
        for ch in text:
            single.feed(ch)
        self.assertEqual(len(whole.packs), len(single.packs))
        self.assertEqual([p.present_ics for p in whole.packs],
                         [p.present_ics for p in single.packs])


class TestDegradedChain(unittest.TestCase):
    def test_a_silent_ic_makes_the_cycle_partial_after_the_timeout(self):
        h = PipelineHarness(ics=3, cells_per_ic=9, cycle_timeout=0.05)
        h.feed(line(1) + line(2))
        self.assertEqual(h.packs, [])
        time.sleep(0.1)
        h.pipeline.tick()
        self.assertEqual(len(h.packs), 1)
        p = h.packs[0]
        self.assertTrue(p.partial)
        self.assertFalse(p.complete)
        self.assertEqual(p.missing_ics, (3,))

    def test_a_partial_cycle_has_no_pack_voltage(self):
        """A pack total from part of a chain is a wrong number that looks
        like a right one."""
        h = PipelineHarness(ics=10, cells_per_ic=9, cycle_timeout=0.05)
        h.feed("".join(line(ic) for ic in range(1, 8)))
        time.sleep(0.1)
        h.pipeline.tick()
        p = h.packs[0]
        self.assertIsNone(p.pack_v)
        self.assertIsNone(p.spread_mv)
        self.assertIsNone(p.min_cell)
        self.assertIsNone(p.mean_v)
        self.assertFalse(p.usable)

    def test_one_bad_pec_poisons_only_the_cycle_not_the_chain(self):
        h = PipelineHarness(ics=3, cells_per_ic=9)
        h.feed(line(1) + line(2, pec=1) + line(3))
        p = h.packs[0]
        self.assertTrue(p.complete)
        self.assertFalse(p.pec_ok)
        self.assertFalse(p.usable)
        self.assertEqual(p.bad_ics, (2,))
        self.assertIsNone(p.pack_v)
        # The next clean cycle is usable again.
        h.feed(line(1) + line(2) + line(3))
        self.assertTrue(h.packs[1].usable)

    def test_an_ic_reporting_the_no_device_signature_is_flagged(self):
        h = PipelineHarness(ics=2, cells_per_ic=9)
        h.feed(line(1) + line(2, pec=1, cells=[1.500000] * CELL_COUNT))
        p = h.packs[0]
        self.assertTrue(p.readings[1].stale)
        self.assertFalse(p.usable)

    def test_all_ics_reporting_no_device_makes_the_cycle_stale(self):
        h = PipelineHarness(ics=2, cells_per_ic=9)
        h.feed(line(1, pec=1, cells=[1.500000] * CELL_COUNT)
               + line(2, pec=1, cells=[1.499850] * CELL_COUNT))
        self.assertTrue(h.packs[0].stale)
        self.assertFalse(h.packs[0].usable)

    def test_an_unexpected_ic_is_surfaced_not_plotted(self):
        h = PipelineHarness(ics=2, cells_per_ic=9)
        h.feed(line(1) + line(2) + line(7))
        kinds = [e.kind for e in h.events]
        self.assertIn("unexpected_ic", kinds)
        for p in h.packs:
            self.assertLessEqual(max(p.present_ics), 2)

    def test_per_ic_command_counters_are_tracked_separately(self):
        h = PipelineHarness(ics=2, cells_per_ic=9)
        h.feed(line(1, ccount=9) + line(2, ccount=40))
        h.feed(line(1, ccount=18) + line(2, ccount=49))
        # Each IC advanced by 9; neither should look like a jump even
        # though their absolute values differ wildly.
        self.assertFalse(h.packs[1].any_jump)

    def test_one_ic_with_a_stalled_counter_is_flagged(self):
        h = PipelineHarness(ics=2, cells_per_ic=9)
        h.feed(line(1, ccount=9) + line(2, ccount=9))
        h.feed(line(1, ccount=18) + line(2, ccount=9))
        jumped = [r.ic for r in h.packs[1].readings if r.ccount_jump]
        self.assertEqual(jumped, [2])


class TestMasking(unittest.TestCase):
    def test_unpopulated_channels_are_masked_out(self):
        """With 9 of 16 channels used per IC, showing the rest would put 70
        indeterminate cells on screen out of 160."""
        h = PipelineHarness(ics=2, cells_per_ic=9)
        cells = [3.70] * 9 + [-3.4] * 7          # unpopulated read as junk
        h.feed(line(1, cells=cells) + line(2, cells=cells))
        p = h.packs[0]
        self.assertEqual(p.cell_count, 18)
        for _ic, cell, volts in p.cell_list():
            self.assertLessEqual(cell, 9)
            self.assertAlmostEqual(volts, 3.70, places=4)
        # The junk is present in the raw reading but never in the view.
        self.assertEqual(len(p.readings[0].cells), CELL_COUNT)

    def test_masking_does_not_affect_a_fully_populated_ic(self):
        h = PipelineHarness(ics=1, cells_per_ic=16)
        h.feed(line(1, count=16))
        self.assertEqual(h.packs[0].cell_count, 16)


if __name__ == "__main__":
    unittest.main()
