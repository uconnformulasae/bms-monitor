/* app.js: state, transport and DOM for the BMS monitor.
 *
 * Rules for this file:
 *
 * 1. Anything that came from the device is written with textContent, never
 *    innerHTML. Serial output can be corrupted, and the raw console shows
 *    lines we couldn't parse.
 *
 * 2. Everything is bounded (client history, raw console, render rate) so a
 *    page left open for hours doesn't keep growing.
 *
 * 3. Don't compute values the hardware can't measure. Pack current, SOC and
 *    balancing show "n/a" with a reason instead of an estimate.
 */

'use strict';

(function () {
  const C = window.Charts;

  /* ------------------------------------------------------------ config */

  const MAX_CLIENT_PACKS = 20000;
  const MAX_AUX = 4000;
  const MAX_CAN_TEMPS = 3600;   /* 30 min of controller updates at 2 Hz */
  const MAX_CONSOLE_LINES = 300;
  const RENDER_MIN_INTERVAL_MS = 80;   /* ~12 fps max */

  /* Fallbacks only. The real values come from /api/snapshot (config). */
  const DEFAULTS = {
    uv: 3.0, ov: 4.2, warn_spread_mv: 30, crit_spread_mv: 60,
    ics: 1, cells_per_ic: 16, ics_per_segment: 2,
    ntc: { beta: 3435, r25: 10000, series: 10000, vref: 3.0, pullup: true }
  };

  /* ------------------------------------------------------------- state */

  const state = {
    packs: [], aux: [], consoleLines: [],
    canTemps: null, canTempHist: [],
    stats: null, summary: null, config: Object.assign({}, DEFAULTS),
    /* Balance test (--allow-tx only): what we last asked for, and when. */
    tx: null, balRequested: null, balRequestedAt: 0, balError: '', balBusy: false,
    /* Latest pack read while nothing was discharging, for the balancing target. */
    balRestPack: null,
    tab: 'live',
    windowS: 30, chgWindowS: 120,
    pinned: null, paused: false, hoverX: null, connected: false,
    cellScale: 'pack', showContext: true, tableSort: 'chain',
    cellHits: [], cellPanels: [],
    cellsView: 'grid', cellUnit: 'v',
    /* null = whole pack, otherwise the segment board we're drilled into. */
    focusSegment: null,
    mode: 'live',
    cursorT: null, playing: false, speed: 1,
    session: null, lastSession: null,
    settingsOpen: false
  };

  let renderQueued = false;
  let lastRenderAt = 0;
  let lastPlayTick = 0;

  /* ----------------------------------------------------------- helpers */

  const $ = (id) => document.getElementById(id);

  function setText(el, value) {
    /* All text writes go through here (textContent, never innerHTML). */
    if (el && el.textContent !== value) el.textContent = value;
  }

  function pref(key, fallback) {
    try {
      const v = window.localStorage.getItem('bms.' + key);
      return v === null ? fallback : v;
    } catch (e) { return fallback; }
  }
  function setPref(key, value) {
    try { window.localStorage.setItem('bms.' + key, String(value)); } catch (e) { /* ignore */ }
  }
  function numPref(key, fallback) {
    const v = parseFloat(pref(key, ''));
    return isFinite(v) ? v : fallback;
  }

  const params = new URLSearchParams(window.location.search);

  function fmt(v, dp) {
    return (typeof v === 'number' && isFinite(v)) ? v.toFixed(dp) : '--';
  }

  function fmtDuration(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '0s';
    const s = Math.floor(seconds % 60);
    const m = Math.floor((seconds / 60) % 60);
    const h = Math.floor(seconds / 3600);
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + String(s).padStart(2, '0') + 's';
    return s + 's';
  }

  function cfg() { return state.config; }

  function pushPack(p) {
    state.packs.push(p);
    if (state.packs.length > MAX_CLIENT_PACKS) {
      state.packs.splice(0, state.packs.length - MAX_CLIENT_PACKS);
    }
  }

  function pushAux(a) {
    state.aux.push(a);
    if (state.aux.length > MAX_AUX) state.aux.splice(0, state.aux.length - MAX_AUX);
  }

  /* CAN thermistors: the latest list, plus one history entry per complete
   * update (same as the hub keeps, so a reload matches). */
  function onCanTemps(payload) {
    state.canTemps = payload;
    if (payload && payload.complete) {
      state.canTempHist.push({ t: payload.t, celsius: payload.sensors.map((s) => s.celsius) });
      if (state.canTempHist.length > MAX_CAN_TEMPS) {
        state.canTempHist.splice(0, state.canTempHist.length - MAX_CAN_TEMPS);
      }
    }
  }

  function pushConsole(text, cls) {
    state.consoleLines.push({ text: text, cls: cls || '' });
    if (state.consoleLines.length > MAX_CONSOLE_LINES) {
      state.consoleLines.splice(0, state.consoleLines.length - MAX_CONSOLE_LINES);
    }
  }

  /* ------------------------------------------------- view of the data
   * In replay the cursor is "now"; in live mode it's the newest cycle.
   * Everything reads through these helpers so both modes behave the same. */

  function timeRange() {
    if (!state.packs.length) return [0, 0];
    return [state.packs[0].t, state.packs[state.packs.length - 1].t];
  }

  function cursor() {
    if (state.mode !== 'replay' || state.cursorT === null) {
      return state.packs.length ? state.packs[state.packs.length - 1].t : 0;
    }
    return state.cursorT;
  }

  function visiblePacks() {
    if (state.mode !== 'replay' || state.cursorT === null) return state.packs;
    const idx = C.nearestIndex(state.packs, state.cursorT);
    if (idx < 0) return [];
    /* Include the cursor sample, exclude the future. */
    let end = idx;
    while (end < state.packs.length && state.packs[end].t <= state.cursorT) end++;
    return state.packs.slice(0, end);
  }

  function latestPack() {
    const v = visiblePacks();
    return v.length ? v[v.length - 1] : null;
  }

  function latestUsablePack() {
    const v = visiblePacks();
    for (let i = v.length - 1; i >= 0; i--) if (C.packUsable(v[i])) return v[i];
    return null;
  }

  /* -------------------------------------------------------- transport */

  function applyConfig(config) {
    const base = Object.assign({}, DEFAULTS, config || {});
    base.ntc = Object.assign({}, DEFAULTS.ntc, (config && config.ntc) || {});
    /* Values saved from the Settings panel override the server config. */
    base.uv = numPref('uv', base.uv);
    base.ov = numPref('ov', base.ov);
    base.warn_spread_mv = numPref('warn', base.warn_spread_mv);
    base.crit_spread_mv = numPref('crit', base.crit_spread_mv);
    base.ntc.beta = numPref('beta', base.ntc.beta);
    base.ntc.r25 = numPref('r25', base.ntc.r25);
    base.ntc.series = numPref('series', base.ntc.series);
    base.ntc.vref = numPref('vref', base.ntc.vref);
    state.configDefaults = Object.assign({}, DEFAULTS, config || {});
    state.configDefaults.ntc = Object.assign({}, DEFAULTS.ntc, (config && config.ntc) || {});
    state.config = base;
    fillSettings();
  }

  function loadSnapshot() {
    /* Load existing history so a reload shows full charts straight away.
     * Also used to catch up after a reconnect. */
    return fetch('/api/snapshot', { cache: 'no-store' })
      .then((r) => r.json())
      .then((data) => {
        applyConfig(data.config);
        state.tx = (data.tx && data.tx.balance_test) ? data.tx : null;
        state.packs = [];
        for (const p of data.samples) pushPack(p);
        state.aux = [];
        for (const a of (data.aux || [])) pushAux(a);
        state.canTempHist = (data.can_temps_history || []).slice(-MAX_CAN_TEMPS);
        const ct = data.stats && data.stats.can_temps;
        state.canTemps = (ct && ct.sensors && ct.sensors.length) ? ct : null;
        state.consoleLines = [];
        for (const line of data.raw_lines) pushConsole(line, lineClass(line));
        state.stats = data.stats;
        if (data.stats && data.stats.source) {
          state.mode = data.stats.source.mode === 'replay' ? 'replay' : 'live';
        }
        if (state.mode === 'replay' && state.cursorT === null) {
          state.cursorT = timeRange()[1];
        }
        syncReplayBar();
        scheduleRender();
      })
      .catch(() => { /* server not up yet; EventSource will retry anyway */ });
  }

  function lineClass(text) {
    if (typeof text !== 'string') return '';
    if (text.indexOf('PECError:1') !== -1 || text.indexOf('<<truncated>>') !== -1) return 'bad';
    if (text.indexOf('Enter cmd:') === 0 || text.indexOf('IC') !== 0) return 'meta';
    return '';
  }

  function connect() {
    /* EventSource reconnects on its own, so we just track open/error. */
    const es = new EventSource('/api/stream');

    es.addEventListener('open', function () {
      state.connected = true;
      loadSnapshot();
      scheduleRender();
    });
    es.addEventListener('error', function () {
      state.connected = false;
      scheduleRender();
    });
    es.addEventListener('sample', function (e) {
      pushPack(JSON.parse(e.data));
      if (state.mode === 'replay' && state.cursorT !== null
          && state.cursorT >= timeRange()[1] - 0.001) {
        state.cursorT = timeRange()[1];
      }
      scheduleRender();
    });
    es.addEventListener('aux', function (e) { pushAux(JSON.parse(e.data)); scheduleRender(); });
    es.addEventListener('temps', function (e) {
      onCanTemps(JSON.parse(e.data));
      if (state.tab === 'temps') scheduleRender();
    });
    es.addEventListener('stats', function (e) {
      state.stats = JSON.parse(e.data);
      if (state.stats.summary && Object.keys(state.stats.summary).length) {
        state.summary = state.stats.summary;
      }
      scheduleRender();
    });
    es.addEventListener('summary', function (e) { state.summary = JSON.parse(e.data); scheduleRender(); });
    es.addEventListener('line', function (e) {
      const text = JSON.parse(e.data).text;
      pushConsole(text, lineClass(text));
      if (state.tab === 'console') scheduleRender();
    });
    es.addEventListener('event', function (e) {
      const item = JSON.parse(e.data);
      pushConsole('[' + item.kind + '] ' + item.text, 'bad');
      if (state.tab === 'console') scheduleRender();
    });
    es.addEventListener('status', function () { scheduleRender(); });
    es.addEventListener('link', function () { scheduleRender(); });
  }

  /* ----------------------------------------------------------- render */

  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(function () {
      renderQueued = false;
      const now = performance.now();
      if (now - lastRenderAt < RENDER_MIN_INTERVAL_MS) {
        setTimeout(scheduleRender, RENDER_MIN_INTERVAL_MS - (now - lastRenderAt));
        return;
      }
      lastRenderAt = now;
      try { render(); } catch (err) {
        /* Don't let one bad render kill the page; the next sample retries. */
        console.error('render failed', err);
      }
    });
  }

  function render() {
    /* The status bar keeps updating while paused, so a paused page doesn't
     * look like a dead one. */
    renderStatusBar();
    renderBanner();
    /* A control, so it keeps updating while the charts are paused. */
    renderBalance();
    if (state.paused) return;
    if (state.mode === 'replay') syncReplayBar();
    if (state.tab === 'live') renderLive();
    else if (state.tab === 'charge') renderCharge();
    else if (state.tab === 'temps') renderTemps();
    else if (state.tab === 'chain') renderChain();
    else if (state.tab === 'console') renderConsole();
  }

  /* ------------------------------------------------- balance test */

  /* How long a request outranks what the controller reports, so the button
   * doesn't flick back before the next 0x6FF (10 Hz) confirms it. */
  const BAL_SETTLE_MS = 1500;

  function balanceFirmwareOn(sum) {
    return !!sum && typeof sum.balance_state === 'string' && sum.balance_state !== 'off';
  }

  /* Hottest die the chain table knows about, as text, or ''. */
  function hottestDie() {
    let best = null;
    for (const c of ((state.stats || {}).chain || [])) {
      if (typeof c.die_temp_c === 'number' && (!best || c.die_temp_c > best.die_temp_c)) best = c;
    }
    return best ? 'Hottest die: IC' + best.ic + ' at ' + best.die_temp_c.toFixed(1) + ' \u00b0C.' : '';
  }

  /* Cells the controller is discharging now, keyed for the grid and table. */
  function currentDischarge() {
    const st = state.stats || {};
    const live = state.mode !== 'replay' && state.connected
      && !!st.link && st.link.link === 'up' && st.data_state === 'live';
    const cells = C.dischargingCells(state.summary, latestPack(), live);
    const byKey = {};
    for (const c of cells) byKey[c.key] = c;
    return { cells: cells, byKey: byKey };
  }

  /* "Target 3.5960 V (IC1 cell 1 + 1.0 mV). Above it at the last rest: ..." */
  function balanceProgressText(sum) {
    const p = C.balanceProgress(sum, state.balRestPack);
    if (!p) return '';
    let text = 'Target ' + p.target.toFixed(4) + ' V (IC' + p.lowest.ic + ' cell ' + p.lowest.cell
      + ' + ' + sum.balance_margin_mv.toFixed(1) + ' mV).';
    if (p.above.length) {
      const shown = p.above.slice(0, 5).map((a) => 'IC' + a.ic + ' C' + a.cell + ' +' + a.mv.toFixed(1) + ' mV');
      text += ' Above it at the last rest: ' + shown.join(', ')
        + (p.above.length > 5 ? ', and ' + (p.above.length - 5) + ' more' : '') + '.';
    }
    return text;
  }

  function renderBalance() {
    const panel = $('balancePanel');
    if (!state.tx) { panel.hidden = true; return; }
    panel.hidden = false;

    const sum = state.summary || {};
    const haveStatus = typeof sum.balance_state === 'string';
    /* Readings sag while discharging, so progress comes from a rest reading. */
    if (haveStatus && sum.balance_state !== 'discharging' && latestPack()) {
      state.balRestPack = latestPack();
    }
    const st = state.stats || {};
    const linkUp = state.connected && st.link && st.link.link === 'up';
    const settling = state.balRequested !== null
      && (Date.now() - state.balRequestedAt) < BAL_SETTLE_MS;

    /* The controller is the authority on whether the test is armed. Our own
     * request only wins while it is in flight. */
    const on = settling ? state.balRequested : balanceFirmwareOn(sum);
    const btn = $('balToggle');
    btn.setAttribute('aria-pressed', String(on));
    setText(btn, on ? 'On' : 'Off');
    btn.disabled = state.balBusy || !linkUp;

    let text = '';
    let cls = '';
    let detail = '';
    if (!linkUp) {
      text = 'CAN link is down.';
      cls = 'bad';
    } else if (!haveStatus) {
      text = 'Waiting for the controller\'s chain status (0x6FF)...';
    } else if (state.balRequested === true && !settling && sum.balance_state === 'off') {
      text = 'No response from the controller.';
      cls = 'bad';
      detail = 'Its firmware may not have the balance test, or 0x740 is not reaching it.';
    } else {
      switch (sum.balance_state) {
        case 'off':
          text = settling ? 'Sending...' : 'Off.';
          detail = 'Turn on to discharge the groups above the lowest until they are within the margin.';
          break;
        case 'pending':
          text = 'Armed, waiting for the next measurement cycle...';
          cls = 'active';
          break;
        case 'discharging': {
          const n = currentDischarge().cells.length;
          text = 'Discharging ' + (n ? n + ' group' + (n === 1 ? '' : 's') : 'the high groups') + '.';
          cls = 'active';
          detail = [balanceProgressText(sum), hottestDie(),
                    'Stops by itself at 80 \u00b0C for over 5 s.'].filter(Boolean).join(' ');
          break;
        }
        case 'resting':
          text = 'Resting so the readings recover from the sag, then measuring again...';
          cls = 'active';
          detail = [balanceProgressText(sum), hottestDie()].filter(Boolean).join(' ');
          break;
        case 'balanced':
          text = 'Balanced: every group is within '
            + (typeof sum.balance_margin_mv === 'number' ? sum.balance_margin_mv.toFixed(1) + ' mV' : 'the margin')
            + ' of ' + (sum.balance_ic && sum.balance_cell ? 'IC' + sum.balance_ic + ' cell ' + sum.balance_cell : 'the lowest group') + '.';
          detail = 'Turn off, then on, to check again.';
          break;
        case 'refused':
          text = 'Refused: not every IC had valid data, or the DCC bits did not read back.';
          cls = 'bad';
          detail = 'Nothing was discharged. Turn off, then on, to try again.';
          break;
        case 'aborted':
          text = 'Aborted: the chain reset during the discharge.';
          cls = 'bad';
          detail = 'Discharge was cleared. Turn off, then on, to try again.';
          break;
        case 'too_hot':
          text = 'Stopped: ' + (sum.balance_ic ? 'IC' + sum.balance_ic + '\'s' : 'a')
            + ' die was 80 \u00b0C or more (or unreadable) for over 5 s.';
          cls = 'bad';
          detail = hottestDie() + ' Turn off, then on, to start again.';
          break;
        case 'cell_low':
          text = 'Stopped: '
            + (sum.balance_ic && sum.balance_cell ? 'IC' + sum.balance_ic + ' cell ' + sum.balance_cell : 'a cell')
            + ' reached the undervoltage threshold.';
          cls = 'bad';
          detail = 'Turn off, then on, to start again.';
          break;
        default:
          text = 'Unknown balance state from the controller.';
          cls = 'bad';
      }
    }
    if (state.balError) {
      detail = state.balError;
      cls = 'bad';
    }
    const status = $('balStatus');
    setText(status, text);
    status.className = 'balstatus' + (cls ? ' ' + cls : '');
    setText($('balDetail'), detail);
  }

  function sendBalance(enable) {
    if (!state.tx || state.balBusy) return;
    if (enable && !window.confirm('Balance the pack? The groups above the lowest are discharged until they are within the margin.')) return;
    state.balBusy = true;
    state.balError = '';
    scheduleRender();
    fetch(state.tx.path, {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-BMS-Token': state.tx.token },
      body: JSON.stringify({ enable: enable })
    })
      .then((r) => r.json().then((body) => ({ ok: r.ok, body: body })))
      .then((res) => {
        if (!res.ok) throw new Error(res.body && res.body.error ? res.body.error : 'request failed');
        state.balRequested = enable;
        state.balRequestedAt = Date.now();
      })
      .catch((err) => { state.balError = 'Not sent: ' + err.message; })
      .then(() => {
        state.balBusy = false;
        scheduleRender();
        /* Re-check once the settle window ends, even if no frame arrives. */
        setTimeout(scheduleRender, BAL_SETTLE_MS + 50);
      });
  }

  /* One word for the whole transport, worst layer first. LIVE only if every
   * layer is healthy: a dead browser stream or a dropped port outranks a
   * data_state based on samples from before the link went down. */
  function overallState(st) {
    if (!state.connected) return ['OFFLINE', 'critical'];
    const link = (st.link && st.link.link) || 'down';
    if (link === 'down') return ['LINK DOWN', 'critical'];
    if (link === 'reconnecting') return ['RECONNECTING', 'serious'];
    if (link === 'connecting') return ['CONNECTING', 'warning'];
    const data = st.data_state || 'no-data';
    if (data === 'live') return ['LIVE', 'good'];
    if (data === 'stale') return ['STALE', 'warning'];
    return ['NO-DATA', 'critical'];
  }

  function renderStatusBar() {
    const st = state.stats || {};
    const src = st.source || {};

    /* The word is the signal; the dot only repeats it (colour is never the
     * only cue, for colour-blind users). */
    const pair = overallState(st);
    setText($('stateText'), pair[0]);
    $('linkDot').className = 'dot ' + pair[1];
    $('statePill').className = 'statepill ' + pair[1];

    /* Only counts ICs in state 'ok', same as the CHAIN tile, so the two
     * numbers always match. */
    const chain = st.chain || [];
    const healthy = chain.filter((c) => c.state === 'ok').length;
    const expected = chain.length || (cfg().ics || 1);
    setText($('chainText'), chain.length ? healthy + '/' + expected : '--');
    $('chainText').className = 'tnum'
      + (chain.length && healthy < expected ? ' bad' : '');
    setText($('rateText'), fmt(st.rate_hz, 1));

    /* Popover detail. Updated every render even while closed, so it's
     * current when opened. */
    let label = src.source || 'unknown';
    if (src.scenario) label += ' · ' + src.scenario;
    if (src.port) label += ' · ' + src.port;
    if (src.channel) label += ' · ' + src.interface + ':' + src.channel;
    if (src.ics) label += ' · ' + src.ics + '×' + (src.cells_per_ic || '?') + ' cells';
    if (src.poll_plan && src.poll_plan.length) label += ' · poll ' + src.poll_plan.join(',');
    setText($('sourceText'), label);

    const link = (st.link && st.link.link) || 'down';
    setText($('linkText'), link.toUpperCase());
    setText($('linkError'), (st.link && st.link.error) ? String(st.link.error) : '');
    setText($('uptimeText'), fmtDuration(st.uptime_s || 0));
    setText($('pecText'), (st.pec_recent || 0) + ' recent · ' + (st.pec_total || 0) + ' total');
    setText($('csvText'), st.csv_path
      ? (st.csv_rows || 0) + ' rows → ' + st.csv_path
      : 'off (start with --csv PATH to log to disk)');
  }

  function renderBanner() {
    const st = state.stats || {};
    const src = st.source || {};
    const chain = st.chain || [];
    const banner = $('banner');
    let title = null, body = null;

    const silent = chain.filter((c) => c.state === 'silent').map((c) => c.ic);
    const flaky = chain.filter((c) => c.state === 'intermittent').map((c) => c.ic);

    if (src.stuck_suspected) {
      title = 'Firmware appears stuck in the scanf loop.';
      body = 'Press the black RESET button on the Nucleo. A non-numeric byte reached '
           + 'scanf("%d"), which fails to convert and leaves the byte in the buffer, so the '
           + 'main loop reprints "Enter cmd:" forever. This tool can\'t clear it; it only '
           + 'sends digits.';
    } else if (!state.connected) {
      title = 'Not connected to the monitor process.';
      body = 'The browser lost its event stream. It reconnects automatically; if this '
           + 'persists, check that python3 -m bms_monitor is still running.';
    } else if ((st.link && st.link.link) === 'reconnecting') {
      title = 'Link lost — reconnecting.';
      body = (st.link.error ? String(st.link.error) + ' ' : '')
           + 'Retrying with back-off. Check the cable and that nothing else holds the port.';
    } else if (silent.length) {
      title = silent.length === 1
        ? ('IC' + silent[0] + ' is not answering.')
        : (silent.length + ' ICs are not answering: IC' + silent.join(', IC') + '.');
      body = 'The poll cycle is coming back without them, so pack voltage and spread are '
           + 'not being computed, since a total from part of the chain would be wrong. '
           + 'Check the isoSPI daisy chain, starting at the '
           + 'segment board before the first missing IC.';
    } else if (flaky.length) {
      title = 'Intermittent response from IC' + flaky.join(', IC') + '.';
      body = 'Those chips are answering some cycles and not others, which usually means a '
           + 'marginal isoSPI connection rather than a dead chip.';
    } else if (src.source === 'can' && state.summary && state.summary.chain_state === 'INIT') {
      /* The F105 is sending on CAN but its ADBMS6830 chain hasn't come up.
       * Expected with no segment board on isoSPI, and a different problem
       * from "no CAN frames". */
      const out = state.summary.not_responding_ics || [];
      title = 'Controller is on CAN, but its BMS chain is not answering.';
      body = 'CAN2 frames are arriving from the controller (chain state INIT'
           + (out.length ? ', IC' + out.join(', IC') + ' not responding' : '') + '). '
           + 'Expected with no segment board on isoSPI. Otherwise check the isoSPI cable '
           + 'into J22 and that each IC has at least 11 V across its 10 taps.';
    } else if (st.data_state === 'no-data' && (st.uptime_s || 0) > 6) {
      title = 'Connected, but no valid frames yet.';
      body = 'If every cell reads exactly 1.500000 V or 1.499850 V with PECError:1, nothing '
           + 'is answering on isoSPI. The EVAL-ADBMS6830BMSW has no supply of its own; it '
           + 'is powered by the cells it measures.';
    }

    if (title) {
      setText($('bannerTitle'), title);
      setText($('bannerBody'), body);
      banner.classList.add('show');
    } else {
      banner.classList.remove('show');
    }
  }

  /* ------------------------------------------------------------- live */

  function spreadClass(mv) {
    if (mv === null || mv === undefined) return '';
    if (mv >= cfg().crit_spread_mv) return 'critical';
    if (mv >= cfg().warn_spread_mv) return 'warning';
    return 'good';
  }

  function renderLive() {
    const pack = latestUsablePack();
    const st = state.stats || {};
    const stats = pack ? C.packStats(pack) : null;

    if (!stats) {
      setText($('kpiPack'), '--');
      const latest = latestPack();
      setText($('kpiPackSub'), latest
        ? (latest.partial ? 'chain incomplete — IC' + latest.missing.join(', IC') + ' silent'
                          : 'last cycle rejected (PEC error or warm-up)')
        : 'waiting for a valid frame');
      for (const id of ['kpiMin', 'kpiMax', 'kpiSpread']) setText($(id), '--');
      setText($('kpiMinSub'), '--'); setText($('kpiMaxSub'), '--');
    } else {
      setText($('kpiPack'), stats.sum.toFixed(2) + ' V');
      setText($('kpiPackSub'), stats.cells.length + ' cells · mean '
        + stats.mean.toFixed(4) + ' V');
      setText($('kpiMin'), stats.min.v.toFixed(4) + ' V');
      setText($('kpiMinSub'), 'IC' + stats.min.ic + ' C' + stats.min.cell + ' · '
        + ((stats.min.v - stats.mean) * 1000).toFixed(1) + ' mV vs mean');
      setText($('kpiMax'), stats.max.v.toFixed(4) + ' V');
      setText($('kpiMaxSub'), 'IC' + stats.max.ic + ' C' + stats.max.cell + ' · +'
        + ((stats.max.v - stats.mean) * 1000).toFixed(1) + ' mV vs mean');
      setText($('kpiSpread'), stats.spreadMv.toFixed(1) + ' mV');
      $('kpiSpread').className = 'value ' + spreadClass(stats.spreadMv);
      setText($('kpiSpreadSub'), 'warn at ' + cfg().warn_spread_mv + ' mV');

      const recent = visiblePacks().slice(-160).map(C.packStats).filter(Boolean);
      const t = C.theme();
      C.drawSparkline($('sparkMin'), recent.map((x) => x.min.v), t.s1);
      C.drawSparkline($('sparkMax'), recent.map((x) => x.max.v), t.s2);
      C.drawSparkline($('sparkSpread'), recent.map((x) => x.spreadMv), t.ink2);
    }

    setText($('kpiPec'), String(st.pec_recent || 0));
    setText($('kpiPecSub'), 'in last ' + (st.pec_window || 0) + ' cycles · '
      + (st.pec_total || 0) + ' total · ' + (st.partial_cycles || 0) + ' partial');

    const chain = st.chain || [];
    const ok = chain.filter((c) => c.state === 'ok').length;
    setText($('kpiChain'), chain.length ? ok + ' / ' + chain.length : '--');
    $('kpiChain').className = 'value ' + (chain.length && ok === chain.length ? 'good' : 'critical');
    const badIcs = chain.filter((c) => c.state !== 'ok').map((c) => 'IC' + c.ic);
    setText($('kpiChainSub'), badIcs.length ? 'problem: ' + badIcs.join(', ') : 'all ICs answering');

    renderIcStrip(chain);
    renderCharts();
    renderCellGrid(stats);
    renderTable(stats);
    renderFaults();
  }

  function renderIcStrip(chain) {
    const strip = $('icStrip');
    if (strip.childElementCount !== chain.length) {
      while (strip.firstChild) strip.removeChild(strip.firstChild);
      for (let i = 0; i < chain.length; i++) {
        const d = document.createElement('div');
        d.className = 'icbox';
        d.appendChild(document.createElement('b'));
        d.appendChild(document.createElement('span'));
        const extra = document.createElement('small');
        extra.className = 'icextra';
        d.appendChild(extra);
        strip.appendChild(d);
      }
    }
    const per = cfg().ics_per_segment || 2;
    const words = { ok: 'ok', pec: 'PEC ERR', silent: 'SILENT', intermittent: 'flaky', unknown: '–' };
    const discharge = currentDischarge();
    for (let i = 0; i < chain.length; i++) {
      const c = chain[i];
      const box = strip.children[i];
      const fw = flagWords(c.flags);
      const bal = discharge.cells.filter((d) => d.ic === c.ic).map((d) => 'C' + d.cell);
      box.className = 'icbox ' + c.state + (fw.length ? ' flagged' : '') + (bal.length ? ' balancing' : '');
      /* Die temperature on the box itself, plus any cell being discharged. */
      const die = (typeof c.die_temp_c === 'number')
        ? c.die_temp_c.toFixed(1) + ' \u00b0C'
        : (c.status_ok === false ? 'die ? \u00b0C' : '');
      const cpi = (latestPack() || {}).cells_per_ic;
      const balText = bal.length && bal.length === cpi ? 'all' : bal.join(' ');
      setText(box.children[2], [die, bal.length ? '\u21af ' + balText : '']
        .filter(Boolean).join(' \u00b7 '));
      setText(box.children[0], 'IC' + c.ic);
      setText(box.children[1], words[c.state] + ' · S' + (Math.floor((c.ic - 1) / per) + 1)
        + (fw.length ? ' · ' + fw.join(' ') : ''));
      box.title = 'IC' + c.ic + ': seen in ' + c.seen + ' of the last ' + c.of
        + ' cycles, ' + c.good + ' with a good PEC'
        + (c.ccount === null || c.ccount === undefined ? '' : ', CCount ' + c.ccount)
        + (c.jumps ? ', ' + c.jumps + ' counter jump(s)' : '')
        + (fw.length ? '\nController flags: ' + fw.join(', ') : '')
        + (c.status_ok === false ? '\nController status read failed PEC: flags unknown' : '')
        + (c.die_temp_c === null || c.die_temp_c === undefined ? '' : '\nDie ' + c.die_temp_c.toFixed(1) + ' \u00b0C')
        + (bal.length ? '\nDischarging ' + bal.join(', ') : '');
    }
    setText($('chainHint'), chain.length + ' IC(s) · ' + (cfg().cells_per_ic)
      + ' populated cells each · grouped S1..Sn by segment board');
  }

  function viewSpec() {
    return {
      packs: visiblePacks(),
      latest: latestPack(),
      latestUsable: latestUsablePack(),
      windowS: state.windowS,
      pinned: state.pinned,
      hoverX: state.hoverX,
      cellScale: state.cellScale,
      focusSegment: state.focusSegment,
      seriesMode: state.showContext ? 'all' : 'selected',
      selected: {},
      icsPerSegment: cfg().ics_per_segment,
      cursorT: state.mode === 'replay' ? state.cursorT : null,
      mode: state.mode,
      uv: cfg().uv, ov: cfg().ov,
      emptyMessage: state.connected ? 'Waiting for a valid frame'
                                    : 'Disconnected from the monitor process'
    };
  }

  function renderCharts() {
    const view = viewSpec();
    const ics = (view.latest && view.latest.expected_ics) || cfg().ics || 1;
    const per = cfg().ics_per_segment || 1;
    const canvas = $('cellCanvas');
    /* Height depends on the number of panels, not chips. */
    const panels = C.cellViewPanels(ics, per, state.focusSegment);
    const wanted = C.cellViewHeight(panels.length) + 'px';
    if (canvas.style.height !== wanted) canvas.style.height = wanted;
    const drawn = C.drawCellView(canvas, view) || {};
    state.cellHits = drawn.cells || [];
    state.cellPanels = drawn.panels || [];
    renderDrillBar(ics, per);
    renderTooltip(C.drawTimeSeries($('tsCanvas'), view));
  }

  /* Board picker. Only rebuilt when the chain shape changes, so the buttons
   * aren't replaced under the pointer. */
  function renderDrillBar(ics, per) {
    const bar = $('drillBar');
    const segs = C.segmentCount(ics, per);
    const signature = segs + ':' + ics + ':' + per;

    if (bar.dataset.signature !== signature) {
      bar.dataset.signature = signature;
      while (bar.firstChild) bar.removeChild(bar.firstChild);
      if (segs > 1) {
        const all = document.createElement('button');
        all.type = 'button';
        all.dataset.focus = '';
        all.textContent = 'Whole pack';
        all.title = 'One panel per segment board';
        bar.appendChild(all);
        for (let sgood = 1; sgood <= segs; sgood++) {
          const b = document.createElement('button');
          b.type = 'button';
          b.dataset.focus = String(sgood);
          const members = C.segmentIcs(sgood, ics, per);
          b.textContent = 'S' + sgood;
          b.title = 'Segment ' + sgood + ' — IC' + members.join(', IC')
            + '. Zooms the voltage axis to this board.';
          bar.appendChild(b);
        }
      }
    }

    for (const b of bar.querySelectorAll('[data-focus]')) {
      const value = b.dataset.focus === '' ? null : Number(b.dataset.focus);
      b.setAttribute('aria-pressed', String(value === state.focusSegment));
    }

    const focused = state.focusSegment !== null;
    /* When drilled in, min/max are for this board, not the whole pack, so
     * the legend says which. */
    const scope = focused ? ' on S' + state.focusSegment : ' in pack';
    setText($('legendMin'), 'min cell' + (segs > 1 ? scope : ''));
    setText($('legendMax'), 'max cell' + (segs > 1 ? scope : ''));
    setText($('cellViewHint'), segs > 1
      ? (focused
        ? ('Segment ' + state.focusSegment + ' only — axis zoomed to this board, '
           + 'pack mean still shown. Click a cell to pin it.')
        : ('One panel per segment board, shared axis. Pick a board to zoom in. '
           + 'Click a cell to pin it.'))
      : 'Dot = cell voltage, bar = deviation from pack mean. Click a cell to pin it.');
  }

  function renderTooltip(hover) {
    const tip = $('tsTooltip');
    if (!hover) { tip.classList.remove('show'); return; }
    const p = hover.pack;
    while (tip.firstChild) tip.removeChild(tip.firstChild);

    const row = (label, value, bad) => {
      const d = document.createElement('div');
      d.className = 'tt-row' + (bad ? ' tt-bad' : '');
      const a = document.createElement('span'); a.textContent = label;
      const b = document.createElement('b'); b.textContent = value;
      d.appendChild(a); d.appendChild(b);
      tip.appendChild(d);
    };

    row('t', '-' + (hover.tEnd - p.t).toFixed(1) + ' s');
    if (p.pec_ok === false) row('PEC', 'ERROR — cycle rejected', true);
    if (p.partial === true) row('chain', 'IC' + p.missing.join(', IC') + ' silent', true);
    if (p.stale === true) row('state', 'stale (warm-up / no device)', true);

    const stats = C.packStats(p);
    if (stats) {
      row('pack', stats.sum.toFixed(2) + ' V');
      row('min', 'IC' + stats.min.ic + ' C' + stats.min.cell + '  ' + stats.min.v.toFixed(4) + ' V');
      row('max', 'IC' + stats.max.ic + ' C' + stats.max.cell + '  ' + stats.max.v.toFixed(4) + ' V');
      row('spread', stats.spreadMv.toFixed(1) + ' mV');
      if (state.pinned) {
        const m = /^IC(\d+)C(\d+)$/.exec(state.pinned);
        if (m) {
          const v = C.valueFor(p, Number(m[1]), Number(m[2]));
          row(state.pinned + ' (pinned)', v === null ? 'no reading' : v.toFixed(4) + ' V', v === null);
        }
      }
    }

    const wrap = $('tsCanvas').parentElement;
    const left = hover.x + 14 + 210 > wrap.clientWidth ? hover.x - 14 - 210 : hover.x + 14;
    tip.style.left = Math.max(4, left) + 'px';
    tip.style.top = '18px';
    tip.classList.add('show');
  }

  /* ------------------------------------------------------- cells grid

     Every cell at once, laid out like the hardware: one row per chip, a
     rule between segment boards.

     Colour is deviation from the pack mean (blue low, orange high, same as
     the series colours). Intensity is relative to a reference value that
     is printed under the grid.

     UV/OV flags also get a heavy border and a glyph, not just colour. */

  /* Per-chip flags from the controller over CAN (0x72n byte 1). They only
   * say which chip has a UV/OV cell, not which cell. `known` is false when
   * no chip has reported flags (text source, or no per-IC frame yet), so we
   * never show "none" without data. */
  const FLAG_WORDS = { any_ov: 'OV', any_uv: 'UV', thsd: 'THSD', spiflt: 'SPI fault', open_wire: 'open wire' };
  function flagWords(flags) {
    return (flags || []).map((f) => FLAG_WORDS[f] || f);
  }
  function chipFlagSummary() {
    const chain = (state.stats && state.stats.chain) || [];
    let known = false;
    const items = [];
    for (const c of chain) {
      if (c.flags === null || c.flags === undefined) continue;
      known = true;
      for (const w of flagWords(c.flags)) items.push('IC' + c.ic + ' ' + w);
    }
    return { known: known, items: items };
  }

  function renderCellGrid(stats) {
    const grid = $('cellGrid');
    const legend = $('cellGridLegend');

    /* Uses two different cycles:
     * - Per-IC state comes from the latest cycle, so a chip that just went
     *   quiet shows up as a SILENT row straight away.
     * - Mean/min/max come from the last usable cycle, since a mean over
     *   part of the chain is wrong. The legend notes when the two differ. */
    const pack = latestPack();
    const usable = latestUsablePack();

    if (!pack || !stats || !usable) {
      while (grid.firstChild) grid.removeChild(grid.firstChild);
      setText(legend, '');
      return;
    }
    const degraded = pack !== usable;

    const ics = pack.expected_ics || 1;
    const cpi = pack.cells_per_ic || 16;
    const per = cfg().ics_per_segment || 1;
    const fields = (state.stats && state.stats.status_fields) || {};
    const polled = !!(state.stats && state.stats.status_polled);
    const byIc = {};
    for (const entry of (pack.ics || [])) byIc[entry.ic] = entry;
    const usableByIc = {};
    for (const entry of (usable.ics || [])) usableByIc[entry.ic] = entry;

    /* One colour reference for the whole grid (shown in the legend).
     * Clamped to 4-60 mV so a well-balanced pack doesn't turn 0.2 mV of
     * noise into full colour. */
    let maxDev = 0;
    for (const c of stats.cells) {
      maxDev = Math.max(maxDev, Math.abs(c.v - stats.mean) * 1000);
    }
    const refMv = Math.max(4, Math.min(60, Math.ceil(maxDev)));
    const discharge = currentDischarge();

    /* Only rebuild when the shape changes; otherwise update in place. */
    const signature = ics + ':' + cpi + ':' + per;
    if (grid.dataset.signature !== signature) {
      grid.dataset.signature = signature;
      while (grid.firstChild) grid.removeChild(grid.firstChild);
      const cols = '54px repeat(' + cpi + ', minmax(0, 1fr))';

      const head = document.createElement('div');
      head.className = 'cg-row cg-head';
      head.style.gridTemplateColumns = cols;
      const corner = document.createElement('span');
      corner.className = 'cg-rowlabel';
      head.appendChild(corner);
      for (let i = 1; i <= cpi; i++) {
        const h = document.createElement('span');
        h.textContent = 'C' + i;
        head.appendChild(h);
      }
      grid.appendChild(head);

      for (let ic = 1; ic <= ics; ic++) {
        const row = document.createElement('div');
        row.className = 'cg-row';
        row.style.gridTemplateColumns = cols;
        if (per > 0 && ic > 1 && (ic - 1) % per === 0) row.classList.add('cg-boardtop');
        const label = document.createElement('span');
        label.className = 'cg-rowlabel';
        row.appendChild(label);
        for (let i = 1; i <= cpi; i++) {
          const cellBtn = document.createElement('button');
          cellBtn.type = 'button';
          cellBtn.className = 'cg-cell';
          cellBtn.dataset.key = C.cellKey(ic, i);
          row.appendChild(cellBtn);
        }
        grid.appendChild(row);
      }
    }

    for (let ic = 1; ic <= ics; ic++) {
      const row = grid.children[ic];          /* 0 is the header row */
      if (!row) continue;
      const live = byIc[ic];
      const silent = !live;
      const pecBad = !!live && live.pec_ok === false;
      /* A silent or PEC-failed chip has no reading this cycle. Don't fall
       * back to its last good value, or stale data would look live. */
      const entry = (live && !pecBad) ? live : null;
      const seg = per > 0 ? C.segmentOf(ic, per) : 1;

      row.className = 'cg-row'
        + (per > 0 && ic > 1 && (ic - 1) % per === 0 ? ' cg-boardtop' : '')
        + (silent ? ' cg-silent' : (pecBad ? ' cg-pec' : ''));
      setText(row.children[0], 'IC' + ic);
      row.children[0].title = 'IC' + ic + ' on segment board S' + seg;

      for (let i = 1; i <= cpi; i++) {
        const btn = row.children[i];
        const key = C.cellKey(ic, i);
        const v = entry ? entry.cells[i - 1] : null;

        btn.className = 'cg-cell';
        if (typeof v !== 'number' || !isFinite(v)) {
          setText(btn, silent ? '—' : (pecBad ? 'PEC' : '—'));
          btn.classList.add('cg-nodata');
          btn.style.background = '';
          btn.title = silent
            ? 'IC' + ic + ' did not answer this cycle'
            : (pecBad ? 'IC' + ic + ' frame failed its checksum — not a measurement'
                      : 'no reading');
          continue;
        }

        const dev = (v - stats.mean) * 1000;
        setText(btn, state.cellUnit === 'mv'
          ? (dev >= 0 ? '+' : '') + dev.toFixed(1)
          : v.toFixed(4));

        const frac = Math.min(1, Math.abs(dev) / refMv);
        const rgb = dev < 0 ? '57, 135, 229' : '217, 89, 38';
        /* Dead band plus a curve, so only real outliers get much colour. */
        btn.style.background = frac < 0.18 ? 'transparent'
          : 'rgba(' + rgb + ', ' + (Math.pow(frac, 1.6) * 0.34).toFixed(3) + ')';

        const uv = polled ? Number(fields['C' + i + 'UV']) : 0;
        const ov = polled ? Number(fields['C' + i + 'OV']) : 0;
        if (uv) btn.classList.add('cg-uv');
        if (ov) btn.classList.add('cg-ov');
        if (key === stats.min.key) btn.classList.add('cg-min');
        if (key === stats.max.key) btn.classList.add('cg-max');
        if (state.pinned === key) btn.classList.add('cg-pinned');
        const bal = discharge.byKey[key];
        if (bal) btn.classList.add('cg-bal');

        btn.title = 'IC' + ic + ' C' + i + ' · S' + seg + '\n'
          + v.toFixed(4) + ' V · ' + (dev >= 0 ? '+' : '') + dev.toFixed(1) + ' mV vs mean'
          + (uv ? '\nUNDER-VOLTAGE flag set' : '')
          + (ov ? '\nOVER-VOLTAGE flag set' : '')
          + (bal ? '\nDISCHARGING (' + (bal.source === '0x6FF' ? 'balance test' : 'DCC bit') + ')' : '')
          + '\nclick to pin on the time series';
      }
    }

    const flagged = [];
    if (polled) {
      for (const c of stats.cells) {
        if (Number(fields['C' + c.cell + 'UV'])) flagged.push('IC' + c.ic + ' C' + c.cell + ' UV');
        if (Number(fields['C' + c.cell + 'OV'])) flagged.push('IC' + c.ic + ' C' + c.cell + ' OV');
      }
    }
    const balText = discharge.cells.length
      ? '\u21af Discharging: ' + discharge.cells.map((d) => 'IC' + d.ic + ' C' + d.cell).join(', ') + '. '
      : '';
    setText(legend, balText + (degraded
        ? 'Live cycle is incomplete — values shown are from the last complete one '
          + ((cursor() - usable.t).toFixed(1)) + ' s ago. '
        : '')
      + 'Colour = deviation from the ' + stats.mean.toFixed(4)
      + ' V pack mean, full intensity at \u00b1' + refMv + ' mV. '
      + 'Blue low, orange high. '
      + (polled
        ? (flagged.length ? 'Flagged: ' + flagged.slice(0, 6).join(', ')
            + (flagged.length > 6 ? ' and ' + (flagged.length - 6) + ' more' : '')
          : 'No UV/OV flags set.')
        : chipFlagText()));
  }

  function chipFlagText() {
    const chips = chipFlagSummary();
    if (!chips.known) return 'UV/OV not polled — add 15 to --poll to read the flags.';
    return chips.items.length
      ? 'Controller flags: ' + chips.items.join(', ') + ' (per chip; CAN does not say which cell).'
      : 'Controller reports no UV/OV flags.';
  }

  function renderTable(stats) {
    const body = $('cellTableBody');
    const pack = latestUsablePack();
    if (!pack || !stats) {
      while (body.firstChild) body.removeChild(body.firstChild);
      setText($('tableHint'), 'no valid cycle yet');
      return;
    }
    const fields = (state.stats && state.stats.status_fields) || {};
    const polled = !!(state.stats && state.stats.status_polled);
    setText($('tableHint'), (polled ? 'UV/OV from RDSTATD'
        : (chipFlagSummary().known ? 'per-cell UV/OV not on CAN; ' + chipFlagText() : 'UV/OV not polled'))
      + ' · ' + stats.cells.length + ' cells');

    const ranked = stats.cells.slice().sort((a, b) => a.v - b.v);
    const rankOf = {};
    ranked.forEach((c, i) => { rankOf[c.key] = i + 1; });
    const rows = state.tableSort === 'voltage' ? ranked : stats.cells;
    const discharge = currentDischarge();

    if (body.childElementCount !== rows.length) {
      while (body.firstChild) body.removeChild(body.firstChild);
      for (let i = 0; i < rows.length; i++) {
        const tr = document.createElement('tr');
        for (let k = 0; k < 6; k++) tr.appendChild(document.createElement('td'));
        body.appendChild(tr);
      }
    }
    for (let i = 0; i < rows.length; i++) {
      const c = rows[i];
      const tr = body.children[i];
      const bal = !!discharge.byKey[c.key];
      tr.className = (c.key === stats.min.key ? 'is-min' : (c.key === stats.max.key ? 'is-max' : ''))
        + (bal ? ' is-bal' : '');
      const td = tr.children;
      setText(td[0], 'IC' + c.ic + ' C' + c.cell + (state.pinned === c.key ? ' ●' : '')
        + (bal ? ' \u21af discharging' : ''));
      setText(td[1], c.v.toFixed(4));
      const dev = (c.v - stats.mean) * 1000;
      setText(td[2], (dev >= 0 ? '+' : '') + dev.toFixed(1));
      setText(td[3], String(rankOf[c.key]));
      td[3].className = rankOf[c.key] <= 3 ? 'rank-worst' : '';
      applyFlag(td[4], polled ? fields['C' + c.cell + 'UV'] : undefined);
      applyFlag(td[5], polled ? fields['C' + c.cell + 'OV'] : undefined);
    }
  }

  function applyFlag(td, value) {
    if (value === undefined) { setText(td, '–'); td.className = 'flag-ok'; return; }
    const bad = Number(value) !== 0;
    setText(td, bad ? 'YES' : 'no');
    td.className = bad ? 'flag-bad' : 'flag-ok';
  }

  /* Fault rows. With status polling off they all read "not polled", not
   * "ok". */
  const FAULTS = [
    ['THSD', 'thermal shutdown'], ['SPIFLT', 'SPI fault'],
    ['CSFLT', 'comparison fault'], ['OSCCHK', 'oscillator check'],
    ['VA_UV', 'analog supply UV'], ['VA_OV', 'analog supply OV'],
    ['VD_UV', 'digital supply UV'], ['VD_OV', 'digital supply OV']
  ];

  function renderFaults() {
    const grid = $('faultGrid');
    const st = state.stats || {};
    const polled = !!st.status_polled;
    const fields = st.status_fields || {};
    const can = state.summary;

    setText($('faultHint'), can && can.faults
      ? ('from CAN 0x602 · ' + (can.faults.length ? can.faults.join(', ') : 'no faults set'))
      : (polled
        ? 'from RDSTATA-E · die ' + (typeof fields.ITMP === 'number' ? fields.ITMP.toFixed(1) + ' °C' : '--')
        : 'status not polled. Add 15 to --poll to enable it (see docs/serial.md)'));

    if (grid.childElementCount !== FAULTS.length) {
      while (grid.firstChild) grid.removeChild(grid.firstChild);
      for (let i = 0; i < FAULTS.length; i++) {
        const div = document.createElement('div');
        div.className = 'fault';
        const name = document.createElement('span'); name.className = 'name';
        const s = document.createElement('span'); s.className = 'state';
        div.appendChild(name); div.appendChild(s);
        grid.appendChild(div);
      }
    }
    for (let i = 0; i < FAULTS.length; i++) {
      const [key, label] = FAULTS[i];
      const div = grid.children[i];
      setText(div.children[0], label);
      if (!polled || fields[key] === undefined) {
        div.className = 'fault notpolled';
        setText(div.children[1], 'not polled');
      } else {
        const bad = Number(fields[key]) !== 0;
        div.className = 'fault' + (bad ? ' bad' : '');
        setText(div.children[1], bad ? 'FAULT' : 'ok');
      }
    }
  }

  /* ----------------------------------------------------------- charge */

  function spreadSeries() {
    const out = [];
    for (const p of visiblePacks()) {
      const s = C.packStats(p);
      out.push({ t: p.t, v: s ? s.spreadMv : null, ok: !!s });
    }
    return out;
  }

  /* Least-squares slope in mV/min over the recent window. Null if there
   * are fewer than 8 good points. */
  function convergenceRate(series, windowS) {
    const good = series.filter((p) => p.ok);
    if (good.length < 8) return null;
    const tEnd = good[good.length - 1].t;
    const win = good.filter((p) => p.t >= tEnd - windowS);
    if (win.length < 8) return null;
    let n = 0, st = 0, sv = 0, stt = 0, stv = 0;
    for (const p of win) { n++; st += p.t; sv += p.v; stt += p.t * p.t; stv += p.t * p.v; }
    const denom = n * stt - st * st;
    if (Math.abs(denom) < 1e-9) return null;
    return ((n * stv - st * sv) / denom) * 60;   /* mV per minute */
  }

  function renderCharge() {
    const series = spreadSeries();
    const latest = series.filter((p) => p.ok).slice(-1)[0];
    const target = cfg().warn_spread_mv;

    if (!latest) {
      setText($('chgSpread'), '--');
      setText($('chgTrend'), '--');
      setText($('chgEta'), '--');
    } else {
      setText($('chgSpread'), latest.v.toFixed(1) + ' mV');
      $('chgSpread').className = 'value ' + spreadClass(latest.v);

      const rate = convergenceRate(series, Math.min(state.chgWindowS, 600));
      if (rate === null) {
        setText($('chgTrend'), '--');
        setText($('chgTrendSub'), 'not enough data yet');
        setText($('chgEta'), '--');
        setText($('chgEtaSub'), 'needs a longer sample');
      } else {
        setText($('chgTrend'), (rate > 0 ? '+' : '') + rate.toFixed(2));
        setText($('chgTrendSub'), 'mV/min · ' + (rate < -0.01 ? 'converging'
          : (rate > 0.01 ? 'diverging' : 'flat')));
        $('chgTrend').className = 'value ' + (rate < -0.01 ? 'good' : (rate > 0.01 ? 'warning' : ''));
        if (rate >= -0.01 || latest.v <= target) {
          setText($('chgEta'), latest.v <= target ? 'at target' : '--');
          setText($('chgEtaSub'), latest.v <= target
            ? 'spread is already under ' + target + ' mV'
            : 'not converging — no estimate');
        } else {
          const minutes = (latest.v - target) / (-rate);
          setText($('chgEta'), minutes > 600 ? '>10 h' : fmtDuration(minutes * 60));
          setText($('chgEtaSub'), 'to reach ' + target + ' mV — straight-line estimate, '
            + 'not a measurement');
        }
      }
    }

    /* SOC and balancing only come from what the controller reports over
     * CAN. Nothing is estimated here. */
    const can = state.summary || {};
    const socTile = $('kpiSoc');
    if (typeof can.soc_pct === 'number') {
      socTile.classList.remove('na');
      socTile.children[1].className = 'value';
      setText(socTile.children[1], can.soc_pct.toFixed(1) + ' %');
      setText($('socSub'), 'reported by the controller over CAN 0x602');
    } else {
      socTile.classList.add('na');
      socTile.children[1].className = 'value na';
      setText(socTile.children[1], 'n/a');
      setText($('socSub'), state.summary
        ? 'the controller reports it is not estimating SOC'
        : 'requires current integration on the controller');
    }
    const balTile = $('kpiBalancing');
    if (typeof can.balancing_count === 'number' && can.balancing_count > 0) {
      balTile.classList.remove('na');
      balTile.children[1].className = 'value';
      setText(balTile.children[1], String(can.balancing_count));
      setText($('balSub'), 'cells balancing, reported over CAN 0x602');
    } else {
      balTile.classList.add('na');
      balTile.children[1].className = 'value na';
      setText(balTile.children[1], 'n/a');
      setText($('balSub'), state.summary
        ? 'controller reports no cells balancing'
        : 'firmware DCC is not enabled — nothing balances yet');
    }

    const t = C.theme();
    C.drawTrend($('chgCanvas'), {
      series: [{ points: series, colour: t.s1, width: 2, label: 'spread' }],
      windowS: state.chgWindowS,
      decimals: 1,
      minSpan: 4,
      fallback: [0, 50],
      refs: [
        { v: cfg().warn_spread_mv, colour: t.warning, label: 'warn ' + cfg().warn_spread_mv + ' mV' },
        { v: cfg().crit_spread_mv, colour: t.critical, label: 'critical ' + cfg().crit_spread_mv + ' mV' }
      ],
      emptyMessage: 'No valid cycles yet'
    });

    renderSession(latest);
    renderRanked();
  }

  function renderSession(latest) {
    const s = state.session;
    const list = $('sessionStats');
    while (list.firstChild) list.removeChild(list.firstChild);
    const add = (k, v) => {
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v;
      list.appendChild(dt); list.appendChild(dd);
    };

    if (!s) {
      setText($('sessionHint'), 'not recording');
      setText($('btnSession'), 'Start session');
      $('btnSessionExport').disabled = !state.lastSession;
      if (state.lastSession) {
        add('last session', fmtDuration(state.lastSession.duration));
        add('spread start → end', state.lastSession.startSpread.toFixed(1) + ' → '
          + state.lastSession.endSpread.toFixed(1) + ' mV');
        add('best / worst', state.lastSession.minSpread.toFixed(1) + ' / '
          + state.lastSession.maxSpread.toFixed(1) + ' mV');
      } else {
        add('', 'Press Start to record spread, min and max over a charge or balance session.');
      }
      return;
    }

    if (latest) {
      s.samples.push({ t: latest.t, v: latest.v });
      if (s.samples.length > 20000) s.samples.splice(0, s.samples.length - 20000);
      s.endSpread = latest.v;
      s.minSpread = Math.min(s.minSpread, latest.v);
      s.maxSpread = Math.max(s.maxSpread, latest.v);
      s.duration = latest.t - s.startT;
    }
    setText($('sessionHint'), 'recording');
    setText($('btnSession'), 'Stop session');
    $('btnSessionExport').disabled = s.samples.length < 2;
    add('duration', fmtDuration(s.duration));
    add('samples', String(s.samples.length));
    add('spread at start', s.startSpread.toFixed(1) + ' mV');
    add('spread now', s.endSpread.toFixed(1) + ' mV');
    add('change', ((s.endSpread - s.startSpread) >= 0 ? '+' : '')
      + (s.endSpread - s.startSpread).toFixed(1) + ' mV');
    add('best / worst', s.minSpread.toFixed(1) + ' / ' + s.maxSpread.toFixed(1) + ' mV');
  }

  function renderRanked() {
    const wrap = $('rankList');
    const pack = latestUsablePack();
    const stats = pack ? C.packStats(pack) : null;
    if (!stats) {
      while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
      return;
    }
    const rows = stats.cells.slice().sort((a, b) => a.v - b.v);
    const devs = rows.map((c) => (c.v - stats.mean) * 1000);
    const span = Math.max(1, Math.max.apply(null, devs.map(Math.abs)));

    if (wrap.childElementCount !== rows.length) {
      while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
      for (let i = 0; i < rows.length; i++) {
        const div = document.createElement('div');
        div.className = 'rankrow';
        const n = document.createElement('span'); n.className = 'name';
        const bar = document.createElement('span'); bar.className = 'bar';
        bar.appendChild(document.createElement('i'));
        const v = document.createElement('span'); v.className = 'val';
        div.appendChild(n); div.appendChild(bar); div.appendChild(v);
        wrap.appendChild(div);
      }
    }
    for (let i = 0; i < rows.length; i++) {
      const c = rows[i];
      const row = wrap.children[i];
      row.className = 'rankrow' + (c.key === stats.min.key ? ' is-min'
        : (c.key === stats.max.key ? ' is-max' : ''));
      setText(row.children[0], 'IC' + c.ic + ' C' + c.cell);
      const dev = devs[i];
      const bar = row.children[1].firstChild;
      const half = 50;
      const pct = Math.min(half, Math.abs(dev) / span * half);
      if (dev < 0) { bar.style.left = (half - pct) + '%'; bar.style.width = pct + '%'; }
      else { bar.style.left = half + '%'; bar.style.width = pct + '%'; }
      setText(row.children[2], (dev >= 0 ? '+' : '') + dev.toFixed(1) + ' mV');
    }
  }

  /* ------------------------------------------------------------ temps */

  /* NTC beta model, same as bms_monitor/model.py. Returns null if the
   * voltage doesn't look like a connected thermistor (open or shorted). */
  function ntcCelsius(volts, n) {
    if (typeof volts !== 'number' || !isFinite(volts)) return null;
    const vref = n.vref;
    if (!(vref > 0) || !(n.series > 0)) return null;
    const margin = vref * 0.001;
    if (!(volts > margin && volts < vref - margin)) return null;
    const r = n.pullup ? n.series * volts / (vref - volts)
                       : n.series * (vref - volts) / volts;
    if (!(r > 0) || !(n.beta > 0) || !(n.r25 > 0)) return null;
    const inv = (1 / 298.15) + (Math.log(r / n.r25) / n.beta);
    if (!(inv > 0)) return null;
    const c = (1 / inv) - 273.15;
    if (!(c >= -60 && c <= 200)) return null;
    return { c: c, r: r };
  }

  function latestAuxByIc() {
    const out = {};
    for (const a of state.aux) out[a.ic] = a;
    return out;
  }

  const TEMP_HEADERS = {
    aux: ['Sensor', 'Volts', 'Resistance', '°C'],
    can: ['Sensor', 'Chip', 'Reading', '°C']
  };

  function setTempMode(mode) {
    const head = $('tempTableHead');
    if (head.dataset.mode === mode) return;
    head.dataset.mode = mode;
    TEMP_HEADERS[mode].forEach((label, i) => setText(head.children[i], label));
    const body = $('tempTableBody');
    while (body.firstChild) body.removeChild(body.firstChild);
    setText($('tCountLabel'), mode === 'can' ? 'Thermistors' : 'Channels');
    setText($('tCountSub'), mode === 'can' ? 'reading a plausible temperature'
                                           : 'AUX GPIO reporting a valid thermistor');
    setText($('tempTableHint'), mode === 'can'
      ? 'Converted on the controller (10 kΩ NTC, B 3435). An open, shorted or off-scale '
        + 'probe, or a chip that is not answering, reads “no reading”.'
      : 'NTC beta model — adjust the curve in Settings');
  }

  /* Thermistors converted on the controller and sent over CAN (0x730).
   * Named RT1..RTn like on the board, with the chip each one is on. */
  function renderCanTemps() {
    setTempMode('can');
    const sensors = state.canTemps.sensors;
    const valid = sensors.filter((s) => typeof s.celsius === 'number');
    setText($('tempHint'), valid.length + ' of ' + sensors.length
      + ' thermistors reading · from the controller over CAN');
    setText($('tCount'), valid.length + ' / ' + sensors.length);
    const name = (s) => 'RT' + s.rt + ' · IC' + s.ic;
    if (valid.length) {
      const hot = valid.reduce((a, b) => (b.celsius > a.celsius ? b : a));
      const cold = valid.reduce((a, b) => (b.celsius < a.celsius ? b : a));
      const mean = valid.reduce((sum, s) => sum + s.celsius, 0) / valid.length;
      setText($('tHot'), hot.celsius.toFixed(1) + ' °C');
      setText($('tHotSub'), name(hot));
      setText($('tCold'), cold.celsius.toFixed(1) + ' °C');
      setText($('tColdSub'), name(cold));
      setText($('tMean'), mean.toFixed(1) + ' °C');
      setText($('tMeanSub'), 'spread ' + (hot.celsius - cold.celsius).toFixed(1) + ' °C');
    } else {
      for (const id of ['tHot', 'tCold', 'tMean']) setText($(id), '--');
      setText($('tHotSub'), 'no thermistor has a reading');
      setText($('tColdSub'), '--');
      setText($('tMeanSub'), '--');
    }

    const pts = { hi: [], mean: [], lo: [] };
    for (const h of state.canTempHist) {
      const vals = (h.celsius || []).filter((c) => typeof c === 'number');
      if (!vals.length || typeof h.t !== 'number') continue;
      pts.hi.push({ t: h.t, v: Math.max.apply(null, vals), ok: true });
      pts.lo.push({ t: h.t, v: Math.min.apply(null, vals), ok: true });
      pts.mean.push({ t: h.t, v: vals.reduce((a, b) => a + b, 0) / vals.length, ok: true });
    }
    const th = C.theme();
    C.drawTrend($('tempCanvas'), {
      series: [
        { points: pts.hi, colour: th.s2, width: 2, label: 'max' },
        { points: pts.mean, colour: th.ink2, width: 1.5, label: 'mean' },
        { points: pts.lo, colour: th.s1, width: 2, label: 'min' }
      ],
      windowS: 600, decimals: 1, minSpan: 2, fallback: [15, 45],
      emptyMessage: 'No thermistor readings yet'
    });

    const body = $('tempTableBody');
    if (body.childElementCount !== sensors.length) {
      while (body.firstChild) body.removeChild(body.firstChild);
      for (let i = 0; i < sensors.length; i++) {
        const tr = document.createElement('tr');
        for (let k = 0; k < 4; k++) tr.appendChild(document.createElement('td'));
        body.appendChild(tr);
      }
    }
    for (let i = 0; i < sensors.length; i++) {
      const s = sensors[i];
      const td = body.children[i].children;
      const ok = typeof s.celsius === 'number';
      setText(td[0], 'RT' + s.rt);
      setText(td[1], 'IC' + s.ic);
      setText(td[2], ok ? 'ok' : (s.stale ? 'stale' : 'no reading'));
      setText(td[3], ok ? s.celsius.toFixed(1) : '--');
      td[2].className = ok ? '' : 'flag-bad';
    }
  }

  function renderTemps() {
    if (state.canTemps && state.canTemps.sensors && state.canTemps.sensors.length) {
      renderCanTemps();
      return;
    }
    setTempMode('aux');
    const polled = !!(cfg().aux_polled) || state.aux.length > 0;
    const byIc = latestAuxByIc();
    const n = cfg().ntc;
    const readings = [];
    for (const ic of Object.keys(byIc).map(Number).sort((a, b) => a - b)) {
      const a = byIc[ic];
      a.channels.forEach((v, i) => {
        const conv = ntcCelsius(v, n);
        readings.push({ ic: ic, ch: i + 1, v: v, c: conv ? conv.c : null,
                        r: conv ? conv.r : null, t: a.t });
      });
    }
    const valid = readings.filter((r) => r.c !== null);

    setText($('tempHint'), polled
      ? (valid.length + ' of ' + readings.length + ' AUX channels convert to a plausible '
         + 'temperature · beta ' + n.beta + ', R25 ' + n.r25 + ' Ω, series ' + n.series
         + ' Ω, VREF2 ' + n.vref + ' V')
      : 'AUX not polled — start with --poll 11,12 to read the thermistor channels');

    if (!valid.length) {
      for (const id of ['tHot', 'tCold', 'tMean', 'tCount']) setText($(id), '--');
      setText($('tHotSub'), polled ? 'no channel converts to a plausible temperature'
                                   : 'add 11,12 to --poll');
      setText($('tColdSub'), '--');
      const tt = $('tempTableBody');
      while (tt.firstChild) tt.removeChild(tt.firstChild);
      C.drawTrend($('tempCanvas'), { series: [], emptyMessage: polled
        ? 'No valid thermistor readings' : 'AUX not polled (--poll 11,12)' });
      return;
    }

    const hot = valid.reduce((a, b) => (b.c > a.c ? b : a));
    const cold = valid.reduce((a, b) => (b.c < a.c ? b : a));
    const mean = valid.reduce((s, r) => s + r.c, 0) / valid.length;
    setText($('tHot'), hot.c.toFixed(1) + ' °C');
    setText($('tHotSub'), 'IC' + hot.ic + ' AUX' + hot.ch);
    setText($('tCold'), cold.c.toFixed(1) + ' °C');
    setText($('tColdSub'), 'IC' + cold.ic + ' AUX' + cold.ch);
    setText($('tMean'), mean.toFixed(1) + ' °C');
    setText($('tMeanSub'), 'spread ' + (hot.c - cold.c).toFixed(1) + ' °C');
    setText($('tCount'), valid.length + ' / ' + readings.length);

    /* Trend shows min/mean/max only. Per-channel values are in the table. */
    const buckets = {};
    for (const a of state.aux) {
      const key = Math.round(a.t * 2) / 2;
      const vals = [];
      for (const v of a.channels) {
        const conv = ntcCelsius(v, n);
        if (conv) vals.push(conv.c);
      }
      if (!vals.length) continue;
      if (!buckets[key]) buckets[key] = [];
      buckets[key] = buckets[key].concat(vals);
    }
    const times = Object.keys(buckets).map(Number).sort((a, b) => a - b);
    const mk = (pick) => times.map((t) => ({ t: t, v: pick(buckets[t]), ok: true }));
    const th = C.theme();
    C.drawTrend($('tempCanvas'), {
      series: [
        { points: mk((v) => Math.max.apply(null, v)), colour: th.s2, width: 2, label: 'max' },
        { points: mk((v) => v.reduce((a, b) => a + b, 0) / v.length), colour: th.ink2, width: 1.5, label: 'mean' },
        { points: mk((v) => Math.min.apply(null, v)), colour: th.s1, width: 2, label: 'min' }
      ],
      windowS: 600, decimals: 1, minSpan: 2, fallback: [15, 45]
    });

    const body = $('tempTableBody');
    if (body.childElementCount !== readings.length) {
      while (body.firstChild) body.removeChild(body.firstChild);
      for (let i = 0; i < readings.length; i++) {
        const tr = document.createElement('tr');
        for (let k = 0; k < 4; k++) tr.appendChild(document.createElement('td'));
        body.appendChild(tr);
      }
    }
    for (let i = 0; i < readings.length; i++) {
      const r = readings[i];
      const td = body.children[i].children;
      setText(td[0], 'IC' + r.ic + ' AUX' + r.ch);
      setText(td[1], typeof r.v === 'number' ? r.v.toFixed(4) : '--');
      setText(td[2], r.r === null ? '--' : (r.r / 1000).toFixed(2) + ' kΩ');
      setText(td[3], r.c === null ? 'no sensor' : r.c.toFixed(1));
      td[3].className = r.c === null ? 'flag-ok' : '';
    }
  }

  /* ------------------------------------------------------------ chain */

  function renderChain() {
    const st = state.stats || {};
    const chain = st.chain || [];
    const per = cfg().ics_per_segment || 2;
    const body = $('chainTableBody');
    if (body.childElementCount !== chain.length) {
      while (body.firstChild) body.removeChild(body.firstChild);
      for (let i = 0; i < chain.length; i++) {
        const tr = document.createElement('tr');
        for (let k = 0; k < 11; k++) tr.appendChild(document.createElement('td'));
        body.appendChild(tr);
      }
    }
    const words = { ok: 'ok', pec: 'PEC ERROR', silent: 'SILENT', intermittent: 'intermittent', unknown: 'unknown' };
    for (let i = 0; i < chain.length; i++) {
      const c = chain[i];
      const tr = body.children[i];
      tr.className = (c.state === 'silent' || c.state === 'pec') ? 'is-silent' : '';
      const td = tr.children;
      setText(td[0], 'IC' + c.ic);
      setText(td[1], 'S' + (Math.floor((c.ic - 1) / per) + 1));
      setText(td[2], words[c.state] || c.state);
      setText(td[3], c.seen + ' / ' + c.of);
      setText(td[4], String(c.good));
      setText(td[5], c.ccount === null || c.ccount === undefined ? '--' : String(c.ccount));
      setText(td[6], String(c.jumps));
      /* Factory serial, for checking chain order and spotting a swapped
       * board. Only present if command 22 is polled. */
      setText(td[7], c.sid ? c.sid : '\u2013');
      /* Controller flags (CAN only). Dash = not reported, which isn't the
       * same as "none". */
      const fw = flagWords(c.flags);
      setText(td[8], c.status_ok === false ? 'status read failed'
        : (c.flags === null || c.flags === undefined ? '\u2013' : (fw.length ? fw.join(', ') : 'none')));
      td[8].className = (fw.length || c.status_ok === false) ? 'flag-bad' : '';
      setText(td[9], c.die_temp_c === null || c.die_temp_c === undefined
        ? '\u2013' : c.die_temp_c.toFixed(1));
      /* DCC bits from the per-IC frame (2 Hz). Dash = not reported. */
      const dcc = C.dccChannels(c.balance_mask);
      setText(td[10], c.balance_mask === null || c.balance_mask === undefined
        ? '\u2013' : (dcc.length ? '\u21af ' + dcc.join(', ') : 'none'));
      td[10].className = dcc.length ? 'flag-bal' : '';
    }

    const list = $('transportStats');
    while (list.firstChild) list.removeChild(list.firstChild);
    const src = st.source || {};
    const add = (k, v) => {
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = String(v);
      list.appendChild(dt); list.appendChild(dd);
    };
    for (const key of ['source', 'mode', 'fw_format', 'transport', 'port', 'interface', 'channel',
                       'baud', 'bitrate', 'ics', 'cells_per_ic', 'poll_plan', 'file',
                       'cycles', 'duration_s', 'lines', 'samples', 'unparsed',
                       'incomplete', 'partial_cycles', 'reconnects', 'frames',
                       'frames_sent', 'frames_decoded', 'crc_errors', 'resyncs',
                       'cell_frames', 'ic_frames_seen', 'oldest_frame_s', 'blocks',
                       'bad_rows', 'last_error']) {
      if (src[key] === undefined || src[key] === '' ) continue;
      add(key, typeof src[key] === 'object' ? JSON.stringify(src[key]) : src[key]);
    }
  }

  function renderConsole() {
    const pre = $('rawConsole');
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    while (pre.firstChild) pre.removeChild(pre.firstChild);
    for (const item of state.consoleLines) {
      const span = document.createElement('span');
      span.className = 'l ' + item.cls;
      /* textContent: this is raw device output. */
      span.textContent = item.text;
      pre.appendChild(span);
    }
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  /* ----------------------------------------------------------- replay */

  function syncReplayBar() {
    const bar = $('replayBar');
    bar.hidden = state.mode !== 'replay';
    if (state.mode !== 'replay') return;
    const [t0, t1] = timeRange();
    const cur = cursor();
    const span = Math.max(1e-6, t1 - t0);
    const slider = $('scrub');
    if (document.activeElement !== slider) {
      slider.value = String(Math.round(((cur - t0) / span) * 1000));
    }
    setText($('replayPos'), (cur - t0).toFixed(1) + ' / ' + span.toFixed(1) + ' s');
    setText($('btnPlay'), state.playing ? '⏸ Pause' : '▶ Play');
    const src = (state.stats && state.stats.source) || {};
    setText($('replayFile'), src.file ? (src.file + '  ·  ' + (src.cycles || 0) + ' cycles') : '');
  }

  function stepReplay() {
    if (state.mode !== 'replay' || !state.playing) { lastPlayTick = 0; return; }
    const now = performance.now();
    if (!lastPlayTick) { lastPlayTick = now; return; }
    const dt = (now - lastPlayTick) / 1000;
    lastPlayTick = now;
    const [t0, t1] = timeRange();
    let next = cursor() + dt * state.speed;
    if (next >= t1) { next = t1; state.playing = false; }
    state.cursorT = Math.max(t0, next);
    scheduleRender();
  }

  /* ---------------------------------------------------------- settings */

  const SETTING_FIELDS = [
    ['setUv', 'uv', (c) => c.uv, (c, v) => { c.uv = v; }],
    ['setOv', 'ov', (c) => c.ov, (c, v) => { c.ov = v; }],
    ['setWarn', 'warn', (c) => c.warn_spread_mv, (c, v) => { c.warn_spread_mv = v; }],
    ['setCrit', 'crit', (c) => c.crit_spread_mv, (c, v) => { c.crit_spread_mv = v; }],
    ['setBeta', 'beta', (c) => c.ntc.beta, (c, v) => { c.ntc.beta = v; }],
    ['setR25', 'r25', (c) => c.ntc.r25, (c, v) => { c.ntc.r25 = v; }],
    ['setSeries', 'series', (c) => c.ntc.series, (c, v) => { c.ntc.series = v; }],
    ['setVref', 'vref', (c) => c.ntc.vref, (c, v) => { c.ntc.vref = v; }]
  ];

  function fillSettings() {
    for (const [id, , get] of SETTING_FIELDS) {
      const el = $(id);
      if (el && document.activeElement !== el) el.value = String(get(cfg()));
    }
  }

  /* ------------------------------------------------------------ events */

  function selectTab(name) {
    state.tab = name;
    for (const btn of document.querySelectorAll('[data-tab]')) {
      btn.setAttribute('aria-selected', String(btn.dataset.tab === name));
    }
    for (const panel of document.querySelectorAll('[data-panel]')) {
      panel.hidden = panel.dataset.panel !== name;
    }
    setPref('tab', name);
    scheduleRender();
  }

  /* null = whole pack. Saved so a reload stays on the same board. */
  function setFocus(segment) {
    state.focusSegment = segment;
    setPref('focusSegment', segment === null ? '' : segment);
    scheduleRender();
  }

  /* Both views stay in the DOM and keep rendering; only visibility changes,
   * so switching is instant and never shows stale data. */
  function applyCellsView() {
    $('cellGrid').hidden = state.cellsView !== 'grid';
    $('cellGridLegend').hidden = state.cellsView !== 'grid';
    $('cellTableWrap').hidden = state.cellsView !== 'table';
    for (const b of document.querySelectorAll('[data-cellunit]')) {
      b.hidden = state.cellsView !== 'grid';
    }
    for (const b of document.querySelectorAll('[data-sort]')) {
      b.hidden = state.cellsView !== 'table';
    }
  }

  function toggleGroup(selector, attr, apply) {
    for (const btn of document.querySelectorAll(selector)) {
      btn.addEventListener('click', function () {
        for (const other of document.querySelectorAll(selector)) {
          other.setAttribute('aria-pressed', String(other === this));
        }
        apply(this.dataset[attr]);
        scheduleRender();
      });
    }
  }

  function wire() {
    state.cellScale = pref('cellScale', 'pack') === 'band' ? 'band' : 'pack';
    state.windowS = numPref('window', 30);
    state.chgWindowS = numPref('chgWindow', 120);
    state.showContext = pref('context', '1') === '1';
    const savedFocus = pref('focusSegment', '');
    state.focusSegment = savedFocus === '' ? null : (Number(savedFocus) || null);
    state.tableSort = pref('sort', 'chain') === 'voltage' ? 'voltage' : 'chain';
    state.cellsView = pref('cellsView', 'grid') === 'table' ? 'table' : 'grid';
    state.cellUnit = pref('cellUnit', 'v') === 'mv' ? 'mv' : 'v';
    const startTab = params.get('tab') || pref('tab', 'live');

    for (const btn of document.querySelectorAll('[data-tab]')) {
      btn.addEventListener('click', function () { selectTab(this.dataset.tab); });
    }
    selectTab(['live', 'charge', 'temps', 'chain', 'console'].indexOf(startTab) >= 0
      ? startTab : 'live');

    for (const b of document.querySelectorAll('[data-cellscale]')) {
      b.setAttribute('aria-pressed', String(b.dataset.cellscale === state.cellScale));
    }
    for (const b of document.querySelectorAll('[data-window]')) {
      b.setAttribute('aria-pressed', String(Number(b.dataset.window) === state.windowS));
    }
    for (const b of document.querySelectorAll('[data-chgwindow]')) {
      b.setAttribute('aria-pressed', String(Number(b.dataset.chgwindow) === state.chgWindowS));
    }
    for (const b of document.querySelectorAll('[data-sort]')) {
      b.setAttribute('aria-pressed', String(b.dataset.sort === state.tableSort));
    }
    for (const b of document.querySelectorAll('[data-cellsview]')) {
      b.setAttribute('aria-pressed', String(b.dataset.cellsview === state.cellsView));
    }
    for (const b of document.querySelectorAll('[data-cellunit]')) {
      b.setAttribute('aria-pressed', String(b.dataset.cellunit === state.cellUnit));
    }
    applyCellsView();
    const ctxBtn = document.querySelector('[data-series]');
    if (ctxBtn) ctxBtn.setAttribute('aria-pressed', String(state.showContext));

    toggleGroup('[data-cellscale]', 'cellscale', (v) => { state.cellScale = v; setPref('cellScale', v); });
    toggleGroup('[data-window]', 'window', (v) => { state.windowS = Number(v); setPref('window', v); });
    toggleGroup('[data-chgwindow]', 'chgwindow', (v) => { state.chgWindowS = Number(v); setPref('chgWindow', v); });
    toggleGroup('[data-sort]', 'sort', (v) => { state.tableSort = v; setPref('sort', v); });
    toggleGroup('[data-cellsview]', 'cellsview', (v) => {
      state.cellsView = v; setPref('cellsView', v); applyCellsView();
    });
    toggleGroup('[data-cellunit]', 'cellunit', (v) => {
      state.cellUnit = v; setPref('cellUnit', v);
    });

    /* Click a tile to pin that cell on the time series (same pin as the
     * cell view). */
    $('cellGrid').addEventListener('click', function (e) {
      const btn = e.target.closest ? e.target.closest('.cg-cell') : null;
      if (!btn || btn.classList.contains('cg-nodata')) return;
      const key = btn.dataset.key;
      state.pinned = (state.pinned === key) ? null : key;
      scheduleRender();
    });

    if (ctxBtn) {
      ctxBtn.addEventListener('click', function () {
        state.showContext = !state.showContext;
        this.setAttribute('aria-pressed', String(state.showContext));
        setPref('context', state.showContext ? '1' : '0');
        scheduleRender();
      });
    }

    $('balToggle').addEventListener('click', function () {
      sendBalance(this.getAttribute('aria-pressed') !== 'true');
    });

    $('btnPause').addEventListener('click', function () {
      state.paused = !state.paused;
      this.setAttribute('aria-pressed', String(state.paused));
      setText(this, state.paused ? 'Resume' : 'Pause');
      /* Pause only freezes the display; the server keeps reading and
       * logging. */
      if (!state.paused) scheduleRender();
    });

    $('btnSettings').addEventListener('click', function () {
      state.settingsOpen = !state.settingsOpen;
      this.setAttribute('aria-pressed', String(state.settingsOpen));
      $('settingsPanel').hidden = !state.settingsOpen;
      if (state.settingsOpen) fillSettings();
    });

    /* --- connection detail popover --- */
    const pill = $('statePill');
    const panel = $('sourcePanel');
    const setPanel = (open) => {
      panel.hidden = !open;
      pill.setAttribute('aria-expanded', String(open));
    };
    pill.addEventListener('click', function (e) {
      e.stopPropagation();
      setPanel(panel.hidden);
    });
    document.addEventListener('click', function (e) {
      if (!panel.hidden && !panel.contains(e.target)) setPanel(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !panel.hidden) setPanel(false);
    });

    /* --- CSV export ---
     * Downloads the server's ring buffer in the same format as --csv, so
     * the file can be replayed with --source replay. Built on the server
     * because the browser only keeps MAX_CLIENT_PACKS cycles, and the
     * Python writer is the one with round-trip tests. */
    $('btnCsv').addEventListener('click', function () {
      const btn = this;
      const original = 'Export CSV';
      setText(btn, 'Preparing…');
      btn.disabled = true;
      fetch('/api/export.csv', { cache: 'no-store' })
        .then((r) => {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.blob();
        })
        .then((blob) => {
          if (blob.size === 0) throw new Error('no samples recorded yet');
          const stamp = new Date().toISOString()
            .replace(/[-:]/g, '').replace(/\..+$/, '').replace('T', '-');
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = 'bms-monitor-' + stamp + '.csv';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(() => URL.revokeObjectURL(a.href), 2000);
          setText(btn, 'Saved');
        })
        .catch((err) => {
          /* Show the failure on the button and log the reason to the
           * console tab. */
          setText(btn, 'Export failed');
          pushConsole('[export] ' + (err && err.message ? err.message : err), 'bad');
          if (state.tab === 'console') scheduleRender();
        })
        .then(() => {
          btn.disabled = false;
          setTimeout(() => setText(btn, original), 2500);
        });
    });

    for (const [id, key, , set] of SETTING_FIELDS) {
      const el = $(id);
      if (!el) continue;
      el.addEventListener('change', function () {
        const v = parseFloat(this.value);
        if (!isFinite(v)) { fillSettings(); return; }
        set(state.config, v);
        setPref(key, v);
        scheduleRender();
      });
    }
    $('btnResetSettings').addEventListener('click', function () {
      for (const [, key] of SETTING_FIELDS) {
        try { window.localStorage.removeItem('bms.' + key); } catch (e) { /* ignore */ }
      }
      applyConfig(state.configDefaults);
      scheduleRender();
    });

    $('btnUnpin').addEventListener('click', function () {
      state.pinned = null;
      scheduleRender();
    });

    const ts = $('tsCanvas');
    ts.addEventListener('mousemove', function (e) {
      state.hoverX = e.clientX - ts.getBoundingClientRect().left;
      scheduleRender();
    });
    ts.addEventListener('mouseleave', function () { state.hoverX = null; scheduleRender(); });

    /* Drill-down buttons. Delegated since the bar gets rebuilt when the
     * chain shape changes. */
    $('drillBar').addEventListener('click', function (e) {
      const btn = e.target.closest ? e.target.closest('[data-focus]') : null;
      if (!btn) return;
      setFocus(btn.dataset.focus === '' ? null : Number(btn.dataset.focus));
    });

    /* Cell view click:
     *   - within 24 px of a dot, pin that cell (dots are small, so the
     *     radius is generous);
     *   - otherwise, at pack level, a click inside a segment panel drills
     *     into that board. */
    $('cellCanvas').addEventListener('click', function (e) {
      const rect = this.getBoundingClientRect();
      const x = e.clientX - rect.left, y = e.clientY - rect.top;
      let best = null, bestDist = 24;
      for (const hit of state.cellHits) {
        const d = Math.hypot(hit.x - x, hit.y - y);
        if (d < bestDist) { bestDist = d; best = hit.key; }
      }
      if (best !== null) {
        state.pinned = (state.pinned === best) ? null : best;
        scheduleRender();
        return;
      }
      for (const box of state.cellPanels) {
        if (box.segment === null) continue;
        if (x >= box.x0 && x <= box.x1 && y >= box.y0 && y <= box.y1) {
          setFocus(box.segment);
          return;
        }
      }
      scheduleRender();
    });

    /* --- replay --- */
    $('btnPlay').addEventListener('click', function () {
      state.playing = !state.playing;
      lastPlayTick = 0;
      if (state.playing && state.cursorT !== null && state.cursorT >= timeRange()[1]) {
        state.cursorT = timeRange()[0];
      }
      scheduleRender();
    });
    $('btnLive').addEventListener('click', function () {
      state.cursorT = timeRange()[1];
      state.playing = false;
      scheduleRender();
    });
    $('scrub').addEventListener('input', function () {
      const [t0, t1] = timeRange();
      state.cursorT = t0 + (Number(this.value) / 1000) * (t1 - t0);
      state.playing = false;
      scheduleRender();
    });
    $('replaySpeed').addEventListener('change', function () {
      state.speed = Number(this.value) || 1;
    });

    /* --- charge session --- */
    $('btnSession').addEventListener('click', function () {
      if (state.session) {
        state.lastSession = state.session;
        state.session = null;
      } else {
        const latest = spreadSeries().filter((p) => p.ok).slice(-1)[0];
        const now = latest ? latest.t : cursor();
        state.session = {
          startT: now, startSpread: latest ? latest.v : 0, endSpread: latest ? latest.v : 0,
          minSpread: latest ? latest.v : 0, maxSpread: latest ? latest.v : 0,
          duration: 0, samples: []
        };
      }
      scheduleRender();
    });
    $('btnSessionExport').addEventListener('click', function () {
      const s = state.session || state.lastSession;
      if (!s || !s.samples.length) return;
      const lines = ['t_s,spread_mv'];
      for (const p of s.samples) lines.push(p.t.toFixed(3) + ',' + p.v.toFixed(3));
      const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/csv' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'bms-session.csv';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });

    window.addEventListener('resize', scheduleRender);

    /* Layout changes that aren't window resizes (tab switch, scrollbar
     * appearing, settings panel opening) leave a canvas stretched until
     * it's redrawn. ResizeObserver redraws right away instead of waiting
     * for the next sample. */
    if (typeof ResizeObserver === 'function') {
      const observer = new ResizeObserver(function () { scheduleRender(); });
      for (const wrap of document.querySelectorAll('.chartwrap')) observer.observe(wrap);
    }
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) scheduleRender();
    });
  }

  /* Repaint once a second even with no traffic, so uptime and STALE still
   * update when the board goes quiet. */
  setInterval(function () { if (!state.paused) scheduleRender(); }, 1000);
  /* Replay playback clock, independent of data arrival. */
  setInterval(stepReplay, 60);

  wire();
  render();
  loadSnapshot().then(connect);
})();
