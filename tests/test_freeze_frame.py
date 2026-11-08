"""Tests for the freeze-frame diagnostic snapshot manager and endpoints."""

import json
import unittest
from bms_monitor.freeze_frame import FreezeFrameManager
from bms_monitor.hub import Hub
from bms_monitor.web.server import MonitorServer

from .packs import pack


def free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestFreezeFrameManager(unittest.TestCase):
    def test_empty_manager(self):
        mgr = FreezeFrameManager(capacity=5)
        self.assertEqual(mgr.count(), 0)
        self.assertEqual(mgr.list_frames(), [])
        self.assertIsNone(mgr.latest_frame())
        self.assertIsNone(mgr.get_frame("FF-0001"))

    def test_capture_and_deduplication(self):
        mgr = FreezeFrameManager(capacity=5, dedupe_window_s=0.5)
        p = pack(seq=1, ics=2, cells_per_ic=6)
        frame1 = mgr.capture(reason="Test Fault UV", pack=p)
        self.assertIsNotNone(frame1)
        self.assertEqual(frame1["id"], "FF-0001")
        self.assertEqual(frame1["reason"], "Test Fault UV")
        self.assertIn("cells", frame1["pack"])
        self.assertEqual(len(frame1["pack"]["cells"]), 12)

        # Immediate repeat should be suppressed by deduplication
        dup = mgr.capture(reason="Test Fault UV", pack=p)
        self.assertIsNone(dup)
        self.assertEqual(mgr.count(), 1)

        # Different reason should be captured
        frame2 = mgr.capture(reason="Test Fault OV", pack=p)
        self.assertIsNotNone(frame2)
        self.assertEqual(frame2["id"], "FF-0002")
        self.assertEqual(mgr.count(), 2)

    def test_capacity_eviction(self):
        mgr = FreezeFrameManager(capacity=3, dedupe_window_s=0.0)
        for i in range(5):
            mgr.capture(reason=f"Fault {i}")
        self.assertEqual(mgr.count(), 3)
        ids = [f["id"] for f in mgr.list_frames()]
        self.assertEqual(ids, ["FF-0003", "FF-0004", "FF-0005"])

    def test_capture_with_temperatures_and_summary(self):
        mgr = FreezeFrameManager(capacity=5)
        temps = {
            "sensors": [
                {"channel": 1, "ic": 1, "celsius": 32.5},
                {"channel": 2, "ic": 1, "celsius": 48.2},
                {"channel": 1, "ic": 2, "celsius": 28.0},
            ]
        }
        summary = {"faults": ["OVER_TEMP"], "pack_current_a": -45.0}
        frame = mgr.capture(reason="OT Trip", temps=temps, summary=summary)
        self.assertIsNotNone(frame)
        self.assertEqual(frame["temps"]["hottest"]["celsius"], 48.2)
        self.assertEqual(frame["temps"]["coldest"]["celsius"], 28.0)
        self.assertEqual(frame["summary"]["faults"], ["OVER_TEMP"])

    def test_clear(self):
        mgr = FreezeFrameManager(capacity=5)
        mgr.capture("Fault A")
        self.assertEqual(mgr.count(), 1)
        mgr.clear()
        self.assertEqual(mgr.count(), 0)
        self.assertIsNone(mgr.latest_frame())


class TestHubFreezeFrame(unittest.TestCase):
    def test_hub_manual_and_reset(self):
        hub = Hub(ui_config={"uv": 3.0, "ov": 4.2})
        self.assertEqual(len(hub.freeze_frames()), 0)

        frame = hub.capture_freeze_frame("Manual audit snapshot", source="manual")
        self.assertIsNotNone(frame)
        self.assertEqual(len(hub.freeze_frames()), 1)
        self.assertIsNotNone(hub.latest_freeze_frame())
        self.assertIsNotNone(hub.get_freeze_frame(frame["id"]))

        hub.reset()
        self.assertEqual(len(hub.freeze_frames()), 0)

    def test_hub_threshold_breach_capture(self):
        hub = Hub(ui_config={"uv": 3.0, "ov": 4.2})
        # Pack with undervoltage cell (base=2.85V < 3.0V)
        p = pack(seq=1, ics=1, cells_per_ic=4, base=2.85)
        hub._on_sample(p)

        frames = hub.freeze_frames()
        self.assertGreaterEqual(len(frames), 1)
        self.assertIn("UNDER_VOLTAGE", frames[0]["reason"])

    def test_hub_can_fault_capture(self):
        hub = Hub()
        hub._on_source_event("summary", {"faults": ["IMD_FAULT", "PRECHARGE_FAIL"]})
        frames = hub.freeze_frames()
        self.assertEqual(len(frames), 1)
        self.assertIn("IMD_FAULT", frames[0]["reason"])


class TestFreezeFrameEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.hub = Hub(ui_config={"uv": 3.0, "ov": 4.2})
        cls.server = MonitorServer(cls.hub, host="127.0.0.1", port=cls.port)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _get(self, path):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_freeze_frames_endpoints(self):
        # Initial empty
        status, data = self._get("/api/freeze-frames")
        self.assertEqual(status, 200)
        self.assertEqual(data["frames"], [])

        # No frame yet
        status, data = self._get("/api/freeze-frame")
        self.assertEqual(status, 404)

        # Trigger capture
        status, data = self._get("/api/freeze-frame/capture?reason=Bench%20Inspection")
        self.assertEqual(status, 200)
        self.assertTrue(data["captured"])
        frame_id = data["frame"]["id"]

        # Fetch latest
        status, latest = self._get("/api/freeze-frame")
        self.assertEqual(status, 200)
        self.assertEqual(latest["id"], frame_id)

        # Fetch by ID
        status, specific = self._get(f"/api/freeze-frame?id={frame_id}")
        self.assertEqual(status, 200)
        self.assertEqual(specific["id"], frame_id)

        # Non-existent ID
        status, err = self._get("/api/freeze-frame?id=FF-9999")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
