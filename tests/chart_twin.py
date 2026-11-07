"""Python copy of the pure maths functions in web/static/charts.js.

test_chart_math.py tests these and checks both files define the same
functions. If you change one file, change the other to match.
"""

from __future__ import annotations

import math
from typing import List, Optional


def scale_linear(v, d0, d1, r0, r1):
    span = d1 - d0
    if not math.isfinite(span) or span == 0:
        return (r0 + r1) / 2
    return r0 + ((v - d0) / span) * (r1 - r0)


def scale_invert(p, d0, d1, r0, r1):
    rspan = r1 - r0
    if not math.isfinite(rspan) or rspan == 0:
        return d0
    return d0 + ((p - r0) / rspan) * (d1 - d0)


def nice_step(raw):
    if not math.isfinite(raw) or raw <= 0:
        return 1
    exp = math.floor(math.log10(raw))
    pow10 = 10 ** exp
    frac = raw / pow10
    if frac <= 1:
        mult = 1
    elif frac <= 2:
        mult = 2
    elif frac <= 5:
        mult = 5
    else:
        mult = 10
    return mult * pow10


def decimals_for(step):
    if not math.isfinite(step) or step <= 0:
        return 0
    return max(0, min(6, math.ceil(-math.log10(step))))


def format_fixed(v, dp):
    x = 0.0 if v == 0 else v          # normalise -0.0
    return "%.*f" % (dp, x)


def nice_ticks(lo, hi, target=5):
    if not math.isfinite(lo) or not math.isfinite(hi):
        return []
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        return [{"v": lo, "label": format_fixed(lo, 3)}]

    step = nice_step((hi - lo) / max(1, target))
    first = math.ceil(lo / step - 1e-9) * step
    raw = []
    v, i = first, 0
    while v <= hi + step * 1e-9 and i < 200:
        raw.append(round(v / step) * step)
        v += step
        i += 1
    if not raw:
        raw.append(lo)

    for dp in range(decimals_for(step), 7):
        labels = [format_fixed(x, dp) for x in raw]
        if len(set(labels)) == len(labels):
            return [{"v": raw[k], "label": labels[k]} for k in range(len(raw))]
    return [{"v": x, "label": format_fixed(x, 6)} for x in raw]


def autoscale(values, pad=0.08, min_span=0.004, fallback=(3.0, 4.2)):
    lo, hi = math.inf, -math.inf
    for v in values:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if not math.isfinite(v):
            continue
        lo = min(lo, v)
        hi = max(hi, v)
    if lo == math.inf:
        return [fallback[0], fallback[1]]
    if hi - lo < min_span:
        mid = (hi + lo) / 2
        lo = mid - min_span / 2
        hi = mid + min_span / 2
    else:
        p = (hi - lo) * pad
        lo -= p
        hi += p
    if not hi > lo:
        hi = lo + (min_span or 1)
    return [lo, hi]


def segment_paths(points):
    """Break a series at every unusable point.

    A single good point between two bad ones comes back as a one-point
    segment, which the renderer draws as a dot. Dropping it would hide a
    real reading.
    """
    out: List[list] = []
    cur: Optional[list] = None
    for p in points:
        ok = (
            p is not None
            and p.get("ok") is True
            and isinstance(p.get("x"), (int, float)) and math.isfinite(p["x"])
            and isinstance(p.get("y"), (int, float)) and math.isfinite(p["y"])
        )
        if ok:
            if cur is None:
                cur = []
                out.append(cur)
            cur.append(p)
        else:
            cur = None
    return out


def window_slice(samples, t_end, window_s):
    t0 = t_end - window_s
    lo, hi = 0, len(samples)
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid]["t"] < t0:
            lo = mid + 1
        else:
            hi = mid
    out = []
    for i in range(lo, len(samples)):
        if samples[i]["t"] > t_end:
            break
        out.append(samples[i])
    return out


def nearest_index(samples, t):
    n = len(samples)
    if n == 0:
        return -1
    lo, hi = 0, n - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if samples[mid]["t"] < t:
            lo = mid + 1
        else:
            hi = mid
    best = lo
    if lo > 0 and abs(samples[lo - 1]["t"] - t) <= abs(samples[lo]["t"] - t):
        best = lo - 1
    return best


# ----------------------------------------------------------------------
# Cell-view layout: which panels the drill-down draws.
#
# Getting this wrong puts a cell under the wrong chip or segment board,
# which sends someone to the wrong part of the pack.
# ----------------------------------------------------------------------


def segment_of(ic, ics_per_segment):
    if not ics_per_segment or ics_per_segment <= 0:
        return 1
    return (ic - 1) // ics_per_segment + 1


def segment_count(ics, ics_per_segment):
    if not ics or ics <= 0:
        return 0
    if not ics_per_segment or ics_per_segment <= 0:
        return ics
    return -(-ics // ics_per_segment)


def segment_ics(segment, ics, ics_per_segment):
    per = ics_per_segment if ics_per_segment and ics_per_segment > 0 else 1
    return [ic for ic in range((segment - 1) * per + 1, segment * per + 1)
            if ic <= ics]


def cell_view_panels(ics, ics_per_segment, focus_segment):
    segs = segment_count(ics, ics_per_segment)
    drilled = (focus_segment is not None
               and focus_segment >= 1 and focus_segment <= segs)
    panels = []

    if segs > 1 and not drilled:
        for s in range(1, segs + 1):
            members = segment_ics(s, ics, ics_per_segment)
            panels.append({
                "key": "S%d" % s, "label": "S%d" % s, "segment": s,
                "ics": members,
                "sub": ("IC%d–IC%d" % (members[0], members[-1])
                        if members else ""),
            })
        return panels

    members = (segment_ics(focus_segment, ics, ics_per_segment) if drilled
               else segment_ics(1, ics, max(ics, 1)))
    for ic in members:
        panels.append({
            "key": "IC%d" % ic, "label": "IC%d" % ic, "ic": ic, "ics": [ic],
            "sub": ("S%d" % segment_of(ic, ics_per_segment)) if segs > 1 else "",
        })
    return panels


def panel_columns(n):
    if n <= 1:
        return 1
    if n <= 3:
        return n
    if n == 4:
        return 2
    if n <= 5:
        return 5
    if n <= 6:
        return 3
    return 5


def cell_view_height(panel_count):
    rows = -(-panel_count // panel_columns(panel_count))
    return 300 if rows == 1 else max(160, round(340 / rows)) * rows + 54
