"""Replaying a recorded CSV session."""

import os
import tempfile
import time
import unittest

from bms_monitor.hub import Hub
from bms_monitor.logging_csv import CsvLogger
from bms_monitor.model import PackSample
from bms_monitor.sources.replay import ReplaySource

from .packs import pack, reading


def record(path, scenario="ic_dropout", ics=10, cells_per_ic=9, seconds=1.2,
           rate=250.0):
    """Write a made-up session to ``path`` and return the number of rows.

    ``normal``: every IC answers every cycle. ``ic_dropout``: the last IC
    misses one cycle in seven and fails its PEC one cycle in twenty.
    """
    logger = CsvLogger(path)
    for seq in range(int(seconds * rate)):
        t = seq / rate
        readings = []
        for ic in range(1, ics + 1):
            flaky = scenario == "ic_dropout" and ic == ics
            if flaky and seq % 7 == 3:
                continue
            cells = [3.70 + 0.001 * i + 0.01 * (ic - 1) for i in range(cells_per_ic)]
            readings.append(reading(ic=ic, seq=seq, t=t, cells=cells,
                                    pec_ok=not (flaky and seq % 20 == 11)))
        logger.write(PackSample(seq=seq, t_mono=t, t_wall=1_700_000_000.0 + t,
                                readings=tuple(readings), expected_ics=ics,
                                cells_per_ic=cells_per_ic))
    logger.close()
    return logger.rows


class ReplayTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "session.csv")


class TestReplay(ReplayTestCase):
    def test_a_recorded_session_replays_identically(self):
        rows = record(self.path)
        self.assertGreater(rows, 100)

        hub = Hub(history=20000)
        source = ReplaySource(self.path)
        hub.attach(source)
        source.start()
        try:
            deadline = time.monotonic() + 15
            while hub.total_samples < rows and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            source.stop()
        self.assertEqual(hub.total_samples, rows)

    def test_replay_preserves_partial_cycles_and_pec_errors(self):
        record(self.path)
        hub = Hub(history=20000)
        source = ReplaySource(self.path)
        hub.attach(source)
        source.start()
        try:
            time.sleep(1.5)
        finally:
            source.stop()
        packs = hub.samples.snapshot()
        self.assertTrue(any(p.partial for p in packs),
                        "the dropout session lost its partial cycles")
        self.assertTrue(any(not p.pec_ok for p in packs),
                        "the session lost its PEC errors")
        for p in packs:
            if not p.usable:
                self.assertIsNone(p.pack_v)

    def test_replay_reports_the_shape_of_the_recording(self):
        record(self.path)
        source = ReplaySource(self.path)
        status = source.status()
        self.assertEqual(status["mode"], "replay")
        self.assertEqual(status["ics"], 10)
        self.assertEqual(status["cells_per_ic"], 9)
        self.assertGreater(status["cycles"], 100)
        self.assertGreater(status["duration_s"], 0.5)
        self.assertEqual(status["bad_rows"], 0)
        self.assertIn("bms-monitor-csv", status["schema"])

    def test_replay_uses_the_same_hub_and_chain_logic(self):
        """Nothing about the Hub knows this is a file."""
        record(self.path)
        hub = Hub(history=20000)
        source = ReplaySource(self.path)
        hub.attach(source)
        source.start()
        try:
            time.sleep(1.5)
        finally:
            source.stop()
        chain = hub.chain_state()
        self.assertEqual(len(chain), 10)
        self.assertEqual(chain[0]["state"], "ok")
        self.assertNotEqual(chain[9]["state"], "ok")
        snap = hub.snapshot()
        self.assertEqual(snap["expected_ics"], 10)
        self.assertEqual(snap["cells_per_ic"], 9)

    def test_round_trip_of_a_single_ic_session(self):
        record(self.path, scenario="normal", ics=1, cells_per_ic=16, seconds=0.8)
        source = ReplaySource(self.path)
        self.assertEqual(source.status()["ics"], 1)
        self.assertEqual(source.status()["cells_per_ic"], 16)

    def test_history_limit_keeps_the_tail_and_says_so(self):
        record(self.path)
        source = ReplaySource(self.path, limit=100)
        self.assertEqual(source.status()["cycles"], 100)
        self.assertIn("showing the last 100", source.status()["last_error"])

    def test_streaming_mode_paces_itself(self):
        """At 1x a 2 s recording must take about 2 s, not arrive at once."""
        total = record(self.path, scenario="normal", ics=2, cells_per_ic=9,
                       seconds=2.0, rate=100)
        hub = Hub(history=20000)
        source = ReplaySource(self.path, load_all=False, speed=1.0)
        hub.attach(source)
        source.start()
        try:
            time.sleep(0.5)
            partway = hub.total_samples
            time.sleep(1.0)
            later = hub.total_samples
        finally:
            source.stop()
        self.assertGreater(partway, 0, "streaming replay published nothing")
        self.assertLess(partway, total, "streaming replay dumped the whole file")
        self.assertGreater(later, partway, "streaming replay did not advance")

    def test_load_all_mode_publishes_everything_immediately(self):
        total = record(self.path, scenario="normal", ics=2, cells_per_ic=9,
                       seconds=1.0, rate=100)
        hub = Hub(history=20000)
        source = ReplaySource(self.path, load_all=True)
        hub.attach(source)
        source.start()
        try:
            deadline = time.monotonic() + 8
            while hub.total_samples < total and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            source.stop()
        self.assertEqual(hub.total_samples, total)

    def test_empty_file_is_a_clear_startup_error(self):
        open(self.path, "w").close()
        with self.assertRaises(ValueError):
            ReplaySource(self.path)

    def test_missing_file_is_a_clear_startup_error(self):
        with self.assertRaises(FileNotFoundError):
            ReplaySource(os.path.join(self.tmp, "nope.csv"))

    def test_header_only_file_is_refused(self):
        logger = CsvLogger(self.path)
        logger.write(pack(seq=0, ics=2, cells_per_ic=9))
        logger.close()
        # Strip the single data row, leaving comment + header.
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines[:2]) + "\n")
        with self.assertRaises(ValueError):
            ReplaySource(self.path)

    def test_a_truncated_tail_is_skipped_not_fatal(self):
        record(self.path, scenario="normal", ics=2, cells_per_ic=9, seconds=0.5)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("1700000000.000,9.9,999,2\n")
        source = ReplaySource(self.path)
        self.assertEqual(source.status()["bad_rows"], 1)
        self.assertGreater(source.status()["cycles"], 10)

    def test_replay_exposes_no_transmit_path(self):
        public = {n for n in dir(ReplaySource) if not n.startswith("_")}
        self.assertEqual(public, {"start", "stop", "subscribe", "status",
                                  "name", "duration_s"})

    def test_stop_is_idempotent(self):
        record(self.path, scenario="normal", ics=1, cells_per_ic=16, seconds=0.4)
        source = ReplaySource(self.path)
        source.start()
        time.sleep(0.3)
        source.stop()
        source.stop()
        self.assertEqual(source.status()["link"], "down")


class TestReplayThroughTheWebApi(ReplayTestCase):
    def test_snapshot_marks_the_mode_and_carries_the_history(self):
        import json
        import urllib.request

        from bms_monitor.web.server import MonitorServer

        record(self.path, scenario="normal", ics=10, cells_per_ic=9, seconds=0.8)
        hub = Hub(history=20000, ui_config={"ics": 10, "cells_per_ic": 9})
        source = ReplaySource(self.path)
        hub.attach(source)
        server = MonitorServer(hub, host="127.0.0.1", port=0)
        server.start()
        source.start()
        try:
            deadline = time.monotonic() + 10
            while hub.total_samples < 50 and time.monotonic() < deadline:
                time.sleep(0.05)
            with urllib.request.urlopen(server.url + "api/snapshot", timeout=5) as r:
                data = json.load(r)
        finally:
            source.stop()
            server.stop()
        self.assertEqual(data["stats"]["source"]["mode"], "replay")
        self.assertEqual(data["expected_ics"], 10)
        self.assertEqual(data["cells_per_ic"], 9)
        self.assertGreater(len(data["samples"]), 40)
        self.assertEqual(len(data["samples"][0]["ics"][0]["cells"]), 9)


if __name__ == "__main__":
    unittest.main()
