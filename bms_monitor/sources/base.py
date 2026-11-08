"""Base class for data sources, plus the shared bytes -> PackSample pipeline.

TelemetrySource has no send/write method. Any outbound bytes come from a
source's own reader thread, follow the fixed poll plan, and must go through
command_gate.encode_command().
"""

from __future__ import annotations

import abc
import threading
import time
from collections import deque
from typing import Callable, List, Optional

from ..model import (
    AUX_COUNT,
    CELL_COUNT,
    AuxReading,
    CellRecord,
    EventRecord,
    PackSample,
    Sample,
    StatusRecord,
    UnparsedRecord,
    ccount_delta,
    looks_disconnected,
)
from ..parsing.adi_text import parse_line
from ..parsing.blocks import BlockAssembler
from ..parsing.lines import LineAssembler

#: Link states. Shown as text, not just colour.
LINK_DOWN = "down"
LINK_CONNECTING = "connecting"
LINK_UP = "up"
LINK_RECONNECTING = "reconnecting"

#: Samples to treat as stale right after link-up.
#:
#: adBms6830_read_cell_voltages() starts continuous conversion and reads
#: with no delay (ADI's option 16 waits 8 ms), so the first reads after a
#: cold start can be stale or zero. They are logged and shown in the raw
#: console but not plotted.
WARMUP_SAMPLES = 2

#: A "stuck firmware" is >30 `Enter cmd:` acks per second with no data.
STUCK_ACK_RATE = 30.0
_STUCK_WINDOW_S = 1.0

#: How long to wait for the rest of the daisy chain before publishing a
#: poll cycle as PARTIAL. This is what makes a silent IC visible instead of
#: hanging or getting merged into the next cycle.
CYCLE_TIMEOUT_S = 1.0


class IngestPipeline:
    """Turns raw bytes into PackSamples, aux readings, events and raw lines.

    Owns no thread. Call feed_bytes() from one thread at a time; callbacks
    run synchronously.

    printVoltages() prints one line per IC, so these are buffered into one
    PackSample per poll cycle. A cycle is published when every IC has
    answered, when an IC index repeats, or when tick() finds it older than
    CYCLE_TIMEOUT_S. The last two publish it as PARTIAL.
    """

    def __init__(self, on_pack, on_line, on_status, on_event,
                 warmup: int = WARMUP_SAMPLES, kind: str = "cell",
                 on_terminator=None, on_aux=None,
                 expected_ics: int = 1, cells_per_ic: int = CELL_COUNT,
                 cycle_timeout: float = CYCLE_TIMEOUT_S):
        self._assembler = LineAssembler()
        # Folds the firmware's titled blocks back into single lines for the
        # parser. Older single-line output passes straight through.
        self._blocks = BlockAssembler()
        self._on_pack = on_pack
        self._on_line = on_line
        self._on_status = on_status
        self._on_event = on_event
        self._on_aux = on_aux or (lambda reading: None)
        # Called when a response is finished, so the serial pacer knows it
        # can send the next command.
        self._on_terminator = on_terminator or (lambda: None)
        self._warmup = warmup
        self._kind = kind
        self._expected_ics = max(1, int(expected_ics))
        self._cells_per_ic = max(1, min(int(cells_per_ic), CELL_COUNT))
        self._cycle_timeout = float(cycle_timeout)

        self._seq = 0
        self._t0 = time.monotonic()
        self._since_linkup = 0
        self._last_ccount = {}          # ic -> last seen counter
        self._status_group = ""
        self._status_fields = {}
        self._ack_times = deque(maxlen=256)
        self._data_since_ack = True
        self.stuck_suspected = False
        self._current_ic = None
        #: Factory serial per IC, from command 22. Confirms chain order.
        self.sids = {}

        # Open poll cycle.
        self._cycle = {}                # ic -> Sample
        self._cycle_opened_at = 0.0
        self._terminator_signaled = False

        # Counters shown in the UI. They only go up.
        self.lines_seen = 0
        self.samples_emitted = 0        # pack cycles published
        self.records_incomplete = 0
        self.unparsed_lines = 0
        self.pec_errors = 0
        self.ccount_jumps = 0
        self.partial_cycles = 0
        self.aux_readings = 0

    # -- configuration -----------------------------------------------------

    @property
    def expected_ics(self) -> int:
        return self._expected_ics

    @property
    def cells_per_ic(self) -> int:
        return self._cells_per_ic

    # -- lifecycle ---------------------------------------------------------

    def mark_linkup(self) -> None:
        """Called when a source (re)connects.

        Resets warm-up, partial lines and any half-built cycle, so data from
        before a disconnect isn't merged into the new connection.
        """
        self._assembler.reset()
        self._blocks.reset()
        self._since_linkup = 0
        self._last_ccount = {}
        self._ack_times.clear()
        self._cycle = {}
        self._cycle_opened_at = 0.0
        self._terminator_signaled = False
        self.stuck_suspected = False

    def tick(self) -> None:
        """Publish a cycle that has waited too long for the rest of the
        chain. Sources call this from their loop."""
        if self._cycle and (time.monotonic() - self._cycle_opened_at) > self._cycle_timeout:
            self._flush_cycle()

    # -- ingestion ---------------------------------------------------------

    def feed_bytes(self, data: bytes) -> None:
        for text, truncated in self._assembler.feed(data):
            self.lines_seen += 1
            if text or truncated:
                self._on_line(text if not truncated else text + "  <<truncated>>")
            if truncated:
                # A truncated line can't be part of a block: close any open
                # block and let the parser classify the fragment.
                for pending in self._blocks.flush():
                    self._handle_line(pending, False)
                self._handle_line(text, True)
                continue
            for normalised in self._blocks.feed_line(text):
                self._handle_line(normalised, False)

    @property
    def blocks_seen(self) -> int:
        return self._blocks.blocks_seen

    def _handle_line(self, text: str, truncated: bool) -> None:
        try:
            record = parse_line(text, truncated=truncated)
        except Exception as exc:                     # pragma: no cover
            # parse_line() shouldn't raise, but if it does the reader
            # thread must keep running.
            self.unparsed_lines += 1
            self._on_event(EventRecord(kind="parser_error", text=repr(exc)))
            return

        if isinstance(record, CellRecord):
            self._handle_cell(record)
        elif isinstance(record, StatusRecord):
            self._handle_status(record)
        elif isinstance(record, UnparsedRecord):
            if record.text:
                self.unparsed_lines += 1
            self._on_event(EventRecord(kind="unparsed", text=record.text))
        elif isinstance(record, EventRecord):
            if record.kind == "ack":
                self._note_ack()
            elif record.kind == "ic_header":
                self._current_ic = record.value
            elif record.kind == "sid":
                if self._current_ic:
                    self.sids[self._current_ic] = record.text
                self._data_since_ack = True
            elif record.kind == "blank":
                # A blank line ends one IC's record. If a cycle is still
                # open, the other ICs are still printing, so don't let the
                # pacer send the next command yet.
                if not self._cycle and not self._terminator_signaled:
                    self._terminator_signaled = True
                    self._on_terminator()
                return
            self._on_event(record)

    def _note_ack(self) -> None:
        """Stuck-firmware detection.

        A wedged scanf loop reprints ``Enter cmd:`` at full speed with no
        data. We only detect it; the fix is the board's RESET button.
        """
        now = time.monotonic()
        self._ack_times.append(now)
        cutoff = now - _STUCK_WINDOW_S
        while self._ack_times and self._ack_times[0] < cutoff:
            self._ack_times.popleft()
        rate = len(self._ack_times) / _STUCK_WINDOW_S
        self.stuck_suspected = rate > STUCK_ACK_RATE and not self._data_since_ack
        self._data_since_ack = False

    # -- cells -------------------------------------------------------------

    def _handle_cell(self, record: CellRecord) -> None:
        self._data_since_ack = True

        if record.kind == "aux":
            self._handle_aux(record)
            return
        if record.kind != self._kind:
            # e.g. an S-voltage or RAUX line while plotting cells. Report it
            # as an event but don't plot it.
            self._on_event(EventRecord(kind="other_channel", text=record.raw))
            return

        if not record.complete:
            self.records_incomplete += 1
            self._on_event(EventRecord(kind="incomplete", text=record.raw))
            return

        values = tuple(record.values)
        if len(values) != CELL_COUNT:                # pragma: no cover
            self.records_incomplete += 1
            return

        ic = record.ic
        if ic < 1 or ic > self._expected_ics:
            # IC index outside --ics: a config error, report it.
            self._on_event(EventRecord(
                kind="unexpected_ic",
                text="IC%d reported but --ics is %d" % (ic, self._expected_ics),
                value=ic,
            ))
            return

        delta = ccount_delta(self._last_ccount.get(ic), record.ccount)
        # A healthy poll advances the counter by a small, steady amount.
        # 0 means the chip never saw the command; a big step means commands
        # were missed or the chip reset.
        jump = delta is not None and (delta == 0 or delta > 24)
        self._last_ccount[ic] = record.ccount

        sample = Sample(
            seq=self._seq,
            t_mono=time.monotonic() - self._t0,
            t_wall=time.time(),
            ic=ic,
            cells=values,
            ccount=record.ccount,
            pec_ok=record.pec_ok,
            # Per-IC staleness is the no-device signature. Warm-up applies
            # to the whole cycle, so it's handled in _flush_cycle().
            stale=looks_disconnected(values[:self._cells_per_ic]),
            ccount_jump=jump,
        )

        if ic in self._cycle:
            # The next cycle has started before this one completed.
            self._flush_cycle()
        if not self._cycle:
            self._cycle_opened_at = time.monotonic()
        self._cycle[ic] = sample

        if len(self._cycle) >= self._expected_ics:
            self._flush_cycle()

    def _flush_cycle(self) -> None:
        if not self._cycle:
            return
        readings = tuple(self._cycle[ic] for ic in sorted(self._cycle))
        self._cycle = {}

        self._since_linkup += 1
        warmup = self._since_linkup <= self._warmup
        # Also stale if every IC that answered shows the no-device signature.
        all_disconnected = bool(readings) and all(r.stale for r in readings)

        pack = PackSample(
            seq=self._seq,
            t_mono=time.monotonic() - self._t0,
            t_wall=time.time(),
            readings=readings,
            expected_ics=self._expected_ics,
            cells_per_ic=self._cells_per_ic,
            stale=warmup or all_disconnected,
        )
        self._seq += 1
        self.samples_emitted += 1
        if not pack.pec_ok:
            self.pec_errors += 1
        if pack.partial:
            self.partial_cycles += 1
        self.ccount_jumps += sum(1 for r in readings if r.ccount_jump)

        self._on_pack(pack)
        if not self._terminator_signaled:
            self._terminator_signaled = True
            self._on_terminator()

    # -- aux ---------------------------------------------------------------

    def _handle_aux(self, record: CellRecord) -> None:
        """AUX lines carry 10 GPIO channels plus VMV and V+.

        Incomplete lines are still published, with None in slots that didn't
        parse, since a flaky thermistor channel is useful to see.
        """
        values = list(record.values) + [None] * AUX_COUNT
        reading = AuxReading(
            ic=record.ic,
            t_mono=time.monotonic() - self._t0,
            channels=tuple(values[:10]),
            vmv=values[10],
            vplus=values[11],
            ccount=record.ccount,
            pec_ok=record.pec_ok,
        )
        self.aux_readings += 1
        self._on_aux(reading)

    # -- status ------------------------------------------------------------

    def _handle_status(self, record: StatusRecord) -> None:
        self._data_since_ack = True
        if record.group:
            self._status_group = record.group
            return
        if not record.fields:
            return
        self._status_fields.update(record.fields)
        self._on_status(dict(self._status_fields))


class TelemetrySource(abc.ABC):
    """The interface the rest of the program uses. It has no way to transmit."""

    name = "base"

    def __init__(self) -> None:
        self._subscribers: List[Callable] = []
        self._sub_lock = threading.Lock()
        self._link = LINK_DOWN
        self._started_at: Optional[float] = None
        self._last_error = ""

    # -- public interface --------------------------------------------------

    @abc.abstractmethod
    def start(self) -> None:
        """Start acquiring. Should return quickly and not raise if the
        hardware is missing."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop acquiring and release the transport.  Idempotent."""

    def subscribe(self, callback: Callable) -> Callable:
        """Register ``callback(kind, payload)``. Returns an unsubscribe fn.

        ``kind`` is one of ``sample``, ``line``, ``status``, ``event``,
        ``link``.
        """
        with self._sub_lock:
            self._subscribers.append(callback)

        def unsubscribe():
            with self._sub_lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def status(self) -> dict:
        """A JSON-safe description of the transport's health."""
        return {
            "source": self.name,
            "link": self._link,
            "uptime_s": (time.monotonic() - self._started_at) if self._started_at else 0.0,
            "last_error": self._last_error,
        }

    # -- for subclasses ----------------------------------------------------

    def _emit(self, kind: str, payload) -> None:
        """Fan out to subscribers. Exceptions are swallowed so a bad
        subscriber can't kill the reader thread."""
        with self._sub_lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(kind, payload)
            except Exception:                        # pragma: no cover
                pass

    def _clear_error(self) -> None:
        """Data is flowing again, so clear the last error.

        _set_link() never clears an error on its own, so without this a
        message like "no CAN frames for 15s" stays up after frames resume.
        """
        if self._last_error:
            self._last_error = ""
            self._emit("link", {"link": self._link, "error": ""})

    def _set_link(self, state: str, error: str = "") -> None:
        if error:
            self._last_error = error
        if state != self._link:
            self._link = state
            self._emit("link", {"link": state, "error": self._last_error})
