"""Fold the firmware's titled block output back into single-line records.

The firmware prints a sample as a titled block::

    --- IC1: Cell Voltages ---
         C1=3.711900V     C2=3.708000V     C3=3.703950V     C4=3.699900V
         C5=3.696000V     C6=3.691950V     C7=3.687900V     C8=3.684000V
         C9=3.679950V    C10=3.675900V    C11=3.672000V    C12=3.667950V
        C13=3.663900V    C14=3.660000V    C15=3.655950V    C16=3.651900V
    CCount:63, PECError:0

The tokens are the same as in the older one-line format, so a block is
joined back into that line and parse_line() handles both::

    IC1:C1=3.711900V,C2=3.708000V,...,C16=3.651900V,CCount:63,PECError:0

Lines outside a block pass through unchanged, so older firmware still
works. A block ends at a line containing ``CCount:`` (included), a blank
line, the next ``--- IC<n>: ... ---`` header, or a ``#####`` marker from
the host test harness.
"""

from __future__ import annotations

import re
from typing import List, Optional

#: ``--- IC1: Cell Voltages ---``
BLOCK_HEADER = re.compile(r"^-{2,}\s*IC(\d+):\s*(.+?)\s*-{2,}$")

#: Titles whose bodies are channel tokens, folded into one measurement
#: line. Everything else passes through line by line.
MEASUREMENT_TITLES = {
    "cell voltages",
    "avg cell voltages",
    "filtered cell voltages",
    "s voltages",
    "aux voltages",
    "raux voltages",
}

#: Titles that map onto the old ``Status X:`` / ``Read Config A:`` headers
#: the parser already knows.
PASSTHROUGH_HEADERS = {
    "status a": "Status A:",
    "status b": "Status B:",
    "status c": "Status C:",
    "status d": "Status D:",
    "status e": "Status E:",
    "read config a": "Read Config A:",
    "read config b": "Read Config B:",
    "write config a": "Write Config A:",
    "write config b": "Write Config B:",
    "device sid": "Device SID:",
}

_CCOUNT_IN_LINE = re.compile(r"\bCCount:\s*-?\d+")
_MARKER = re.compile(r"^#{3,}")


class BlockAssembler:
    """Line in, zero or more normalised lines out. Never raises."""

    __slots__ = ("_ic", "_title", "_tokens", "blocks_seen", "legacy_lines")

    def __init__(self) -> None:
        self._ic: Optional[int] = None
        self._title: str = ""
        self._tokens: List[str] = []
        self.blocks_seen = 0
        self.legacy_lines = 0

    # -- public ------------------------------------------------------------

    @property
    def in_measurement_block(self) -> bool:
        return self._ic is not None and self._title in MEASUREMENT_TITLES

    def reset(self) -> None:
        self._ic = None
        self._title = ""
        self._tokens = []

    def feed_line(self, text: str) -> List[str]:
        """Normalise one input line.

        Returns the lines the parser should see: usually one, zero while a
        block is still accumulating, more when a header is emitted alongside
        a flush.
        """
        if text is None:
            return []
        stripped = text.strip()

        header = BLOCK_HEADER.match(stripped)
        if header:
            out = self._flush()
            try:
                self._ic = int(header.group(1))
            except ValueError:                       # pragma: no cover
                self._ic = None
            self._title = header.group(2).strip().lower()
            self._tokens = []
            self.blocks_seen += 1
            # Status/config blocks become the old two-line form: an IC
            # header, then the group header.
            legacy = PASSTHROUGH_HEADERS.get(self._title)
            if legacy:
                out.append("IC%d:" % self._ic)
                out.append(legacy)
            return out

        if not stripped:
            # A blank line ends a block. It's also the firmware's record
            # separator (the serial pacer uses it), so pass it through.
            return self._flush() + [""]

        if _MARKER.match(stripped):
            return self._flush() + [stripped]

        if self.in_measurement_block:
            self._tokens.extend(self._split(stripped))
            if _CCOUNT_IN_LINE.search(stripped):
                return self._flush()
            return []

        # Not in a measurement block: an old-format line or a status/config
        # body line. The parser handles these as-is.
        self.legacy_lines += 1
        return [stripped]

    def flush(self) -> List[str]:
        """Emit any block still open. For end-of-stream and disconnects."""
        return self._flush()

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _split(text: str) -> List[str]:
        """Whitespace-separated tokens with any trailing comma removed.

        ``CCount:63, PECError:0`` -> ``["CCount:63", "PECError:0"]``
        """
        return [token.rstrip(",") for token in text.split() if token.strip(",")]

    def _flush(self) -> List[str]:
        if self._ic is None:
            return []
        ic, tokens = self._ic, self._tokens
        title = self._title
        self.reset()
        if title not in MEASUREMENT_TITLES or not tokens:
            return []
        # Rebuild the single line the older firmware printed.
        return ["IC%d:%s" % (ic, ",".join(tokens))]
