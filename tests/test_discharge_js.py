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
  if (c.fn === 'balanceProgress') {
    const p = C.balanceProgress(c.summary, c.pack);
    return p && { target: Math.round(p.target * 1e5) / 1e5,
                  above: p.above.map((a) => [a.ic, a.cell, Math.round(a.mv * 10) / 10]) };
  }
  return C.dischargingCells(c.summary, c.pack, c.live).map((d) => [d.ic, d.cell, d.key, d.source]);
});
process.stdout.write(JSON.stringify(out));
"""


def pack(masks, cells_per_ic=10):
    return {"cells_per_ic": cells_per_ic,
            "ics": [{"ic": i + 1, "balance_mask": m} for i, m in enumerate(masks)]}


def volts_pack(rows):
    return {"cells_per_ic": len(rows[0]),
            "ics": [{"ic": i + 1, "cells": cells} for i, cells in enumerate(rows)]}


def discharging(summary, pack_=None, live=True):
    return {"fn": "dischargingCells", "summary": summary, "pack": pack_, "live": live}


def progress(summary, pack_):
    return {"fn": "balanceProgress", "summary": summary, "pack": pack_}


ROUND = {"balance_state": "discharging", "balance_ic": 1, "balance_cell": 1,
         "balance_margin_mv": 1.0}

CASES = [
    ({"fn": "dccChannels", "mask": 0}, []),
    ({"fn": "dccChannels", "mask": (1 << 0) | (1 << 5) | (1 << 15)}, ["C1", "C6", "C16"]),
    ({"fn": "dccChannels", "mask": None}, []),
    # A round names the lowest group in 0x6FF, not a discharged one, so the
    # marks come from the DCC bits.
    (discharging(ROUND, pack([1 << 2, 1 << 5])), [[1, 3, "IC1C3", "DCC"], [2, 6, "IC2C6", "DCC"]]),
    (discharging(ROUND), []),
    # Resting, balanced or stopped: DCC bits left in the last 2 Hz frame are not shown.
    (discharging(dict(ROUND, balance_state="resting"), pack([0x3FF, 0x3FF])), []),
    (discharging(dict(ROUND, balance_state="balanced"), pack([0x3FF, 0x3FF])), []),
    (discharging({"balance_state": "too_hot", "balance_ic": 2}, pack([0x3FF, 0x3FF])), []),
    (discharging({"balance_state": "cell_low", "balance_ic": 1, "balance_cell": 4},
                 pack([0x3FF, 0x3FF])), []),
    (discharging({"balance_state": "pending"}, pack([0, 1 << 5])), []),
    # Not balancing: the DCC bits are shown, cut to the populated cells.
    (discharging({"balance_state": "off"}, pack([(1 << 0) | (1 << 12), 1 << 9])),
     [[1, 1, "IC1C1", "DCC"], [2, 10, "IC2C10", "DCC"]]),
    (discharging(None, pack([1 << 3, None])), [[1, 4, "IC1C4", "DCC"]]),
    # Not live: nothing, however recent the summary looks.
    (discharging(ROUND, pack([1 << 3]), live=False), []),
    # Target is the lowest group plus the margin; the rest is how far above it.
    (progress(ROUND, volts_pack([[3.5950, 3.6000, 3.5955], [3.5962, 3.5958, 3.6031]])),
     {"target": 3.596, "above": [[2, 3, 7.1], [1, 2, 4.0], [2, 1, 0.2]]}),
    (progress(dict(ROUND, balance_state="balanced"), volts_pack([[3.5950, 3.5955]])),
     {"target": 3.596, "above": []}),
    # Nothing to say without a named lowest group, a margin, or its reading.
    (progress({"balance_state": "off"}, volts_pack([[3.6]])), None),
    (progress(dict(ROUND, balance_margin_mv=None), volts_pack([[3.6]])), None),
    (progress(dict(ROUND, balance_ic=3), volts_pack([[3.6]])), None),
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
