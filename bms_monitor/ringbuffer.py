"""A bounded, sequence-numbered ring buffer.

A thread-safe ``deque(maxlen=...)`` plus sequence numbers, so a client can
ask for just the items it missed.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable, List


class RingBuffer:
    """Thread-safe bounded buffer with monotonic sequence numbers.

    ``next_seq`` keeps counting past evictions, so a sequence number is a
    stable identity for an item even after it has fallen out of the window.
    """

    __slots__ = ("_items", "_lock", "_next_seq", "_dropped", "maxlen")

    def __init__(self, maxlen: int):
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self.maxlen = maxlen
        self._items = deque(maxlen=maxlen)     # of (seq, item)
        self._lock = threading.Lock()
        self._next_seq = 0
        self._dropped = 0

    def append(self, item) -> int:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            if len(self._items) == self.maxlen:
                self._dropped += 1
            self._items.append((seq, item))
            return seq

    def extend(self, items: Iterable) -> None:
        for item in items:
            self.append(item)

    def snapshot(self, limit: int = None) -> List:
        """Most recent ``limit`` items, oldest first."""
        with self._lock:
            n = len(self._items)
            if limit is not None:
                if limit <= 0:
                    return []
                if limit < n:
                    return [self._items[i][1] for i in range(n - limit, n)]
            return [item for _, item in self._items]

    def since(self, seq: int) -> List:
        """Items with a sequence number strictly greater than ``seq``."""
        with self._lock:
            return [item for s, item in self._items if s > seq]

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq

    @property
    def dropped(self) -> int:
        """How many items have been evicted. Shown in the UI."""
        with self._lock:
            return self._dropped

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def reset(self) -> None:
        with self._lock:
            self._items.clear()
            self._next_seq = 0
            self._dropped = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
