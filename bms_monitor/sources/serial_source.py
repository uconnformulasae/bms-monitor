"""``--source serial``: read the board over a serial port (pyserial).

pyserial is imported lazily in SerialSource.start() so the rest of the tool
runs without it. The port is expected to drop (cable pulled, Nucleo
browning out and re-enumerating, laptop suspend), so read/write errors move
the link to RECONNECTING and retry with back-off instead of raising.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from . import command_gate
from .base import (
    LINK_CONNECTING,
    LINK_DOWN,
    LINK_RECONNECTING,
    LINK_UP,
    IngestPipeline,
    TelemetrySource,
)

#: Reconnect back-off, seconds. Capped at 5 s so a board that comes back
#: is picked up quickly.
BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 5.0, 5.0)

#: Blocking read timeout. Short enough that stop() is responsive, long
#: enough not to spin the CPU.
READ_TIMEOUT_S = 0.2

#: No traffic at all for this long -> cycle the port. A USB-serial device
#: can stay open but stop delivering data, and only a close/open fixes it.
STALL_TIMEOUT_S = 15.0

#: ST-LINK virtual COM port on the Nucleo boards.
STLINK_VID = 0x0483
STLINK_PID = 0x374B

#: Commands whose response includes a printVoltages() cell line.
_SAMPLE_PRODUCING = frozenset({4, 6, 8, 10})


def port_score(device: str, description: str = "",
               vid=None, pid=None) -> int:
    """Score one port. ``0`` means not a candidate.

    A port qualifies by ST-LINK VID/PID, a ``usbmodem``/``ttyACM`` name, or
    an ST-LINK description. ``/dev/cu.`` is only a tiebreaker: on a Mac,
    ports like ``/dev/cu.Bluetooth-Incoming-Port`` open fine and then never
    send anything.
    """
    device = device or ""
    lowered = device.lower()
    flat_desc = (description or "").lower().replace("-", "").replace(" ", "")

    is_stlink_id = (vid == STLINK_VID and pid == STLINK_PID)
    is_usb_cdc_name = ("usbmodem" in lowered) or ("ttyacm" in lowered)
    is_stlink_desc = "stlink" in flat_desc or "stmicro" in flat_desc

    if not (is_stlink_id or is_usb_cdc_name or is_stlink_desc):
        return 0

    score = 0
    if is_stlink_id:
        score += 100
    if is_stlink_desc:
        score += 30
    if is_usb_cdc_name:
        score += 20
    # Tiebreakers only, applied to already-qualified candidates.
    if lowered.startswith("/dev/cu."):
        score += 5
    if lowered.startswith("/dev/tty."):
        # Opening /dev/tty.* blocks waiting for DCD and hangs.
        score -= 50
    return max(0, score)


def list_candidate_ports() -> List[dict]:
    """Rank plausible Nucleo VCPs, best first.

    The device name changes with USB topology, so it isn't hardcoded.
    Returns ``[]`` if pyserial is missing.
    """
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    try:
        ports = list(list_ports.comports())
    except Exception:
        return []

    scored = []
    for info in ports:
        device = getattr(info, "device", "") or ""
        desc = getattr(info, "description", "") or ""
        score = port_score(device, desc,
                           getattr(info, "vid", None), getattr(info, "pid", None))
        if score <= 0:
            continue
        scored.append({
            "device": device,
            "description": desc,
            "vid": getattr(info, "vid", None),
            "pid": getattr(info, "pid", None),
            "score": score,
        })
    scored.sort(key=lambda item: (-item["score"], item["device"]))
    return scored


class SerialSource(TelemetrySource):
    name = "serial"

    def __init__(self, port: str = "auto", baud: int = 115200,
                 poll_plan=(4,), cmd_timeout: float = 1.5, warmup: int = 2,
                 max_rate_hz: float = 50.0, total_ic: int = 1,
                 cells_per_ic: int = 16):
        super().__init__()
        # Cap on how often a command is sent. On a real UART it never kicks
        # in: a 16-cell printVoltages() line is ~280 bytes (~24 ms at 115200
        # baud), slower than the 20 ms period. It only matters on faster
        # links, which would otherwise churn the history buffer in seconds.
        # macOS adds ~7 ms to short sleeps, so the real rate ends up lower
        # (~19 Hz for 50). --max-rate 0 disables it.
        self._min_period = 1.0 / max_rate_hz if max_rate_hz > 0 else 0.0
        self.requested_port = port
        self.baud = int(baud)
        self._plan = command_gate.validate_poll_plan(poll_plan)
        self._cmd_timeout = max(0.2, float(cmd_timeout))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._serial = None
        self._resolved_port = ""
        self._response_pending = False
        self._last_line_t = 0.0
        self._last_sample_t = 0.0
        self._reconnects = 0
        self._expects_samples = any(c in _SAMPLE_PRODUCING for c in self._plan)

        self.total_ic = max(1, int(total_ic))
        self._pipeline = IngestPipeline(
            on_pack=self._on_pack,
            on_line=lambda line: self._on_line(line),
            on_status=lambda fields: self._emit("status", fields),
            on_event=lambda ev: self._emit("event", ev),
            on_aux=lambda reading: self._emit("aux", reading),
            on_terminator=self._on_terminator,
            warmup=warmup,
            expected_ics=self.total_ic,
            cells_per_ic=cells_per_ic,
            cycle_timeout=max(0.3, self._cmd_timeout),
        )

    # -- TelemetrySource ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        # Imported here so the rest of the tool works without pyserial.
        try:
            import serial  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is required for --source serial.\n"
                "  install it with:  pip install -r requirements.txt\n"
                "  (underlying error: %s)" % exc
            ) from None

        self._started_at = time.monotonic()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="serial-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        self._close_port()
        if thread is not None:
            thread.join(timeout=3.0)
        self._set_link(LINK_DOWN)

    def status(self) -> dict:
        base = super().status()
        base.update({
            "port": self._resolved_port or self.requested_port,
            "mode": "live",
            "ics": self.total_ic,
            "cells_per_ic": self._pipeline.cells_per_ic,
            "partial_cycles": self._pipeline.partial_cycles,
            "sids": dict(self._pipeline.sids),
            "blocks": self._pipeline.blocks_seen,
            "aux_readings": self._pipeline.aux_readings,
            "baud": self.baud,
            "poll_plan": list(self._plan),
            "max_rate_hz": round(1.0 / self._min_period, 1) if self._min_period else 0,
            "reconnects": self._reconnects,
            "stuck_suspected": self._pipeline.stuck_suspected,
            "lines": self._pipeline.lines_seen,
            "samples": self._pipeline.samples_emitted,
            "unparsed": self._pipeline.unparsed_lines,
            "incomplete": self._pipeline.records_incomplete,
        })
        return base

    # -- pipeline callbacks ------------------------------------------------

    def _on_pack(self, pack) -> None:
        self._last_sample_t = time.monotonic()
        self._emit("sample", pack)

    def _on_line(self, line) -> None:
        self._last_line_t = time.monotonic()
        self._emit("line", line)

    def _on_terminator(self) -> None:
        """A record finished; the pacer may issue the next command."""
        self._response_pending = False

    # -- port handling -----------------------------------------------------

    def _resolve_port(self) -> str:
        """Re-resolve on every connect attempt, since the device name can
        change when the board is replugged."""
        if self.requested_port and self.requested_port != "auto":
            return self.requested_port
        candidates = list_candidate_ports()
        if not candidates:
            raise RuntimeError(
                "no Nucleo serial port found (looked for usbmodem/ttyACM and "
                "ST-LINK VID:PID %04x:%04x)" % (STLINK_VID, STLINK_PID)
            )
        return candidates[0]["device"]

    def _open_port(self):
        import serial

        url = self._resolve_port()
        # serial_for_url() so --port accepts any pyserial URL, not just a
        # device path.
        handle = serial.serial_for_url(
            url,
            baudrate=self.baud,
            timeout=READ_TIMEOUT_S,
            write_timeout=1.0,
            do_not_open=True,
        )
        handle.open()
        self._resolved_port = url
        try:
            handle.reset_input_buffer()
        except Exception:
            # Not every URL handler supports this.
            pass
        return handle

    def _close_port(self) -> None:
        handle, self._serial = self._serial, None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:
            pass

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        import serial

        attempt = 0
        while not self._stop_event.is_set():
            self._set_link(LINK_CONNECTING if attempt == 0 else LINK_RECONNECTING)
            try:
                self._serial = self._open_port()
            except Exception as exc:
                self._set_link(LINK_RECONNECTING, error=str(exc))
                delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                attempt += 1
                self._stop_event.wait(delay)
                continue

            attempt = 0
            if self._reconnects:
                pass
            self._pipeline.mark_linkup()
            now = time.monotonic()
            self._last_line_t = now
            self._last_sample_t = now
            self._response_pending = False
            self._set_link(LINK_UP, error="")

            try:
                self._session(serial)
            except Exception as exc:                 # pragma: no cover
                self._set_link(LINK_RECONNECTING, error=repr(exc))
            finally:
                self._close_port()

            if self._stop_event.is_set():
                break
            self._reconnects += 1
            self._set_link(LINK_RECONNECTING)
            self._stop_event.wait(BACKOFF_S[0])

        self._set_link(LINK_DOWN)

    def _session(self, serial_module) -> None:
        """Read/pace loop for one open port. Returns to trigger a reconnect."""
        SerialException = serial_module.SerialException
        index = 0
        sent_at = 0.0
        next_send = time.monotonic()

        while not self._stop_event.is_set():
            handle = self._serial
            if handle is None:
                return

            now = time.monotonic()

            # ---- pace -------------------------------------------------
            # Send before reading, otherwise every cycle waits a full
            # READ_TIMEOUT_S on an idle port before the next command.
            #
            # At most one command is outstanding. Move on when the response
            # ends (a Sample, or the blank line after a printVoltages()
            # record) or when --cmd-timeout expires because the board missed
            # the byte or is resetting.
            if self._response_pending and (now - sent_at) > self._cmd_timeout:
                self._response_pending = False
                self._emit("event", _timeout_event(self._plan[(index - 1) % len(self._plan)],
                                                   self._cmd_timeout))

            if not self._response_pending and now >= next_send:
                command = self._plan[index % len(self._plan)]
                index += 1
                payload = command_gate.encode_command(command)
                try:
                    handle.write(payload)
                    handle.flush()
                except (SerialException, OSError) as exc:
                    self._set_link(LINK_RECONNECTING, error="write: %s" % exc)
                    return
                except Exception as exc:
                    self._set_link(LINK_RECONNECTING, error="write: %r" % exc)
                    return
                sent_at = now
                next_send = now + self._min_period
                self._response_pending = True

            # ---- idle wait --------------------------------------------
            # Nothing outstanding, just waiting on the rate limit. Wait on
            # the stop event instead of a blocking read so the next command
            # goes out on time and stop() stays responsive.
            if not self._response_pending:
                delay = next_send - time.monotonic()
                if delay > 0:
                    self._stop_event.wait(min(delay, READ_TIMEOUT_S))
                    continue

            # ---- read -------------------------------------------------
            # in_waiting isn't reliable on every pyserial URL handler
            # (the socket handler reports 1 whenever it is readable), so treat
            # it as a hint and always read at least one byte.
            try:
                try:
                    waiting = handle.in_waiting
                except Exception:
                    waiting = 0
                data = handle.read(max(1, min(waiting, 8192)))
            except (SerialException, OSError) as exc:
                self._set_link(LINK_RECONNECTING, error="%s" % exc)
                return
            if data:
                self._clear_error()
                try:
                    self._pipeline.feed_bytes(data)
                except Exception as exc:             # pragma: no cover
                    # The pipeline shouldn't raise. If it does, drop the
                    # bytes, not the link.
                    self._set_link(LINK_UP, error="pipeline error: %r" % exc)

            # Publish a cycle the rest of the chain never completed, so a
            # silent IC becomes visible instead of stalling the pacer.
            self._pipeline.tick()

            # ---- watchdogs --------------------------------------------
            now = time.monotonic()
            if (now - self._last_line_t) > STALL_TIMEOUT_S:
                self._set_link(
                    LINK_RECONNECTING,
                    error="no output for %.0fs -- cycling the port" % STALL_TIMEOUT_S,
                )
                return
            if self._expects_samples and (now - self._last_sample_t) > STALL_TIMEOUT_S:
                self._set_link(
                    LINK_RECONNECTING,
                    error="no valid sample for %.0fs -- cycling the port" % STALL_TIMEOUT_S,
                )
                return


def _timeout_event(command: int, timeout: float):
    from ..model import EventRecord
    return EventRecord(
        kind="cmd_timeout",
        text="no response to command %d within %.1fs" % (command, timeout),
        value=command,
    )
