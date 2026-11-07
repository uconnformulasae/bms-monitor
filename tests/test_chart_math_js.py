"""Runs charts.js in node and checks its output matches chart_twin.py.

test_chart_math.py tests the Python copy; this checks the browser code gives
the same answers. Skipped when node isn't installed.
"""

import json
import os
import shutil
import subprocess
import unittest

from . import chart_twin as M

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_PATH = os.path.join(ROOT, "bms_monitor", "web", "static", "charts.js")
NODE = shutil.which("node")

HARNESS = r"""
global.window = {};
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const C = global.window.Charts;
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = [];
for (const c of cases) {
  const fn = C[c.fn];
  let r;
  switch (c.fn) {
    case 'scaleLinear': r = fn(c.a[0], c.a[1], c.a[2], c.a[3], c.a[4]); break;
    case 'scaleInvert': r = fn(c.a[0], c.a[1], c.a[2], c.a[3], c.a[4]); break;
    case 'niceStep':    r = fn(c.a[0]); break;
    case 'decimalsFor': r = fn(c.a[0]); break;
    case 'niceTicks':   r = fn(c.a[0], c.a[1], c.a[2]).map(t => t.label); break;
    case 'autoscale':   r = fn(c.a[0], c.a[1] || {}); break;
    case 'segmentPaths':r = fn(c.a[0]).map(s => s.map(p => p.x)); break;
    case 'windowSlice': r = fn(c.a[0], c.a[1], c.a[2]).map(s => s.t); break;
    case 'nearestIndex':r = fn(c.a[0], c.a[1]); break;
    default: throw new Error('unknown fn ' + c.fn);
  }
  out.push(r);
}
process.stdout.write(JSON.stringify(out));
"""


def _pts(flags):
    return [{"x": float(i), "y": float(i), "ok": f} for i, f in enumerate(flags)]


def _samples(ts):
    return [{"t": float(t)} for t in ts]


CASES = [
    {"fn": "scaleLinear", "a": [0.5, 0, 1, 0, 100]},
    {"fn": "scaleLinear", "a": [3.7, 3.7, 3.7, 0, 100]},
    {"fn": "scaleLinear", "a": [3.71, 3.6, 3.8, 300, 20]},
    {"fn": "scaleInvert", "a": [150, 3.0, 4.2, 20, 300]},
    {"fn": "scaleInvert", "a": [50, 3.0, 4.2, 10, 10]},
    {"fn": "niceStep", "a": [0.0009]},
    {"fn": "niceStep", "a": [0.0011]},
    {"fn": "niceStep", "a": [23.0]},
    {"fn": "niceStep", "a": [0]},
    {"fn": "decimalsFor", "a": [0.001]},
    {"fn": "decimalsFor", "a": [50]},
    {"fn": "niceTicks", "a": [3.698, 3.702, 5]},
    {"fn": "niceTicks", "a": [3.70000, 3.70040, 5]},
    {"fn": "niceTicks", "a": [3.7, 3.7, 5]},
    {"fn": "niceTicks", "a": [3.0, 4.2, 5]},
    {"fn": "niceTicks", "a": [-0.5, 0.5, 4]},
    {"fn": "niceTicks", "a": [0, 600, 5]},
    {"fn": "autoscale", "a": [[3.7] * 16]},
    {"fn": "autoscale", "a": [[]]},
    {"fn": "autoscale", "a": [[3.0, 4.0], {"pad": 0.1, "minSpan": 0.001}]},
    {"fn": "autoscale", "a": [[3.6, 3.8]]},
    {"fn": "segmentPaths", "a": [_pts([True, True, False, True, True])]},
    {"fn": "segmentPaths", "a": [_pts([False, True, False])]},
    {"fn": "segmentPaths", "a": [_pts([False] * 4)]},
    {"fn": "segmentPaths", "a": [_pts([True, False] * 4)]},
    {"fn": "windowSlice", "a": [_samples(range(11)), 10.0, 3.0]},
    {"fn": "windowSlice", "a": [_samples([0, 1, 2]), 2.0, 1000.0]},
    {"fn": "nearestIndex", "a": [_samples([0, 1, 2]), 1.4]},
    {"fn": "nearestIndex", "a": [_samples([0, 1, 2]), 1.6]},
    {"fn": "nearestIndex", "a": [_samples([10, 11, 12]), -100.0]},
]


def _python_result(case):
    fn, a = case["fn"], case["a"]
    if fn == "scaleLinear":
        return M.scale_linear(*a)
    if fn == "scaleInvert":
        return M.scale_invert(*a)
    if fn == "niceStep":
        return M.nice_step(a[0])
    if fn == "decimalsFor":
        return M.decimals_for(a[0])
    if fn == "niceTicks":
        return [t["label"] for t in M.nice_ticks(a[0], a[1], a[2])]
    if fn == "autoscale":
        opts = a[1] if len(a) > 1 else {}
        return M.autoscale(a[0],
                           pad=opts.get("pad", 0.08),
                           min_span=opts.get("minSpan", 0.004))
    if fn == "segmentPaths":
        return [[p["x"] for p in seg] for seg in M.segment_paths(a[0])]
    if fn == "windowSlice":
        return [s["t"] for s in M.window_slice(a[0], a[1], a[2])]
    if fn == "nearestIndex":
        return M.nearest_index(a[0], a[1])
    raise AssertionError(fn)


def _close(a, b, tol=1e-9):
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol * max(1.0, abs(a), abs(b))
    return a == b


@unittest.skipUnless(NODE, "node is not installed; JS/Python twin equivalence NOT verified")
class TestJsMatchesPythonTwin(unittest.TestCase):
    def test_every_case_agrees(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            harness = os.path.join(tmp, "harness.js")
            cases = os.path.join(tmp, "cases.json")
            with open(harness, "w", encoding="utf-8") as handle:
                handle.write(HARNESS)
            with open(cases, "w", encoding="utf-8") as handle:
                json.dump(CASES, handle)
            proc = subprocess.run(
                [NODE, harness, JS_PATH, cases],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        js_results = json.loads(proc.stdout)
        self.assertEqual(len(js_results), len(CASES))

        for case, js in zip(CASES, js_results):
            py = _python_result(case)
            self.assertTrue(
                _close(py, js),
                "%s%s\n  python: %r\n  js    : %r" % (case["fn"], case["a"], py, js),
            )


if __name__ == "__main__":
    unittest.main()
