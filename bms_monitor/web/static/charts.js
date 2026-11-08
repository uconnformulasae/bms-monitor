/* charts.js: chart maths and Canvas 2D rendering.
 *
 * Bad chart maths is easy to miss (a wrong axis still looks like a chart),
 * so all the numeric logic is in pure functions at the top of this file,
 * with no canvas or DOM. Each one has a Python twin in tests/chart_twin.py
 * that is unit tested. The rendering code below just draws their output.
 *
 * If you change a function in the PURE MATHS block, change its twin too.
 * tests/test_chart_math.py checks that both files declare the same names.
 */

'use strict';

/* ======================================================================
 * PURE MATHS (mirrored in tests/chart_twin.py)
 * ====================================================================== */

/* Map a value from a domain onto a pixel range.
 * A zero-width domain maps to the middle of the range instead of
 * NaN/Infinity, which would blank the canvas. */
function scaleLinear(v, d0, d1, r0, r1) {
  const span = d1 - d0;
  if (!isFinite(span) || span === 0) return (r0 + r1) / 2;
  return r0 + ((v - d0) / span) * (r1 - r0);
}

/* Inverse of scaleLinear: pixel back to domain. Used by the crosshair. */
function scaleInvert(p, d0, d1, r0, r1) {
  const rspan = r1 - r0;
  if (!isFinite(rspan) || rspan === 0) return d0;
  return d0 + ((p - r0) / rspan) * (d1 - d0);
}

/* Round a raw step up to the nearest 1/2/5 x 10^n. */
function niceStep(raw) {
  if (!isFinite(raw) || raw <= 0) return 1;
  const exp = Math.floor(Math.log10(raw));
  const pow = Math.pow(10, exp);
  const frac = raw / pow;
  let mult;
  if (frac <= 1) mult = 1;
  else if (frac <= 2) mult = 2;
  else if (frac <= 5) mult = 5;
  else mult = 10;
  return mult * pow;
}

/* Decimal places needed to write `step` without losing it to rounding. */
function decimalsFor(step) {
  if (!isFinite(step) || step <= 0) return 0;
  const d = Math.ceil(-Math.log10(step));
  return Math.max(0, Math.min(6, d));
}

/* Axis ticks as [{v, label}].
 *
 * Edge cases:
 *  - a 4 mV span at 3.700 V: with 3 decimals every label reads "3.700",
 *    so precision goes up (to at most 6 dp) until the labels are distinct.
 *  - lo === hi: returns a single tick instead of looping forever.
 */
function niceTicks(lo, hi, target) {
  target = target || 5;
  if (!isFinite(lo) || !isFinite(hi)) return [];
  if (hi < lo) { const t = lo; lo = hi; hi = t; }
  if (hi === lo) return [{ v: lo, label: formatFixed(lo, 3) }];

  const step = niceStep((hi - lo) / Math.max(1, target));
  const first = Math.ceil(lo / step - 1e-9) * step;
  const raw = [];
  for (let v = first, i = 0; v <= hi + step * 1e-9 && i < 200; v += step, i++) {
    /* Snap away float error like 3.7000000000000004. */
    raw.push(Math.round(v / step) * step);
  }
  if (raw.length === 0) raw.push(lo);

  for (let dp = decimalsFor(step); dp <= 6; dp++) {
    const labels = raw.map((v) => formatFixed(v, dp));
    if (new Set(labels).size === labels.length) {
      return raw.map((v, i) => ({ v: v, label: labels[i] }));
    }
  }
  return raw.map((v) => ({ v: v, label: formatFixed(v, 6) }));
}

function formatFixed(v, dp) {
  /* Normalise -0 to 0 so an axis never shows "-0.000". */
  const x = Object.is(v, -0) ? 0 : v;
  return x.toFixed(dp);
}

/* Pick a display domain for a set of values.
 *
 * Always returns lo < hi. An all-equal series (or a single sample) would
 * otherwise give a zero-height domain and divide by zero later.
 */
function autoscale(values, opts) {
  opts = opts || {};
  const pad = opts.pad === undefined ? 0.08 : opts.pad;
  const minSpan = opts.minSpan === undefined ? 0.004 : opts.minSpan;
  const fallback = opts.fallback === undefined ? [3.0, 4.2] : opts.fallback;

  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (typeof v !== 'number' || !isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (lo === Infinity) return [fallback[0], fallback[1]];

  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  } else {
    const p = (hi - lo) * pad;
    lo -= p; hi += p;
  }
  if (!(hi > lo)) { hi = lo + (minSpan || 1); }
  return [lo, hi];
}

/* Split a series into drawable segments at every unusable point.
 *
 * A PECError:1 frame isn't data. Interpolating across it would draw a
 * smooth line through a value that was never measured, so the path breaks
 * and the gap stays visible.
 *
 * A single good point between two bad ones comes back as a one-point
 * segment. The renderer has to draw it as a dot or it disappears.
 */
function segmentPaths(points) {
  const out = [];
  let cur = null;
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const ok = p && p.ok === true &&
      typeof p.x === 'number' && isFinite(p.x) &&
      typeof p.y === 'number' && isFinite(p.y);
    if (ok) {
      if (cur === null) { cur = []; out.push(cur); }
      cur.push(p);
    } else {
      cur = null;
    }
  }
  return out;
}

/* Samples within [tEnd - windowS, tEnd]. Assumes t is non-decreasing. */
function windowSlice(samples, tEnd, windowS) {
  const t0 = tEnd - windowS;
  let lo = 0, hi = samples.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (samples[mid].t < t0) lo = mid + 1; else hi = mid;
  }
  const out = [];
  for (let i = lo; i < samples.length; i++) {
    if (samples[i].t > tEnd) break;
    out.push(samples[i]);
  }
  return out;
}

/* Index of the sample whose t is closest to `t`. -1 for an empty list. */
function nearestIndex(samples, t) {
  const n = samples.length;
  if (n === 0) return -1;
  let lo = 0, hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (samples[mid].t < t) lo = mid + 1; else hi = mid;
  }
  let best = lo;
  if (lo > 0 && Math.abs(samples[lo - 1].t - t) <= Math.abs(samples[lo].t - t)) {
    best = lo - 1;
  }
  return best;
}

/* ======================================================================
 * RENDERING (draws the output of the maths above)
 * ====================================================================== */

const CSSVAR = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* Size a canvas for the device pixel ratio (otherwise it's blurry on
 * retina screens) and return a ready context. */
function prepCanvas(canvas) {
  const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.floor(rect.width));
  const h = Math.max(1, Math.floor(rect.height));
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr;
    canvas.height = h * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx: ctx, w: w, h: h };
}

function theme() {
  return {
    grid: CSSVAR('--grid') || '#2c2c2a',
    axis: CSSVAR('--axis') || '#383835',
    ink: CSSVAR('--ink') || '#ffffff',
    ink2: CSSVAR('--ink-2') || '#c3c2b7',
    muted: CSSVAR('--muted') || '#898781',
    surface: CSSVAR('--surface-1') || '#1a1a19',
    s1: CSSVAR('--series-1') || '#3987e5',
    s2: CSSVAR('--series-2') || '#d95926',
    critical: CSSVAR('--critical') || '#d03b3b',
    warning: CSSVAR('--warning') || '#fab219',
    good: CSSVAR('--good') || '#0ca30c',
    context: CSSVAR('--context') || '#55544f'
  };
}

const AXIS_FONT = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
const LABEL_FONT = '12px system-ui, -apple-system, "Segoe UI", sans-serif';

function drawYAxis(ctx, ticks, d0, d1, plot, t, opts) {
  opts = opts || {};
  ctx.font = AXIS_FONT;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const tick of ticks) {
    const y = Math.round(scaleLinear(tick.v, d0, d1, plot.y1, plot.y0)) + 0.5;
    if (y < plot.y0 - 1 || y > plot.y1 + 1) continue;
    ctx.strokeStyle = t.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(plot.x0, y);
    ctx.lineTo(plot.x1, y);
    ctx.stroke();
    ctx.fillStyle = t.muted;
    ctx.fillText(opts.format ? opts.format(tick) : tick.label, plot.x0 - 8, y);
  }
  ctx.strokeStyle = t.axis;
  ctx.beginPath();
  ctx.moveTo(plot.x0 + 0.5, plot.y0);
  ctx.lineTo(plot.x0 + 0.5, plot.y1);
  ctx.stroke();
}

function drawEmpty(ctx, w, h, t, message) {
  ctx.font = LABEL_FONT;
  ctx.fillStyle = t.muted;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(message, w / 2, h / 2);
}

/* ---------------------------------------------------------------------
 * Pack helpers. A "pack" is one poll cycle across the whole daisy chain.
 *
 * Only these know how a PackSample is laid out; the charts below just get
 * a flat, chain-ordered cell list.
 * ------------------------------------------------------------------- */

function packUsable(p) {
  return !!p && p.complete === true && p.pec_ok === true && p.stale !== true;
}

function cellKey(ic, cell) { return 'IC' + ic + 'C' + cell; }

/* Channel names for a DCC (discharge) mask, bit 0 = C1. */
function dccChannels(mask) {
  const out = [];
  if (typeof mask !== 'number' || !isFinite(mask)) return out;
  for (let b = 0; b < 16; b++) if ((mask >> b) & 1) out.push('C' + (b + 1));
  return out;
}

/* Balancing states in which the controller owns the DCC bits. */
const BALANCE_TEST_BUSY = {
  pending: 1, discharging: 1, resting: 1, balanced: 1, refused: 1, aborted: 1,
  too_hot: 1, cell_low: 1
};

/* Cells being discharged right now: [{ic, cell, key, source}].
 *
 * - While a balancing round discharges, the DCC bits are used. In the
 *   other balancing states nothing is discharging, even if a 2 Hz per-IC
 *   frame from a moment ago still carries DCC bits.
 * - Otherwise (test off, or firmware without it) the per-IC DCC bits from
 *   0x72n are used, cut to the populated cells like everything else.
 * - Nothing when the data isn't live: a frozen summary would look current. */
function dischargingCells(summary, pack, live) {
  if (!live) return [];
  const sum = summary || {};
  const discharging = sum.balance_state === 'discharging';
  if ((BALANCE_TEST_BUSY[sum.balance_state] && !discharging) || !pack) return [];
  const cpi = pack.cells_per_ic || 16;
  const out = [];
  for (const entry of (pack.ics || [])) {
    const mask = entry.balance_mask;
    if (typeof mask !== 'number' || !mask) continue;
    for (let b = 0; b < cpi && b < 16; b++) {
      if ((mask >> b) & 1) {
        out.push({ ic: entry.ic, cell: b + 1, key: cellKey(entry.ic, b + 1), source: 'DCC' });
      }
    }
  }
  return out;
}

/* Balancing target and what is left, from one pack's readings:
 * {lowest: {ic, cell, v}, target, above: [{ic, cell, key, mv}]}, with
 * target = the lowest group (named in 0x6FF) + the margin, and `above` the
 * groups over it, most first. Null when 0x6FF names no group or margin, or
 * the pack has no reading for that group. Pass a pack taken at rest: during
 * discharge the readings sag. */
function balanceProgress(summary, pack) {
  const sum = summary || {};
  if (!pack || !sum.balance_ic || !sum.balance_cell || typeof sum.balance_margin_mv !== 'number') {
    return null;
  }
  const cells = flatCells(pack);
  const low = cells.find((c) => c.ic === sum.balance_ic && c.cell === sum.balance_cell);
  if (!low || typeof low.v !== 'number') return null;
  const target = low.v + sum.balance_margin_mv / 1000;
  const above = cells
    .filter((c) => typeof c.v === 'number' && c.v > target + 1e-9)
    .map((c) => ({ ic: c.ic, cell: c.cell, key: c.key, mv: (c.v - target) * 1000 }))
    .sort((a, b) => b.mv - a.mv);
  return { lowest: { ic: low.ic, cell: low.cell, v: low.v }, target: target, above: above };
}

/* [{ic, cell, v, key}] in chain order, masked to the populated count. */
function flatCells(pack) {
  const out = [];
  if (!pack || !pack.ics) return out;
  for (const entry of pack.ics) {
    for (let i = 0; i < entry.cells.length; i++) {
      out.push({ ic: entry.ic, cell: i + 1, v: entry.cells[i],
                 key: cellKey(entry.ic, i + 1), icPecOk: entry.pec_ok !== false });
    }
  }
  return out;
}

/* packStats for a subset of chips.
 *
 * When drilled into one segment board, min/max need to be the extremes on
 * that board, since the pack-wide ones are usually on another board. The
 * charts still draw the full-pack mean line. */
function packStatsFor(pack, allowIc) {
  if (!packUsable(pack)) return null;
  const cells = flatCells(pack).filter((c) => allowIc(c.ic));
  if (!cells.length) return null;
  let lo = cells[0], hi = cells[0], sum = 0;
  for (const c of cells) {
    if (c.v < lo.v) lo = c;
    if (c.v > hi.v) hi = c;
    sum += c.v;
  }
  return {
    cells: cells, min: lo, max: hi, sum: sum,
    mean: sum / cells.length,
    spreadMv: (hi.v - lo.v) * 1000
  };
}

function packStats(pack) {
  if (!packUsable(pack)) return null;
  const cells = flatCells(pack);
  if (!cells.length) return null;
  let lo = cells[0], hi = cells[0], sum = 0;
  for (const c of cells) {
    if (c.v < lo.v) lo = c;
    if (c.v > hi.v) hi = c;
    sum += c.v;
  }
  return {
    cells: cells, min: lo, max: hi, sum: sum,
    mean: sum / cells.length,
    spreadMv: (hi.v - lo.v) * 1000
  };
}

/* Expected chain-order keys, built from the config rather than from the
 * ICs that answered, so a silent IC keeps its slot. */
function expectedKeys(ics, cellsPerIc) {
  const out = [];
  for (let ic = 1; ic <= ics; ic++) {
    for (let c = 1; c <= cellsPerIc; c++) out.push({ ic: ic, cell: c, key: cellKey(ic, c) });
  }
  return out;
}

function valueFor(pack, ic, cell) {
  if (!pack || !pack.ics) return null;
  for (const entry of pack.ics) {
    if (entry.ic !== ic) continue;
    if (entry.pec_ok === false) return null;
    const v = entry.cells[cell - 1];
    return (typeof v === 'number' && isFinite(v)) ? v : null;
  }
  return null;                    /* this IC said nothing this cycle */
}

/* ---------------------------------------------------------------------
 * Cell view: small multiples with a segment-board drill-down.
 *
 * Not a bar chart: bars from zero show magnitude, but what matters here is
 * deviation from the pack mean (~20 mV on ~3700 mV). Not ninety dots on
 * one axis either. The panels match the physical layout of the pack.
 *
 * Two levels:
 *
 *   pack level     one panel per segment board (2 chips, 18 cells each),
 *                  so five panels on the car. Shows whether one board
 *                  differs from the others.
 *   segment level  one panel per IC on the chosen board, with the vertical
 *                  scale narrowed to that board's cells. Shows which cell
 *                  on the board is the outlier.
 *
 * Narrowing the scale is the zoom: one axis across all ninety cells
 * flattens a 3 mV difference to nothing.
 *
 * A chain with only one segment board skips the pack level.
 *
 * A silent IC keeps its slot and is labelled SILENT, so the remaining
 * chips are never renumbered.
 * ------------------------------------------------------------------- */

/* Which segment board an IC sits on. Both 1-based. */
function segmentOf(ic, icsPerSegment) {
  if (!(icsPerSegment > 0)) return 1;
  return Math.floor((ic - 1) / icsPerSegment) + 1;
}

/* How many segment boards a chain of `ics` chips covers. */
function segmentCount(ics, icsPerSegment) {
  if (!(ics > 0)) return 0;
  if (!(icsPerSegment > 0)) return ics;
  return Math.ceil(ics / icsPerSegment);
}

/* The chips on one board, clipped to the chain length. A final board that
 * is only half populated returns the chips that exist, not a fixed pair. */
function segmentIcs(segment, ics, icsPerSegment) {
  const per = icsPerSegment > 0 ? icsPerSegment : 1;
  const out = [];
  for (let ic = (segment - 1) * per + 1; ic <= segment * per && ic <= ics; ic++) {
    out.push(ic);
  }
  return out;
}

/* The panels to draw, in order.
 *
 * Returns [{key, label, sub, ics:[...]}]. A pack-level panel has every
 * chip on one board; a segment-level panel has exactly one. Built from the
 * configured chain, not from which chips answered, so a silent chip keeps
 * its position.
 */
function cellViewPanels(ics, icsPerSegment, focusSegment) {
  const segs = segmentCount(ics, icsPerSegment);
  const panels = [];
  const drilled = focusSegment >= 1 && focusSegment <= segs;

  if (segs > 1 && !drilled) {
    for (let s = 1; s <= segs; s++) {
      const members = segmentIcs(s, ics, icsPerSegment);
      panels.push({
        key: 'S' + s, label: 'S' + s, segment: s, ics: members,
        sub: members.length ? 'IC' + members[0] + '–IC' + members[members.length - 1] : ''
      });
    }
    return panels;
  }

  const members = drilled ? segmentIcs(focusSegment, ics, icsPerSegment)
                          : segmentIcs(1, ics, Math.max(ics, 1));
  for (const ic of members) {
    panels.push({
      key: 'IC' + ic, label: 'IC' + ic, ic: ic, ics: [ic],
      sub: segs > 1 ? 'S' + segmentOf(ic, icsPerSegment) : ''
    });
  }
  return panels;
}

/* Panels per row. 5 puts the car's five segment boards on one row. */
function panelColumns(n) {
  if (n <= 1) return 1;
  if (n <= 3) return n;
  if (n === 4) return 2;
  if (n <= 5) return 5;
  if (n <= 6) return 3;
  return 5;
}

function cellViewHeight(panelCount) {
  const rows = Math.ceil(panelCount / panelColumns(panelCount));
  return rows === 1 ? 300 : Math.max(160, Math.round(340 / rows)) * rows + 54;
}

function drawCellView(canvas, state) {
  const { ctx, w, h } = prepCanvas(canvas);
  const t = theme();
  const pack = state.latestUsable || state.latest;
  const hits = [];
  const panelBoxes = [];

  if (!pack) {
    drawEmpty(ctx, w, h, t, state.emptyMessage || 'Waiting for a valid frame');
    return { cells: hits, panels: panelBoxes };
  }

  const ics = pack.expected_ics || 1;
  const cpi = pack.cells_per_ic || 16;
  const per = state.icsPerSegment > 0 ? state.icsPerSegment : 1;
  const stats = packStats(state.latestUsable);
  const panels = cellViewPanels(ics, per, state.focusSegment);
  const drilled = state.focusSegment >= 1;
  const cols = panelColumns(panels.length);
  const rows = Math.ceil(panels.length / cols);

  /* Chips the vertical scale covers. When drilled in, only this board. */
  const scopeIcs = {};
  for (const panel of panels) for (const ic of panel.ics) scopeIcs[ic] = true;

  /* Min/max emphasis follows what's on screen; the mean line stays the
   * pack mean. */
  const emph = drilled
    ? packStatsFor(state.latestUsable, (ic) => scopeIcs[ic] === true)
    : stats;

  const present = [];
  for (const entry of (pack.ics || [])) {
    if (entry.pec_ok === false || !scopeIcs[entry.ic]) continue;
    for (const v of entry.cells) {
      if (typeof v === 'number' && isFinite(v)) present.push(v);
    }
  }
  const bandMode = state.cellScale === 'band';
  const dom = bandMode
    ? autoscale(present.concat([state.uv, state.ov]),
        { pad: 0.06, minSpan: 0.05, fallback: [state.uv, state.ov] })
    : autoscale(present, { pad: 0.45, minSpan: 0.02, fallback: [state.uv, state.ov] });
  const ticks = niceTicks(dom[0], dom[1], rows > 1 ? 3 : 5);

  /* Off-scale UV/OV notes get their own strip at the top, since with ten
   * ICs the corners are taken by panel headers and cell numbers. */
  const notes = [];
  if (state.ov > dom[1]) notes.push('↑ OV ' + state.ov.toFixed(3) + ' V off scale');
  if (state.uv < dom[0]) notes.push('↓ UV ' + state.uv.toFixed(3) + ' V off scale');

  /* padB clears the cell-number row, plus the per-chip label that a
   * multi-chip segment panel adds underneath it. */
  const multiIc = panels.length > 0 && panels[0].ics.length > 1;
  const padL = 52, padR = 12;
  const padT = 20 + notes.length * 13;
  const padB = multiIc ? 34 : 24;
  const gapX = 14, gapY = 32;

  ctx.font = AXIS_FONT; ctx.textAlign = 'right'; ctx.textBaseline = 'top';
  ctx.fillStyle = t.muted;
  for (let i = 0; i < notes.length; i++) ctx.fillText(notes[i], w - padR, 2 + i * 13);
  const panelW = (w - padL - padR - gapX * (cols - 1)) / cols;
  const panelH = (h - padT - padB - gapY * (rows - 1)) / rows;

  const meanV = stats ? stats.mean : null;

  for (let k = 0; k < panels.length; k++) {
    const panel = panels[k];
    const col = k % cols, row = Math.floor(k / cols);
    const x0 = padL + col * (panelW + gapX);
    const y0 = padT + row * (panelH + gapY);
    const plot = { x0: x0, x1: x0 + panelW, y0: y0, y1: y0 + panelH };
    panelBoxes.push({
      key: panel.key, segment: panel.segment || null, ic: panel.ic || null,
      x0: plot.x0, x1: plot.x1, y0: plot.y0 - 16, y1: plot.y1
    });

    const entries = {};
    for (const entry of (pack.ics || [])) {
      if (panel.ics.indexOf(entry.ic) >= 0) entries[entry.ic] = entry;
    }
    const liveIcs = panel.ics.filter((ic) => !!entries[ic]);
    const allSilent = liveIcs.length === 0;

    /* Panel background + threshold band. */
    const bandTop = scaleLinear(Math.min(state.ov, dom[1]), dom[0], dom[1], plot.y1, plot.y0);
    const bandBot = scaleLinear(Math.max(state.uv, dom[0]), dom[0], dom[1], plot.y1, plot.y0);
    ctx.fillStyle = 'rgba(255,255,255,0.025)';
    ctx.fillRect(plot.x0, Math.min(bandTop, bandBot), panelW, Math.abs(bandBot - bandTop));

    /* Gridlines in every panel; tick labels only on the left column since
     * the scale is shared. */
    ctx.font = AXIS_FONT; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (const tick of ticks) {
      const y = Math.round(scaleLinear(tick.v, dom[0], dom[1], plot.y1, plot.y0)) + 0.5;
      if (y < plot.y0 - 1 || y > plot.y1 + 1) continue;
      ctx.strokeStyle = t.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(plot.x0, y); ctx.lineTo(plot.x1, y); ctx.stroke();
      if (col === 0) { ctx.fillStyle = t.muted; ctx.fillText(tick.label, plot.x0 - 8, y); }
    }
    ctx.strokeStyle = t.axis; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(plot.x0 + 0.5, plot.y0); ctx.lineTo(plot.x0 + 0.5, plot.y1); ctx.stroke();

    /* Pack-mean line through every panel. Still the pack mean when drilled
     * in, so you can see if a board sits low compared to the rest. */
    let yMean = null;
    if (meanV !== null && meanV >= dom[0] && meanV <= dom[1]) {
      yMean = Math.round(scaleLinear(meanV, dom[0], dom[1], plot.y1, plot.y0)) + 0.5;
      ctx.strokeStyle = t.axis; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(plot.x0, yMean); ctx.lineTo(plot.x1, yMean); ctx.stroke();
      ctx.setLineDash([]);
    }

    /* Header: panel name, plus SILENT / PEC ERROR if any chip has a problem. */
    const pecBadIcs = panel.ics.filter((ic) => entries[ic] && entries[ic].pec_ok === false);
    const silentIcs = panel.ics.filter((ic) => !entries[ic]);
    let note = '';
    if (allSilent) note = '  SILENT';
    else if (silentIcs.length) note = '  IC' + silentIcs.join(', IC') + ' SILENT';
    else if (pecBadIcs.length === panel.ics.length) note = '  PEC ERROR';
    else if (pecBadIcs.length) note = '  IC' + pecBadIcs.join(', IC') + ' PEC ERROR';

    ctx.font = AXIS_FONT; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
    ctx.fillStyle = (note ? t.critical : t.ink2);
    ctx.fillText(panel.label + (panel.sub ? '  ' + panel.sub : '') + note,
      plot.x0, plot.y0 - 5);

    if (allSilent) {
      ctx.fillStyle = 'rgba(208,59,59,0.07)';
      ctx.fillRect(plot.x0, plot.y0, panelW, panelH);
      ctx.font = AXIS_FONT; ctx.fillStyle = t.critical;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText('no response', plot.x0 + panelW / 2, plot.y0 + panelH / 2);
      continue;
    }

    /* Slots run across the panel in chain order, so a segment panel puts
     * its two chips side by side on one axis. */
    const slotCount = panel.ics.length * cpi;
    const step = panelW / slotCount;
    const labelEvery = step >= 16 ? 1 : (step >= 9 ? 2 : (step >= 6 ? 3 : 5));

    for (let g = 0; g < panel.ics.length; g++) {
      const ic = panel.ics[g];
      const entry = entries[ic];
      const base = g * cpi;

      /* Divider and chip label between chips inside a segment panel. */
      if (g > 0) {
        const xd = Math.round(plot.x0 + step * base) + 0.5;
        ctx.strokeStyle = t.axis; ctx.lineWidth = 1;
        ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(xd, plot.y0); ctx.lineTo(xd, plot.y1); ctx.stroke();
        ctx.setLineDash([]);
      }
      if (panel.ics.length > 1) {
        ctx.font = AXIS_FONT; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.fillStyle = t.muted;
        ctx.fillText('IC' + ic, plot.x0 + step * (base + cpi / 2), plot.y1 + 13);
      }

      if (!entry || entry.pec_ok === false) {
        ctx.fillStyle = 'rgba(208,59,59,0.07)';
        ctx.fillRect(plot.x0 + step * base, plot.y0, step * cpi, panelH);
        ctx.font = AXIS_FONT; ctx.fillStyle = t.critical;
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(entry ? 'PEC' : 'silent',
          plot.x0 + step * (base + cpi / 2), plot.y0 + panelH / 2);
        continue;
      }

      for (let i = 0; i < cpi; i++) {
        const v = entry.cells[i];
        if (typeof v !== 'number' || !isFinite(v)) continue;
        const cx = plot.x0 + step * (base + i + 0.5);
        const cy = scaleLinear(v, dom[0], dom[1], plot.y1, plot.y0);
        const key = cellKey(ic, i + 1);
        const isMin = !!emph && emph.min.key === key;
        const isMax = !!emph && emph.max.key === key;
        const colour = isMin ? t.s1 : (isMax ? t.s2 : t.ink2);
        const clamped = Math.max(plot.y0, Math.min(plot.y1, cy));
        const offScale = cy < plot.y0 || cy > plot.y1;

        if (yMean !== null && !offScale) {
          ctx.strokeStyle = (isMin || isMax) ? colour : t.context;
          ctx.lineWidth = (isMin || isMax) ? 2 : 1.4;
          ctx.beginPath(); ctx.moveTo(cx, yMean); ctx.lineTo(cx, cy); ctx.stroke();
        }
        const r = (isMin || isMax) ? 5 : 4;
        if (offScale) {
          /* Cell outside the axis range: draw a caret at the edge instead
           * of dropping the dot, which would look like "no reading". */
          ctx.fillStyle = colour;
          ctx.beginPath();
          const up = cy < plot.y0;
          ctx.moveTo(cx, clamped + (up ? -1 : 1) * 0);
          ctx.lineTo(cx - 4, clamped + (up ? 6 : -6));
          ctx.lineTo(cx + 4, clamped + (up ? 6 : -6));
          ctx.closePath(); ctx.fill();
        } else {
          ctx.beginPath(); ctx.arc(cx, cy, r + 2, 0, Math.PI * 2);
          ctx.fillStyle = t.surface; ctx.fill();
          ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
          ctx.fillStyle = colour; ctx.fill();
        }
        if (state.pinned === key) {
          ctx.strokeStyle = t.ink; ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.arc(cx, clamped, r + 4, 0, Math.PI * 2); ctx.stroke();
        }
        if ((i % labelEvery) === 0 || i === cpi - 1) {
          ctx.font = AXIS_FONT; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
          ctx.fillStyle = state.pinned === key ? t.ink : t.muted;
          ctx.fillText(String(i + 1), cx, plot.y1 + 2);
        }
        hits.push({ key: key, ic: ic, cell: i + 1, x: cx, y: clamped, v: v });
      }
    }
  }

  /* Label the min and max cells, but only if they're drawn. */
  if (emph) {
    for (const spec of [[emph.min, t.s1, 'min'], [emph.max, t.s2, 'max']]) {
      const entry = spec[0], colour = spec[1], side = spec[2];
      const hit = hits.filter((x) => x.key === entry.key)[0];
      if (!hit) continue;
      ctx.font = LABEL_FONT;
      ctx.fillStyle = colour;
      ctx.textAlign = hit.x > w / 2 ? 'right' : 'left';
      ctx.textBaseline = side === 'min' ? 'top' : 'bottom';
      ctx.fillText(side + ' IC' + entry.ic + ' C' + entry.cell + ' ' + entry.v.toFixed(4) + ' V',
        hit.x + (ctx.textAlign === 'right' ? -9 : 9), hit.y + (side === 'min' ? 9 : -9));
    }
  }

  return { cells: hits, panels: panelBoxes };
}

/* ---------------------------------------------------------------------
 * Time series across the whole chain.
 *
 * Every cell is a thin grey context line. Only the current min (blue) and
 * max (orange) get colour, plus any pinned or selected cell. One colour
 * per cell would be unreadable and not colour-blind safe.
 * ------------------------------------------------------------------- */
function drawTimeSeries(canvas, state) {
  const { ctx, w, h } = prepCanvas(canvas);
  const t = theme();
  const plot = { x0: 56, x1: w - 74, y0: 14, y1: h - 40 };
  const stripY = plot.y1 + 12;

  const all = state.packs;
  if (!all || !all.length) {
    drawEmpty(ctx, w, h, t, state.emptyMessage || 'Waiting for data');
    return null;
  }

  const tEnd = state.cursorT !== null && state.cursorT !== undefined
    ? state.cursorT : all[all.length - 1].t;
  const win = windowSlice(all, tEnd, state.windowS);
  if (!win.length) { drawEmpty(ctx, w, h, t, 'No samples in this window'); return null; }

  const x0d = tEnd - state.windowS, x1d = tEnd;
  canvas._plot = {
    x0: plot.x0,
    x1: plot.x1,
    y0: plot.y0,
    y1: plot.y1,
    x0d: x0d,
    x1d: x1d,
    tEnd: tEnd,
    windowS: state.windowS,
    allTMin: all[0].t,
    allTMax: all[all.length - 1].t
  };

  /* Follows the cell view's drill-down: only that board's cells are drawn
   * and the autoscale narrows to them. Ninety lines on one axis is
   * unreadable. */
  const icsAll = win[win.length - 1].expected_ics || 1;
  const cpiAll = win[win.length - 1].cells_per_ic || 16;
  const per = state.icsPerSegment > 0 ? state.icsPerSegment : 1;
  const segs = segmentCount(icsAll, per);
  const focused = state.focusSegment >= 1 && state.focusSegment <= segs;
  const inScope = {};
  if (focused) {
    for (const ic of segmentIcs(state.focusSegment, icsAll, per)) inScope[ic] = true;
  }
  const scoped = (ic) => !focused || inScope[ic] === true;

  const flat = [];
  for (const p of win) {
    if (!packUsable(p)) continue;
    for (const c of flatCells(p)) if (scoped(c.ic)) flat.push(c.v);
  }
  const dom = autoscale(flat, { pad: 0.12, minSpan: 0.01, fallback: [state.uv, state.ov] });
  const ticks = niceTicks(dom[0], dom[1], 5);
  drawYAxis(ctx, ticks, dom[0], dom[1], plot, t);

  ctx.font = AXIS_FONT; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const tick of niceTicks(0, state.windowS, 5)) {
    const x = Math.round(scaleLinear(x1d - tick.v, x0d, x1d, plot.x0, plot.x1)) + 0.5;
    if (x < plot.x0 || x > plot.x1) continue;
    ctx.strokeStyle = t.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, plot.y0); ctx.lineTo(x, plot.y1); ctx.stroke();
    ctx.fillStyle = t.muted;
    ctx.fillText(tick.v === 0 ? (state.mode === 'replay' ? 'cursor' : 'now')
      : '-' + Math.round(tick.v) + 's', x, plot.y1 + 18);
  }
  ctx.strokeStyle = t.axis;
  ctx.beginPath(); ctx.moveTo(plot.x0, plot.y1 + 0.5); ctx.lineTo(plot.x1, plot.y1 + 0.5); ctx.stroke();

  const lastUsable = (function () {
    for (let i = win.length - 1; i >= 0; i--) if (packUsable(win[i])) return win[i];
    return null;
  })();
  const stats = focused
    ? packStatsFor(lastUsable, (ic) => inScope[ic] === true)
    : packStats(lastUsable);
  const minKey = stats ? stats.min.key : null;
  const maxKey = stats ? stats.max.key : null;

  const slots = expectedKeys(icsAll, cpiAll).filter((slot) => scoped(slot.ic));

  const strokeSeries = (slot, colour, width) => {
    const points = win.map((p) => {
      const v = valueFor(p, slot.ic, slot.cell);
      return {
        x: scaleLinear(p.t, x0d, x1d, plot.x0, plot.x1),
        y: v === null ? 0 : scaleLinear(v, dom[0], dom[1], plot.y1, plot.y0),
        /* PEC-bad, stale, partial or missing-IC frames aren't data. Break
         * the line here instead of interpolating. */
        ok: v !== null && packUsable(p)
      };
    });
    const segs = segmentPaths(points);
    ctx.strokeStyle = colour; ctx.lineWidth = width;
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    for (const seg of segs) {
      if (seg.length === 1) {
        /* Single good sample between bad frames. Draw a dot, since
         * stroking a 1-point path draws nothing. */
        ctx.fillStyle = colour;
        ctx.beginPath(); ctx.arc(seg[0].x, seg[0].y, width, 0, Math.PI * 2); ctx.fill();
        continue;
      }
      ctx.beginPath();
      ctx.moveTo(seg[0].x, seg[0].y);
      for (let k = 1; k < seg.length; k++) ctx.lineTo(seg[k].x, seg[k].y);
      ctx.stroke();
    }
  };

  const selected = state.selected || {};
  const anySelected = Object.keys(selected).length > 0;
  const emphasised = {};
  if (minKey) emphasised[minKey] = t.s1;
  if (maxKey) emphasised[maxKey] = t.s2;
  if (state.pinned) emphasised[state.pinned] = '#facc15';
  for (const key in selected) emphasised[key] = emphasised[key] || t.ink2;

  /* Shaded envelope band between min and max cell traces */
  const envelope = [];
  for (const p of win) {
    if (!packUsable(p)) continue;
    const s = focused
      ? packStatsFor(p, (ic) => inScope[ic] === true)
      : packStats(p);
    if (!s) continue;
    envelope.push({
      x: scaleLinear(p.t, x0d, x1d, plot.x0, plot.x1),
      yMin: scaleLinear(s.min.v, dom[0], dom[1], plot.y1, plot.y0),
      yMax: scaleLinear(s.max.v, dom[0], dom[1], plot.y1, plot.y0)
    });
  }
  if (envelope.length >= 2) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(plot.x0, plot.y0, plot.x1 - plot.x0, plot.y1 - plot.y0);
    ctx.clip();
    ctx.beginPath();
    ctx.moveTo(envelope[0].x, envelope[0].yMax);
    for (let i = 1; i < envelope.length; i++) ctx.lineTo(envelope[i].x, envelope[i].yMax);
    for (let i = envelope.length - 1; i >= 0; i--) ctx.lineTo(envelope[i].x, envelope[i].yMin);
    ctx.closePath();
    ctx.fillStyle = 'rgba(57, 135, 229, 0.08)';
    ctx.fill();
    ctx.restore();
  }

  /* Context lines first so the emphasised ones draw on top. */
  if (!(anySelected && state.seriesMode === 'selected')) {
    for (const slot of slots) {
      if (emphasised[slot.key]) continue;
      strokeSeries(slot, t.context, 1.2);
    }
  }
  for (const slot of slots) {
    const colour = emphasised[slot.key];
    if (!colour) continue;
    const width = slot.key === state.pinned ? 2.5 : (colour === t.ink2 ? 2 : 2.25);
    strokeSeries(slot, colour, width);
  }

  /* End labels in the right gutter, tagged with the IC. */
  if (lastUsable) {
    ctx.font = AXIS_FONT; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    const placed = [];
    for (const slot of slots) {
      const colour = emphasised[slot.key];
      if (!colour) continue;
      const v = valueFor(lastUsable, slot.ic, slot.cell);
      if (v === null) continue;
      let y = scaleLinear(v, dom[0], dom[1], plot.y1, plot.y0);
      for (const py of placed) if (Math.abs(py - y) < 13) y = py + 13;
      placed.push(y);
      ctx.fillStyle = colour;
      ctx.fillText('IC' + slot.ic + ' C' + slot.cell, plot.x1 + 8,
        Math.max(plot.y0, Math.min(plot.y1, y)));
    }
  }

  /* Bottom strip: a red tick per PEC-bad cycle and an amber tick per
   * partial chain. */
  let badCount = 0, partialCount = 0;
  for (const p of win) {
    const x = Math.round(scaleLinear(p.t, x0d, x1d, plot.x0, plot.x1));
    if (p.pec_ok === false) { badCount++; ctx.fillStyle = t.critical; ctx.fillRect(x, stripY, 2, 8); }
    else if (p.partial === true) { partialCount++; ctx.fillStyle = t.warning; ctx.fillRect(x, stripY, 2, 8); }
  }
  ctx.font = AXIS_FONT; ctx.textAlign = 'right'; ctx.textBaseline = 'top';
  const notes = [];
  if (badCount) notes.push(badCount + ' PEC-bad');
  if (partialCount) notes.push(partialCount + ' partial chain');
  ctx.fillStyle = notes.length ? t.critical : t.muted;
  ctx.fillText(notes.length ? notes.join(' · ') + ' in window'
    : 'no PEC errors in window', plot.x1, stripY + 12);

  /* Crosshair snapped to the nearest real cycle. */
  let hover = null;
  if (state.hoverX !== null && state.hoverX >= plot.x0 && state.hoverX <= plot.x1) {
    const tHover = scaleInvert(state.hoverX, x0d, x1d, plot.x0, plot.x1);
    const idx = nearestIndex(win, tHover);
    if (idx >= 0) {
      const p = win[idx];
      const x = Math.round(scaleLinear(p.t, x0d, x1d, plot.x0, plot.x1)) + 0.5;
      ctx.strokeStyle = t.axis; ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, plot.y0); ctx.lineTo(x, plot.y1); ctx.stroke();
      ctx.setLineDash([]);

      /* Highlight points under the cursor on primary traces */
      const markerKeys = [minKey, maxKey, state.pinned].filter(Boolean);
      for (const k of markerKeys) {
        const m = /^IC(\d+)C(\d+)$/.exec(k);
        if (!m) continue;
        const v = valueFor(p, Number(m[1]), Number(m[2]));
        if (v === null) continue;
        const y = Math.round(scaleLinear(v, dom[0], dom[1], plot.y1, plot.y0)) + 0.5;
        const col = k === state.pinned ? '#facc15' : (k === minKey ? t.s1 : t.s2);
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fillStyle = col;
        ctx.fill();
        ctx.strokeStyle = t.surface;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      hover = { pack: p, x: x, minKey: minKey, maxKey: maxKey, tEnd: tEnd };
    }
  }
  return hover;
}

/* ---------------------------------------------------------------------
 * Trend: one or more series against time, with the same gap breaks.
 * Used for the charge tab's spread and for temperatures.
 *
 * `series` is [{points: [{t, v, ok}], colour, width, label}].
 * ------------------------------------------------------------------- */
function drawTrend(canvas, spec) {
  const { ctx, w, h } = prepCanvas(canvas);
  const t = theme();
  const plot = { x0: spec.leftPad || 56, x1: w - (spec.rightPad || 60),
                 y0: 14, y1: h - 34 };
  const series = (spec.series || []).filter((s) => s.points && s.points.length);
  if (!series.length) {
    drawEmpty(ctx, w, h, t, spec.emptyMessage || 'No data yet');
    return;
  }

  let tMin = Infinity, tMax = -Infinity;
  const values = [];
  for (const s of series) {
    for (const p of s.points) {
      if (p.t < tMin) tMin = p.t;
      if (p.t > tMax) tMax = p.t;
      if (p.ok && typeof p.v === 'number' && isFinite(p.v)) values.push(p.v);
    }
  }
  if (spec.windowS && (tMax - tMin) > spec.windowS) tMin = tMax - spec.windowS;
  if (!(tMax > tMin)) { tMax = tMin + 1; }

  const dom = autoscale(values, {
    pad: 0.12,
    minSpan: spec.minSpan === undefined ? 1 : spec.minSpan,
    fallback: spec.fallback || [0, 1]
  });
  const dp = spec.decimals === undefined ? 1 : spec.decimals;
  const ticks = niceTicks(dom[0], dom[1], 5);
  drawYAxis(ctx, ticks, dom[0], dom[1], plot, t, {
    format: (tick) => Number(tick.v).toFixed(dp)
  });

  ctx.font = AXIS_FONT; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const tick of niceTicks(0, tMax - tMin, 5)) {
    const x = Math.round(scaleLinear(tMax - tick.v, tMin, tMax, plot.x0, plot.x1)) + 0.5;
    if (x < plot.x0 || x > plot.x1) continue;
    ctx.strokeStyle = t.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, plot.y0); ctx.lineTo(x, plot.y1); ctx.stroke();
    ctx.fillStyle = t.muted;
    ctx.fillText(tick.v === 0 ? 'now' : '-' + Math.round(tick.v) + 's', x, plot.y1 + 6);
  }
  ctx.strokeStyle = t.axis;
  ctx.beginPath(); ctx.moveTo(plot.x0, plot.y1 + 0.5); ctx.lineTo(plot.x1, plot.y1 + 0.5); ctx.stroke();

  /* Optional reference lines, e.g. a target spread. */
  for (const ref of (spec.refs || [])) {
    if (ref.v < dom[0] || ref.v > dom[1]) continue;
    const y = Math.round(scaleLinear(ref.v, dom[0], dom[1], plot.y1, plot.y0)) + 0.5;
    ctx.strokeStyle = ref.colour || t.axis;
    ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(plot.x0, y); ctx.lineTo(plot.x1, y); ctx.stroke();
    ctx.setLineDash([]);
    if (ref.label) {
      ctx.font = AXIS_FONT; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
      ctx.fillStyle = ref.colour || t.muted;
      ctx.fillText(ref.label, plot.x0 + 4, y - 3);
    }
  }

  for (const s of series) {
    const points = s.points
      .filter((p) => p.t >= tMin)
      .map((p) => ({
        x: scaleLinear(p.t, tMin, tMax, plot.x0, plot.x1),
        y: (typeof p.v === 'number' && isFinite(p.v))
          ? scaleLinear(p.v, dom[0], dom[1], plot.y1, plot.y0) : 0,
        ok: p.ok === true && typeof p.v === 'number' && isFinite(p.v)
      }));
    const segs = segmentPaths(points);
    ctx.strokeStyle = s.colour || t.s1;
    ctx.lineWidth = s.width || 2;
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    for (const seg of segs) {
      if (seg.length === 1) {
        ctx.fillStyle = s.colour || t.s1;
        ctx.beginPath(); ctx.arc(seg[0].x, seg[0].y, s.width || 2, 0, Math.PI * 2); ctx.fill();
        continue;
      }
      ctx.beginPath();
      ctx.moveTo(seg[0].x, seg[0].y);
      for (let k = 1; k < seg.length; k++) ctx.lineTo(seg[k].x, seg[k].y);
      ctx.stroke();
    }
    if (s.label && segs.length) {
      const last = segs[segs.length - 1];
      const end = last[last.length - 1];
      ctx.font = AXIS_FONT; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillStyle = s.colour || t.s1;
      ctx.fillText(s.label, plot.x1 + 6, Math.max(plot.y0, Math.min(plot.y1, end.y)));
    }
  }

  /* Optional synchronized crosshair cursor */
  if (spec.hoverX !== undefined && spec.hoverX !== null && spec.hoverX >= plot.x0 && spec.hoverX <= plot.x1) {
    const x = Math.round(spec.hoverX) + 0.5;
    ctx.strokeStyle = t.axis; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(x, plot.y0); ctx.lineTo(x, plot.y1); ctx.stroke();
    ctx.setLineDash([]);
  } else if (spec.hoverT !== undefined && spec.hoverT !== null && spec.hoverT >= tMin && spec.hoverT <= tMax) {
    const x = Math.round(scaleLinear(spec.hoverT, tMin, tMax, plot.x0, plot.x1)) + 0.5;
    ctx.strokeStyle = t.axis; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(x, plot.y0); ctx.lineTo(x, plot.y1); ctx.stroke();
    ctx.setLineDash([]);
  }
}

/* Sparkline for a stat tile. Area fill + stroked line for high legibility. */
function drawSparkline(canvas, values, colour) {
  const { ctx, w, h } = prepCanvas(canvas);
  const clean = values.filter((v) => typeof v === 'number' && isFinite(v));
  if (clean.length < 2) return;
  const dom = autoscale(clean, { pad: 0.15, minSpan: 0.002, fallback: [0, 1] });
  
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';

  const firstX = scaleLinear(0, 0, clean.length - 1, 1, w - 1);
  const firstY = scaleLinear(clean[0], dom[0], dom[1], h - 2, 2);

  /* Subtle area fill */
  ctx.beginPath();
  ctx.moveTo(firstX, firstY);
  for (let i = 1; i < clean.length; i++) {
    const x = scaleLinear(i, 0, clean.length - 1, 1, w - 1);
    const y = scaleLinear(clean[i], dom[0], dom[1], h - 2, 2);
    ctx.lineTo(x, y);
  }
  const lastX = scaleLinear(clean.length - 1, 0, clean.length - 1, 1, w - 1);
  ctx.lineTo(lastX, h);
  ctx.lineTo(firstX, h);
  ctx.closePath();
  ctx.fillStyle = colour.startsWith('#')
    ? colour + '18'
    : 'rgba(255, 255, 255, 0.06)';
  ctx.fill();

  /* Stroke line */
  ctx.beginPath();
  ctx.moveTo(firstX, firstY);
  for (let i = 1; i < clean.length; i++) {
    const x = scaleLinear(i, 0, clean.length - 1, 1, w - 1);
    const y = scaleLinear(clean[i], dom[0], dom[1], h - 2, 2);
    ctx.lineTo(x, y);
  }
  ctx.strokeStyle = colour;
  ctx.lineWidth = 1.6;
  ctx.stroke();
}

window.Charts = {
  scaleLinear, scaleInvert, niceStep, decimalsFor, niceTicks, autoscale,
  segmentPaths, windowSlice, nearestIndex, formatFixed,
  packUsable, flatCells, packStats, packStatsFor, expectedKeys, valueFor, cellKey,
  dccChannels, dischargingCells, balanceProgress,
  segmentOf, segmentCount, segmentIcs, cellViewPanels, panelColumns,
  cellViewHeight,
  drawCellView, drawTimeSeries, drawTrend, drawSparkline, prepCanvas, theme
};
