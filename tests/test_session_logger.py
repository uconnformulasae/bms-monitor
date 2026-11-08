"""Unit tests for SessionLogger."""

import os
import shutil
import tempfile
import time
import unittest

from bms_monitor.session_logger import SessionLogger
from .packs import pack


class TestSessionLogger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.logger = SessionLogger(log_dir=self.tmp)

    def tearDown(self):
        self.logger.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_and_stop_lifecycle(self):
        self.assertFalse(self.logger.is_active)
        status1 = self.logger.start("test_log")
        self.assertTrue(status1["active"])
        self.assertTrue(self.logger.is_active)
        self.assertIn("test_log", status1["filename"])

        # Redundant start returns current status
        status2 = self.logger.start("test_log_2")
        self.assertEqual(status1["filename"], status2["filename"])

        status3 = self.logger.stop()
        self.assertFalse(status3["active"])
        self.assertFalse(self.logger.is_active)

        # Redundant stop returns current status
        status4 = self.logger.stop()
        self.assertFalse(status4["active"])

    def test_logging_frames_and_samples(self):
        self.logger.start("run")
        self.logger.log_can_frame({
            "t_wall": 1700000000.0,
            "t_mono": 1.23,
            "id": 0x600,
            "id_hex": "0x600",
            "dlc": 8,
            "data_hex": "AABBCCDD",
            "kind": "summary",
            "details": "summary test",
        })

        p = pack(seq=1, ics=2, cells_per_ic=9)
        self.logger.log_sample(p)

        self.logger.log_temps({
            "t": 1.5,
            "sensors": [{"celsius": 25.1}, {"celsius": 24.9}],
            "id_hex": "0x730",
        })

        self.logger.log_event("alert", "over-temperature warning")

        self.logger.stop()

        path = self.logger.get_latest_log_path()
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))

        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()

        self.assertIn("timestamp,t_mono,source", content)
        self.assertIn("0x600", content)
        self.assertIn("summary test", content)
        self.assertIn("sample", content)
        self.assertIn("sensors=[25.1;24.9]", content)
        self.assertIn("over-temperature warning", content)

    def test_inactive_logger_ignores_logs(self):
        # Must not raise when inactive
        self.logger.log_can_frame({})
        self.logger.log_sample(None)
        self.logger.log_temps({})
        self.logger.log_event("info", "test")
        self.assertIsNone(self.logger.get_latest_log_path())


if __name__ == "__main__":
    unittest.main()
