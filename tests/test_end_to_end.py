"""The CLI end to end: argument checks, a short run, CSV output."""

import ast
import io
import os
import shutil
import sys
import tempfile
import unittest

from bms_monitor.cli import main
from bms_monitor.hub import Hub
from bms_monitor.logging_csv import CsvLogger
from bms_monitor.web.server import MonitorServer

from .packs import pack


def free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.session = os.path.join(cls.tmp, "session.csv")
        logger = CsvLogger(cls.session)
        for seq in range(50):
            logger.write(pack(seq=seq, ics=2, cells_per_ic=9))
        logger.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = main(argv)
        finally:
            sys.stdout, sys.stderr = old
        return code, out.getvalue(), err.getvalue()

    def test_list_commands(self):
        code, out, _ = self.run_main(["--list-commands"])
        self.assertEqual(code, 0)
        self.assertIn("Denied", out)
        self.assertIn("LOOP MEASUREMENT", out)

    def test_rejects_a_denied_poll_command(self):
        for spec in ("1", "16", "4,17"):
            code, _, err = self.run_main(
                ["--poll", spec, "--no-browser", "--duration", "0.1"])
            self.assertEqual(code, 2, spec)
            self.assertIn("read-only", err)

    def test_rejects_a_non_numeric_poll_command(self):
        code, _, err = self.run_main(
            ["--poll", "4;1", "--no-browser", "--duration", "0.1"])
        self.assertEqual(code, 2)
        self.assertIn("not a plain number", err)

    def test_short_replay_run(self):
        code, out, _ = self.run_main(
            ["--source", "replay", "--file", self.session, "--no-browser",
             "--duration", "0.5", "--http-port", str(free_port())])
        self.assertEqual(code, 0)
        self.assertIn("samples:", out)
        self.assertIn("loaded     : 50 cycles", out)

    def test_csv_output(self):
        path = os.path.join(self.tmp, "run.csv")
        code, out, _ = self.run_main(
            ["--source", "replay", "--file", self.session, "--no-browser",
             "--duration", "0.5", "--csv", path, "--http-port", str(free_port())])
        self.assertEqual(code, 0)
        self.assertIn("csv written", out)
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertGreater(len(lines), 5)
        self.assertTrue(lines[0].startswith("# bms-monitor-csv/"), lines[0])
        self.assertTrue(lines[1].startswith("t_wall,"), lines[1])

    def test_port_already_in_use_is_reported(self):
        port = free_port()
        hub = Hub()
        server = MonitorServer(hub, host="127.0.0.1", port=port)
        server.start()
        try:
            code, _, err = self.run_main(
                ["--source", "serial", "--no-browser", "--duration", "0.2",
                 "--http-port", str(port)])
            self.assertEqual(code, 2)
            self.assertIn("cannot bind", err)
        finally:
            server.stop()


class TestClock(unittest.TestCase):
    def test_deltas_use_monotonic_time(self):
        """time.time() is only for the wall-clock column (t_wall). Anything
        that measures a duration uses time.monotonic(), so a clock change
        mid-session cannot make a rate negative."""
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bms_monitor")
        offenders = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d != "__pycache__" and not d.startswith(".")]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as handle:
                    src = handle.read()
                tree = ast.parse(src, filename=path)
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "time"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "time"):
                        line = src.splitlines()[node.lineno - 1]
                        if "t_wall" in line:
                            continue
                        offenders.append("%s:%d %s" % (name, node.lineno, line.strip()))
        self.assertEqual(offenders, [],
                         "time.time() used for a delta; use time.monotonic()")


if __name__ == "__main__":
    unittest.main()
