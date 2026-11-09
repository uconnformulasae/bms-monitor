"""Every CAN frame this tool transmits is built here.

Transmitting is off unless the CLI is started with ``--allow-tx``. The one
command is the F105's balance test (``0x740``): while it's on, the firmware
discharges every wired cell. The firmware does the timing and the safety
checks; this side only sends the toggle.

ON is a heartbeat. The firmware stops a discharge that hears no ON for
``F.BALANCE_CMD_TIMEOUT_S``, so while the toggle is on ON is queued again
every ``REPEAT_S``. OFF, losing the bus, or an ON that expired unsent ends
the repeat; this process exiting ends it too, and the firmware stops.

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

#: While the toggle is on, ON is queued again this long after the last one.
#: A repeat can wait up to ``can_source.TX_MAX_WAIT_S`` for a quiet bus, and
#: the two together must stay inside the firmware's timeout.
REPEAT_S = 0.25


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
        # True while ON is to be repeated, and when the last ON was queued.
        self._repeating = False
        self._last_on_at = 0.0
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
            self._repeating = enable
            self._last_on_at = now

    def _refresh(self, now: float) -> None:
        """Drop frames too old to send, and queue the next ON if one is due.
        Called with the lock held."""
        while self._queue and now - self._queue[0][0] > MAX_AGE_S:
            _, (_, data) = self._queue.popleft()
            self._expired += 1
            if data[0] == 1:
                # Repeating now would start a discharge long after the click.
                self._repeating = False
        if (self._repeating and not self._queue
                and now - self._last_on_at >= REPEAT_S):
            self._queue.append((now, encode_balance_test(True)))
            self._last_on_at = now

    def take(self) -> List[Tuple[int, bytes]]:
        """Frames to send now, oldest first. Called by the reader thread."""
        with self._lock:
            self._refresh(self._clock())
            out = [frame for _, frame in self._queue]
            self._queue.clear()
        for can_id, data in out:
            check_frame(can_id, data)
        return out

    def oldest_age(self) -> Optional[float]:
        """Seconds the oldest queued frame has waited, or None if empty."""
        with self._lock:
            now = self._clock()
            self._refresh(now)
            return (now - self._queue[0][0]) if self._queue else None

    def discard(self) -> None:
        """Drop anything queued and stop repeating, e.g. when the bus goes
        down. The firmware then times the discharge out by itself."""
        with self._lock:
            self._expired += len(self._queue)
            self._queue.clear()
            self._requested = None
            self._requested_at = None
            self._repeating = False

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
