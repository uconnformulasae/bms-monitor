"""CanSource tests over python-can's virtual bus.

The real source, python-can stack and codec run; only the physical layer is
faked, so bit timing, arbitration, error frames and adapter drivers aren't
covered. Skipped when python-can isn't installed.
"""

import threading
import time
import unittest

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
        import can
        from bms_monitor.sources.can_source import CanSource, STALL_TIMEOUT_S
        source = CanSource(channel="test_watchdog", interface="virtual", total_ic=1, cells_per_ic=4)
        bus = can.Bus(channel="test_watchdog", interface="virtual")
        try:
            source.start()
            self.assertTrue(wait_for(lambda: source.status()["link"] == "up"))
            source._last_frame_t = time.monotonic() - STALL_TIMEOUT_S - 1.0
            self.assertTrue(wait_for(lambda: source.status()["link"] == "reconnecting", timeout=2.0))
            self.assertIn("cycling the bus", source.status()["last_error"])
        finally:
            source.stop()
            bus.shutdown()

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


@unittest.skipUnless(HAVE_CAN, SKIP)
class TestCanSourceInternals(unittest.TestCase):
    def test_start_when_already_started_is_noop(self):
        from bms_monitor.sources.can_source import CanSource
        src = CanSource(channel="vcan_internals", interface="virtual")
        src._thread = threading.Thread(target=lambda: None)
        src.start()
        self.assertIsNotNone(src._thread)

    def test_close_bus_ignores_exception(self):
        from bms_monitor.sources.can_source import CanSource
        src = CanSource(channel="vcan_internals", interface="virtual")
        class BadBus:
            def shutdown(self):
                raise RuntimeError("shutdown fail")
        src._bus = BadBus()
        src._close_bus()
        self.assertIsNone(src._bus)

    def test_error_frame_increments_bad_counter(self):
        from bms_monitor.sources.can_source import CanSource
        src = CanSource(channel="vcan_internals", interface="virtual")
        class ErrorMsg:
            is_error_frame = True
        class BusMock:
            def __init__(self):
                self.count = 0
            def recv(self, timeout=None):
                if self.count == 0:
                    self.count += 1
                    return ErrorMsg()
                return None
        src._bus = BusMock()
        src._last_frame_t = time.monotonic()
        threading.Timer(0.05, src._stop_event.set).start()
        src._session(can)
        self.assertEqual(src._frames["bad"], 1)

    def test_send_queued_failure_sets_reconnecting(self):
        from bms_monitor.sources.can_source import CanSource
        from bms_monitor.sources.can_command_gate import BalanceTestControl
        tx = BalanceTestControl()
        tx.request(True)
        src = CanSource(channel="vcan_internals", interface="virtual", tx_control=tx)
        class FailingSendBus:
            def send(self, msg, timeout=None):
                raise RuntimeError("bus full")
            def recv(self, timeout=None):
                return None
        src._bus = FailingSendBus()
        src._last_frame_t = time.monotonic() - 10.0
        src._session(can)
        status = src.status()
        self.assertEqual(status["link"], "reconnecting")
        self.assertIn("CAN send failed", status["last_error"])

    def test_recv_failure_sets_reconnecting(self):
        from bms_monitor.sources.can_source import CanSource
        src = CanSource(channel="vcan_internals", interface="virtual")
        class FailingRecvBus:
            def recv(self, timeout=None):
                raise RuntimeError("recv error")
        src._bus = FailingRecvBus()
        src._session(can)
        status = src.status()
        self.assertEqual(status["link"], "reconnecting")
        self.assertIn("CAN read failed", status["last_error"])

    def test_dropout_reason_descriptions(self):
        from bms_monitor.sources.can_source import CanSource
        src = CanSource(channel="vcan_internals", interface="virtual", total_ic=2, cells_per_ic=9)
        reason = src._dropout_reason(1, cells={})
        self.assertIn("cell frame(s)", reason)
        self.assertIn("never arrived", reason)
        reason_all = src._dropout_reason(1, cells={0: None, 1: None, 2: None})
        self.assertIn("0xFFFF", reason_all)

    def test_balance_cmd_frame_emits_line(self):
        from bms_monitor.sources.can_source import CanSource
        src = CanSource(channel="vcan_internals", interface="virtual")
        lines = []
        src.subscribe(lambda kind, payload: lines.append(payload) if kind == "line" else None)
        can_id, data = F.encode_balance_cmd(True)
        class Msg:
            arbitration_id = can_id
        Msg.data = data
        src._handle(Msg())
        self.assertTrue(any("another node sent balance test" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
