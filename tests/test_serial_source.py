"""SerialSource with no hardware: a ``socket://`` peer that isn't there or
only sends noise, plus serial port scoring and discovery. Tests that need
pyserial skip without it.
"""

import threading
import time
import unittest

try:
    import serial  # noqa: F401
    HAVE_PYSERIAL = True
except ImportError:
    HAVE_PYSERIAL = False

from bms_monitor.hub import Hub

SKIP_REASON = ("pyserial is not installed; the real serial code path is NOT "
               "exercised by this run (pip install pyserial to enable)")


def wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestUnpluggedDevice(unittest.TestCase):
    @unittest.skipUnless(HAVE_PYSERIAL, SKIP_REASON)
    def test_nothing_listening_reconnects_forever_without_raising(self):
        from bms_monitor.sources.serial_source import SerialSource
        import socket

        # Grab a port, then release it, so nothing is listening there.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        source = SerialSource(port="socket://127.0.0.1:%d" % port, poll_plan=(4,))
        hub = Hub()
        hub.attach(source)
        try:
            source.start()
            self.assertTrue(wait_for(
                lambda: source.status()["link"] == "reconnecting", timeout=8))
            time.sleep(1.5)
            status = source.status()
            self.assertEqual(status["link"], "reconnecting")
            self.assertTrue(status["last_error"], "no error message recorded")
            self.assertEqual(hub.total_samples, 0)
            self.assertEqual(hub.data_state(), "no-data")
        finally:
            source.stop()

    @unittest.skipUnless(HAVE_PYSERIAL, SKIP_REASON)
    def test_auto_port_with_no_board_reports_clearly(self):
        from bms_monitor.sources.serial_source import SerialSource
        source = SerialSource(port="auto", poll_plan=(4,))
        try:
            source.start()
            self.assertTrue(wait_for(
                lambda: source.status()["link"] == "reconnecting", timeout=8))
            # Either a board really is attached (then we are up) or the
            # error explains itself.
            error = source.status()["last_error"]
            if error:
                self.assertIn("no Nucleo serial port found", error)
        finally:
            source.stop()


class TestGarbageStream(unittest.TestCase):
    @unittest.skipUnless(HAVE_PYSERIAL, SKIP_REASON)
    def test_a_peer_that_sends_pure_noise_does_not_crash_anything(self):
        import random
        import socket
        import socketserver

        from bms_monitor.sources.serial_source import SerialSource

        class NoiseHandler(socketserver.BaseRequestHandler):
            def handle(self):
                rng = random.Random(1)
                try:
                    self.request.settimeout(0.2)
                    for _ in range(400):
                        self.request.sendall(
                            bytes(rng.randrange(256) for _ in range(256)))
                        time.sleep(0.01)
                except OSError:
                    pass

        class Noise(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Noise(("127.0.0.1", 0), NoiseHandler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True).start()
        url = "socket://127.0.0.1:%d" % server.server_address[1]

        source = SerialSource(port=url, poll_plan=(4,))
        hub = Hub()
        hub.attach(source)
        try:
            source.start()
            time.sleep(3.0)
            status = source.status()
            self.assertGreater(status["lines"], 0, "no lines assembled from noise")
            self.assertEqual(hub.total_samples, 0,
                             "random noise produced a 'valid' sample")
            self.assertEqual(hub.data_state(), "no-data")
        finally:
            source.stop()
            server.shutdown()
            server.server_close()


class TestPortScoring(unittest.TestCase):
    """Runs with or without pyserial, since port_score() is pure.

    Regression test: --port auto used to accept any /dev/cu.* device, so it
    picked /dev/cu.wlan-debug and showed LINK UP with no data.
    """

    def test_stlink_vid_pid_wins(self):
        from bms_monitor.sources.serial_source import STLINK_PID, STLINK_VID, port_score
        self.assertGreaterEqual(
            port_score("/dev/cu.usbmodem1103", "STM32 STLink", STLINK_VID, STLINK_PID),
            100)

    def test_usbmodem_and_ttyacm_qualify(self):
        from bms_monitor.sources.serial_source import port_score
        self.assertGreater(port_score("/dev/cu.usbmodem1403"), 0)
        self.assertGreater(port_score("/dev/ttyACM0"), 0)
        self.assertGreater(port_score("COM7", "STMicroelectronics STLink"), 0)

    def test_unrelated_macos_devices_do_not_qualify(self):
        from bms_monitor.sources.serial_source import port_score
        for device in ("/dev/cu.wlan-debug", "/dev/cu.debug-console",
                       "/dev/cu.Bluetooth-Incoming-Port",
                       "/dev/cu.SomeonesMacBookPro", "/dev/cu.EMBERTONII",
                       "/dev/cu.SOC", "/dev/tty.Bluetooth-Incoming-Port"):
            self.assertEqual(port_score(device), 0, device)

    def test_cu_is_preferred_over_tty_for_the_same_board(self):
        from bms_monitor.sources.serial_source import port_score
        self.assertGreater(port_score("/dev/cu.usbmodem1103"),
                           port_score("/dev/tty.usbmodem1103"))

    def test_empty_and_garbage_inputs(self):
        from bms_monitor.sources.serial_source import port_score
        self.assertEqual(port_score(""), 0)
        self.assertEqual(port_score("", "", None, None), 0)


@unittest.skipUnless(HAVE_PYSERIAL, SKIP_REASON)
class TestPortDiscovery(unittest.TestCase):
    def test_list_candidate_ports_never_raises(self):
        from bms_monitor.sources.serial_source import list_candidate_ports
        ports = list_candidate_ports()
        self.assertIsInstance(ports, list)
        for entry in ports:
            self.assertIn("device", entry)
            self.assertGreater(entry["score"], 0)

    def test_discovery_on_this_machine_returns_only_qualified_ports(self):
        from bms_monitor.sources.serial_source import port_score, STLINK_VID, STLINK_PID
        # Ideal ST-Link VCP on macOS /dev/cu.
        self.assertEqual(port_score("/dev/cu.usbmodem14103", "STM32 STLink", STLINK_VID, STLINK_PID), 155)
        # Linux standard ST-Link /dev/ttyACM0
        self.assertEqual(port_score("/dev/ttyACM0", "STMicroelectronics ST-LINK/V2-1", STLINK_VID, STLINK_PID), 150)
        # Blocking macOS /dev/tty port penalized by tiebreaker
        self.assertEqual(port_score("/dev/tty.usbmodem14103", "STM32 STLink", STLINK_VID, STLINK_PID), 100)
        # Bluetooth or foreign serial port returns 0 (unqualified)
        self.assertEqual(port_score("/dev/cu.Bluetooth-Incoming-Port", "Bluetooth Port", None, None), 0)
        self.assertEqual(port_score("/dev/cu.usbserial-FT123", "FTDI Device", 0x0403, 0x6001), 0)

    def test_candidate_ports_handles_comports_exception(self):
        from unittest import mock
        from bms_monitor.sources.serial_source import list_candidate_ports
        with mock.patch("serial.tools.list_ports.comports", side_effect=Exception("error")):
            self.assertEqual(list_candidate_ports(), [])


@unittest.skipUnless(HAVE_PYSERIAL, SKIP_REASON)
class TestSerialSourceInternals(unittest.TestCase):
    def test_start_when_already_started_is_noop(self):
        from bms_monitor.sources.serial_source import SerialSource
        src = SerialSource(port="loop://")
        src._thread = threading.Thread(target=lambda: None)
        src.start()
        self.assertIsNotNone(src._thread)

    def test_close_port_ignores_exceptions(self):
        from bms_monitor.sources.serial_source import SerialSource
        src = SerialSource(port="loop://")
        class BadPort:
            def close(self):
                raise OSError("close error")
        src._serial = BadPort()
        src._close_port()
        self.assertIsNone(src._serial)

    def test_resolve_port_auto_empty_raises(self):
        from unittest import mock
        from bms_monitor.sources.serial_source import SerialSource
        src = SerialSource(port="auto")
        with mock.patch("bms_monitor.sources.serial_source.list_candidate_ports", return_value=[]):
            with self.assertRaises(RuntimeError):
                src._resolve_port()

    def test_session_write_exception(self):
        from bms_monitor.sources.serial_source import SerialSource
        src = SerialSource(port="loop://", poll_plan=(4,))
        class FailingWrite:
            def write(self, data):
                raise serial.SerialException("write exploded")
            def flush(self):
                pass
        src._serial = FailingWrite()
        src._session(serial)
        status = src.status()
        self.assertEqual(status["link"], "reconnecting")
        self.assertIn("write exploded", status["last_error"])

    def test_session_read_exception(self):
        from bms_monitor.sources.serial_source import SerialSource
        src = SerialSource(port="loop://", poll_plan=(4,))
        class FailingRead:
            in_waiting = 1
            def write(self, data):
                pass
            def flush(self):
                pass
            def read(self, n):
                raise serial.SerialException("read exploded")
        src._serial = FailingRead()
        src._session(serial)
        status = src.status()
        self.assertEqual(status["link"], "reconnecting")
        self.assertIn("read exploded", status["last_error"])

    def test_session_cmd_timeout_event(self):
        from bms_monitor.sources.serial_source import SerialSource
        src = SerialSource(port="loop://", poll_plan=(4,), cmd_timeout=0.01)
        events = []
        src.subscribe(lambda kind, payload: events.append(payload) if kind == "event" else None)

        class IdleSerial:
            in_waiting = 0
            def write(self, data):
                pass
            def flush(self):
                pass
            def read(self, n):
                time.sleep(0.02)
                return b""

        src._serial = IdleSerial()
        src._response_pending = True
        # Set stop event after a brief delay so session loop runs once
        threading.Timer(0.04, src._stop_event.set).start()
        src._session(serial)
        timeout_events = [e for e in events if getattr(e, "kind", "") == "cmd_timeout"]
        self.assertGreaterEqual(len(timeout_events), 1)

    def test_session_stall_watchdogs(self):
        from bms_monitor.sources.serial_source import SerialSource, STALL_TIMEOUT_S
        src = SerialSource(port="loop://", poll_plan=(4,))

        class QuietSerial:
            in_waiting = 0
            def write(self, data):
                pass
            def flush(self):
                pass
            def read(self, n):
                return b""

        # Test line stall watchdog
        src._serial = QuietSerial()
        src._last_line_t = time.monotonic() - STALL_TIMEOUT_S - 2.0
        src._session(serial)
        status = src.status()
        self.assertEqual(status["link"], "reconnecting")
        self.assertIn("no output for", status["last_error"])

        # Test sample stall watchdog
        src._clear_error()
        src._last_line_t = time.monotonic()
        src._last_sample_t = time.monotonic() - STALL_TIMEOUT_S - 2.0
        src._session(serial)
        status = src.status()
        self.assertEqual(status["link"], "reconnecting")
        self.assertIn("no valid sample for", status["last_error"])


if __name__ == "__main__":
    unittest.main()
