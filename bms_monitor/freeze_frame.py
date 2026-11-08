"""In-memory diagnostic freeze-frame buffer.

Captures a complete snapshot of all series cell voltages, temperatures,
IC health flags, and safety states at the exact moment a fault or threshold
violation is detected.
"""

from __future__ import annotations

import datetime
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional


class FreezeFrameManager:
    """Bounded, thread-safe ring buffer of diagnostic fault freeze-frames."""

    def __init__(self, capacity: int = 30, dedupe_window_s: float = 1.0):
        self._capacity = max(1, capacity)
        self._dedupe_window_s = dedupe_window_s
        self._lock = threading.Lock()
        self._frames: deque[Dict[str, Any]] = deque(maxlen=self._capacity)
        self._counter = 0
        self._last_triggers: Dict[str, float] = {}

    def capture(
        self,
        reason: str,
        source: str = "fault",
        pack: Optional[Any] = None,
        summary: Optional[Dict[str, Any]] = None,
        temps: Optional[Dict[str, Any]] = None,
        chain: Optional[List[Dict[str, Any]]] = None,
        status_fields: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        now_mono = time.monotonic()
        t_wall = time.time()

        with self._lock:
            last_t = self._last_triggers.get(reason)
            if last_t is not None and (now_mono - last_t) < self._dedupe_window_s:
                return None
            self._last_triggers[reason] = now_mono

            self._counter += 1
            frame_id = f"FF-{self._counter:04d}"

            cell_list: List[Dict[str, Any]] = []
            pack_sum = 0.0
            min_cell: Optional[Dict[str, Any]] = None
            max_cell: Optional[Dict[str, Any]] = None

            if pack is not None and hasattr(pack, "readings"):
                cpi = getattr(pack, "cells_per_ic", None)
                for r in pack.readings:
                    cells_to_use = r.cells[:cpi] if (cpi and cpi > 0) else r.cells
                    for idx, v in enumerate(cells_to_use):
                        if v is not None:
                            c_num = idx + 1
                            cell_obj = {"ic": r.ic, "cell": c_num, "v": round(float(v), 4)}
                            cell_list.append(cell_obj)
                            pack_sum += float(v)
                            if min_cell is None or float(v) < min_cell["v"]:
                                min_cell = cell_obj
                            if max_cell is None or float(v) > max_cell["v"]:
                                max_cell = cell_obj
            elif pack is not None and isinstance(pack, dict):
                cell_list = list(pack.get("cells", []))
                pack_sum = float(pack.get("sum", 0.0))
                min_cell = pack.get("min")
                max_cell = pack.get("max")

            mean_v = round(pack_sum / len(cell_list), 4) if cell_list else 0.0
            spread_mv = round((max_cell["v"] - min_cell["v"]) * 1000, 1) if (min_cell and max_cell) else 0.0

            sensor_list: List[Dict[str, Any]] = []
            t_hot: Optional[Dict[str, Any]] = None
            t_cold: Optional[Dict[str, Any]] = None
            if temps:
                raw_sensors = temps.get("sensors") or []
                for s in raw_sensors:
                    if isinstance(s, dict) and s.get("celsius") is not None:
                        c_val = round(float(s["celsius"]), 1)
                        s_obj = {
                            "channel": s.get("channel"),
                            "ic": s.get("ic"),
                            "celsius": c_val,
                        }
                        sensor_list.append(s_obj)
                        if t_hot is None or c_val > t_hot["celsius"]:
                            t_hot = s_obj
                        if t_cold is None or c_val < t_cold["celsius"]:
                            t_cold = s_obj

            iso_ts = datetime.datetime.fromtimestamp(t_wall, tz=datetime.timezone.utc).isoformat()

            frame = {
                "id": frame_id,
                "reason": reason,
                "source": source,
                "t_mono": round(now_mono, 3),
                "t_wall": round(t_wall, 3),
                "timestamp": iso_ts,
                "pack": {
                    "sum_v": round(pack_sum, 3),
                    "mean_v": mean_v,
                    "spread_mv": spread_mv,
                    "min_cell": min_cell,
                    "max_cell": max_cell,
                    "cell_count": len(cell_list),
                    "cells": cell_list,
                },
                "temps": {
                    "hottest": t_hot,
                    "coldest": t_cold,
                    "sensors": sensor_list,
                },
                "summary": dict(summary or {}),
                "chain": list(chain or []),
                "status_fields": dict(status_fields or {}),
                "extra": dict(extra or {}),
            }

            self._frames.append(frame)
            return frame

    def list_frames(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": f["id"],
                    "reason": f["reason"],
                    "source": f["source"],
                    "timestamp": f["timestamp"],
                    "t_mono": f["t_mono"],
                    "pack_v": f["pack"]["sum_v"],
                    "spread_mv": f["pack"]["spread_mv"],
                    "min_cell": f["pack"]["min_cell"],
                    "max_cell": f["pack"]["max_cell"],
                    "hottest_temp": f["temps"]["hottest"],
                }
                for f in self._frames
            ]

    def get_frame(self, frame_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for f in self._frames:
                if f["id"] == frame_id:
                    return dict(f)
            return None

    def latest_frame(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._frames[-1]) if self._frames else None

    def count(self) -> int:
        with self._lock:
            return len(self._frames)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._last_triggers.clear()
