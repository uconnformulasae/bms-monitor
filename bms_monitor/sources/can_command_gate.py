"""Every CAN frame this tool transmits is built here.

Transmitting is off unless the CLI is started with ``--allow-tx``. The one
command is the F105's balance test (``0x740``): each off -> on discharges the
highest cell for 500 ms, once. The firmware does the selection, the timing
and the safety checks; this side only sends the toggle.

Flow: the web layer calls :meth:`BalanceTestControl.request` with a bool.
That encodes the frame here and queues it. ``CanSource``'s reader thread
drains the queue with :meth:`BalanceTestControl.take` and is the only caller
of ``bus.send``. ``tests/test_readonly.py`` checks both.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import List, Optional, Tuple

from . import can_frames as F

#: The only identifiers the tool may transmit.
ALLOWED_TX_IDS = frozenset({F.ID_BALANCE_CMD})

#: A request not sent within this long is dropped instead of going out late,
#: e.g. after the CANdapter reconnects minutes later.
MAX_AGE_S = 1.0

#: Requests waiting for the reader thread. Old ones are dropped first.
QUEUE_LEN = 4


class CanCommandRejected(ValueError):
    """Raised for anything the gate won't emit."""


def encode_balance_test(enable) -> Tuple[int, bytes]:
    """The exact frame for the balance test toggle, or raise."""
    if not isinstance(enable, bool):
        raise CanCommandRejected("enable must be true or false, not %r" % (enable,))
    can_id, data = F.encode_balance_cmd(enable)
    check_frame(can_id, data)
    return can_id, data


def check_frame(can_id: int, data: bytes) -> None:
    """Last check before a frame reaches the bus."""
    if can_id not in ALLOWED_TX_IDS:
        raise CanCommandRejected("0x%03X is not an allowed transmit ID" % can_id)
    try:
        F.decode_balance_cmd(bytes(data))
    except ValueError as exc:
        raise CanCommandRejected(str(exc)) from None


class BalanceTestControl:
    """Queue between the web layer and the CAN reader thread."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._queue = collections.deque(maxlen=QUEUE_LEN)
        self._requested: Optional[bool] = None
        self._requested_at: Optional[float] = None
        self._sent = 0
        self._expired = 0
        self._last_error = ""

    def request(self, enable) -> None:
        """Queue the toggle. Raises :class:`CanCommandRejected` if invalid."""
        frame = encode_balance_test(enable)
        now = self._clock()
        with self._lock:
            self._queue.append((now, frame))
            self._requested = enable
            self._requested_at = now

    def take(self) -> List[Tuple[int, bytes]]:
        """Frames to send now, oldest first. Called by the reader thread."""
        now = self._clock()
        out = []
        with self._lock:
            while self._queue:
                queued_at, frame = self._queue.popleft()
                if now - queued_at > MAX_AGE_S:
                    self._expired += 1
                    continue
                out.append(frame)
        for can_id, data in out:
            check_frame(can_id, data)
        return out

    def oldest_age(self) -> Optional[float]:
        """Seconds the oldest queued frame has waited, or None if empty."""
        with self._lock:
            return (self._clock() - self._queue[0][0]) if self._queue else None

    def discard(self) -> None:
        """Drop anything queued, e.g. when the bus goes down."""
        with self._lock:
            self._expired += len(self._queue)
            self._queue.clear()
            self._requested = None
            self._requested_at = None

    def note_sent(self) -> None:
        with self._lock:
            self._sent += 1
            self._last_error = ""

    def note_error(self, error: str) -> None:
        with self._lock:
            self._last_error = error

    def status(self) -> dict:
        with self._lock:
            return {
                "requested": self._requested,
                "requested_age_s": (round(self._clock() - self._requested_at, 2)
                                    if self._requested_at is not None else None),
                "sent": self._sent,
                "expired": self._expired,
                "last_error": self._last_error,
            }
