"""The CANdapter answers each command with ASCII ACK (0x06) and no line end,
so the ACK lands in front of the next frame: b"\\x06t6FF8...\\r". python-can's
slcan reader drops any line that doesn't start with a frame letter, so every
transmit cost us one received frame. Bytes below are from a real capture."""

import unittest

try:
    import can
    import serial  # noqa: F401
    from can.interfaces.slcan import slcanBus
    from bms_monitor.sources.candapter import candapter_bus_class
    CandapterSlcanBus = candapter_bus_class()
    HAVE_CAN = True
except ImportError:                                   # pragma: no cover
    HAVE_CAN = False

from bms_monitor.sources import can_source

CHAIN_STATUS = b"t6FF80139000000FFFF00\r"
CELLS = b"t70180A2D0A2D0A2D0A2D\r"


def open_loop(cls):
    """A bus on pyserial's loop:// port: whatever we write comes back."""
    bus = cls(channel="loop://", bitrate=250000, sleep_after_open=0)
    while bus.recv(timeout=0.05) is not None:        # its own C / S5 / O echo
        pass
    return bus


def feed(bus, data):
    bus.serialPortOrig.write(data)


@unittest.skipUnless(HAVE_CAN, "python-can or pyserial is not installed")
class TestAckBeforeAFrame(unittest.TestCase):
    def setUp(self):
        self.bus = open_loop(CandapterSlcanBus)

    def tearDown(self):
        self.bus.shutdown()

    def test_frame_after_an_ack_is_received(self):
        feed(self.bus, b"\x06" + CHAIN_STATUS)
        message = self.bus.recv(timeout=0.5)
        self.assertIsNotNone(message)
        self.assertEqual(message.arbitration_id, 0x6FF)
        self.assertEqual(bytes(message.data).hex(), "0139000000ffff00")

    def test_two_acks_in_a_row(self):
        feed(self.bus, b"\x06\x06" + CELLS)
        message = self.bus.recv(timeout=0.5)
        self.assertIsNotNone(message)
        self.assertEqual(message.arbitration_id, 0x701)

    def test_frames_without_an_ack_are_unchanged(self):
        feed(self.bus, CHAIN_STATUS + CELLS)
        ids = [self.bus.recv(timeout=0.5).arbitration_id for _ in range(2)]
        self.assertEqual(ids, [0x6FF, 0x701])

    def test_a_garbled_line_is_reported(self):
        seen = []
        self.bus.on_unparsed = seen.append
        feed(self.bus, b"8840883A\r" + b"\x06" + b"z\r" + CHAIN_STATUS + b"\a")
        while self.bus.recv(timeout=0.1) is not None:
            pass
        self.assertEqual(seen, ["8840883A\r"])

    def test_a_lone_ack_is_not_a_frame(self):
        feed(self.bus, b"\x06" + b"\r")
        self.assertIsNone(self.bus.recv(timeout=0.1))


@unittest.skipUnless(HAVE_CAN, "python-can or pyserial is not installed")
class TestPlainSlcanDropsIt(unittest.TestCase):
    """Why the subclass exists. If python-can starts accepting these lines,
    this fails and the workaround can go."""

    def test_python_can_alone_loses_the_frame(self):
        bus = open_loop(slcanBus)
        try:
            feed(bus, b"\x06" + CHAIN_STATUS)
            self.assertIsNone(bus.recv(timeout=0.2))
        finally:
            bus.shutdown()


@unittest.skipUnless(HAVE_CAN, "python-can or pyserial is not installed")
class TestSourceUsesIt(unittest.TestCase):
    def test_slcan_opens_the_candapter_bus(self):
        source = can_source.CanSource(channel="loop://", interface="slcan", total_ic=2,
                                      cells_per_ic=10)
        bus = source._open_bus(sleep_after_open=0)
        try:
            self.assertIsInstance(bus, CandapterSlcanBus)
        finally:
            bus.shutdown()

    def test_unparsed_lines_reach_the_raw_console(self):
        source = can_source.CanSource(channel="loop://", interface="slcan", total_ic=2,
                                      cells_per_ic=10)
        lines = []
        source.subscribe(lambda kind, payload: lines.append(payload) if kind == "line" else None)
        bus = source._open_bus(sleep_after_open=0)
        try:
            report, bus.on_unparsed = bus.on_unparsed, None
            while bus.recv(timeout=0.05) is not None:     # loop:// echoes C / S5 / O
                pass
            bus.on_unparsed = report
            feed(bus, b"70\x0638840\r")
            bus.recv(timeout=0.2)
        finally:
            bus.shutdown()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("adapter line not parsed: '70\\x0638840\\r'", lines[0])

    def test_other_interfaces_are_left_alone(self):
        source = can_source.CanSource(channel="ack-test", interface="virtual", total_ic=2,
                                      cells_per_ic=10)
        bus = source._open_bus()
        try:
            self.assertNotIsInstance(bus, slcanBus)
        finally:
            bus.shutdown()


if __name__ == "__main__":
    unittest.main()
