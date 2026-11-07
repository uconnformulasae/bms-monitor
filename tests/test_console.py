"""The interactive debug console, its PEC arithmetic and its two gates."""

import io
import unittest

from bms_monitor.console import (
    DebugConsole,
    format_decode,
    format_tx,
    hexdump,
    parse_hex,
)
from bms_monitor.parsing import adi_pec as P
from bms_monitor.sources import command_gate as G


class TestPecAgainstFirmwareSource(unittest.TestCase):
    """The CRC15 table is generated rather than copied, so check it against
    the constant in ADI's source."""

    #: First sixteen entries of Crc15Table[] in
    #: ADBMS6830/lib/src/adBms6830GenericType.c
    ADI_TABLE_HEAD = (
        0x0000, 0xc599, 0xceab, 0x0b32, 0xd8cf, 0x1d56, 0x1664, 0xd3fd,
        0xf407, 0x319e, 0x3aac, 0xff35, 0x2cc8, 0xe951, 0xe263, 0x27fa,
    )

    def test_generated_table_matches_adis_constant(self):
        self.assertEqual(P.CRC15_TABLE[:16], self.ADI_TABLE_HEAD)
        self.assertEqual(len(P.CRC15_TABLE), 256)

    def test_pec15_of_rdcva(self):
        # RDCVA = {0x00, 0x04}; the wire frame is 00 04 07 C2.
        self.assertEqual(P.pec15(b"\x00\x04"), 0x07C2)
        self.assertEqual(P.command_frame("RDCVA"), b"\x00\x04\x07\xc2")

    def test_pec15_lsb_is_always_zero(self):
        for name in P.OPCODES:
            self.assertEqual(P.pec15(bytes(P.OPCODES[name])) & 1, 0, name)

    def test_pec15_detects_every_single_bit_flip_in_a_command(self):
        base = b"\x00\x04"
        good = P.pec15(base)
        for byte_index in range(2):
            for bit in range(8):
                mutated = bytearray(base)
                mutated[byte_index] ^= 1 << bit
                self.assertNotEqual(P.pec15(bytes(mutated)), good)

    def test_pec10_round_trip_and_corruption(self):
        payload = bytes([0x70, 0x0E, 0x6C, 0x0E, 0x68, 0x0E])
        counter_byte = (23 << 2) & 0xFC
        value = P.pec10(payload, is_rx=True, command_counter=counter_byte)
        frame = payload + bytes(((counter_byte | ((value >> 8) & 0x03)), value & 0xFF))
        result = P.check_rx_group(frame)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cmd_counter"], 23)
        self.assertEqual(result["pec_rx"], result["pec_calc"])

        for index in range(6):
            for bit in (0, 3, 7):
                bad = bytearray(frame)
                bad[index] ^= 1 << bit
                self.assertFalse(P.check_rx_group(bytes(bad))["ok"],
                                 "bit %d:%d slipped through" % (index, bit))

    def test_pec10_rx_and_tx_variants_differ(self):
        payload = bytes(range(6))
        self.assertNotEqual(P.pec10(payload, is_rx=False),
                            P.pec10(payload, is_rx=True, command_counter=0xFC))

    def test_pec10_is_ten_bits(self):
        import random
        rng = random.Random(3)
        for _ in range(500):
            data = bytes(rng.randrange(256) for _ in range(6))
            self.assertLessEqual(P.pec10(data), 0x3FF)

    def test_register_group_of_the_wrong_length_is_refused(self):
        self.assertFalse(P.check_rx_group(b"\x00" * 6)["ok"])
        self.assertIn("8 bytes", P.check_rx_group(b"\x00" * 6)["error"])


class TestByteOrderDecode(unittest.TestCase):
    """Decoding cell bytes both ways so a byte swap is easy to spot."""

    def test_little_and_big_endian_give_different_answers(self):
        data = bytes([0x70, 0x0E])
        little = P.decode_cell_group(data, little_endian=True)[0]
        big = P.decode_cell_group(data, little_endian=False)[0]
        self.assertEqual(little["raw"], 0x0E70)
        self.assertEqual(big["raw"], 0x700E)
        self.assertNotAlmostEqual(little["volts"], big["volts"], places=3)

    def test_a_swap_is_not_always_out_of_range(self):
        """3.70 V is code 0x394B; swapped, 0x4B39 decodes to 4.3886 V, which
        is still plausible. So the console checks coherence, not range.
        """
        from bms_monitor.model import is_plausible_voltage, volts_to_code
        code = volts_to_code(3.70)
        wire = (code & 0xFFFF).to_bytes(2, "little")
        correct = P.decode_cell_group(wire, little_endian=True)[0]
        swapped = P.decode_cell_group(wire, little_endian=False)[0]
        self.assertAlmostEqual(correct["volts"], 3.70, places=4)
        self.assertTrue(is_plausible_voltage(swapped["volts"]))

    def test_a_swap_always_destroys_coherence(self):
        """Real cells cluster together; swapped ones scatter."""
        from bms_monitor.model import volts_to_code
        volts = [3.700, 3.702, 3.698, 3.705, 3.699, 3.701]
        wire = b"".join((volts_to_code(v) & 0xFFFF).to_bytes(2, "little")
                        for v in volts)
        little = P.decode_cell_group(wire, little_endian=True)
        big = P.decode_cell_group(wire, little_endian=False)
        lo_spread = max(c["volts"] for c in little) - min(c["volts"] for c in little)
        hi_spread = max(c["volts"] for c in big) - min(c["volts"] for c in big)
        self.assertLess(lo_spread, 0.01)
        self.assertGreater(hi_spread, 0.5)
        self.assertGreater(hi_spread, lo_spread * 20)

    def test_voltage_formula_matches_the_firmware(self):
        for raw in (-32768, -10000, 0, 14640, 32767):
            wire = (raw & 0xFFFF).to_bytes(2, "little")
            got = P.decode_cell_group(wire)[0]
            self.assertEqual(got["raw"], raw)
            self.assertAlmostEqual(got["volts"], (raw + 10000) * 0.000150, places=9)

    def test_odd_byte_count_is_truncated_not_crashed(self):
        self.assertEqual(len(P.decode_cell_group(bytes([1, 2, 3]))), 1)

    def test_empty(self):
        self.assertEqual(P.decode_cell_group(b""), [])


class TestOpcodeTable(unittest.TestCase):
    def test_key_opcodes_match_the_firmware_header(self):
        for name, expected in (("WRCFGA", (0x00, 0x01)), ("RDCFGA", (0x00, 0x02)),
                               ("RDCVA", (0x00, 0x04)), ("RDSTATA", (0x00, 0x30)),
                               ("SNAP", (0x00, 0x2D)), ("UNSNAP", (0x00, 0x2F)),
                               ("CLRCELL", (0x07, 0x11))):
            self.assertEqual(P.OPCODES[name], expected, name)

    def test_menu_command_4_is_self_sufficient(self):
        """Command 4 runs ADCV, SNAP, six reads, then UNSNAP, so sending 3
        before 4 isn't needed."""
        names = [name for name, _ in P.describe_menu_command(4)]
        self.assertEqual(names[0], "ADCV*")
        self.assertIn("SNAP", names)
        self.assertIn("UNSNAP", names)
        self.assertEqual(len([n for n in names if n.startswith("RDCV")]), 6)

    def test_menu_command_15_writes_config_first(self):
        names = [name for name, _ in P.describe_menu_command(15)]
        self.assertEqual(names[:2], ["WRCFGA", "WRCFGB"])

    def test_runtime_built_commands_have_no_fixed_frame(self):
        frames = dict(P.describe_menu_command(3))
        self.assertIsNone(frames["ADCV*"])

    def test_every_fixed_opcode_produces_a_four_byte_frame(self):
        for command in P.MENU_OPCODES:
            for name, frame in P.describe_menu_command(command):
                if frame is not None:
                    self.assertEqual(len(frame), 4, name)


class TestConsoleGate(unittest.TestCase):
    def test_console_set_is_wider_than_the_polling_set(self):
        self.assertTrue(G.ALLOWED_READ_COMMANDS < G.CONSOLE_COMMANDS)
        self.assertEqual(sorted(G.CONSOLE_COMMANDS - G.ALLOWED_READ_COMMANDS),
                         [1, 16, 21])
        # 22 is a pure read, so it's in both sets.
        self.assertIn(22, G.ALLOWED_READ_COMMANDS)
        self.assertIn(22, G.CONSOLE_COMMANDS)

    def test_register_clears_are_blocked_in_both_sets(self):
        for command in (17, 18, 19, 20):
            self.assertNotIn(command, G.ALLOWED_READ_COMMANDS)
            self.assertNotIn(command, G.CONSOLE_COMMANDS)
            with self.assertRaises(G.CommandRejected):
                G.encode_console_command(command)
            with self.assertRaises(G.CommandRejected):
                G.encode_command(command)

    def test_the_polling_gate_still_refuses_the_console_extras(self):
        for command in (1, 16, 21):
            with self.assertRaises(G.CommandRejected):
                G.encode_command(command)

    def test_console_encoding_is_still_digits_and_one_cr(self):
        for command in G.CONSOLE_COMMANDS:
            payload = G.encode_console_command(command)
            self.assertTrue(payload.endswith(b"\r"))
            self.assertEqual(payload.count(b"\r"), 1)
            self.assertTrue(payload[:-1].isdigit())

    def test_console_rejects_non_ints_and_injection(self):
        for bad in ("4", "4\r1\r", 4.0, True, False, None, b"4", [4], -1, 23, 999):
            with self.assertRaises(G.CommandRejected, msg=repr(bad)):
                G.encode_console_command(bad)

    def test_warnings_exist_for_every_config_write(self):
        for command in G.CONSOLE_WRITES_CONFIG:
            warning = G.console_warning(command)
            self.assertIsNotNone(warning, command)
            self.assertIn("WRITES CONFIGURATION", warning)
            # It re-sends the compiled-in config; it can't pick new values.
            self.assertIn("cannot change it", warning)
            self.assertIn("reflashing", warning)

    def test_flood_warning_for_16(self):
        self.assertIn("wall of output", G.console_warning(16))

    def test_no_warning_for_a_plain_read(self):
        for command in (0, 2, 4, 6, 12, 14):
            self.assertIsNone(G.console_warning(command))

    def test_15_warning_mentions_the_hidden_write(self):
        self.assertIn("WRCFGA", G.console_warning(15))


class TestFormatting(unittest.TestCase):
    def test_hexdump_shows_offset_hex_and_ascii(self):
        lines = hexdump(b"IC1:C1=3.7V\n")
        self.assertEqual(len(lines), 1)
        self.assertIn("0000", lines[0])
        self.assertIn("49 43 31", lines[0])
        self.assertIn("|IC1:C1=3.7V.|", lines[0])

    def test_hexdump_wraps(self):
        self.assertEqual(len(hexdump(bytes(48))), 3)

    def test_hexdump_of_empty(self):
        self.assertEqual(hexdump(b""), [])

    def test_parse_hex_accepts_the_ways_people_paste_bytes(self):
        want = bytes([0x70, 0x0E, 0x6C])
        for tokens in (["70", "0E", "6C"], ["700E6C"], ["0x70,0x0e,0x6c"],
                       ["70:0e:6c"], ["70-0E-6C"]):
            self.assertEqual(parse_hex(tokens), want, tokens)

    def test_parse_hex_rejects_rubbish(self):
        for tokens in ([], ["zz"], ["70", "0"], ["hello"], ["0x7"]):
            self.assertIsNone(parse_hex(tokens), tokens)

    def test_format_tx_shows_the_opcode_and_its_pec(self):
        lines = format_tx(4, b"4\r")
        text = "\n".join(lines)
        self.assertIn("34 0D", text)
        self.assertIn("00 04 07 C2", text)
        self.assertIn("NOT observed", text,
                      "computed values must be labelled as computed")

    def test_format_decode_shows_the_code_in_hex(self):
        from bms_monitor.parsing.adi_text import parse_line
        body = ",".join("C%d=3.700000V" % (i + 1) for i in range(16))
        record = parse_line("IC1:%s,CCount:9,PECError:0" % body)
        text = "\n".join(format_decode([record], cells_per_ic=3))
        self.assertIn("ccount=9", text)
        self.assertIn("0x", text)
        self.assertIn("wire", text)
        # Masked to three cells.
        self.assertIn("c3", text)
        self.assertNotIn("c4  ", text)

    def test_format_decode_flags_an_implausible_voltage(self):
        from bms_monitor.model import CellRecord
        record = CellRecord(ic=1, kind="cell", values=(99.0,) + (None,) * 15,
                            ccount=1, pec_error=0, complete=False, raw="")
        text = "\n".join(format_decode([record], cells_per_ic=1))
        self.assertIn("IMPLAUSIBLE", text)

    def test_format_decode_of_nothing(self):
        self.assertIn("nothing decodable", "\n".join(format_decode([])))


if __name__ == "__main__":
    unittest.main()
