"""Argument validation and the settings passed to the browser."""

import os
import shutil
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


if __name__ == "__main__":
    unittest.main()
