"""Fan-out from one data source to the SSE clients, CSV logger and snapshot.

A slow consumer must never block the reader thread, so each SSE client gets
its own bounded queue and is disconnected if that queue fills.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import Optional

from .freeze_frame import FreezeFrameManager
from .model import CELL_COUNT, PackSample
from .ringbuffer import RingBuffer
from .session_logger import SessionLogger

#: Max concurrent SSE clients.
MAX_SSE_CLIENTS = 8

#: Per-client queue depth: ~25 s of backlog at 10 Hz before the client is
#: dropped.
SSE_QUEUE_DEPTH = 256

#: How long without a usable sample before DATA goes STALE.
STALE_AFTER_S = 3.0

#: Window for the "PEC errors in last N frames" tile.
PEC_WINDOW = 100

#: Window used to judge per-IC chain health.
CHAIN_WINDOW = 40


class _Client:
    """One SSE subscriber.  Owns a bounded queue and a drop flag."""

    __slots__ = ("q", "dropped", "id")

    def __init__(self, client_id: int):
        self.id = client_id
        self.q = queue.Queue(maxsize=SSE_QUEUE_DEPTH)
        self.dropped = False

    def put(self, payload: str) -> None:
        if self.dropped:
            return
        try:
            self.q.put_nowait(payload)
        except queue.Full:
            # Never block the producer. Mark the client dropped; its reader
            # loop closes the connection.
            self.dropped = True


class Hub:
    """Bounded history + live fan-out.

    ``history`` is in samples. 18000 samples is roughly 20 MB worst case and
    covers 30 minutes at 10 Hz.
    """

    def __init__(self, history: int = 18000, raw_lines: int = 300,
                 events: int = 400, csv_logger=None, aux_history: int = 4000,
                 ui_config: Optional[dict] = None, temp_csv_logger=None,
                 temp_history: int = 3600):
        self.samples = RingBuffer(history)
        self.raw_lines = RingBuffer(raw_lines)
        self.events = RingBuffer(events)
        self.aux = RingBuffer(aux_history)
        #: One entry per complete thermistor update from the controller
        #: (2 Hz on the F105): 3600 is 30 minutes, matching ``samples``.
        self.can_temps_history = RingBuffer(temp_history)
        self._csv = csv_logger
        self._temp_csv = temp_csv_logger
        self._session_logger = SessionLogger()
        self._freeze_frame_mgr = FreezeFrameManager()
        # Display-only config sent to the browser on page load. Never sent
        # to the BMS.
        self._ui_config = dict(ui_config or {})

        self._lock = threading.Lock()
        self._clients = {}
        self._next_client_id = 1

        self._t0 = time.monotonic()
        self._last_sample_t: Optional[float] = None
        self._last_usable_t: Optional[float] = None
        self._rate_times = deque(maxlen=64)
        self._pec_window = deque(maxlen=PEC_WINDOW)
        self._latest: Optional[PackSample] = None
        self._latest_aux: dict = {}
        self._summary: dict = {}
        self._can_temps: dict = {}
        #: Recent packs, used for per-IC health in chain_state().
        self._chain_window = deque(maxlen=CHAIN_WINDOW)
        self._status_fields: dict = {}
        self._status_polled = False
        self._link = {"link": "down", "error": ""}
        self._source_status = {}

        self.total_samples = 0
        self.total_pec_errors = 0
        self.total_ccount_jumps = 0
        self.total_partial_cycles = 0
        self.total_aux = 0

    # -- source plumbing ---------------------------------------------------

    def attach(self, source) -> None:
        """Subscribe to a :class:`TelemetrySource`. The hub only receives
        from the source and has no way to send anything back."""
        self._source = source
        source.subscribe(self._on_source_event)

    def _on_source_event(self, kind: str, payload) -> None:
        # Runs on the reader thread.  Must be fast and must never raise.
        try:
            if kind == "sample":
                self._on_sample(payload)
            elif kind == "can_frame":
                self._session_logger.log_can_frame(payload)
            elif kind == "line":
                self.raw_lines.append(payload)
                self._broadcast("line", {"text": payload})
            elif kind == "aux":
                self._on_aux(payload)
            elif kind == "temps":
                self._session_logger.log_temps(payload)
                # Already in Celsius from the controller over CAN, so stored
                # as-is with no NTC conversion.
                with self._lock:
                    self._can_temps = dict(payload)
                if payload.get("complete"):
                    self.can_temps_history.append({
                        "t": payload.get("t"),
                        "celsius": [s.get("celsius") for s in payload.get("sensors", [])],
                    })
                    if self._temp_csv is not None:
                        try:
                            self._temp_csv.write(payload)
                        except Exception as exc:
                            self.events.append({"kind": "csv_error", "text": "Temperature CSV logger error: %s" % exc})
                            self._temp_csv = None
                self._broadcast("temps", payload)
            elif kind == "summary":
                # Pack-level values from the controller over CAN, stored
                # as-is.
                with self._lock:
                    self._summary = dict(payload)
                faults = payload.get("faults")
                if faults:
                    names = ", ".join(str(f) for f in faults)
                    self.capture_freeze_frame(f"CAN_FAULT: {names}", source="can")
                self._broadcast("summary", payload)
            elif kind == "status":
                with self._lock:
                    self._status_polled = True
                    self._status_fields = dict(payload)
                self._broadcast("status", payload)
            elif kind == "event":
                item = {"kind": payload.kind, "text": payload.text}
                self.events.append(item)
                self._session_logger.log_event(payload.kind, payload.text)
                if payload.kind in ("firmware_error", "parser_error"):
                    self.capture_freeze_frame(f"SYSTEM_ERROR: {payload.kind} - {payload.text}", source="event")
                if payload.kind in ("incomplete", "unparsed", "registers_cleared",
                                    "firmware_error", "parser_error"):
                    self._broadcast("event", item)
            elif kind == "link":
                with self._lock:
                    self._link = dict(payload)
                self._broadcast("link", payload)
        except Exception:                            # pragma: no cover
            pass

    def _on_sample(self, pack: PackSample) -> None:
        now = time.monotonic()
        with self._lock:
            self.total_samples += 1
            self._latest = pack
            self._last_sample_t = now
            self._rate_times.append(now)
            self._pec_window.append(0 if pack.pec_ok else 1)
            if not pack.pec_ok:
                self.total_pec_errors += 1
            if pack.partial:
                self.total_partial_cycles += 1
            jumps = sum(1 for r in pack.readings if r.ccount_jump)
            self.total_ccount_jumps += jumps
            self._chain_window.append(pack)
            if pack.usable:
                self._last_usable_t = now
        self.samples.append(pack)
        self._session_logger.log_sample(pack)
        if pack.usable and self._ui_config:
            uv = self._ui_config.get("uv")
            ov = self._ui_config.get("ov")
            cpi = pack.cells_per_ic
            breached = False
            for r in pack.readings:
                cells_to_check = r.cells[:cpi] if (cpi and cpi > 0) else r.cells
                for idx, v in enumerate(cells_to_check):
                    if v is not None:
                        if uv is not None and v < uv:
                            self.capture_freeze_frame(f"UNDER_VOLTAGE: IC{r.ic} C{idx + 1} ({v:.4f}V < {uv}V)", source="threshold")
                            breached = True
                            break
                        if ov is not None and v > ov:
                            self.capture_freeze_frame(f"OVER_VOLTAGE: IC{r.ic} C{idx + 1} ({v:.4f}V > {ov}V)", source="threshold")
                            breached = True
                            break
                if breached:
                    break
        if self._csv is not None:
            try:
                self._csv.write(pack)
            except Exception as exc:
                self.events.append({"kind": "csv_error", "text": "CSV logger error: %s" % exc})
                self._csv = None
        self._broadcast("sample", pack.to_json())

    def _on_aux(self, reading) -> None:
        with self._lock:
            self._latest_aux[reading.ic] = reading
            self.total_aux += 1
        self.aux.append(reading)
        self._broadcast("aux", reading.to_json())

    # -- SSE fan-out -------------------------------------------------------

    def add_client(self) -> Optional[_Client]:
        """Register an SSE client, or return None when at capacity."""
        with self._lock:
            if len(self._clients) >= MAX_SSE_CLIENTS:
                return None
            client = _Client(self._next_client_id)
            self._next_client_id += 1
            self._clients[client.id] = client
            return client

    def remove_client(self, client: _Client) -> None:
        with self._lock:
            self._clients.pop(client.id, None)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _broadcast(self, event: str, data: dict) -> None:
        with self._lock:
            clients = list(self._clients.values())
        if not clients:
            return
        payload = "event: %s\ndata: %s\n\n" % (event, json.dumps(data, separators=(",", ":")))
        for client in clients:
            client.put(payload)

    def heartbeat(self) -> None:
        """Push a keep-alive plus fresh stats, so the Hz / uptime / link
        tiles keep updating even when data has stopped."""
        self._broadcast("stats", self.stats())

    # -- derived state -----------------------------------------------------

    def data_state(self) -> str:
        """Return "live", "stale" or "no-data"."""
        with self._lock:
            last = self._last_usable_t
        if last is None:
            return "no-data"
        return "live" if (time.monotonic() - last) < STALE_AFTER_S else "stale"

    def rate_hz(self) -> float:
        with self._lock:
            times = list(self._rate_times)
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        if span <= 0:
            return 0.0
        # Report 0 if nothing has arrived recently.
        if time.monotonic() - times[-1] > STALE_AFTER_S:
            return 0.0
        return (len(times) - 1) / span

    def chain_state(self) -> list:
        """Per-IC health over the recent window.

        ``ok``: answering with good PECs. ``pec``: answering but failing PEC.
        ``intermittent``: missing from some cycles. ``silent``: missing from
        all of them. ``unknown``: no data yet.
        """
        with self._lock:
            window = list(self._chain_window)
            latest = self._latest
        # Factory serial IDs, if command 22 has been polled. Useful for
        # confirming chain order and spotting a swapped segment board.
        sids = {}
        source = getattr(self, "_source", None)
        if source is not None:
            try:
                sids = source.status().get("sids") or {}
            except Exception:
                sids = {}
        expected = latest.expected_ics if latest is not None else 0
        out = []
        for ic in range(1, expected + 1):
            seen = 0
            good = 0
            ccount = None
            jumps = 0
            last = None                 # newest reading of this IC in the window
            for pack in window:
                for reading in pack.readings:
                    if reading.ic != ic:
                        continue
                    seen += 1
                    if reading.pec_ok:
                        good += 1
                    ccount = reading.ccount
                    if reading.ccount_jump:
                        jumps += 1
                    last = reading
            total = len(window)
            if total == 0:
                state = "unknown"
            elif seen == 0:
                state = "silent"
            elif seen < total:
                state = "intermittent"
            elif good < seen:
                state = "pec"
            else:
                state = "ok"
            out.append({
                "ic": ic,
                "state": state,
                "seen": seen,
                "of": total,
                "good": good,
                "ccount": ccount,
                "jumps": jumps,
                "sid": sids.get(str(ic)) or sids.get(ic),
                # Per-chip diagnostics from the controller (CAN 0x72n), newest
                # reading. None means not reported, which is not the same as
                # "no flags".
                "flags": (list(last.flags) if last is not None and last.flags is not None
                          else None),
                "die_temp_c": last.die_temp_c if last is not None else None,
                "invalid_run": last.invalid_run if last is not None else None,
                "status_ok": last.status_ok if last is not None else None,
                "balance_mask": last.balance_mask if last is not None else None,
            })
        return out

    def stats(self) -> dict:
        with self._lock:
            latest = self._latest
            pec_recent = sum(self._pec_window)
            pec_window_n = len(self._pec_window)
            total = self.total_samples
            jumps = self.total_ccount_jumps
            total_pec = self.total_pec_errors
            link = dict(self._link)
            status_polled = self._status_polled
            status_fields = dict(self._status_fields)
            partial = self.total_partial_cycles
            aux_latest = {ic: r.to_json() for ic, r in self._latest_aux.items()}
            summary = dict(self._summary)
            can_temps = dict(self._can_temps)
        source_status = {}
        source = getattr(self, "_source", None)
        if source is not None:
            try:
                source_status = source.status()
            except Exception:                        # pragma: no cover
                source_status = {}
        out = {
            "uptime_s": round(time.monotonic() - self._t0, 1),
            "data_state": self.data_state(),
            "rate_hz": round(self.rate_hz(), 2),
            "total_samples": total,
            "pec_recent": pec_recent,
            "pec_window": pec_window_n,
            "pec_total": total_pec,
            "ccount_jumps": jumps,
            "partial_cycles": partial,
            "chain": self.chain_state(),
            "aux": aux_latest,
            "summary": summary,
            "can_temps": can_temps,
            "clients": self.client_count,
            "dropped_samples": self.samples.dropped,
            "link": link,
            "status_polled": status_polled,
            "status_fields": status_fields,
            "source": source_status,
            "csv_path": self._csv.path if self._csv else None,
            "csv_rows": self._csv.rows if self._csv else 0,
            "temps_csv_path": self._temp_csv.path if self._temp_csv else None,
            "temps_csv_rows": self._temp_csv.rows if self._temp_csv else 0,
            "freeze_frames": self._freeze_frame_mgr.list_frames(),
        }
        if latest is not None:
            out["latest"] = latest.to_json()
        out["logging"] = self._session_logger.status()
        return out

    def snapshot(self, history: int = 1200, aux_history: int = 600,
                 temp_history: int = 1200) -> dict:
        """Everything a freshly loaded page needs in one request, so a reload
        doesn't start with empty charts."""
        with self._lock:
            latest = self._latest
        stats_val = self.stats()
        samples = self.samples.snapshot(limit=history)
        return {
            "cell_count": CELL_COUNT,
            "config": dict(self._ui_config),
            "expected_ics": latest.expected_ics if latest is not None else 1,
            "cells_per_ic": latest.cells_per_ic if latest is not None else CELL_COUNT,
            "samples": [s.to_json() for s in samples],
            "aux": [r.to_json() for r in self.aux.snapshot(limit=aux_history)],
            "can_temps_history": self.can_temps_history.snapshot(limit=temp_history),
            "raw_lines": self.raw_lines.snapshot(limit=300),
            "events": self.events.snapshot(limit=100),
            "stats": stats_val,
            "logging": self._session_logger.status(),
            "freeze_frames": self._freeze_frame_mgr.list_frames(),
            "latest_freeze_frame": self._freeze_frame_mgr.latest_frame(),
        }

    def reset(self) -> None:
        """Reset hub history and counters to initial baseline."""
        with self._lock:
            self.samples.reset()
            self.raw_lines.reset()
            self.events.reset()
            self.aux.reset()
            self.can_temps_history.reset()
            self._freeze_frame_mgr.clear()
            self._latest = None
            self._latest_aux.clear()
            self._summary.clear()
            self._can_temps.clear()
            self._chain_window.clear()
            self._pec_window.clear()
            self._rate_times.clear()
            self._status_fields.clear()
            self._status_polled = False
            self.total_samples = 0
            self.total_pec_errors = 0
            self.total_ccount_jumps = 0
            self.total_partial_cycles = 0
            self.total_aux = 0
            self._t0 = time.monotonic()
            self._last_sample_t = None
            self._last_usable_t = None

    def export_csv(self) -> str:
        """The full retained history (not just the snapshot window) as a
        replayable CSV. Returns ``""`` when nothing has been recorded."""
        from .logging_csv import render_csv
        return render_csv(self.samples.snapshot())

    def start_logging(self, name_prefix: str = "bms_can_log") -> dict:
        status = self._session_logger.start(name_prefix)
        self._broadcast("logging", status)
        return status

    def stop_logging(self) -> dict:
        status = self._session_logger.stop()
        self._broadcast("logging", status)
        return status

    def logging_status(self) -> dict:
        return self._session_logger.status()

    def get_latest_log_path(self) -> Optional[str]:
        return self._session_logger.get_latest_log_path()

    def capture_freeze_frame(self, reason: str, source: str = "fault",
                             extra: Optional[dict] = None) -> Optional[dict]:
        with self._lock:
            latest = self._latest
            summary = dict(self._summary)
            temps = dict(self._can_temps)
            status_fields = dict(self._status_fields)
        chain = self.chain_state()
        frame = self._freeze_frame_mgr.capture(
            reason=reason,
            source=source,
            pack=latest,
            summary=summary,
            temps=temps,
            chain=chain,
            status_fields=status_fields,
            extra=extra,
        )
        if frame is not None:
            self._broadcast("freeze_frame", frame)
        return frame

    def freeze_frames(self) -> list:
        return self._freeze_frame_mgr.list_frames()

    def get_freeze_frame(self, frame_id: str) -> Optional[dict]:
        return self._freeze_frame_mgr.get_frame(frame_id)

    def latest_freeze_frame(self) -> Optional[dict]:
        return self._freeze_frame_mgr.latest_frame()
