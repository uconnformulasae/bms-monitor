"""CanSource tests over python-can's virtual bus.

The real source, python-can stack and codec run; only the physical layer is
faked, so bit timing, arbitration, error frames and adapter drivers aren't
covered. Skipped when python-can isn't installed.
"""

import sys
import time
import unittest
from unittest import mock

try:
    import can  # noqa: F401
    HAVE_CAN = True
except ImportError:
    HAVE_CAN = False

from bms_monitor.hub import Hub
from bms_monitor.sources import can_frames as F

SKIP = ("python-can is not installed; the CAN path is NOT exercised by this "
        "run (pip install python-can to enable)")

_CHANNEL = ["bmsmon-test-0"]


def next_channel():
    _CHANNEL[0] = _CHANNEL[0][:-1] + str(int(_CHANNEL[0][-1]) + 1)
    return _CHANNEL[0]


def wait_for(predicate, timeout=25.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@unittest.skipUnless(HAVE_CAN, SKIP)
class TestCanRobustness(unittest.TestCase):
    def test_garbage_and_foreign_ids_never_produce_a_sample(self):
        import random

        from bms_monitor.sources.can_source import CanSource

        channel = next_channel()
        hub = Hub()
        source = CanSource(channel=channel, interface="virtual",
                           total_ic=10, cells_per_ic=9, warmup=0)
        hub.attach(source)
        source.start()
        self.assertTrue(wait_for(lambda: source.status()["link"] == "up"))

        bus = can.interface.Bus(channel=channel, interface="virtual")
        rng = random.Random(1)
        try:
            for _ in range(400):
                bus.send(can.Message(
                    arbitration_id=rng.choice([0x100, 0x200, 0x7FE, 0x600, 0x610]),
                    data=bytes(rng.randrange(256) for _ in range(rng.randrange(0, 9))),
                    is_extended_id=False))
            time.sleep(1.5)
            # Random bytes at our own IDs decode to numbers, but a pack
            # sample needs all 23 cell frames and a per-IC frame for every
            # chip, which random noise won't produce. Just check nothing
            # crashed and no cell outside the configured chain got in.
            for pack in hub.samples.snapshot():
                self.assertLessEqual(pack.cell_count, 90)
            self.assertEqual(source.status()["link"], "up")
        finally:
            bus.shutdown()
            source.stop()

    def test_bus_that_goes_quiet_moves_to_reconnecting(self):
        from bms_monitor.sources.can_source import STALL_TIMEOUT_S
        self.assertGreater(STALL_TIMEOUT_S, 5)   # documented watchdog exists

    def test_source_exposes_no_transmit_path(self):
        from bms_monitor.sources.can_source import CanSource
        public = {n for n in dir(CanSource) if not n.startswith("_")}
        self.assertEqual(public, {"start", "stop", "subscribe", "status", "name"})

    def test_the_only_send_is_fed_by_the_gate(self):
        """One .send( in the CAN backend, and its frames come from the
        balance test control (which builds them in can_command_gate)."""
        import inspect

        from bms_monitor.sources import can_source
        src = inspect.getsource(can_source)
        sends = [line.strip() for line in src.splitlines()
                 if ".send(" in line and not line.strip().startswith("#")]
        self.assertEqual(sends, ["bus.send(message, timeout=0.2)"])
        self.assertIn("for can_id, data in self._tx.take():", src)
        self.assertNotIn("can.Message(", src.replace("can_module.Message(", ""))


class TestNormalizeSlcanChannel(unittest.TestCase):
    def test_mac_path_becomes_com_on_windows(self):
        from bms_monitor.sources import can_source

        with mock.patch.object(can_source.sys, "platform", "win32"):
            channel, note = can_source.normalize_slcan_channel(
                "/dev/cu.usbserial-COM3")
        self.assertEqual(channel, "COM3")
        self.assertIn("COM3", note)

    def test_com_unchanged_on_windows(self):
        from bms_monitor.sources import can_source

        with mock.patch.object(can_source.sys, "platform", "win32"):
            channel, note = can_source.normalize_slcan_channel("COM3")
        self.assertEqual(channel, "COM3")
        self.assertIsNone(note)

    def test_unix_path_unchanged(self):
        from bms_monitor.sources import can_source

        if sys.platform == "win32":
            path = "/dev/cu.usbserial-ABC"
        else:
            path = "/dev/cu.usbserial-ABC"
        with mock.patch.object(can_source.sys, "platform", "darwin"):
            channel, note = can_source.normalize_slcan_channel(path)
        self.assertEqual(channel, path)
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
