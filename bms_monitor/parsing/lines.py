"""Byte stream -> text lines.

Handles chunk boundaries inside a multi-byte character or between CR and
LF, NUL/0xFF noise while a port enumerates, and firmware that stops
mid-line. The firmware sends bare LF, but CR and CRLF are accepted too
since a USB-serial bridge or terminal server can add them.
"""

from __future__ import annotations

from typing import List

#: Cap on a single unterminated line. A 16-cell printVoltages() line is
#: ~250 bytes. Past this we emit what we have, flagged as truncated, so a
#: babbling device can't grow the buffer forever.
MAX_LINE_BYTES = 4096

#: Bytes that never appear in the firmware's ASCII output but show up on a
#: port that was just opened or lost sync. Dropped before assembly.
_NOISE_BYTES = b"\x00\xff"


class LineAssembler:
    """Incremental line splitter. Feeding a stream one byte at a time gives
    the same lines as feeding it in one chunk."""

    __slots__ = ("_buf", "_pending_lf", "_truncated_next", "dropped_noise_bytes",
                 "truncated_lines")

    def __init__(self) -> None:
        self._buf = bytearray()
        # True when the previous chunk ended on CR, so a leading LF in the
        # next chunk is the tail of a CRLF pair and must be swallowed.
        self._pending_lf = False
        self._truncated_next = False
        self.dropped_noise_bytes = 0
        self.truncated_lines = 0

    # -- public ------------------------------------------------------------

    def feed(self, data: bytes) -> List[tuple]:
        """Consume bytes, return a list of ``(text, truncated)`` tuples.

        ``truncated`` is True for a line cut off by MAX_LINE_BYTES rather than
        a real terminator. Empty lines are returned too, since the firmware
        uses a blank line as a record separator.
        """
        out: List[tuple] = []
        if not data:
            return out

        for byte in data:
            if byte in (0x00, 0xFF):
                self.dropped_noise_bytes += 1
                continue

            if byte == 0x0A:                    # LF
                if self._pending_lf:
                    # Tail of CRLF; the line was already emitted on the CR.
                    self._pending_lf = False
                    continue
                out.append(self._take())
                continue

            self._pending_lf = False

            if byte == 0x0D:                    # CR
                out.append(self._take())
                self._pending_lf = True
                continue

            self._buf.append(byte)
            if len(self._buf) >= MAX_LINE_BYTES:
                self.truncated_lines += 1
                out.append((self._decode(bytes(self._buf)), True))
                self._buf.clear()

        return out

    def flush(self) -> List[tuple]:
        """Emit any buffered partial line. Only call on clean shutdown;
        calling it on every read would split lines at chunk boundaries."""
        if not self._buf:
            return []
        return [self._take()]

    def reset(self) -> None:
        """Discard partial state. Called on reconnect."""
        self._buf.clear()
        self._pending_lf = False

    # -- internals ---------------------------------------------------------

    def _take(self) -> tuple:
        text = self._decode(bytes(self._buf))
        self._buf.clear()
        return (text, False)

    @staticmethod
    def _decode(raw: bytes) -> str:
        """UTF-8 first, Latin-1 fallback.

        Older builds of ADI's code print a Latin-1 degree sign
        (``ITMP:27.5\\xb0C``). Latin-1 decoding can't fail.
        """
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
