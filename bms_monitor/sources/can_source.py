"""``--source can``: receive the F105 controller's telemetry on CAN2.

python-can is imported lazily so the serial path doesn't need it. Frame
layout is in can_frames.py. CAN1 summary frames are decoded if they show up.

The F105 (``BMS/App/Src/bms_monitor.c``) sends 0x6FF chain status first in
each cycle, then that cycle's cell frames, so each 0x6FF closes the open
round. A repeated cell frame inside a round means a 0x6FF was lost. Until
the first 0x6FF is seen, a 0x700 closes the round instead.

With 0x6FF, a chip's cells are valid unless the chain-status bitmap lists it
or a cell is 0xFFFF; pec_fail on its per-IC frame only means the status read
failed. Without 0x6FF, a chip needs a fresh per-IC frame with no pec_fail.
"""

from __future__ import annotations

import datetime
import re
import sys
import threading
import time
from typing import Optional

from ..model import CELL_COUNT, PackSample, Sample
from . import can_frames as F
from .base import (
    LINK_CONNECTING,
    LINK_DOWN,
    LINK_RECONNECTING,
    LINK_UP,
    TelemetrySource,
)

#: Data older than this is dropped instead of shown as current.
CELL_STALE_S = 2.0

#: Reconnect back-off, seconds.
BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 5.0, 5.0)

#: No frames at all for this long -> cycle the bus.
STALL_TIMEOUT_S = 15.0

#: With --allow-tx, a queued toggle goes out only once no frame has arrived
#: for this long. The controller sends a burst every 100 ms, and the
#: CANdapter corrupts the frame it is forwarding if our command reaches it at
#: the same moment. Its USB chip holds partial data for up to 16 ms, so 25 ms
#: of silence here means the burst is over on the adapter too.
TX_GAP_S = 0.025

#: On a bus that is never quiet that long, send anyway after this wait
#: (inside can_command_gate.MAX_AGE_S, so the toggle is not dropped).
TX_MAX_WAIT_S = 0.5

#: Unparsed adapter lines and unknown frames logged per run, so a busy
#: shared bus can't flood the raw console.
ODD_LOG_LIMIT = 20

DEFAULT_INTERFACE = "socketcan"
DEFAULT_CHANNEL = "can0"
DEFAULT_BITRATE = 250000

_COM_PORT = re.compile(r"COM\d+", re.I)


def normalize_slcan_channel(channel: str) -> tuple[str, Optional[str]]:
    """Map macOS-style device paths to a Windows COM name when needed.

    People often paste ``/dev/cu.usbserial-COM3`` from the docs; on Windows the
    slcan backend needs ``COM3``.
    """
    if sys.platform != "win32":
        return channel, None
    match = _COM_PORT.search(channel)
    if match:
        fixed = match.group(0).upper()
        if fixed == channel:
            return channel, None
        return fixed, "%s on Windows (not %r)" % (fixed, channel)
    if channel.startswith("/dev/"):
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports()]
        except Exception:
            ports = []
        if len(ports) == 1:
            return ports[0], "only serial port found (%s); %r is invalid on Windows" % (
                ports[0], channel)
    return channel, None


def _stamp() -> str:
    """Wall-clock time to the millisecond, for raw-console lines."""
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class CanSource(TelemetrySource):
    """CAN receiver. Read-only unless given a ``tx_control``
    (``--allow-tx``); then its reader thread also sends the frames that
    control queues, and nothing else."""

    name = "can"

    def __init__(self, channel: str = DEFAULT_CHANNEL,
                 interface: str = DEFAULT_INTERFACE,
                 bitrate: int = DEFAULT_BITRATE,
                 total_ic: int = 10, cells_per_ic: int = 9,
                 warmup: int = 2, tx_control=None):
        super().__init__()
        self._tx = tx_control
        self.channel = channel
        self.interface = interface
        self.bitrate = int(bitrate)
        self.total_ic = max(1, int(total_ic))
        self.cells_per_ic = max(1, min(int(cells_per_ic), CELL_COUNT))
        self._warmup = warmup

        self._stop_event = threading.Event()
        self._thread = None
        self._bus = None
        self._lock = threading.Lock()

        # frame index -> ([4 volts], t_mono)
        self._cells = {}
        # ic index (0-based) -> (decoded 0x72n frame, t_mono)
        self._ics = {}
        # frame index -> ([4 celsius], [4 fitted], t_mono)
        self._temps = {}
        self._seq = 0
        self._since_linkup = 0
        self._t0 = time.monotonic()
        self._last_frame_t = 0.0
        self._reconnects = 0

        self._summary_parts = {}
        # True once a 0x6FF has been seen: rounds are then delimited by it.
        self._chain_mode = False
        # The 0x6FF that opened the round currently being assembled.
        self._round_status: Optional[dict] = None
        self._odd_logged = 0
        self._frames = {"cells": 0, "ic": 0, "temperatures": 0,
                        "summary": 0, "extremes": 0, "status": 0,
                        "unknown": 0, "bad": 0}

    # -- TelemetrySource ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            import can  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "python-can is required for --source can.\n"
                "  install it with:  pip install -r requirements.txt\n"
                "  (underlying error: %s)" % exc
            ) from None

        self._started_at = time.monotonic()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="can-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        self._close_bus()
        if thread is not None:
            thread.join(timeout=3.0)
        self._set_link(LINK_DOWN)

    def status(self) -> dict:
        base = super().status()
        with self._lock:
            now = time.monotonic() - self._t0
            cell_frames = len(self._cells)
            ics_seen = len(self._ics)
            oldest = 0.0
            stamps = ([entry[1] for entry in self._cells.values()]
                      + [entry[1] for entry in self._ics.values()])
            if stamps:
                oldest = max(now - stamp for stamp in stamps)
            frames = dict(self._frames)
            parts = dict(self._summary_parts)
        base.update({
            "mode": "live",
            "transport": "CAN (python-can)",
            "interface": self.interface,
            "channel": self.channel,
            "bitrate": self.bitrate,
            "ics": self.total_ic,
            "cells_per_ic": self.cells_per_ic,
            "cell_frames": "%d/%d" % (cell_frames, self._expected_cell_frames()),
            "layout": "F105 (0x6FF rounds)" if self._chain_mode else "0x700 rounds",
            "ic_frames_seen": ics_seen,
            "oldest_frame_s": round(oldest, 2),
            "frames": frames,
            "reconnects": self._reconnects,
            "tx": self._tx.status() if self._tx is not None else None,
            "poll_plan": [],
            "stuck_suspected": False,
            "lines": sum(frames.values()),
            "samples": self._seq,
            "unparsed": frames["unknown"] + frames["bad"],
            "incomplete": 0,
            "partial_cycles": 0,
        })
        return base

    # -- bus ---------------------------------------------------------------

    def _open_bus(self, **extra):
        import can
        if self.interface == "slcan":
            # The CANdapter's command ACK would otherwise eat a frame per send.
            from .candapter import candapter_bus_class
            bus = candapter_bus_class()(channel=self.channel, bitrate=self.bitrate, **extra)
            bus.on_unparsed = self._note_unparsed
            return bus
        kwargs = {"channel": self.channel, "interface": self.interface}
        # Some python-can versions reject a bitrate on the virtual bus.
        if self.interface not in ("virtual",):
            kwargs["bitrate"] = self.bitrate
        return can.interface.Bus(**kwargs, **extra)

    def _close_bus(self) -> None:
        bus, self._bus = self._bus, None
        if bus is None:
            return
        try:
            bus.shutdown()
        except Exception:
            pass

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        import can

        attempt = 0
        while not self._stop_event.is_set():
            self._set_link(LINK_CONNECTING if attempt == 0 else LINK_RECONNECTING)
            try:
                self._bus = self._open_bus()
            except Exception as exc:
                self._set_link(LINK_RECONNECTING, error="cannot open CAN bus: %s" % exc)
                self._stop_event.wait(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                attempt += 1
                continue

            attempt = 0
            with self._lock:
                self._cells = {}
                self._ics = {}
                self._temps = {}
                self._since_linkup = 0
                self._chain_mode = False
                self._round_status = None
            self._last_frame_t = time.monotonic()
            self._set_link(LINK_UP, error="")
            self._emit("line", "can: listening on %s:%s at %d bit/s (CAN2: chain "
                       "status 0x6FF, cells 0x700-0x%03X, per-IC 0x720-0x%03X, "
                       "temps 0x730+)"
                       % (self.interface, self.channel, self.bitrate,
                          F.ID_CELLS_BASE + self._expected_cell_frames() - 1,
                          F.ID_IC_BASE + self.total_ic - 1))

            try:
                self._session(can)
            except Exception as exc:                 # pragma: no cover
                self._set_link(LINK_RECONNECTING, error=repr(exc))
            finally:
                if self._tx is not None:
                    self._tx.discard()
                self._close_bus()

            if self._stop_event.is_set():
                break
            self._reconnects += 1
            self._stop_event.wait(BACKOFF_S[0])

        self._set_link(LINK_DOWN)

    def _send_queued(self, can_module, bus) -> bool:
        """Send what the balance test control queued. False if the bus
        failed and should be reopened."""
        for can_id, data in self._tx.take():
            message = can_module.Message(arbitration_id=can_id, data=data,
                                         is_extended_id=False)
            try:
                bus.send(message, timeout=0.2)
            except Exception as exc:
                self._tx.note_error("send failed: %s" % exc)
                self._emit("line", "can: TX 0x%03X %s failed: %s" % (can_id, data.hex(" "), exc))
                return False
            self._tx.note_sent()
            self._emit("line", "can: %s TX 0x%03X %s" % (_stamp(), can_id, data.hex(" ")))
        return True

    def _tx_due(self) -> bool:
        """A toggle is queued and the bus has been quiet for TX_GAP_S, or
        it has waited TX_MAX_WAIT_S for a gap that never came."""
        if self._tx is None:
            return False
        age = self._tx.oldest_age()
        if age is None:
            return False
        quiet = (time.monotonic() - self._last_frame_t) >= TX_GAP_S
        return quiet or age >= TX_MAX_WAIT_S

    def _session(self, can_module) -> None:
        bus = self._bus
        # With transmit on, wake every TX_GAP_S so a toggle can go out in the
        # first quiet gap between the controller's bursts.
        recv_timeout = TX_GAP_S if self._tx is not None else 0.2
        while not self._stop_event.is_set():
            if self._tx_due() and not self._send_queued(can_module, bus):
                self._set_link(LINK_RECONNECTING, error="CAN send failed")
                return
            try:
                message = bus.recv(timeout=recv_timeout)
            except Exception as exc:
                self._set_link(LINK_RECONNECTING, error="CAN read failed: %s" % exc)
                return
            now = time.monotonic()
            if message is None:
                if (now - self._last_frame_t) > STALL_TIMEOUT_S:
                    self._set_link(
                        LINK_RECONNECTING,
                        error="no CAN frames for %.0fs -- cycling the bus" % STALL_TIMEOUT_S)
                    return
                continue

            self._last_frame_t = now
            if not getattr(message, "is_error_frame", False):
                self._clear_error()
            if getattr(message, "is_error_frame", False):
                with self._lock:
                    self._frames["bad"] += 1
                continue
            self._handle(message)

    def _expected_cell_frames(self) -> int:
        cells = self.total_ic * self.cells_per_ic
        return (cells + F.CELLS_PER_FRAME - 1) // F.CELLS_PER_FRAME

    def _handle(self, message) -> None:
        decoded = F.decode(message.arbitration_id, bytes(message.data))
        if decoded is None:
            with self._lock:
                key = "bad" if F.owns_id(message.arbitration_id) else "unknown"
                self._frames[key] += 1
                logged = self._odd_logged
                self._odd_logged += 1
            if logged < ODD_LOG_LIMIT:
                self._emit("line", "can: %s %s frame 0x%03X %s" % (
                    _stamp(), key, message.arbitration_id, bytes(message.data).hex(" ")))
            return

        kind = decoded["kind"]
        with self._lock:
            self._frames[kind] = self._frames.get(kind, 0) + 1
        now = time.monotonic() - self._t0

        if kind == "cells":
            self._handle_cells(decoded, now)
        elif kind == "ic":
            with self._lock:
                self._ics[decoded["ic_index"]] = (decoded, now)
        elif kind == "temperatures":
            self._handle_temps(decoded, now)
        elif kind == "balance_cmd":
            self._emit("line", "can: another node sent balance test %s on 0x%03X"
                       % ("on" if decoded["enable"] else "off", decoded["id"]))
        elif kind in ("summary", "extremes", "status"):
            if decoded["id"] == F.ID_CHAIN_STATUS:
                self._handle_chain_status(decoded)
            with self._lock:
                self._summary_parts[kind] = decoded
            self._emit("summary", self._summary_block())

    def _summary_block(self) -> dict:
        """Latest 0x6FF chain status and any CAN1 summary frames, merged
        into one dict."""
        with self._lock:
            parts = dict(self._summary_parts)
        out = {"source": "can"}
        for part in parts.values():
            out.update({k: v for k, v in part.items() if k not in ("kind", "id")})
        return out

    def _handle_chain_status(self, decoded: dict) -> None:
        """``0x6FF`` starts a controller cycle: publish the open round.

        The first ``0x6FF`` after link-up only starts a round. Cells that
        arrived before it have no chain status, so they are dropped.
        """
        with self._lock:
            had_mode = self._chain_mode
            self._chain_mode = True
            closing = self._cells
            closing_status = self._round_status
            self._cells = {}
            self._round_status = decoded
        if had_mode and closing:
            self._publish(closing, closing_status)

    def _handle_cells(self, decoded: dict, now: float) -> None:
        """Store one fixed-ID cell frame, publishing a round when it closes."""
        frame = decoded["frame"]
        closing = None
        closing_status = None
        with self._lock:
            if self._chain_mode:
                if frame in self._cells:
                    # A 0x6FF went missing: this frame opens the next cycle.
                    # Everything already held is the previous cycle.
                    closing, closing_status = self._cells, self._round_status
                    self._cells = {}
                    self._round_status = None
            elif frame == 0 and self._cells:
                # No 0x6FF seen yet: 0x700 starts the next round. Publish
                # what came before it; this frame belongs to the new round.
                closing = self._cells
                closing_status = self._summary_parts.get("status")
                self._cells = {}
            self._cells[frame] = (list(decoded["volts"]), now)
        if closing:
            self._publish(closing, closing_status)

    def _expected_temp_frames(self) -> int:
        slots = self.total_ic * F.TEMP_SLOTS_PER_IC
        return min(F.TEMP_FRAME_COUNT, (slots + F.TEMPS_PER_FRAME - 1) // F.TEMPS_PER_FRAME)

    def _handle_temps(self, decoded: dict, now: float) -> None:
        """Rebuild the thermistor list from the latest frame of each slot.

        Slots with no probe fitted are left out. A fitted probe with no
        reading, or a stale frame, stays in with ``celsius`` None so the RT
        numbers (counted across the chain in slot order, as on the board)
        don't shift.
        """
        frame = decoded["frame"]
        with self._lock:
            self._temps[frame] = (list(decoded["celsius"]), list(decoded["fitted"]), now)
            sensors = []
            rt = 0
            for frame_index in sorted(self._temps):
                values, fitted, stamp = self._temps[frame_index]
                stale = (now - stamp) > CELL_STALE_S
                for offset, celsius in enumerate(values):
                    index = frame_index * F.TEMPS_PER_FRAME + offset
                    ic = index // F.TEMP_SLOTS_PER_IC + 1
                    if not fitted[offset] or ic > self.total_ic:
                        continue
                    rt += 1
                    sensors.append({
                        "rt": rt,
                        "ic": ic,
                        "slot": index % F.TEMP_SLOTS_PER_IC + 1,
                        "celsius": None if (celsius is None or stale) else round(celsius, 1),
                        "stale": stale,
                    })
        # The controller sends every slot frame in order, so the last one
        # closes an update; history and the CSV take only those.
        complete = frame == self._expected_temp_frames() - 1
        if sensors:
            self._emit("temps", {"source": "can", "t": round(now, 4), "t_wall": time.time(),
                                 "complete": complete, "sensors": sensors})

    def _publish(self, round_cells: dict, status: Optional[dict]) -> None:
        """Publish one completed round as a PackSample.

        ``round_cells`` is exactly one cycle's cell frames and ``status``
        the chain status that cycle carried (or ``None`` if unknown).
        """
        now = time.monotonic() - self._t0
        with self._lock:
            chain_mode = self._chain_mode
            ic_state = {ic: data for ic, (data, stamp) in self._ics.items()
                        if (now - stamp) <= CELL_STALE_S}
        cells = {frame_index: values
                 for frame_index, (values, stamp) in round_cells.items()
                 if (now - stamp) <= CELL_STALE_S}

        flat = {}
        for frame_index, values in cells.items():
            for offset, volts in enumerate(values):
                flat[frame_index * F.CELLS_PER_FRAME + offset] = volts

        not_responding = set((status or {}).get("not_responding_ics") or ())
        readings = []
        for ic in range(1, self.total_ic + 1):
            if ic in not_responding:
                if chain_mode:
                    self._note_dropout(ic, "the controller flagged it invalid in 0x6FF")
                continue
            diag = ic_state.get(ic - 1)
            volts = [flat.get((ic - 1) * self.cells_per_ic + cell)
                     for cell in range(self.cells_per_ic)]
            if not chain_mode:
                # No 0x6FF: the per-IC frame is the only per-chip validity
                # signal, so it's required.
                if diag is None:
                    continue
                if diag.get("pec_fail"):
                    # Still include it so the UI can show why the cycle is bad.
                    readings.append(self._reading(ic, [0.0] * self.cells_per_ic,
                                                  diag, pec_ok=False, now=now))
                    continue
            if any(v is None for v in volts):
                # No data (0xFFFF) or frame missing.
                if chain_mode:
                    self._note_dropout(ic, self._dropout_reason(ic, cells))
                continue
            readings.append(self._reading(ic, volts, diag, pec_ok=True, now=now))

        if not readings:
            return
        self._since_linkup += 1
        pack = PackSample(
            seq=self._seq, t_mono=now, t_wall=time.time(),
            readings=tuple(sorted(readings, key=lambda r: r.ic)),
            expected_ics=self.total_ic,
            cells_per_ic=self.cells_per_ic,
            stale=self._since_linkup <= self._warmup,
        )
        self._seq += 1
        self._emit("sample", pack)

    def _note_unparsed(self, line: str) -> None:
        """An adapter line python-can couldn't parse: usually a lost frame."""
        with self._lock:
            logged = self._odd_logged
            self._odd_logged += 1
        if logged < ODD_LOG_LIMIT:
            self._emit("line", "can: %s adapter line not parsed: %r" % (_stamp(), line))

    def _dropout_reason(self, ic: int, cells: dict) -> str:
        """Why IC ``ic`` (1-based) has no complete reading this round."""
        first = (ic - 1) * self.cells_per_ic
        frames = sorted({(first + c) // F.CELLS_PER_FRAME for c in range(self.cells_per_ic)})
        missing = [n for n in frames if n not in cells]
        if missing:
            return "cell frame(s) %s never arrived" % ", ".join(
                "0x%03X" % (F.ID_CELLS_BASE + n) for n in missing)
        return "the controller sent no data (0xFFFF) for some of its cells"

    def _note_dropout(self, ic: int, reason: str) -> None:
        """Raw-console line for an IC left out of a round, with the time, so
        dropouts can be lined up against anything else on the bus."""
        self._emit("line", "can: %s IC%d left out of this cycle: %s" % (_stamp(), ic, reason))

    def _reading(self, ic, volts, diag, pec_ok, now):
        padded = tuple(volts) + (0.0,) * (CELL_COUNT - len(volts))
        status_ok = None if diag is None else not diag.get("pec_fail")
        flags = die_temp = ccount = balance_mask = None
        if status_ok:
            flags = tuple(f for f in diag.get("flags", ()) if f != "pec_fail")
            balance_mask = diag.get("balancing_bitmap")
            die_temp = diag.get("die_temp_c")
            if die_temp is not None:
                die_temp = round(die_temp, 1)   # the wire carries 0.1 degC
            ccount = diag.get("cmd_counter")
        return Sample(
            seq=self._seq, t_mono=now, t_wall=time.time(), ic=ic,
            cells=padded[:CELL_COUNT],
            ccount=ccount,
            pec_ok=pec_ok, stale=False, ccount_jump=False,
            flags=flags, die_temp_c=die_temp,
            invalid_run=diag.get("consecutive_pec") if diag else None,
            status_ok=status_ok,
            balance_mask=balance_mask,
        )
