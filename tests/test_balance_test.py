"""The balance test toggle (``--allow-tx``): frames, gate, CAN send and the
one POST endpoint."""

import http.client
import json
import time
import unittest

try:
    import can  # noqa: F401
    HAVE_CAN = True
except ImportError:
    HAVE_CAN = False

from bms_monitor.hub import Hub
from bms_monitor.sources import can_frames as F
from bms_monitor.sources.can_command_gate import (
    MAX_AGE_S,
    QUEUE_LEN,
    BalanceTestControl,
    CanCommandRejected,
    check_frame,
    encode_balance_test,
)
from bms_monitor.web.server import BALANCE_TEST_PATH, TOKEN_HEADER, MonitorServer


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class TestFrames(unittest.TestCase):
    def test_command_bytes_match_the_firmware(self):
        self.assertEqual(F.encode_balance_cmd(True), (0x740, b"\x01\xA5"))
        self.assertEqual(F.encode_balance_cmd(False), (0x740, b"\x00\xA5"))

    def test_command_needs_a_real_bool(self):
        for bad in (1, 0, "true", None, 1.0):
            with self.assertRaises(ValueError):
                F.encode_balance_cmd(bad)

    def test_chain_status_from_firmware_without_the_test_reads_off(self):
        _, data = F.encode_chain_status(1, 7, 0)
        out = F.decode_chain_status(data)
        self.assertEqual(out["balance_state"], "off")
        self.assertIsNone(out["balance_ic"])
        self.assertIsNone(out["balance_cell"])

    def test_chain_status_names_the_discharged_cell_one_based(self):
        _, data = F.encode_chain_status(1, 7, 0, balance_state=2, balance_ic=1, balance_cell=5)
        out = F.decode_chain_status(data)
        self.assertEqual(out["balance_state"], "discharging")
        self.assertEqual((out["balance_ic"], out["balance_cell"]), (2, 6))

    def test_pending_and_refused_have_no_cell(self):
        for state, name in ((1, "pending"), (4, "refused")):
            _, data = F.encode_chain_status(1, 0, 0, state, 0xFF, 0xFF)
            out = F.decode_chain_status(data)
            self.assertEqual(out["balance_state"], name)
            self.assertIsNone(out["balance_cell"])

    def test_whole_segment_discharge_names_no_cell(self):
        _, data = F.encode_chain_status(1, 0, 0, 2, 0xFF, 0xFF)
        out = F.decode_chain_status(data)
        self.assertEqual(out["balance_state"], "discharging")
        self.assertIsNone(out["balance_ic"])
        self.assertIsNone(out["balance_cell"])

    def test_too_hot_names_the_ic_only(self):
        _, data = F.encode_chain_status(1, 0, 0, 6, 1, 0xFF)
        out = F.decode_chain_status(data)
        self.assertEqual(out["balance_state"], "too_hot")
        self.assertEqual(out["balance_ic"], 2)
        self.assertIsNone(out["balance_cell"])

    def test_cell_low_names_the_ic_and_cell(self):
        _, data = F.encode_chain_status(1, 0, 0, 7, 0, 3)
        out = F.decode_chain_status(data)
        self.assertEqual(out["balance_state"], "cell_low")
        self.assertEqual((out["balance_ic"], out["balance_cell"]), (1, 4))

    def test_a_round_names_the_lowest_group_and_the_margin(self):
        for state, name in ((2, "discharging"), (8, "resting"), (3, "balanced")):
            _, data = F.encode_chain_status(1, 0, 0, state, 1, 4, balance_margin_0p1mv=10)
            out = F.decode_chain_status(data)
            self.assertEqual(out["balance_state"], name)
            self.assertEqual((out["balance_ic"], out["balance_cell"]), (2, 5))
            self.assertEqual(out["balance_margin_mv"], 1.0)

    def test_margin_only_while_balancing(self):
        _, data = F.encode_chain_status(1, 0, 0, 0, 0xFF, 0xFF, balance_margin_0p1mv=10)
        self.assertIsNone(F.decode_chain_status(data)["balance_margin_mv"])

    def test_unknown_state_is_reported_as_unknown(self):
        _, data = F.encode_chain_status(1, 0, 0, 9, 0, 0)
        self.assertEqual(F.decode_chain_status(data)["balance_state"], "unknown")

    def test_command_frame_is_ours(self):
        self.assertTrue(F.owns_id(0x740))
        self.assertEqual(F.decode(0x740, b"\x01\xA5")["kind"], "balance_cmd")
        self.assertIsNone(F.decode(0x740, b"\x01\x00"))


class TestGate(unittest.TestCase):
    def test_only_the_balance_command_passes(self):
        self.assertEqual(encode_balance_test(True), (0x740, b"\x01\xA5"))
        with self.assertRaises(CanCommandRejected):
            check_frame(0x741, b"\x01\xA5")
        with self.assertRaises(CanCommandRejected):
            check_frame(0x740, b"\x02\xA5")
        with self.assertRaises(CanCommandRejected):
            check_frame(0x740, b"\x01\xA5\x00")
        with self.assertRaises(CanCommandRejected):
            encode_balance_test("on")

    def test_requests_are_taken_in_order(self):
        control = BalanceTestControl(clock=FakeClock())
        control.request(True)
        control.request(False)
        self.assertEqual(control.take(), [(0x740, b"\x01\xA5"), (0x740, b"\x00\xA5")])
        self.assertEqual(control.take(), [])
        self.assertFalse(control.status()["requested"])

    def test_late_requests_are_dropped_not_sent(self):
        clock = FakeClock()
        control = BalanceTestControl(clock=clock)
        control.request(True)
        clock.t += MAX_AGE_S + 0.01
        self.assertEqual(control.take(), [])
        self.assertEqual(control.status()["expired"], 1)

    def test_discard_drops_the_queue(self):
        control = BalanceTestControl(clock=FakeClock())
        control.request(True)
        control.discard()
        self.assertEqual(control.take(), [])

    def test_oldest_age(self):
        clock = FakeClock()
        control = BalanceTestControl(clock=clock)
        self.assertIsNone(control.oldest_age())
        control.request(True)
        clock.t += 0.3
        control.request(False)
        clock.t += 0.1
        self.assertAlmostEqual(control.oldest_age(), 0.4)
        control.take()
        self.assertIsNone(control.oldest_age())

    def test_queue_is_bounded(self):
        control = BalanceTestControl(clock=FakeClock())
        for _ in range(QUEUE_LEN + 3):
            control.request(True)
        self.assertEqual(len(control.take()), QUEUE_LEN)


def wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@unittest.skipUnless(HAVE_CAN, "python-can is not installed")
class TestCanSend(unittest.TestCase):
    """The real CanSource and python-can stack on the virtual bus."""

    _n = [0]

    def channel(self):
        self._n[0] += 1
        return "bmsmon-baltest-%d" % self._n[0]

    def start(self, tx_control):
        from bms_monitor.sources.can_source import CanSource

        channel = self.channel()
        source = CanSource(channel=channel, interface="virtual", total_ic=2,
                           cells_per_ic=16, warmup=0, tx_control=tx_control)
        Hub().attach(source)
        controller = can.interface.Bus(channel=channel, interface="virtual")
        source.start()
        self.addCleanup(source.stop)
        self.addCleanup(controller.shutdown)
        self.assertTrue(wait_for(lambda: source.status()["link"] == "up"))
        return source, controller

    def test_toggle_reaches_the_controller(self):
        control = BalanceTestControl()
        source, controller = self.start(control)
        control.request(True)
        message = controller.recv(timeout=2.0)
        self.assertIsNotNone(message)
        self.assertEqual(message.arbitration_id, 0x740)
        self.assertFalse(message.is_extended_id)
        self.assertEqual(bytes(message.data), b"\x01\xA5")
        self.assertTrue(wait_for(lambda: source.status()["tx"]["sent"] == 1))

    def run_bursts(self, controller, period, stop_event, sent_at):
        """Send a 6-frame burst every `period` s, like the F105 at 10 Hz,
        noting when each frame went out."""
        cycle = 0
        while not stop_event.is_set():
            frames = [F.encode_chain_status(1, cycle, 0)]
            frames += [F.encode_cells(n, [3.6] * 4) for n in range(5)]
            for can_id, data in frames:
                if stop_event.is_set():
                    return
                try:
                    controller.send(can.Message(arbitration_id=can_id, data=data,
                                                is_extended_id=False))
                except Exception:
                    return
                sent_at.append(time.monotonic())
                time.sleep(0.0005)
            cycle += 1
            if stop_event.wait(period):
                return

    def test_transmit_waits_for_the_gap_between_bursts(self):
        # The CANdapter corrupts the frame it is forwarding if a command
        # arrives at the same moment, so a send must not land in a burst.
        import random
        import threading
        from bms_monitor.sources.can_source import TX_GAP_S

        control = BalanceTestControl()
        source, controller = self.start(control)
        stop_event = threading.Event()
        sent_at = []
        thread = threading.Thread(target=self.run_bursts,
                                  args=(controller, 0.1, stop_event, sent_at), daemon=True)
        thread.start()
        def cleanup_thread():
            stop_event.set()
            thread.join(timeout=1.0)
        self.addCleanup(cleanup_thread)
        gaps = []
        for _ in range(8):
            time.sleep(0.15 + random.random() * 0.1)
            # Wait until a burst is in progress, then ask.
            while not sent_at or time.monotonic() - sent_at[-1] > 0.001:
                time.sleep(0.0002)
            control.request(False)
            message = None
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                message = controller.recv(timeout=0.1)
                if message is not None and message.arbitration_id == F.ID_BALANCE_CMD:
                    break
            self.assertIsNotNone(message)
            gaps.append(time.monotonic() - sent_at[-1])
        self.assertGreaterEqual(min(gaps), TX_GAP_S * 0.8, gaps)

    def test_a_bus_that_never_goes_quiet_still_gets_the_toggle(self):
        import threading
        from bms_monitor.sources.can_source import TX_MAX_WAIT_S

        control = BalanceTestControl()
        source, controller = self.start(control)
        stop_event = threading.Event()
        sent_at = []
        thread = threading.Thread(target=self.run_bursts,
                                  args=(controller, 0.002, stop_event, sent_at), daemon=True)
        thread.start()
        def cleanup_thread():
            stop_event.set()
            thread.join(timeout=1.0)
        self.addCleanup(cleanup_thread)
        time.sleep(0.2)
        asked = time.monotonic()
        control.request(True)
        message = None
        while time.monotonic() - asked < 2.0:
            message = controller.recv(timeout=0.1)
            if message is not None and message.arbitration_id == F.ID_BALANCE_CMD:
                break
        self.assertIsNotNone(message)
        waited = time.monotonic() - asked
        self.assertGreaterEqual(waited, TX_MAX_WAIT_S * 0.9)
        self.assertLess(waited, MAX_AGE_S)

    def test_without_tx_control_nothing_is_sent(self):
        source, controller = self.start(None)
        time.sleep(0.5)
        self.assertIsNone(controller.recv(timeout=0.3))
        self.assertIsNone(source.status()["tx"])

    def test_firmware_state_reaches_the_summary(self):
        control = BalanceTestControl()
        source, controller = self.start(control)
        seen = []
        source.subscribe(lambda kind, payload: seen.append(payload) if kind == "summary" else None)
        can_id, data = F.encode_chain_status(1, 1, 0, 2, 0, 3)
        controller.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=False))
        self.assertTrue(wait_for(lambda: any(p.get("balance_state") == "discharging" for p in seen)))
        last = [p for p in seen if p.get("balance_state")][-1]
        self.assertEqual((last["balance_ic"], last["balance_cell"]), (1, 4))


class TestEndpoint(unittest.TestCase):
    def start(self, control):
        server = MonitorServer(Hub(history=100), host="127.0.0.1", port=0, tx_control=control)
        server.start()
        self.addCleanup(server.stop)
        return server

    def post(self, server, body, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        self.addCleanup(conn.close)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json",
                "Host": host or "127.0.0.1:%d" % server.port}
        hdrs.update(headers or {})
        conn.request("POST", BALANCE_TEST_PATH, body=raw, headers=hdrs)
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")

    def token(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/snapshot")
        data = json.loads(conn.getresponse().read())
        return data["tx"]["token"]

    def test_valid_request_is_queued(self):
        control = BalanceTestControl()
        server = self.start(control)
        status, body = self.post(server, {"enable": True}, {
            TOKEN_HEADER: self.token(server),
            "Origin": "http://127.0.0.1:%d" % server.port,
            "Sec-Fetch-Site": "same-origin",
        })
        self.assertEqual((status, body), (202, {"requested": True}))
        self.assertEqual(control.take(), [(0x740, b"\x01\xA5")])

    def test_snapshot_has_no_token_without_allow_tx(self):
        server = self.start(None)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/snapshot")
        self.assertNotIn("tx", json.loads(conn.getresponse().read()))

    def test_post_is_405_without_allow_tx(self):
        server = self.start(None)
        status, _ = self.post(server, {"enable": True})
        self.assertEqual(status, 405)

    def test_missing_or_wrong_token_is_refused(self):
        control = BalanceTestControl()
        server = self.start(control)
        self.assertEqual(self.post(server, {"enable": True})[0], 403)
        self.assertEqual(self.post(server, {"enable": True}, {TOKEN_HEADER: "nope"})[0], 403)
        self.assertEqual(control.take(), [])

    def test_cross_origin_is_refused_even_with_the_token(self):
        control = BalanceTestControl()
        server = self.start(control)
        token = self.token(server)
        for headers, host in (
            ({"Origin": "http://evil.example"}, None),
            ({"Sec-Fetch-Site": "cross-site"}, None),
            ({}, "evil.example:%d" % server.port),        # DNS rebinding
        ):
            headers[TOKEN_HEADER] = token
            self.assertEqual(self.post(server, {"enable": True}, headers, host)[0], 403,
                             (headers, host))
        self.assertEqual(control.take(), [])

    def test_bad_bodies_are_refused(self):
        control = BalanceTestControl()
        server = self.start(control)
        token = {TOKEN_HEADER: self.token(server)}
        for body in ({"enable": 1}, {"enable": "true"}, {"enable": True, "x": 1}, [], b"not json"):
            self.assertEqual(self.post(server, body, token)[0], 400, body)
        self.assertEqual(self.post(server, b"x" * 300, token)[0], 413)
        self.assertEqual(self.post(server, {"enable": True},
                                   dict(token, **{"Content-Type": "text/plain"}))[0], 415)
        self.assertEqual(control.take(), [])

    def test_other_post_paths_are_405(self):
        control = BalanceTestControl()
        server = self.start(control)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", "/api/snapshot", body=b"{}",
                     headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 405)


if __name__ == "__main__":
    unittest.main()
