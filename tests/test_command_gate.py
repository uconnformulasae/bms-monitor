"""Tests for the command gate, the only code that sends bytes to the firmware.

It only emits digits from an allow-list. With test_readonly.py, these show the
tool can't write config, clear a register or hang the firmware.
"""

import unittest

from bms_monitor.sources import command_gate as G


class TestAllowList(unittest.TestCase):
    def test_exact_allow_list(self):
        # 22 (Read Device SID) is a pure read, and reading the serial is how
        # the daisy-chain order gets confirmed.
        self.assertEqual(
            sorted(G.ALLOWED_READ_COMMANDS),
            [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 22],
        )

    def test_write_commands_are_not_in_the_allow_list(self):
        for cmd in (1, 16, 17, 18, 19, 20, 21):
            self.assertNotIn(cmd, G.ALLOWED_READ_COMMANDS, cmd)

    def test_allow_and_deny_do_not_overlap(self):
        self.assertFalse(G.ALLOWED_READ_COMMANDS & set(G.DENIED_COMMANDS))

    def test_command_16_is_documented_as_a_write(self):
        # 16 looks like a read ("Loop Measurements") and is not: it calls
        # adBmsWriteData(WRCFGA) and (WRCFGB) first. Easy to get wrong.
        self.assertIn("WRCFGA", G.DENIED_COMMANDS[16])

    def test_command_15_side_effect_is_recorded(self):
        # Verified in adBms_Application.c:503-518.
        self.assertIn(15, G.WRITES_CONFIG_SIDE_EFFECT)


class TestEncode(unittest.TestCase):
    def test_encodes_exactly_digits_and_one_cr(self):
        self.assertEqual(G.encode_command(4), b"4\r")
        self.assertEqual(G.encode_command(0), b"0\r")
        self.assertEqual(G.encode_command(15), b"15\r")

    def test_every_allowed_command_encodes(self):
        for cmd in G.ALLOWED_READ_COMMANDS:
            payload = G.encode_command(cmd)
            self.assertTrue(payload.endswith(b"\r"))
            self.assertTrue(payload[:-1].isdigit())
            self.assertEqual(payload.count(b"\r"), 1)

    def test_output_is_never_non_numeric(self):
        # A non-numeric byte hangs the firmware's scanf("%d") until a
        # physical reset.
        for cmd in G.ALLOWED_READ_COMMANDS:
            for byte in G.encode_command(cmd)[:-1]:
                self.assertTrue(0x30 <= byte <= 0x39, cmd)


class TestRejection(unittest.TestCase):
    def assert_rejected(self, value):
        with self.assertRaises(G.CommandRejected, msg=repr(value)):
            G.encode_command(value)

    def test_write_config_1(self):
        self.assert_rejected(1)

    def test_loop_measurement_16(self):
        self.assert_rejected(16)

    def test_clear_registers_17_to_20(self):
        for cmd in (17, 18, 19, 20):
            self.assert_rejected(cmd)

    def test_write_configuration_21(self):
        self.assert_rejected(21)

    def test_negative(self):
        self.assert_rejected(-1)
        self.assert_rejected(-4)

    def test_out_of_range(self):
        for cmd in (23, 99, 1000, 2 ** 40):
            self.assert_rejected(cmd)

    def test_string_digit(self):
        self.assert_rejected("4")

    def test_injection_via_string(self):
        # A string must not be able to sneak in a second command.
        self.assert_rejected("4\r1\r")
        self.assert_rejected("4\n1")
        self.assert_rejected("4;1")

    def test_float(self):
        self.assert_rejected(4.0)
        self.assert_rejected(4.5)

    def test_bool_is_rejected_even_though_it_is_an_int(self):
        # True == 1 == "write config". isinstance(True, int) is True, so a
        # naive check would let this through.
        self.assert_rejected(True)
        self.assert_rejected(False)

    def test_none_and_bytes_and_containers(self):
        for value in (None, b"4", [4], (4,), {4}, {"cmd": 4}, object()):
            self.assert_rejected(value)

    def test_int_subclass_is_rejected(self):
        class Sneaky(int):
            pass
        self.assert_rejected(Sneaky(4))

    def test_rejection_message_explains_why(self):
        try:
            G.encode_command(16)
        except G.CommandRejected as exc:
            self.assertIn("read-only", str(exc))
            self.assertIn("16", str(exc))


class TestPollPlan(unittest.TestCase):
    def test_default(self):
        self.assertEqual(G.parse_poll_plan("4"), (4,))

    def test_multiple(self):
        self.assertEqual(G.parse_poll_plan("3,4"), (3, 4))
        self.assertEqual(G.parse_poll_plan(" 3 , 4 , 15 "), (3, 4, 15))

    def test_rejects_a_denied_command(self):
        for spec in ("1", "4,1", "16", "4,17,4"):
            with self.assertRaises(G.CommandRejected, msg=spec):
                G.parse_poll_plan(spec)

    def test_rejects_non_numeric(self):
        for spec in ("four", "4;1", "-4", "4.0", "0x4", "4\r1", "+4"):
            with self.assertRaises(G.CommandRejected, msg=spec):
                G.parse_poll_plan(spec)

    def test_rejects_empty(self):
        for spec in ("", "   ", ",,,"):
            with self.assertRaises(G.CommandRejected):
                G.parse_poll_plan(spec)

    def test_rejects_absurdly_long_plan(self):
        with self.assertRaises(G.CommandRejected):
            G.validate_poll_plan([4] * 32)

    def test_validate_returns_a_tuple(self):
        self.assertIsInstance(G.validate_poll_plan([4, 3]), tuple)


class TestIsAllowed(unittest.TestCase):
    def test_non_raising_helper_agrees_with_encode(self):
        for value in list(range(-3, 25)) + ["4", 4.0, True, None]:
            allowed = G.is_allowed(value)
            try:
                G.encode_command(value)
                encoded = True
            except G.CommandRejected:
                encoded = False
            self.assertEqual(allowed, encoded, repr(value))


if __name__ == "__main__":
    unittest.main()
