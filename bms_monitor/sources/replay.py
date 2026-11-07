"""``--source replay``: load a recorded CSV and review it.

The whole file is pushed into the Hub at start and play/pause/seek happen
in the browser, so the server stays GET-only.
"""

from __future__ import annotations

import os
import threading
import time

from ..logging_csv import CsvReader
from .base import LINK_DOWN, LINK_UP, TelemetrySource


class ReplaySource(TelemetrySource):
    """Replays a bms-monitor CSV into the Hub.

    ``load_all=True`` (default) publishes every row at once so the timeline
    is complete when the page opens. ``load_all=False`` streams at
    wall-clock speed, which is handy for demos.
    """

    name = "replay"

    def __init__(self, path: str, load_all: bool = True, speed: float = 1.0,
                 limit: int = 0):
        super().__init__()
        self.path = os.path.abspath(path)
        self._reader = CsvReader(self.path)
        self._load_all = bool(load_all)
        self._speed = max(0.01, float(speed))
        self._limit = max(0, int(limit))
        self._packs = []
        self._published = 0
        self._stop_event = threading.Event()
        self._thread = None
        self._error = ""

        # Load here so a bad file is a startup error, not an empty UI.
        packs = self._reader.load()
        if not packs:
            raise ValueError(
                "%s contains no usable rows (%d rows rejected)"
                % (self.path, self._reader.bad_rows)
            )
        if self._limit and len(packs) > self._limit:
            # Keep the end of the session; that's usually the interesting
            # part.
            self._error = ("file has %d cycles; showing the last %d "
                           "(raise --history to see more)"
                           % (len(packs), self._limit))
            packs = packs[-self._limit:]
        self._packs = packs

    # -- description -------------------------------------------------------

    @property
    def duration_s(self) -> float:
        if not self._packs:
            return 0.0
        return self._packs[-1].t_mono - self._packs[0].t_mono

    def status(self) -> dict:
        base = super().status()
        first = self._packs[0] if self._packs else None
        base.update({
            "mode": "replay",
            "file": self.path,
            "cycles": len(self._packs),
            "published": self._published,
            "duration_s": round(self.duration_s, 2),
            "bad_rows": self._reader.bad_rows,
            "schema": self._reader.schema_comment,
            "ics": first.expected_ics if first else 1,
            "cells_per_ic": first.cells_per_ic if first else 16,
            "partial_cycles": sum(1 for p in self._packs if p.partial),
            "pec_bad_cycles": sum(1 for p in self._packs if not p.pec_ok),
            "poll_plan": [],
            "stuck_suspected": False,
            "lines": len(self._packs),
            "samples": self._published,
            "unparsed": self._reader.bad_rows,
            "incomplete": sum(1 for p in self._packs if p.partial),
            "last_error": self._error,
        })
        return base

    # -- TelemetrySource ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.monotonic()
        self._stop_event.clear()
        self._set_link(LINK_UP, error=self._error)
        self._thread = threading.Thread(target=self._run, name="replay-source",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=3.0)
        self._set_link(LINK_DOWN)

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        self._emit("line", "replay: %s" % self.path)
        self._emit("line", "replay: %d cycles, %.1f s, %d IC(s), %d cells/IC"
                   % (len(self._packs), self.duration_s,
                      self._packs[0].expected_ics, self._packs[0].cells_per_ic))
        if self._reader.bad_rows:
            self._emit("line", "replay: %d row(s) rejected as damaged"
                       % self._reader.bad_rows)

        if self._load_all:
            # Push everything, then idle. One sample at a time so the Hub's
            # bookkeeping (PEC window, chain state, CSV) runs the same path
            # as live data.
            for pack in self._packs:
                if self._stop_event.is_set():
                    return
                self._emit("sample", pack)
                self._published += 1
            self._emit("line", "replay: loaded; scrub with the timeline")
            while not self._stop_event.is_set():
                self._stop_event.wait(0.5)
            return

        t0 = self._packs[0].t_mono
        started = time.monotonic()
        for pack in self._packs:
            if self._stop_event.is_set():
                return
            target = (pack.t_mono - t0) / self._speed
            delay = target - (time.monotonic() - started)
            if delay > 0:
                if self._stop_event.wait(delay):
                    return
            self._emit("sample", pack)
            self._published += 1
        while not self._stop_event.is_set():
            self._stop_event.wait(0.5)
