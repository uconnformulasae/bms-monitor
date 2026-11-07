"""CSV logging, and the reader that feeds ``--source replay``.

Rows with a bad PEC are kept (``pec_ok=0``, blank voltages) and ICs missing
from a partial cycle are written blank, so dropouts show up in the log. The
header defines the column layout, and :class:`CsvReader` uses it to rebuild
``PackSample`` objects.
"""

from __future__ import annotations

import csv
import io
import os
import re
import threading
import time
from typing import List, Optional

from .model import CELL_COUNT, PackSample, Sample

FLUSH_INTERVAL_S = 1.0

#: Bump if the column layout changes incompatibly. Written as a comment
#: line so a reader can refuse a file it doesn't understand.
SCHEMA = "bms-monitor-csv/2"

#: Matches a per-IC column such as ``ic2_c7``, but not ``ics_expected`` /
#: ``ics_present``.
_IC_COLUMN = re.compile(r"^ic(\d+)_")
#: Matches ``ic1_c7`` but not ``ic1_ccount``.
_IC_CELL = re.compile(r"^ic1_c(\d+)$")

_FIXED = [
    "t_wall", "t_mono", "seq",
    "ics_expected", "ics_present", "cells_per_ic",
    "complete", "partial", "stale", "pec_ok",
    "pack_v", "min_v", "min_ic", "min_cell",
    "max_v", "max_ic", "max_cell", "spread_mv",
]


def _ic_columns(ic: int, cells_per_ic: int) -> List[str]:
    return (["ic%d_pec_ok" % ic, "ic%d_present" % ic,
             "ic%d_ccount" % ic, "ic%d_jump" % ic]
            + ["ic%d_c%d" % (ic, c) for c in range(1, cells_per_ic + 1)])


def header_for(expected_ics: int, cells_per_ic: int) -> List[str]:
    columns = list(_FIXED)
    for ic in range(1, expected_ics + 1):
        columns += _ic_columns(ic, cells_per_ic)
    return columns


class CsvLogger:
    """Append-only CSV writer, flushed about once a second and on close.

    The header is written from the first sample, since its shape depends on
    the chain config. Appending to an existing file with a different header
    raises instead of mixing layouts.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self.rows = 0
        self._header: Optional[List[str]] = None
        self._existing_header: Optional[List[str]] = None

        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            self._existing_header = self._read_existing_header()

        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)

    def _read_existing_header(self) -> Optional[List[str]]:
        try:
            with open(self.path, newline="", encoding="utf-8") as handle:
                for row in csv.reader(handle):
                    if not row:
                        continue
                    if row[0].startswith("#"):
                        continue
                    return row
        except OSError:
            return None
        return None

    def _ensure_header(self, pack: PackSample) -> bool:
        if self._header is not None:
            return True
        wanted = header_for(pack.expected_ics, pack.cells_per_ic)
        if self._existing_header is not None:
            if self._existing_header != wanted:
                raise ValueError(
                    "%s was written with a different chain configuration\n"
                    "  existing: %d columns\n  this session: %d columns "
                    "(%d ICs x %d cells)\n"
                    "  use a different --csv path rather than mixing shapes"
                    % (self.path, len(self._existing_header), len(wanted),
                       pack.expected_ics, pack.cells_per_ic)
                )
            self._header = wanted
            return True
        self._writer.writerow(["# %s ics=%d cells_per_ic=%d"
                               % (SCHEMA, pack.expected_ics, pack.cells_per_ic)])
        self._writer.writerow(wanted)
        self._fh.flush()
        self._header = wanted
        return True

    def write(self, pack: PackSample) -> None:
        with self._lock:
            if self._fh.closed:
                return
            try:
                self._ensure_header(pack)
            except ValueError:
                raise
            except Exception:                        # pragma: no cover
                return
            try:
                self._writer.writerow(self._row(pack))
                self.rows += 1
                now = time.monotonic()
                if now - self._last_flush >= FLUSH_INTERVAL_S:
                    self._fh.flush()
                    self._last_flush = now
            except (ValueError, OSError):
                # Disk full, file removed, etc. Logging must never take the
                # monitor down.
                pass

    @staticmethod
    def _row(pack: PackSample) -> list:
        def num(value, fmt="%.6f"):
            return "" if value is None else fmt % value

        lo, hi = pack.min_cell, pack.max_cell
        row = [
            "%.3f" % pack.t_wall,
            "%.4f" % pack.t_mono,
            pack.seq,
            pack.expected_ics,
            len(pack.readings),
            pack.cells_per_ic,
            1 if pack.complete else 0,
            1 if pack.partial else 0,
            1 if pack.stale else 0,
            1 if pack.pec_ok else 0,
            num(pack.pack_v),
            num(lo[2]) if lo else "", lo[0] if lo else "", lo[1] if lo else "",
            num(hi[2]) if hi else "", hi[0] if hi else "", hi[1] if hi else "",
            num(pack.spread_mv, "%.3f"),
        ]
        by_ic = {r.ic: r for r in pack.readings}
        for ic in range(1, pack.expected_ics + 1):
            reading = by_ic.get(ic)
            if reading is None:
                # IC didn't answer this cycle: present=0, the rest blank.
                row += ["", 0, "", 0] + [""] * pack.cells_per_ic
                continue
            cells = pack.masked(reading)
            row += [
                1 if reading.pec_ok else 0,
                1,
                "" if reading.ccount is None else reading.ccount,
                1 if reading.ccount_jump else 0,
            ]
            # Failed PEC: keep the row but blank the values.
            row += (["%.6f" % v for v in cells] if reading.pec_ok
                    else [""] * pack.cells_per_ic)
        return row

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()


TEMPS_SCHEMA = "bms-monitor-temps-csv/1"


def temps_path_for(sample_csv: str) -> str:
    """``run1.csv`` -> ``run1_temps.csv``, next to the sample log."""
    root, ext = os.path.splitext(sample_csv)
    return root + "_temps" + (ext if ext.lower() == ".csv" else ".csv")


class TempCsvLogger:
    """Controller thermistor readings, one row per complete update.

    Kept separate from the sample CSV so the replay format doesn't change.
    The file is created on the first update and its columns
    (``rt<n>_ic<i>_c``) are fixed from that update. A probe with no reading
    is a blank cell, not 0.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._fh = None
        self._writer = None
        self._columns: Optional[List[str]] = None
        self._closed = False
        self._last_flush = time.monotonic()
        self.rows = 0

    @staticmethod
    def _column(sensor: dict) -> str:
        return "rt%d_ic%d_c" % (sensor["rt"], sensor["ic"])

    def _open(self, sensors: list) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fresh = not (os.path.exists(self.path) and os.path.getsize(self.path) > 0)
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._columns = [self._column(s) for s in sensors]
        if fresh:
            self._writer.writerow(["# %s" % TEMPS_SCHEMA])
            self._writer.writerow(["t_wall", "t_mono"] + self._columns)

    def write(self, payload: dict) -> None:
        if not payload.get("complete"):
            return
        sensors = payload.get("sensors") or []
        with self._lock:
            if self._closed or not sensors:
                return
            try:
                if self._fh is None:
                    self._open(sensors)
                by_column = {self._column(s): s.get("celsius") for s in sensors}
                values = [by_column.get(c) for c in self._columns]
                t_mono, t_wall = payload.get("t"), payload.get("t_wall")
                self._writer.writerow(
                    ["" if t_wall is None else "%.3f" % t_wall,
                     "" if t_mono is None else "%.3f" % t_mono]
                    + ["" if v is None else "%.1f" % v for v in values])
                self.rows += 1
                now = time.monotonic()
                if now - self._last_flush >= FLUSH_INTERVAL_S:
                    self._fh.flush()
                    self._last_flush = now
            except (ValueError, OSError, KeyError, TypeError):
                # Logging is secondary; it must never take the monitor down.
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._fh is not None and not self._fh.closed:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()


def render_csv(packs) -> str:
    """Render samples as one CSV string, for the UI's Export button.

    Reuses :func:`header_for` and :meth:`CsvLogger._row` so an export has the
    same format as a ``--csv`` log and can be replayed. If the chain shape
    changed mid-run, only rows matching the newest shape are kept. Returns
    ``""`` if there's nothing to export.
    """
    packs = [p for p in packs if p is not None]
    if not packs:
        return ""

    newest = packs[-1]
    ics, cells = newest.expected_ics, newest.cells_per_ic
    columns = header_for(ics, cells)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["# %s ics=%d cells_per_ic=%d" % (SCHEMA, ics, cells)])
    writer.writerow(columns)
    for pack in packs:
        if pack.expected_ics != ics or pack.cells_per_ic != cells:
            continue
        writer.writerow(CsvLogger._row(pack))
    return buf.getvalue()


class CsvReader:
    """Rebuild :class:`PackSample` objects from a log written by
    :class:`CsvLogger`.

    Strict about the header, forgiving about rows: damaged rows (e.g. a
    short last row from a killed session) are counted and skipped.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.expected_ics = 1
        self.cells_per_ic = CELL_COUNT
        self.bad_rows = 0
        self.schema_comment = ""

    def load(self) -> List[PackSample]:
        if not os.path.exists(self.path):
            raise FileNotFoundError("no such CSV: %s" % self.path)
        with open(self.path, newline="", encoding="utf-8", errors="replace") as handle:
            rows = list(csv.reader(handle))

        header = None
        body = []
        for row in rows:
            if not row:
                continue
            if row[0].startswith("#"):
                self.schema_comment = row[0].lstrip("# ").strip()
                continue
            if header is None:
                header = row
                continue
            body.append(row)

        if header is None:
            raise ValueError("%s has no header row" % self.path)
        if header[:len(_FIXED)] != _FIXED:
            raise ValueError(
                "%s does not look like a bms-monitor CSV (expected a %r column)\n"
                "  got: %s" % (self.path, _FIXED[0], ",".join(header[:6]))
            )

        index = {name: i for i, name in enumerate(header)}
        # Get the chain shape from the header. The regex is anchored because
        # "ics_expected" and "ics_present" also start with "ic".
        ic_numbers = sorted({
            int(m.group(1)) for m in (_IC_COLUMN.match(name) for name in header) if m
        })
        self.expected_ics = max(ic_numbers) if ic_numbers else 1
        # Anchored again: "ic1_ccount" also starts with "ic1_c".
        self.cells_per_ic = sum(
            1 for name in header if _IC_CELL.match(name)
        ) or CELL_COUNT

        out = []
        for row in body:
            pack = self._parse_row(row, index)
            if pack is None:
                self.bad_rows += 1
                continue
            out.append(pack)
        # Replay assumes monotonic time; a concatenated log might not be.
        out.sort(key=lambda p: p.t_mono)
        return out

    def _parse_row(self, row, index) -> Optional[PackSample]:
        def cell(name):
            i = index.get(name)
            if i is None or i >= len(row):
                return ""
            return row[i].strip()

        def as_float(name):
            text = cell(name)
            try:
                return float(text) if text else None
            except ValueError:
                return None

        def as_int(name, default=None):
            text = cell(name)
            try:
                return int(float(text)) if text else default
            except ValueError:
                return default

        # A session killed mid-flush can leave a short final row; skip it.
        if len(row) < len(_FIXED):
            return None
        t_mono = as_float("t_mono")
        if t_mono is None:
            return None
        t_wall = as_float("t_wall") or 0.0
        seq = as_int("seq", 0)
        expected = as_int("ics_expected", self.expected_ics) or self.expected_ics
        cells_per_ic = as_int("cells_per_ic", self.cells_per_ic) or self.cells_per_ic
        stale = bool(as_int("stale", 0))

        readings = []
        for ic in range(1, expected + 1):
            present = as_int("ic%d_present" % ic, 0)
            if not present:
                continue
            pec_ok = bool(as_int("ic%d_pec_ok" % ic, 0))
            ccount = as_int("ic%d_ccount" % ic, None)
            jump = bool(as_int("ic%d_jump" % ic, 0))
            cells = []
            for c in range(1, cells_per_ic + 1):
                value = as_float("ic%d_c%d" % (ic, c))
                cells.append(value)
            if pec_ok and any(v is None for v in cells):
                # PEC ok but values missing: treat the row as damaged.
                return None
            if not pec_ok:
                # Bad-PEC values are blank in the file. The placeholder is
                # never plotted because the reading isn't usable.
                cells = [0.0] * cells_per_ic
            # Sample carries a full 16-wide tuple; pad the masked part.
            padded = tuple(cells) + (0.0,) * (CELL_COUNT - len(cells))
            readings.append(Sample(
                seq=seq, t_mono=t_mono, t_wall=t_wall, ic=ic,
                cells=padded[:CELL_COUNT], ccount=ccount,
                pec_ok=pec_ok, stale=False, ccount_jump=jump,
            ))

        return PackSample(
            seq=seq, t_mono=t_mono, t_wall=t_wall,
            readings=tuple(readings), expected_ics=expected,
            cells_per_ic=cells_per_ic, stale=stale,
        )
