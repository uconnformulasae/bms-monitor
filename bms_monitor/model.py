"""Core data types for the BMS monitor.

Record: one parsed line of firmware output. Sample: one IC's timestamped cell
reading, built only from a complete record. PackSample: one poll cycle across
the whole chain. Its pack voltage, mean, min/max and spread are None unless
every expected IC answered with a good PEC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Firmware constants (verified against ADBMS6830/program/src/*.c)
# ---------------------------------------------------------------------------

CELL_COUNT = 16          # serialPrintResult.c: CELL
AUX_COUNT = 12           # serialPrintResult.c: AUX
RAUX_COUNT = 10          # serialPrintResult.c: RAUX

#: getVoltage(code) = (code + 10000) * 0.000150   [serialPrintResult.c]
VOLTS_PER_LSB = 0.000150
CODE_OFFSET = 10000

#: c_codes is int16_t, so the printable range is bounded and includes
#: negative voltages.  Anything outside this cannot have come from the chip.
VOLTAGE_MIN = (-32768 + CODE_OFFSET) * VOLTS_PER_LSB   # -3.4152
VOLTAGE_MAX = (32767 + CODE_OFFSET) * VOLTS_PER_LSB    # +6.41505

#: cmd_cntr is a 6-bit chip counter reported through a uint8_t.
CCOUNT_MODULO = 64

#: Exact readings with no chip attached (every byte 0x00 or every byte 0xFF).
DISCONNECTED_ALL_ZERO = 1.500000     # all 0x00
DISCONNECTED_ALL_FF = 1.499850       # all 0xFF -> int16 -1
_DISCONNECT_EPS = 1e-6

#: Default thresholds from adBms_Application.c. Display only, never written
#: to the chip.
DEFAULT_UV_THRESHOLD = 3.0
DEFAULT_OV_THRESHOLD = 4.2


def code_to_volts(code: int) -> float:
    """Firmware's getVoltage(), exactly."""
    return (code + CODE_OFFSET) * VOLTS_PER_LSB


def volts_to_code(volts: float) -> int:
    """Inverse of :func:`code_to_volts`, clamped to int16."""
    code = round(volts / VOLTS_PER_LSB) - CODE_OFFSET
    return max(-32768, min(32767, int(code)))


def is_plausible_voltage(v: float) -> bool:
    """True if ``v`` could have been printed by the firmware.

    Rejects NaN and inf, since ``nan`` parses as a float and would break
    min/max downstream.
    """
    if v is None:
        return False
    if math.isnan(v) or math.isinf(v):
        return False
    # Small tolerance for the 6-decimal printf rounding at the extremes.
    return (VOLTAGE_MIN - 0.001) <= v <= (VOLTAGE_MAX + 0.001)


def looks_disconnected(values: Sequence[float]) -> bool:
    """True if every cell is exactly 1.500000 or exactly 1.499850 (no chip
    attached). Kept strict so a real pack with very even cells isn't hidden."""
    if not values:
        return False
    for target in (DISCONNECTED_ALL_ZERO, DISCONNECTED_ALL_FF):
        if all(abs(v - target) < _DISCONNECT_EPS for v in values):
            return True
    return False


# ---------------------------------------------------------------------------
# Records: one per parsed line
# ---------------------------------------------------------------------------

#: Channel kinds emitted by printVoltages(), keyed by their text prefix.
CHANNEL_COUNTS = {
    "cell": CELL_COUNT,
    "acell": CELL_COUNT,
    "fcell": CELL_COUNT,
    "scell": CELL_COUNT,
    "aux": AUX_COUNT,
    "raux": RAUX_COUNT,
}


@dataclass(frozen=True)
class CellRecord:
    """One printVoltages() line.

    ``complete`` is False when the line was truncated, a number failed to
    parse, or CCount/PECError were missing.  Incomplete records are kept
    (they point to a link problem) but never become Samples.
    """

    ic: int
    kind: str
    values: tuple = ()
    ccount: Optional[int] = None
    pec_error: Optional[int] = None
    complete: bool = False
    raw: str = ""

    @property
    def pec_ok(self) -> bool:
        return self.pec_error == 0

    @property
    def expected(self) -> int:
        return CHANNEL_COUNTS.get(self.kind, 0)


@dataclass(frozen=True)
class StatusRecord:
    """One line from printStatus().  ``group`` is 'A'..'E' or '' when the
    line arrived before any 'Status X:' header."""

    group: str
    fields: dict = field(default_factory=dict)
    raw: str = ""


@dataclass(frozen=True)
class EventRecord:
    """A non-measurement line worth showing: ack, menu, conversion done,
    error text, IC header."""

    kind: str
    text: str
    value: Optional[int] = None


@dataclass(frozen=True)
class UnparsedRecord:
    """A line we could not classify. Never discarded; it goes to the raw
    console view."""

    text: str
    truncated: bool = False


# ---------------------------------------------------------------------------
# Sample: what the UI plots
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Sample:
    seq: int
    t_mono: float           # seconds since the source started
    t_wall: float           # unix epoch, for the CSV only
    ic: int
    cells: tuple            # length CELL_COUNT, volts
    ccount: Optional[int]
    pec_ok: bool
    stale: bool             # warm-up or no-device signature -> do not plot
    ccount_jump: bool = False
    # Per-chip diagnostics the controller reports over CAN (0x720 + n).
    # ``None`` means not reported (the text path, or no per-IC frame yet),
    # and the UI shows it as unknown, not as healthy.
    #: Names from ``can_frames.IC_STATUS_BITS`` minus ``pec_fail``, e.g.
    #: ``("any_uv",)``. ``()`` = reported and clear.
    flags: Optional[tuple] = None
    die_temp_c: Optional[float] = None
    #: Consecutive controller cycles this chip gave no valid data.
    invalid_run: Optional[int] = None
    #: ``False`` when the controller says its status read (the one carrying
    #: the flags and die temperature) failed PEC, even though the cells were
    #: good. ``flags`` and ``die_temp_c`` are then unknown.
    status_ok: Optional[bool] = None

    # -- derived -----------------------------------------------------------
    @property
    def usable(self) -> bool:
        """A sample is plottable only when the PEC checked out and it is not
        flagged stale.  Every chart path must gate on this."""
        return self.pec_ok and not self.stale

    @property
    def pack_v(self) -> float:
        return sum(self.cells)

    @property
    def min_v(self) -> float:
        return min(self.cells)

    @property
    def max_v(self) -> float:
        return max(self.cells)

    @property
    def min_index(self) -> int:
        return self.cells.index(self.min_v)

    @property
    def max_index(self) -> int:
        return self.cells.index(self.max_v)

    @property
    def mean_v(self) -> float:
        return sum(self.cells) / len(self.cells) if self.cells else 0.0

    @property
    def spread_mv(self) -> float:
        return (self.max_v - self.min_v) * 1000.0

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "t": round(self.t_mono, 4),
            "ic": self.ic,
            "cells": [round(v, 6) for v in self.cells],
            "ccount": self.ccount,
            "pec_ok": self.pec_ok,
            "stale": self.stale,
            "jump": self.ccount_jump,
            "flags": list(self.flags) if self.flags is not None else None,
            "die_temp_c": self.die_temp_c,
            "invalid_run": self.invalid_run,
            "status_ok": self.status_ok,
        }


# ---------------------------------------------------------------------------
# PackSample: one poll cycle across the whole daisy chain
# ---------------------------------------------------------------------------
#
# A Sample is one chip's reading. A PackSample is the whole chain for one
# poll cycle, so "IC1 fine, IC2 silent" stays visible instead of being
# averaged away.

@dataclass(frozen=True)
class PackSample:
    seq: int
    t_mono: float
    t_wall: float
    readings: tuple           # of Sample, sorted by .ic, one per IC that answered
    expected_ics: int
    cells_per_ic: int         # populated channels per IC; the rest are masked
    stale: bool = False       # warm-up

    # -- chain state -------------------------------------------------------

    @property
    def present_ics(self) -> tuple:
        return tuple(r.ic for r in self.readings)

    @property
    def missing_ics(self) -> tuple:
        """ICs we expected to hear from and did not."""
        present = set(self.present_ics)
        return tuple(i for i in range(1, self.expected_ics + 1) if i not in present)

    @property
    def complete(self) -> bool:
        return not self.missing_ics

    @property
    def partial(self) -> bool:
        """Some ICs answered and some did not (not the same as nothing
        answering)."""
        return bool(self.readings) and bool(self.missing_ics)

    @property
    def pec_ok(self) -> bool:
        return bool(self.readings) and all(r.pec_ok for r in self.readings)

    @property
    def usable(self) -> bool:
        """Plottable only when every expected IC answered, every PEC checked
        out, and we are past warm-up."""
        return self.complete and self.pec_ok and not self.stale

    @property
    def bad_ics(self) -> tuple:
        return tuple(r.ic for r in self.readings if not r.pec_ok)

    @property
    def any_jump(self) -> bool:
        return any(r.ccount_jump for r in self.readings)

    # -- cell access -------------------------------------------------------

    def masked(self, reading) -> tuple:
        """Cells of one IC, cut to the populated channel count.

        Unused channels read garbage and latch UV flags (the BMSCMU segment
        board uses C1-C10 on each of its two ICs), so they're dropped here and
        nothing downstream sees them.
        """
        return tuple(reading.cells[:self.cells_per_ic])

    def cell_list(self) -> list:
        """``[(ic, cell_index_1based, volts), ...]`` across the whole chain,
        masked, in chain order."""
        out = []
        for reading in self.readings:
            for index, volts in enumerate(self.masked(reading)):
                out.append((reading.ic, index + 1, volts))
        return out

    @property
    def cell_count(self) -> int:
        return sum(len(self.masked(r)) for r in self.readings)

    # -- aggregates. None when the data does not support them --------------

    @property
    def pack_v(self) -> Optional[float]:
        """Sum across the whole chain, or ``None`` unless every expected IC
        answered with a good PEC."""
        if not self.usable:
            return None
        return sum(v for _, _, v in self.cell_list())

    @property
    def mean_v(self) -> Optional[float]:
        cells = self.cell_list()
        if not self.usable or not cells:
            return None
        return sum(v for _, _, v in cells) / len(cells)

    def _extreme(self, pick):
        cells = self.cell_list()
        if not self.usable or not cells:
            return None
        return pick(cells, key=lambda item: item[2])

    @property
    def min_cell(self):
        """``(ic, cell, volts)`` of the lowest cell in the chain, or None."""
        return self._extreme(min)

    @property
    def max_cell(self):
        return self._extreme(max)

    @property
    def spread_mv(self) -> Optional[float]:
        lo, hi = self.min_cell, self.max_cell
        if lo is None or hi is None:
            return None
        return (hi[2] - lo[2]) * 1000.0

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "t": round(self.t_mono, 4),
            "expected_ics": self.expected_ics,
            "cells_per_ic": self.cells_per_ic,
            "complete": self.complete,
            "partial": self.partial,
            "stale": self.stale,
            "pec_ok": self.pec_ok,
            "missing": list(self.missing_ics),
            "ics": [
                {
                    "ic": r.ic,
                    "cells": [round(v, 6) for v in self.masked(r)],
                    "ccount": r.ccount,
                    "pec_ok": r.pec_ok,
                    "jump": r.ccount_jump,
                    "flags": list(r.flags) if r.flags is not None else None,
                    "die_temp_c": r.die_temp_c,
                    "status_ok": r.status_ok,
                }
                for r in self.readings
            ],
        }


# ---------------------------------------------------------------------------
# AUX / temperature
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuxReading:
    """One printVoltages() AUX line: 10 GPIO channels plus VMV and V+.

    ``vmv`` and ``vplus`` are stored exactly as printed, including the
    firmware's known 20x scaling bug, so they match what ``screen`` shows.
    """

    ic: int
    t_mono: float
    channels: tuple           # 10 GPIO aux voltages
    vmv: Optional[float]
    vplus: Optional[float]
    ccount: Optional[int]
    pec_ok: bool

    def to_json(self) -> dict:
        return {
            "ic": self.ic,
            "t": round(self.t_mono, 4),
            "channels": [None if v is None else round(v, 6) for v in self.channels],
            "vmv": self.vmv,
            "vplus": self.vplus,
            "ccount": self.ccount,
            "pec_ok": self.pec_ok,
        }


@dataclass(frozen=True)
class NtcConfig:
    """Thermistor divider description (beta and the divider vary by part and
    board revision).

    Default: series resistor from VREF2 to the AUX pin and NTC from the AUX
    pin to ground, so a 10k/10k pair at 25 degC reads VREF2/2 = 1.5 V.
    """

    beta: float = 3435.0          # B25/85, typical for a 10k NTC
    r25: float = 10000.0          # NTC resistance at 25 degC
    series: float = 10000.0       # pull-up from VREF2
    vref: float = 3.0             # VREF2 as the 6830 drives it
    pullup: bool = True           # series resistor to VREF2, NTC to ground


#: 25 degC in kelvin.
_T25_K = 298.15


def ntc_resistance(volts: float, cfg: NtcConfig) -> Optional[float]:
    """Divider node voltage -> NTC resistance in ohms.

    Returns None for a voltage outside the divider's reachable range, which
    is what an open or shorted thermistor looks like.
    """
    if volts is None or not is_finite(volts):
        return None
    vref = cfg.vref
    if vref <= 0 or cfg.series <= 0:
        return None
    # Leave headroom: at the rails the divider equation goes singular.
    margin = vref * 0.001
    if not (margin < volts < vref - margin):
        return None
    if cfg.pullup:
        # Vout = Vref * Rntc / (Rs + Rntc)
        return cfg.series * volts / (vref - volts)
    # NTC to Vref, series to ground: Vout = Vref * Rs / (Rs + Rntc)
    return cfg.series * (vref - volts) / volts


def ntc_celsius(volts: float, cfg: NtcConfig) -> Optional[float]:
    """Divider node voltage -> degrees Celsius via the beta model.

    Good to about a degree over normal pack temperatures. Returns None when
    the voltage doesn't fit a connected thermistor.
    """
    resistance = ntc_resistance(volts, cfg)
    if resistance is None or resistance <= 0 or cfg.beta <= 0 or cfg.r25 <= 0:
        return None
    try:
        inv = (1.0 / _T25_K) + (math.log(resistance / cfg.r25) / cfg.beta)
    except (ValueError, ZeroDivisionError):
        return None
    if inv <= 0:
        return None
    celsius = (1.0 / inv) - 273.15
    # Outside this band it's a broken sensor, not a real temperature.
    if not (-60.0 <= celsius <= 200.0):
        return None
    return celsius


def is_finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and not math.isnan(value) and not math.isinf(value)


def ccount_delta(prev: Optional[int], cur: Optional[int]) -> Optional[int]:
    """Forward distance on the mod-64 chip command counter.

    Returns None when either side is unknown.  A healthy poll loop advances
    by a small fixed amount; anything else means a missed command or a chip
    reset.
    """
    if prev is None or cur is None:
        return None
    return (cur - prev) % CCOUNT_MODULO
