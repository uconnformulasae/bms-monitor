"""Session logger for CAN frames, cell telemetry, and events.

Records raw bus frames and high-level decoded samples to a timestamped CSV
file for post-run analysis.
"""

from __future__ import annotations

import csv
import os
import threading
import time
from typing import Dict, List, Optional


class SessionLogger:
    """Thread-safe session recorder for CAN frames and telemetry."""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = os.path.abspath(log_dir)
        self._lock = threading.Lock()
        self._active = False
        self._file = None
        self._writer = None
        self._filename = ""
        self._filepath = ""
        self._t0_wall = 0.0
        self._t0_mono = 0.0
        self._frames_count = 0
        self._samples_count = 0
        self._last_flush = 0.0

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def start(self, name_prefix: str = "bms_can_log") -> dict:
        with self._lock:
            if self._active:
                return self._status_locked()

            os.makedirs(self.log_dir, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self._filename = "%s_%s.csv" % (name_prefix, timestamp)
            self._filepath = os.path.join(self.log_dir, self._filename)

            self._file = open(self._filepath, "w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow([
                "timestamp", "t_mono", "source", "id_hex", "dlc",
                "data_hex", "kind", "details"
            ])
            self._file.flush()

            self._active = True
            t_wall = time.time()
            self._t0_wall = t_wall
            self._t0_mono = time.monotonic()
            self._frames_count = 0
            self._samples_count = 0
            self._last_flush = self._t0_mono
            return self._status_locked()

    def stop(self) -> dict:
        with self._lock:
            if not self._active:
                return self._status_locked()

            if self._file is not None:
                try:
                    self._file.flush()
                    self._file.close()
                except Exception:
                    pass
                self._file = None
                self._writer = None

            self._active = False
            return self._status_locked()

    def log_can_frame(self, frame: dict) -> None:
        with self._lock:
            if not self._active or self._writer is None:
                return
            t_wall = frame.get("t_wall", time.time())
            t_mono = frame.get("t_mono", time.monotonic() - self._t0_mono)
            id_hex = frame.get("id_hex", "")
            dlc = frame.get("dlc", "")
            data_hex = frame.get("data_hex", "")
            kind = frame.get("kind", "can")
            details = frame.get("details", "")

            self._writer.writerow([
                "%.4f" % t_wall, "%.4f" % t_mono, "CAN",
                id_hex, dlc, data_hex, kind, details
            ])
            self._frames_count += 1
            self._maybe_flush_locked()

    def log_sample(self, pack) -> None:
        with self._lock:
            if not self._active or self._writer is None:
                return
            t_wall = getattr(pack, "t_wall", time.time())
            t_mono = getattr(pack, "t_mono", 0.0)
            pack_v = getattr(pack, "pack_v", 0.0)
            min_v = getattr(pack, "min_v", 0.0)
            max_v = getattr(pack, "max_v", 0.0)
            spread_mv = getattr(pack, "spread_mv", 0.0)
            pec_ok = int(getattr(pack, "pec_ok", True))
            seq = getattr(pack, "seq", 0)

            cells_summary = ";".join(
                "%.4f" % v for v in getattr(pack, "cells", ())
            )
            details = "seq=%d pack_v=%.4f min=%.4f max=%.4f spread_mv=%.1f pec_ok=%d cells=[%s]" % (
                seq, pack_v, min_v, max_v, spread_mv, pec_ok, cells_summary
            )

            self._writer.writerow([
                "%.4f" % t_wall, "%.4f" % t_mono, "BMS",
                "", "", "", "sample", details
            ])
            self._samples_count += 1
            self._maybe_flush_locked()

    def log_temps(self, payload: dict) -> None:
        with self._lock:
            if not self._active or self._writer is None:
                return
            t_mono = payload.get("t", 0.0)
            sensors = payload.get("sensors", [])
            temps_str = ";".join(
                str(s.get("celsius")) for s in sensors if s.get("celsius") is not None
            )
            details = "sensors=[%s]" % temps_str
            t_wall = time.time()
            self._writer.writerow([
                "%.4f" % t_wall, "%.4f" % t_mono, "BMS",
                payload.get("id_hex", "0x730"), str(len(sensors)),
                "", "temps", details
            ])
            self._frames_count += 1
            self._maybe_flush_locked()

    def log_event(self, kind: str, text: str) -> None:
        with self._lock:
            if not self._active or self._writer is None:
                return
            t_wall = time.time()
            self._writer.writerow([
                "%.4f" % t_wall, "%.4f" % (time.monotonic() - self._t0_mono),
                "EVENT", "", "", "", kind, text
            ])
            self._maybe_flush_locked()

    def _maybe_flush_locked(self) -> None:
        now = time.monotonic()
        if (now - self._last_flush) >= 1.0 and self._file is not None:
            try:
                self._file.flush()
            except Exception:
                pass
            self._last_flush = now

    def _status_locked(self) -> dict:
        duration = (time.monotonic() - self._t0_mono) if self._active else 0.0
        filesize = 0
        if self._filepath and os.path.exists(self._filepath):
            try:
                filesize = os.path.getsize(self._filepath)
            except OSError:
                filesize = 0
        return {
            "active": self._active,
            "filename": self._filename,
            "filepath": self._filepath,
            "duration_s": round(duration, 1),
            "frames": self._frames_count,
            "samples": self._samples_count,
            "filesize": filesize,
        }

    def status(self) -> dict:
        with self._lock:
            return self._status_locked()

    def get_latest_log_path(self) -> Optional[str]:
        with self._lock:
            if self._filepath and os.path.exists(self._filepath):
                return self._filepath
            return None
