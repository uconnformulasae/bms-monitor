"""Parse one line of ADBMS6830 firmware text into a Record.

parse_line() has no state and never raises, since it runs on the reader
thread. Bad lines come back as UnparsedRecord, and a truncated or malformed
measurement comes back with ``complete=False``.

In serialPrintResult.c, printVoltages() prints ``IC%d:`` with no newline,
so a whole sample is one line::

    IC1:C1=3.712000V,C2=3.708000V,...,C16=3.700000V,CCount:63,PECError:0

printStatus() and printReadConfig() put the IC header on its own line.
"""

from __future__ import annotations

import re

from ..model import (
    CHANNEL_COUNTS,
    CellRecord,
    EventRecord,
    StatusRecord,
    UnparsedRecord,
    is_plausible_voltage,
)

__all__ = ["parse_line"]

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# The IC header can be preceded by garbage when we join a stream mid-line or
# a glitch injects bytes, so we search rather than match from position 0.
_IC_HEADER = re.compile(r"IC(\d+):")

# printVoltages() channel tokens. Longest prefixes first so "AC" and "FC"
# aren't matched as "C". Tokens can end in a comma or whitespace: the
# current firmware separates them with whitespace, and lines the
# BlockAssembler folds back use commas.
_CHANNEL_TOKEN = re.compile(
    r"\b(?P<prefix>RAUX|AUX|AC|FC|S|C)(?P<index>\d+)=(?P<value>[^,\s]*?)V(?=[\s,]|$)"
)

# Special AUX channels 11 and 12 print as names, not indices.
_NAMED_AUX = re.compile(r"\b(?P<name>VMV|V\+):(?P<value>[^,\s]*?)V(?=[\s,]|$)")

_CCOUNT = re.compile(r"CCount:\s*(-?\d+)")
_PECERROR = re.compile(r"PECError:\s*(-?\d+)")

_ENTER_CMD = re.compile(r"Enter cmd:\s*(-?\d+)\s*$")
_ADC_TIME = re.compile(r"Adc Conversion Time\s*=\s*([-\w.+]+)ms")

_STATUS_HEADER = re.compile(r"^Status ([A-E]):$")
_CONFIG_HEADER = re.compile(r"^(?:Read|Write) Config ([AB]):$")

# Generic "KEY:VALUE" pairs used by printStatus().  Values are either a float
# with a V/degC suffix or a 0xNN hex flag.
_KV_HEX = re.compile(r"\b([A-Z][A-Z0-9_\[\]:+]*):0x([0-9A-Fa-f]+)")
_KV_VOLT = re.compile(r"\b([A-Z][A-Z0-9_+]*):([-\w.+]+)V\b")
_KV_TEMP = re.compile(r"\bITMP:([-\w.+]+)\s*\xb0?(?:\u00b0)?C")

# Lines the firmware prints that carry no data but confirm liveness.
_CONVERSION_DONE = re.compile(
    r"^(Cell|S-Voltage|Avg Cell voltage|F Cell voltage|Aux voltage|RAux voltage)"
    r" conversion completed$"
)
_CLEARED = re.compile(r"^(Cell|Aux|Spin|Fcell) Registers Cleared$")

# The first two only appear in the older menu text; kept so older
# firmware still parses.
_MENU_PREFIXES = (
    "List of ADBMS6830 Command:",
    "Print '0' for menu",
    "Please enter command:",
    "ADBMS6830 COMMAND MENU",
)
#: Old menu item: ``Read Cell Voltages: 4``
_MENU_ITEM = re.compile(r"^[A-Z][A-Za-z0-9 \-/]+:\s*\d+\s*$")
#: New menu item: ``   1  Write and Read Configuration        (writes)``
_MENU_ITEM_V2 = re.compile(r"^\d{1,2}\s{2,}[A-Z][A-Za-z0-9 \-/()+]*$")
#: New menu group heading: ``-- Configuration ------------------``
_MENU_GROUP = re.compile(r"^-{2,}\s*[A-Z][A-Za-z0-9 /]*\s*-{2,}$")
#: Rule and banner lines: the 62-char ``=`` separator after ``Enter cmd:N``,
#: the ``====== ADBMS6830 COMMAND MENU ======`` banner, and plain ``-----``.
#: Checked AFTER the block header, which also starts with dashes.
_RULE = re.compile(r"^[=\-]{3,}([A-Za-z0-9 /()\-]*[=\-]{3,})?$")

#: The six raw bytes a Write Config block prints, e.g.
#: ``0x81, 0x0, 0x0, 0x0, 0xFF, 0x3``. Nothing to extract.
_CONFIG_BYTES = re.compile(r"^0x[0-9A-Fa-f]{1,2}(\s*,\s*0x[0-9A-Fa-f]{1,2})+$")
#: ``##### ... #####`` markers from the host test harness. The firmware
#: never prints these; accepting them lets harness logs be used as fixtures.
_HARNESS_MARKER = re.compile(r"^#{3,}.*#{3,}$")

#: ``SID:A10BC300E5F6``: always 12 hex characters (%02X x 6).
_SID = re.compile(r"^SID:([0-9A-Fa-f]{12})$")
_SID_INVALID = "SID NOT VALID - PEC error"

#: ``--- IC1: Cell Voltages ---`` for a block title the BlockAssembler
#: doesn't fold.
_BLOCK_HEADER = re.compile(r"^-{2,}\s*IC(\d+):\s*(.+?)\s*-{2,}$")

_PREFIX_TO_KIND = {
    "C": "cell",
    "AC": "acell",
    "FC": "fcell",
    "S": "scell",
    "AUX": "aux",
    "RAUX": "raux",
}


#: Bound for fields that aren't raw cell codes. printVoltages() prints VMV
#: and V+ with a 20x multiplier (firmware bug), so they can exceed the
#: int16 cell range.
_WIDE_LIMIT = 1000.0


def _parse_number(text: str):
    """Strict numeric conversion, before any range check.

    ``float()`` accepts ``nan``, ``inf`` and underscores, and a NaN in the
    cell array breaks every min/max and autoscale. Only digits, a sign, a
    dot and an exponent are allowed.
    """
    t = text.strip()
    if not t or not re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", t):
        return None
    try:
        value = float(t)
    except (ValueError, OverflowError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _to_float(text: str):
    """A raw cell/aux channel value: must lie in the int16 code range."""
    value = _parse_number(text)
    if value is None or not is_plausible_voltage(value):
        return None
    return value


def _to_float_wide(text: str):
    """A scaled field (VMV, V+): finite and physically sane, but not bound
    to the int16 cell range."""
    value = _parse_number(text)
    if value is None or not (-_WIDE_LIMIT <= value <= _WIDE_LIMIT):
        return None
    return value


def _parse_measurement(line: str, ic: int, body: str):
    """Build a CellRecord from the part of the line after ``ICn:``."""
    by_kind = {}
    for m in _CHANNEL_TOKEN.finditer(body):
        kind = _PREFIX_TO_KIND.get(m.group("prefix"))
        if kind is None:
            continue
        idx = int(m.group("index"))
        if idx < 1 or idx > CHANNEL_COUNTS[kind]:
            continue
        by_kind.setdefault(kind, {})[idx] = _to_float(m.group("value"))

    if not by_kind:
        return None

    # A single line only ever carries one channel family; if several matched
    # (garbage splice) take the one with the most channels, deterministically.
    kind = max(sorted(by_kind), key=lambda k: len(by_kind[k]))
    slots = by_kind[kind]

    # AUX lines carry VMV and V+ as channels 11 and 12.
    if kind == "aux":
        for m in _NAMED_AUX.finditer(body):
            raw = _to_float_wide(m.group("value"))
            # printVoltages() applies a 20x multiplier to both (firmware
            # bug). Store what was printed; the UI labels these as raw.
            slots[11 if m.group("name") == "VMV" else 12] = raw

    expected = CHANNEL_COUNTS[kind]
    values = tuple(slots.get(i) for i in range(1, expected + 1))

    cc = _CCOUNT.search(body)
    pe = _PECERROR.search(body)
    ccount = int(cc.group(1)) if cc else None
    pec = int(pe.group(1)) if pe else None
    if ccount is not None and not (0 <= ccount <= 255):
        ccount = None
    if pec is not None and pec not in (0, 1):
        pec = None

    complete = (
        ccount is not None
        and pec is not None
        and all(v is not None for v in values)
    )
    return CellRecord(
        ic=ic,
        kind=kind,
        values=values,
        ccount=ccount,
        pec_error=pec,
        complete=complete,
        raw=line,
    )


def _parse_status_fields(line: str):
    fields = {}
    for m in _KV_HEX.finditer(line):
        try:
            fields[m.group(1)] = int(m.group(2), 16)
        except ValueError:
            pass
    for m in _KV_VOLT.finditer(line):
        value = _to_float_wide(m.group(2))
        if value is not None:
            fields[m.group(1)] = value
    m = _KV_TEMP.search(line)
    if m:
        # ITMP is already in degrees C, so the voltage range check doesn't
        # apply. Accept a wide but finite range.
        try:
            temp = float(m.group(1))
        except (ValueError, OverflowError):
            temp = None
        if temp is not None and -300.0 <= temp <= 500.0:
            fields["ITMP"] = temp
    cc = _CCOUNT.search(line)
    if cc:
        fields["CCount"] = int(cc.group(1))
    pe = _PECERROR.search(line)
    if pe:
        fields["PECError"] = int(pe.group(1))
    return fields


def parse_line(line: str, truncated: bool = False):
    """Classify one line. Never raises; always returns a Record."""
    if line is None:
        return UnparsedRecord(text="", truncated=truncated)

    # Only the parsed copy is stripped; the raw console gets the original.
    text = line.strip()

    if not text:
        return EventRecord(kind="blank", text="")

    # -- liveness / protocol lines ----------------------------------------
    m = _ENTER_CMD.search(text)
    if m:
        return EventRecord(kind="ack", text=text, value=int(m.group(1)))

    if _CONVERSION_DONE.match(text):
        return EventRecord(kind="conversion_done", text=text)

    if _CLEARED.match(text):
        # We never send 17-20, so something else is driving the port.
        return EventRecord(kind="registers_cleared", text=text)

    if text == "Incorrect Option":
        return EventRecord(kind="firmware_error", text=text)

    if text == "Wrong Register Group Select":
        return EventRecord(kind="firmware_error", text=text)

    m = _ADC_TIME.search(text)
    if m:
        return EventRecord(kind="adc_time", text=text)

    if (text.startswith(_MENU_PREFIXES)
            or _MENU_ITEM.match(text)
            or _MENU_ITEM_V2.match(text)):
        return EventRecord(kind="menu", text=text)

    m = _STATUS_HEADER.match(text)
    if m:
        return StatusRecord(group=m.group(1), fields={}, raw=text)

    m = _CONFIG_HEADER.match(text)
    if m:
        return EventRecord(kind="config_header", text=text)

    if text in ("Device SID:", "Read Device SID:",
                "Write Config A:", "Write Config B:"):
        return EventRecord(kind="config_header", text=text)

    m = _SID.match(text)
    if m:
        # Factory serial, used to confirm the daisy-chain order.
        return EventRecord(kind="sid", text=m.group(1).upper())

    if text == _SID_INVALID:
        return EventRecord(kind="sid_invalid", text=text)

    if _HARNESS_MARKER.match(text):
        return EventRecord(kind="harness_marker", text=text)

    m = _BLOCK_HEADER.match(text)
    if m:
        return EventRecord(kind="ic_header", text=text, value=int(m.group(1)))

    if _CONFIG_BYTES.match(text):
        return EventRecord(kind="config_bytes", text=text)

    if _RULE.match(text) or _MENU_GROUP.match(text):
        return EventRecord(kind="menu", text=text)

    # -- ICn: lines --------------------------------------------------------
    m = _IC_HEADER.search(text)
    if m:
        ic = int(m.group(1))
        body = text[m.end():]
        if not body.strip():
            # printStatus()/printReadConfig() style: header on its own line.
            return EventRecord(kind="ic_header", text=text, value=ic)
        record = _parse_measurement(text, ic, body)
        if record is not None:
            return record
        # An ICn: line that isn't a measurement (e.g. truncated before the
        # first '='). Keep the text.
        return UnparsedRecord(text=text, truncated=truncated)

    # -- status payload lines ---------------------------------------------
    fields = _parse_status_fields(text)
    if fields:
        return StatusRecord(group="", fields=fields, raw=text)

    return UnparsedRecord(text=text, truncated=truncated)
