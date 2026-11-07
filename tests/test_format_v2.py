"""Tests for the v2 firmware output format, plus the old v1 format.

v2 prints a 16-cell sample as a titled multi-line block instead of one long
line, and ``syscalls.c _write()`` now turns LF into CRLF. The fixtures in
``v1/`` and ``v2/`` are real serialPrintResult.c output from before and after
that change. Both must parse, since you can't tell which build is on a board.
"""

import os
import unittest

from bms_monitor.model import CellRecord, EventRecord, StatusRecord, UnparsedRecord
from bms_monitor.parsing.adi_text import parse_line
from bms_monitor.parsing.blocks import BlockAssembler
from bms_monitor.parsing.lines import LineAssembler
from bms_monitor.sources.base import IngestPipeline
from bms_monitor.sources.command_gate import encode_command

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(*parts) -> bytes:
    with open(os.path.join(FIXTURES, *parts), "rb") as handle:
        return handle.read()


def run_pipeline(raw: bytes, ics=1, cells_per_ic=16):
    packs, events, lines = [], [], []
    pipeline = IngestPipeline(
        on_pack=packs.append, on_line=lines.append,
        on_status=lambda fields: None, on_event=events.append,
        warmup=0, expected_ics=ics, cells_per_ic=cells_per_ic,
    )
    pipeline.feed_bytes(raw)
    for pending in pipeline._blocks.flush():
        pipeline._handle_line(pending, False)
    return pipeline, packs, events


class TestBlockAssembler(unittest.TestCase):
    """Folds a block back into the single line the old firmware printed, so
    the parser only needs one grammar."""

    BLOCK = [
        "",
        "--- IC1: Cell Voltages ---",
        "     C1=3.711900V     C2=3.708000V     C3=3.703950V     C4=3.699900V",
        "     C5=3.696000V     C6=3.691950V     C7=3.687900V     C8=3.684000V",
        "     C9=3.679950V    C10=3.675900V    C11=3.672000V    C12=3.667950V",
        "    C13=3.663900V    C14=3.660000V    C15=3.655950V    C16=3.651900V",
        "CCount:63, PECError:0",
    ]

    def assemble(self, lines):
        assembler = BlockAssembler()
        out = []
        for line in lines:
            out.extend(assembler.feed_line(line))
        out.extend(assembler.flush())
        return out

    def test_a_cell_block_becomes_one_legacy_line(self):
        out = self.assemble(self.BLOCK)
        merged = [line for line in out if line.startswith("IC1:C")]
        self.assertEqual(len(merged), 1)
        expected = ("IC1:"
                    "C1=3.711900V,C2=3.708000V,C3=3.703950V,C4=3.699900V,"
                    "C5=3.696000V,C6=3.691950V,C7=3.687900V,C8=3.684000V,"
                    "C9=3.679950V,C10=3.675900V,C11=3.672000V,C12=3.667950V,"
                    "C13=3.663900V,C14=3.660000V,C15=3.655950V,C16=3.651900V,"
                    "CCount:63,PECError:0")
        self.assertEqual(merged[0], expected)

    def test_the_merged_line_parses_as_a_complete_record(self):
        merged = [l for l in self.assemble(self.BLOCK) if l.startswith("IC1:C")][0]
        record = parse_line(merged)
        self.assertIsInstance(record, CellRecord)
        self.assertTrue(record.complete)
        self.assertEqual(record.ccount, 63)
        self.assertEqual(record.pec_error, 0)
        self.assertAlmostEqual(record.values[0], 3.711900)
        self.assertAlmostEqual(record.values[15], 3.651900)

    def test_the_space_after_the_comma_is_handled(self):
        """`CCount:63,PECError:0` became `CCount:63, PECError:0`. A literal
        match on the old format would break here."""
        out = self.assemble(self.BLOCK)
        merged = [l for l in out if l.startswith("IC1:C")][0]
        self.assertIn("CCount:63,PECError:0", merged)

    def test_ccount_line_terminates_the_block(self):
        lines = self.BLOCK + ["C1=9.999999V"]     # stray, must not be folded in
        merged = [l for l in self.assemble(lines) if l.startswith("IC1:C")][0]
        self.assertNotIn("9.999999", merged)

    def test_a_blank_line_terminates_an_unfinished_block(self):
        out = self.assemble(self.BLOCK[:4] + [""])
        merged = [l for l in out if l.startswith("IC1:C")]
        self.assertEqual(len(merged), 1)
        record = parse_line(merged[0])
        self.assertFalse(record.complete, "a truncated block must not look complete")

    def test_a_new_header_terminates_the_previous_block(self):
        lines = self.BLOCK[:4] + ["--- IC2: Cell Voltages ---"] + self.BLOCK[2:]
        out = self.assemble(lines)
        self.assertEqual(len([l for l in out if l.startswith("IC1:C")]), 1)
        self.assertEqual(len([l for l in out if l.startswith("IC2:C")]), 1)

    def test_status_blocks_emit_the_legacy_headers(self):
        out = self.assemble(["--- IC1: Status A ---",
                             "VREF2:3.000000V, VREF3:3.000000V, ITMP:31.0°C"])
        self.assertEqual(out[0], "IC1:")
        self.assertEqual(out[1], "Status A:")
        self.assertIn("VREF2", out[2])

    def test_lines_outside_a_block_pass_through_untouched(self):
        legacy = "IC1:C1=3.700000V,CCount:1,PECError:0"
        out = self.assemble([legacy])
        self.assertEqual(out, [legacy])
        self.assertEqual(BlockAssembler().blocks_seen, 0)

    def test_aux_block_keeps_vmv_and_vplus(self):
        out = self.assemble([
            "--- IC1: Aux Voltages ---",
            "   AUX1=1.500000V   AUX2=1.500000V   AUX3=1.500000V   AUX4=1.500000V",
            "   AUX5=1.500000V   AUX6=1.500000V   AUX7=1.500000V   AUX8=1.500000V",
            "   AUX9=1.500000V  AUX10=1.500000V",
            "   VMV:30.000000V    V+:30.000000V",
            "CCount:4, PECError:0",
        ])
        merged = [l for l in out if l.startswith("IC1:AUX")][0]
        record = parse_line(merged)
        self.assertEqual(record.kind, "aux")
        self.assertEqual(len(record.values), 12)
        self.assertTrue(record.complete)
        self.assertAlmostEqual(record.values[10], 30.0)

    def test_reset_drops_a_half_built_block(self):
        assembler = BlockAssembler()
        for line in self.BLOCK[:4]:
            assembler.feed_line(line)
        assembler.reset()
        self.assertEqual(assembler.flush(), [])

    def test_none_and_garbage_never_raise(self):
        assembler = BlockAssembler()
        self.assertEqual(assembler.feed_line(None), [])
        for junk in ("---", "--- ---", "--- IC: x ---", "--- ICzz: y ---",
                     "\x00\xff", "-" * 200):
            assembler.feed_line(junk)


class TestGoldenFixturesV2(unittest.TestCase):
    """Real v2 output from the firmware's serialPrintResult.c."""

    def test_full_capture_parses_with_nothing_unclassified(self):
        # As the board now sends it: LF translated to CRLF by _write().
        raw = load("v2", "after.txt").replace(b"\n", b"\r\n")
        pipeline, packs, events = run_pipeline(raw)
        unparsed = [e.text for e in events if e.kind == "unparsed"]
        self.assertEqual(unparsed, [], unparsed[:5])
        self.assertGreater(pipeline.blocks_seen, 10)
        self.assertEqual(len(packs), 1)
        self.assertEqual(pipeline.records_incomplete, 0)

    def test_the_cell_sample_decodes_correctly(self):
        raw = load("v2", "after.txt").replace(b"\n", b"\r\n")
        _, packs, _ = run_pipeline(raw)
        pack = packs[0]
        self.assertEqual(pack.cell_count, 16)
        self.assertTrue(pack.usable)
        self.assertAlmostEqual(pack.pack_v, 58.911, places=3)
        self.assertEqual(pack.readings[0].ccount, 63)
        self.assertAlmostEqual(pack.readings[0].cells[0], 3.711900, places=6)
        self.assertAlmostEqual(pack.readings[0].cells[15], 3.651900, places=6)

    def test_the_device_sid_is_captured(self):
        raw = load("v2", "after.txt").replace(b"\n", b"\r\n")
        pipeline, _, _ = run_pipeline(raw)
        self.assertEqual(pipeline.sids, {1: "A10BC300E5F6"})

    def test_the_no_device_signature_is_preserved(self):
        raw = load("v2", "after_nodev.txt").replace(b"\n", b"\r\n")
        pipeline, packs, events = run_pipeline(raw)
        self.assertEqual([e.text for e in events if e.kind == "unparsed"], [])
        self.assertEqual(len(packs), 1)
        pack = packs[0]
        self.assertFalse(pack.pec_ok)
        self.assertFalse(pack.usable)
        self.assertIsNone(pack.pack_v)
        for _ic, _cell, volts in [(1, i, v) for i, v in
                                  enumerate(pack.readings[0].cells[:16])]:
            self.assertIn(round(volts, 6), (1.500000, 1.499850))

    def test_status_fields_survive_the_new_multi_row_layout(self):
        raw = load("v2", "after.txt").replace(b"\n", b"\r\n")
        merged = {}
        assembler = LineAssembler()
        blocks = BlockAssembler()
        for text, _trunc in assembler.feed(raw):
            for line in blocks.feed_line(text):
                record = parse_line(line)
                if isinstance(record, StatusRecord) and record.fields:
                    merged.update(record.fields)
        # Status C is now four rows, Status D six per row across three rows.
        for key in ("VREF2", "ITMP", "VA", "VD", "VR4K", "CSFLT", "THSD",
                    "SPIFLT", "OSCCHK", "VA_UV", "VD_OV",
                    "C1UV", "C16UV", "C1OV", "C16OV", "REFON", "VUV", "VOV"):
            self.assertIn(key, merged, key)

    def test_the_new_menu_is_recognised_as_menu_not_garbage(self):
        raw = load("v2", "after.txt").replace(b"\n", b"\r\n")
        _, _, events = run_pipeline(raw)
        menu = [e for e in events if e.kind == "menu"]
        self.assertGreater(len(menu), 20)
        text = "\n".join(e.text for e in menu)
        self.assertIn("ADBMS6830 COMMAND MENU", text)
        self.assertIn("Reprint this menu", text)
        self.assertIn("-- Configuration", text)

    def test_write_config_raw_bytes_are_classified(self):
        raw = load("v2", "after.txt").replace(b"\n", b"\r\n")
        _, _, events = run_pipeline(raw)
        self.assertTrue([e for e in events if e.kind == "config_bytes"])

    def test_bare_lf_capture_also_parses(self):
        """The fixture files have bare LF, without the STM32 _write() CRLF
        translation. The parser should accept either."""
        pipeline, packs, events = run_pipeline(load("v2", "after.txt"))
        self.assertEqual([e.text for e in events if e.kind == "unparsed"], [])
        self.assertEqual(len(packs), 1)

    def test_split_at_every_byte_position_gives_the_same_result(self):
        raw = load("v2", "after.txt").replace(b"\n", b"\r\n")
        reference = run_pipeline(raw)[1][0].readings[0].cells
        for cut in range(0, len(raw), 97):     # sampled; every byte is 4800 runs
            pipeline, packs, _ = run_pipeline(b"")
            pipeline.feed_bytes(raw[:cut])
            pipeline.feed_bytes(raw[cut:])
            for pending in pipeline._blocks.flush():
                pipeline._handle_line(pending, False)
            self.assertEqual(len(packs), 1, "split at %d" % cut)
            self.assertEqual(packs[0].readings[0].cells, reference, "split at %d" % cut)


class TestGoldenFixturesV1(unittest.TestCase):
    """The old pre-reformat firmware, which some boards may still run."""

    def test_legacy_capture_still_parses(self):
        pipeline, packs, events = run_pipeline(load("v1", "before.txt"))
        unparsed = [e.text for e in events if e.kind == "unparsed"]
        self.assertEqual(unparsed, [], unparsed[:5])
        self.assertEqual(pipeline.blocks_seen, 0, "v1 has no blocks to assemble")
        self.assertEqual(len(packs), 1)
        self.assertTrue(packs[0].usable)
        self.assertAlmostEqual(packs[0].pack_v, 58.911, places=3)

    def test_legacy_no_device_capture(self):
        pipeline, packs, _ = run_pipeline(load("v1", "before_nodev.txt"))
        self.assertEqual(len(packs), 1)
        self.assertFalse(packs[0].usable)

    def test_the_two_captures_agree_on_the_measurement(self):
        """The reformat changed presentation, not values. If the two
        fixtures disagreed, one of the parser paths would be wrong."""
        _, v1_packs, _ = run_pipeline(load("v1", "before.txt"))
        _, v2_packs, _ = run_pipeline(load("v2", "after.txt").replace(b"\n", b"\r\n"))
        self.assertEqual(v1_packs[0].readings[0].cells,
                         v2_packs[0].readings[0].cells)
        self.assertEqual(v1_packs[0].readings[0].ccount,
                         v2_packs[0].readings[0].ccount)


if __name__ == "__main__":
    unittest.main()
