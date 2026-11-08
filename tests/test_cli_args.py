"""Argument validation and the settings passed to the browser."""

import os
import shutil
import sys
import tempfile
import unittest

from bms_monitor import cli
from bms_monitor.logging_csv import CsvLogger

from .packs import pack


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


class TestIcsPerSegment(unittest.TestCase):
    """The cell view groups the chain into segment boards with this."""

    def test_it_reaches_the_browser_config(self):
        args = parse("--ics", "10", "--ics-per-segment", "2")
        config = cli._ui_config(args, (4,))
        self.assertEqual(config["ics_per_segment"], 2)
        self.assertEqual(config["ics"], 10)

    def test_default_is_the_two_chip_segment_board(self):
        config = cli._ui_config(parse(), (4,))
        self.assertEqual(config["ics_per_segment"], 2)


class TestValidation(unittest.TestCase):
    """Runs the real CLI for a moment against a short recorded session."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.csv = os.path.join(cls.tmp, "session.csv")
        logger = CsvLogger(cls.csv)
        for seq in range(20):
            logger.write(pack(seq=seq, ics=2, cells_per_ic=9))
        logger.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cli(self, *argv):
        return cli.main(["--source", "replay", "--file", self.csv,
                         "--duration", "0.3", "--no-browser", "--http-port", "0"]
                        + list(argv))

    def test_the_default_chain_settings_start(self):
        # --ics defaults to 1 and --ics-per-segment to 2. A single chip must
        # not be rejected for being smaller than one segment board.
        self.assertEqual(self.run_cli(), 0)

    def test_a_partly_populated_board_is_not_an_error(self):
        self.assertEqual(self.run_cli("--ics", "1", "--ics-per-segment", "2"), 0)

    def test_the_whole_car_starts(self):
        self.assertEqual(
            self.run_cli("--ics", "10", "--cells-per-ic", "9",
                         "--ics-per-segment", "2"), 0)

    def test_nonsense_segment_sizes_are_refused(self):
        for bad in ("0", "-1", "17"):
            self.assertEqual(self.run_cli("--ics-per-segment", bad), 2, bad)

    def test_allow_tx_is_refused_off_can(self):
        self.assertEqual(self.run_cli("--allow-tx"), 2)

    def test_transmit_is_off_by_default(self):
        self.assertFalse(parse("--source", "can").allow_tx)

    def test_list_commands_flag(self):
        self.assertEqual(self.run_cli("--list-commands"), 0)

    def test_can_channels_flag(self):
        self.assertEqual(self.run_cli("--can-channels"), 0)

    def test_invalid_ranges_rejected(self):
        self.assertEqual(self.run_cli("--ics", "0"), 2)
        self.assertEqual(self.run_cli("--ics", "17"), 2)
        self.assertEqual(self.run_cli("--cells-per-ic", "0"), 2)
        self.assertEqual(self.run_cli("--cells-per-ic", "17"), 2)
        self.assertEqual(self.run_cli("--ics-per-segment", "0"), 2)
        self.assertEqual(self.run_cli("--http-port", "70000"), 2)

    def test_replay_without_file_rejected(self):
        self.assertEqual(cli.main(["--source", "replay", "--no-browser"]), 2)

    def test_main_module_execution(self):
        import runpy
        import subprocess
        res = subprocess.run([sys.executable, "-m", "bms_monitor", "--list-commands"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Allowed (read / measure only):", res.stdout)

        from unittest import mock
        with mock.patch("sys.argv", ["bms_monitor", "--list-commands"]):
            with self.assertRaises(SystemExit) as cm:
                runpy.run_module("bms_monitor", run_name="__main__")
            self.assertEqual(cm.exception.code, 0)

    def test_can_source_execution(self):
        ret = cli.main(["--source", "can", "--can-interface", "virtual",
                        "--can-channel", "vcan_cli_test", "--duration", "0.2",
                        "--no-browser", "--http-port", "0"])
        self.assertEqual(ret, 0)

    def test_can_source_with_allow_tx(self):
        ret = cli.main(["--source", "can", "--allow-tx", "--can-interface", "virtual",
                        "--can-channel", "vcan_cli_tx", "--duration", "0.2",
                        "--no-browser", "--http-port", "0"])
        self.assertEqual(ret, 0)

    def test_csv_logging_path(self):
        out_csv = os.path.join(self.tmp, "cli_test.csv")
        ret = self.run_cli("--csv", out_csv)
        self.assertEqual(ret, 0)
        self.assertTrue(os.path.exists(out_csv))

    def test_csv_cannot_open_reports_error(self):
        ret = self.run_cli("--csv", "/nonexistent_dir/impossible/out.csv")
        self.assertEqual(ret, 2)

    def test_http_port_busy_reports_error(self):
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        try:
            ret = self.run_cli("--http-port", str(port))
            self.assertEqual(ret, 2)
        finally:
            sock.close()

    def test_console_flag(self):
        from unittest import mock
        with mock.patch("bms_monitor.console.main", return_value=0):
            ret = cli.main(["--console", "--source", "serial", "--port", "loop://"])
            self.assertEqual(ret, 0)

    def test_try_open_function(self):
        from unittest import mock
        with mock.patch("webbrowser.open", side_effect=Exception("browser error")):
            cli._try_open("http://127.0.0.1:0")

    def test_build_source_unknown_raises(self):
        args = parse()
        args.source = "unknown_source"
        with self.assertRaises(SystemExit):
            cli._build_source(args, (4,))

    def test_build_source_serial(self):
        args = parse("--source", "serial", "--port", "loop://")
        source = cli._build_source(args, (4,))
        self.assertEqual(source.name, "serial")

    def test_serial_source_cli_run(self):
        ret = cli.main(["--source", "serial", "--port", "loop://",
                        "--poll", "4,15", "--duration", "0.2",
                        "--no-browser", "--http-port", "0"])
        self.assertEqual(ret, 0)

    def test_build_source_error_exits_2(self):
        from unittest import mock
        with mock.patch("bms_monitor.cli._build_source", side_effect=RuntimeError("build fail")):
            self.assertEqual(self.run_cli(), 2)

    def test_source_start_error_exits_2(self):
        from unittest import mock
        with mock.patch("bms_monitor.sources.replay.ReplaySource.start", side_effect=RuntimeError("start fail")):
            self.assertEqual(self.run_cli(), 2)


if __name__ == "__main__":
    unittest.main()
