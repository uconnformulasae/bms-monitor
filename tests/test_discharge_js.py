"""Runs charts.js's discharge helpers in node: which cells the dashboard marks
as discharging. Skipped when node isn't installed."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_PATH = os.path.join(ROOT, "bms_monitor", "web", "static", "charts.js")
NODE = shutil.which("node")

HARNESS = r"""
global.window = {};
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const C = global.window.Charts;
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = cases.map((c) => {
  if (c.fn === 'dccChannels') return C.dccChannels(c.mask);
  return C.dischargingCells(c.pack, c.live).map((d) => [d.ic, d.cell, d.key, d.source]);
});
process.stdout.write(JSON.stringify(out));
"""


def pack(masks, cells_per_ic=10):
    return {"cells_per_ic": cells_per_ic,
            "ics": [{"ic": i + 1, "balance_mask": m} for i, m in enumerate(masks)]}


def discharging(pack_=None, live=True):
    return {"fn": "dischargingCells", "pack": pack_, "live": live}


CASES = [
    ({"fn": "dccChannels", "mask": 0}, []),
    ({"fn": "dccChannels", "mask": (1 << 0) | (1 << 5) | (1 << 15)}, ["C1", "C6", "C16"]),
    ({"fn": "dccChannels", "mask": None}, []),
    # The DCC bits are the chips' own readback, so they are shown whatever the
    # balance state: a chip still discharging after a stop stays visible.
    (discharging(pack([1 << 2, 1 << 5])), [[1, 3, "IC1C3", "DCC"], [2, 6, "IC2C6", "DCC"]]),
    (discharging(pack([0x3FF, 0])), [[1, c, "IC1C%d" % c, "DCC"] for c in range(1, 11)]),
    (discharging(), []),
    # Cut to the populated cells; an IC with no report is skipped.
    (discharging(pack([(1 << 0) | (1 << 12), 1 << 9])),
     [[1, 1, "IC1C1", "DCC"], [2, 10, "IC2C10", "DCC"]]),
    (discharging(pack([1 << 3, None])), [[1, 4, "IC1C4", "DCC"]]),
    # Not live: nothing, however recent the pack looks.
    (discharging(pack([1 << 3]), live=False), []),
]


@unittest.skipUnless(NODE, "node is not installed")
class TestDischargeHelpers(unittest.TestCase):
    def test_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.js")
            cases = os.path.join(tmp, "cases.json")
            with open(harness, "w") as handle:
                handle.write(HARNESS)
            with open(cases, "w") as handle:
                json.dump([c for c, _ in CASES], handle)
            out = subprocess.run([NODE, harness, JS_PATH, cases], check=True,
                                 capture_output=True, text=True).stdout
        for (case, expected), got in zip(CASES, json.loads(out)):
            self.assertEqual(got, expected, case)


if __name__ == "__main__":
    unittest.main()
