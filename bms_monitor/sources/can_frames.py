"""Encode/decode for the BMS CAN frames. No bus or I/O here; can_source.py
does that.

All multi-byte fields are little-endian ("Intel" in Race Studio). The wrong
byte order gives plausible but wrong numbers rather than an error, so check
it first if a channel looks off. Values are scaled integers, never floats.

Cell voltages are u16 at 0.1 mV. The ADBMS6830 can report down to -3.4 V on
a broken channel; that is clamped to 0 instead of wrapping to ~6.5 V.
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# CAN1 identifiers (summary frames for the AIM datalogger). These are
# decoded, but the F105 doesn't send them.
# ---------------------------------------------------------------------------

ID_PACK_SUMMARY = 0x600
ID_TEMPS_EXTREMES = 0x601
ID_STATUS = 0x602
#: CAN1 summary frames.
CAN1_IDS = (ID_PACK_SUMMARY, ID_TEMPS_EXTREMES, ID_STATUS)

#: Largest flat cell index in a 90-cell pack.
MAX_CELL_INDEX = 89

# -- scaling ----------------------------------------------------------------
SCALE_PACK_V = 0.01          # u16 -> volts
SCALE_CELL_V = 0.0001        # u16 -> volts
SCALE_SPREAD_MV = 1.0        # u16 -> millivolts
SCALE_TEMP_C = 0.1           # s16 -> degrees C
SCALE_SOC_PCT = 0.01         # u16 -> percent

#: Cell-voltage sentinel: no valid reading (chip silent or PEC failed).
#: The largest real ADBMS6830 code is 0xFAA7 (6.4151 V), so it can't collide.
CELL_NO_DATA = 0xFFFF

# -- 0x602 byte 0 fault bits ------------------------------------------------
FLT_PEC = 1 << 0
FLT_OV = 1 << 1
FLT_UV = 1 << 2
FLT_OT = 1 << 3
FLT_UT = 1 << 4
FLT_OPEN_WIRE = 1 << 5
FLT_THSD = 1 << 6
FLT_AMS = 1 << 7             # shutdown circuit commanded open

FAULT_BITS = (
    (FLT_PEC, "PEC error this cycle"),
    (FLT_OV, "cell over-voltage"),
    (FLT_UV, "cell under-voltage"),
    (FLT_OT, "over-temperature"),
    (FLT_UT, "under-temperature"),
    (FLT_OPEN_WIRE, "open wire detected"),
    (FLT_THSD, "thermal shutdown (THSD)"),
    (FLT_AMS, "AMS FAULT -- shutdown circuit commanded open"),
)

# -- per-cell flag bits -----------------------------------------------------
CELL_OV = 1 << 0
CELL_UV = 1 << 1
CELL_BALANCING = 1 << 2
CELL_OPEN_WIRE = 1 << 3

#: CAN1 channel table, printed by race_studio_rows().
CHANNELS = [
    # (can_id, name, start_byte, length_bytes, signed, scale, offset, units)
    (ID_PACK_SUMMARY, "BMS_PackVoltage", 0, 2, False, SCALE_PACK_V, 0.0, "V"),
    (ID_PACK_SUMMARY, "BMS_MinCellV", 2, 2, False, SCALE_CELL_V, 0.0, "V"),
    (ID_PACK_SUMMARY, "BMS_MaxCellV", 4, 2, False, SCALE_CELL_V, 0.0, "V"),
    (ID_PACK_SUMMARY, "BMS_CellSpread", 6, 2, False, SCALE_SPREAD_MV, 0.0, "mV"),
    (ID_TEMPS_EXTREMES, "BMS_MinCellIndex", 0, 1, False, 1.0, 0.0, ""),
    (ID_TEMPS_EXTREMES, "BMS_MaxCellIndex", 1, 1, False, 1.0, 0.0, ""),
    (ID_TEMPS_EXTREMES, "BMS_MinTemp", 2, 2, True, SCALE_TEMP_C, 0.0, "degC"),
    (ID_TEMPS_EXTREMES, "BMS_MaxTemp", 4, 2, True, SCALE_TEMP_C, 0.0, "degC"),
    (ID_TEMPS_EXTREMES, "BMS_MaxTempSensor", 6, 1, False, 1.0, 0.0, ""),
    (ID_TEMPS_EXTREMES, "BMS_ICsResponding", 7, 1, False, 1.0, 0.0, ""),
    (ID_STATUS, "BMS_FaultBits", 0, 1, False, 1.0, 0.0, ""),
    (ID_STATUS, "BMS_ConsecPecFails", 1, 1, False, 1.0, 0.0, ""),
    (ID_STATUS, "BMS_ICsNotRespondingBitmap", 2, 2, False, 1.0, 0.0, ""),
    (ID_STATUS, "BMS_SOC", 4, 2, False, SCALE_SOC_PCT, 0.0, "%"),
    (ID_STATUS, "BMS_BalancingCount", 6, 1, False, 1.0, 0.0, ""),
    (ID_STATUS, "BMS_FwState", 7, 1, False, 1.0, 0.0, ""),
]

#: CAN2 channel families, one row per ID range instead of one per channel.
CAN2_FAMILIES = [
    ("0x6FF", "BMS_ChainStatus", "see note", 1.0, "",
     "b0 chain state (0 starting up, 1 running), b1 cycle counter, "
     "b2-3 bitmap of ICs with no valid data u16 LE"),
    ("0x700-0x716", "BMS_Cell<4n+1..4n+4>", "4 x u16 LE", SCALE_CELL_V, "V",
     "23 frames, cells 1-90; 0x700 = cells 1-4. 0xFFFF means no reading; "
     "the last two slots of 0x716 carry it."),
    ("0x720-0x729", "BMS_IC<n>_*", "see table", 1.0, "",
     "b0 IC index, b1 status bits, b2 CCount, b3 cycles in a row with no valid data, "
     "b4-5 die temp s16 LE x0.1 degC, b6-7 balancing bitmap u16 LE"),
    ("0x730-0x734", "BMS_Temp<4n+1..4n+4>", "4 x s16 LE", SCALE_TEMP_C, "degC",
     "thermistor slots, 10 per IC in RT order; -32768 = no probe fitted, "
     "32767 = probe fitted but no reading"),
]


def race_studio_rows() -> List[str]:
    """Channel table for setting up the logger. Printed by ``--can-channels``."""
    rows = [
        "CAN1 -> AIM datalogger.  3 frames, ~15 channels.  Not sent by the F105 yet.",
        "These are the channels to enter in Race Studio.",
        "",
        "%-6s %-28s %-6s %-4s %-13s %-11s %s"
        % ("ID", "Channel", "Start", "Len", "Type", "Multiplier", "Units"),
        "-" * 92,
    ]
    for can_id, name, start, length, signed, scale, _offset, units in CHANNELS:
        kind = ("s%d" % (length * 8)) if signed else ("u%d" % (length * 8))
        if length > 1:
            kind += " LE/Intel"
        rows.append("0x%03X %-28s %-6d %-4d %-13s x%-10g %s"
                    % (can_id, name, start, length, kind, scale, units or "-"))
    rows += [
        "",
        "CAN2 -> BMS bus, dedicated, 250 kbit/s.  Fixed IDs, no multiplexing.",
        "This is the bus the monitor tool listens on.",
        "",
        "%-13s %-26s %-12s %-11s %s"
        % ("IDs", "Channels", "Type", "Multiplier", "Units"),
        "-" * 92,
    ]
    for ids, name, kind, scale, units, note in CAN2_FAMILIES:
        rows.append("%-13s %-26s %-12s x%-10g %s" % (ids, name, kind, scale, units or "-"))
        rows.append("%13s   %s" % ("", note))
    rows += [
        "",
        "BYTE ORDER IS LITTLE-ENDIAN (Intel) FOR EVERY MULTI-BYTE FIELD ON BOTH",
        "BUSES.  In Race Studio that is the 'Intel' setting.  A mismatch does not",
        "produce an obvious error; it produces plausible-looking wrong numbers",
        "that still move when the real value moves.  If a channel reads as",
        "garbage but responds to reality, check this first.",
        "",
        "Bus load for the full 90 cells at 10 Hz: 23 frames x 10 Hz x ~120 bits",
        "= ~27.6 kbit/s, about 11% of the 250 kbit/s bus.",
    ]
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _u16(value: float, scale: float) -> int:
    """Quantise and clamp to u16. Clamped so out-of-range values look
    obviously wrong instead of wrapping to something plausible."""
    if value is None:
        return 0
    raw = int(round(value / scale))
    return max(0, min(0xFFFF, raw))


def _s16(value: float, scale: float) -> int:
    if value is None:
        return -32768          # sentinel: no reading
    raw = int(round(value / scale))
    # Both ends of the range are "no reading" sentinels (S16_NO_DATA), so a
    # real value clamps one step inside them.
    return max(-32767, min(32766, raw))


#: s16 values that mean "no reading". The F105 uses INT16_MAX
#: (``CAN_FRAMES_TEMP_UNKNOWN`` in ``BMS/App/Inc/can_frames.h``) and this
#: codec encodes -32768, so both decode to None.
S16_NO_DATA = (-32768, 32767)


def _from_s16(raw: int, scale: float) -> Optional[float]:
    if raw in S16_NO_DATA:
        return None            # sentinel
    return raw * scale


def _cell_from_u16(raw: int) -> Optional[float]:
    """Cell voltage, or None for CELL_NO_DATA (otherwise a silent IC would
    show up as 6.5535 V)."""
    if raw == CELL_NO_DATA:
        return None
    return raw * SCALE_CELL_V


def _check(data: bytes, name: str) -> bytes:
    if len(data) != 8:
        raise ValueError("%s payload must be exactly 8 bytes, got %d" % (name, len(data)))
    return bytes(data)


# ---------------------------------------------------------------------------
# 0x600: pack summary
# ---------------------------------------------------------------------------

def encode_pack_summary(pack_v, min_cell_v, max_cell_v, spread_mv) -> Tuple[int, bytes]:
    return ID_PACK_SUMMARY, struct.pack(
        "<HHHH",
        _u16(pack_v, SCALE_PACK_V),
        _u16(min_cell_v, SCALE_CELL_V),
        _u16(max_cell_v, SCALE_CELL_V),
        _u16(spread_mv, SCALE_SPREAD_MV),
    )


def decode_pack_summary(data: bytes) -> dict:
    pack_raw, lo_raw, hi_raw, spread_raw = struct.unpack("<HHHH", _check(data, "0x600"))
    return {
        "pack_v": pack_raw * SCALE_PACK_V,
        "min_cell_v": lo_raw * SCALE_CELL_V,
        "max_cell_v": hi_raw * SCALE_CELL_V,
        "spread_mv": spread_raw * SCALE_SPREAD_MV,
    }


# ---------------------------------------------------------------------------
# 0x601: temperatures and where the extremes are
# ---------------------------------------------------------------------------

def encode_temps_extremes(min_cell_index, max_cell_index, min_temp_c, max_temp_c,
                          max_temp_sensor, ics_responding) -> Tuple[int, bytes]:
    return ID_TEMPS_EXTREMES, struct.pack(
        "<BBhhBB",
        min(255, max(0, int(min_cell_index or 0))),
        min(255, max(0, int(max_cell_index or 0))),
        _s16(min_temp_c, SCALE_TEMP_C),
        _s16(max_temp_c, SCALE_TEMP_C),
        min(255, max(0, int(max_temp_sensor or 0))),
        min(255, max(0, int(ics_responding or 0))),
    )


def decode_temps_extremes(data: bytes) -> dict:
    lo_i, hi_i, lo_t, hi_t, sensor, ics = struct.unpack("<BBhhBB", _check(data, "0x601"))
    return {
        "min_cell_index": lo_i,
        "max_cell_index": hi_i,
        "min_temp_c": _from_s16(lo_t, SCALE_TEMP_C),
        "max_temp_c": _from_s16(hi_t, SCALE_TEMP_C),
        "max_temp_sensor": sensor,
        "ics_responding": ics,
    }


# ---------------------------------------------------------------------------
# 0x602: status and faults
# ---------------------------------------------------------------------------

def encode_status(fault_bits, consecutive_pec, not_responding_bitmap,
                  soc_pct, balancing_count, fw_state) -> Tuple[int, bytes]:
    return ID_STATUS, struct.pack(
        "<BBHHBB",
        int(fault_bits) & 0xFF,
        min(255, max(0, int(consecutive_pec or 0))),
        int(not_responding_bitmap) & 0xFFFF,
        _u16(soc_pct, SCALE_SOC_PCT) if soc_pct is not None else 0xFFFF,
        min(255, max(0, int(balancing_count or 0))),
        int(fw_state) & 0xFF,
    )


def decode_status(data: bytes) -> dict:
    bits, pec, bitmap, soc_raw, balancing, state = struct.unpack("<BBHHBB", _check(data, "0x602"))
    return {
        "fault_bits": bits,
        "faults": [name for mask, name in FAULT_BITS if bits & mask],
        "ams_fault": bool(bits & FLT_AMS),
        "consecutive_pec": pec,
        "not_responding_bitmap": bitmap,
        "not_responding_ics": [i + 1 for i in range(16) if bitmap & (1 << i)],
        # 0xFFFF means the controller isn't estimating SOC.
        "soc_pct": None if soc_raw == 0xFFFF else soc_raw * SCALE_SOC_PCT,
        "balancing_count": balancing,
        "fw_state": state,
    }


# ---------------------------------------------------------------------------
# CAN2: the dedicated BMS bus. Fixed IDs, no multiplexing.
# ---------------------------------------------------------------------------
#
# This is the bus the F105 sends on and the one the monitor listens to.
# Cell n is always in the same byte of the same ID.

#: Chain-status frame, sent every cycle by the F105
#: (``BMS_CAN_STATUS_ID`` in ``App/Inc/bms_config.h``).
#:
#: It's below 0x700 so it wins arbitration over the cell frames. Not the
#: same message as 0x602 (the CAN1 summary with SOC, fault bits and
#: balancing count), which the F105 doesn't send.
ID_CHAIN_STATUS = 0x6FF

#: Chain-state byte values, matching CAN_FRAMES_CHAIN_* in the firmware.
CHAIN_STATE_INIT = 0
CHAIN_STATE_RUNNING = 1

#: Balancing state, byte 4 of 0x6FF (CAN_FRAMES_BAL_* in the firmware).
#: Firmware without balancing sends 0 there, which reads as "off".
BALANCE_STATES = {
    0: "off",
    1: "pending",
    2: "discharging",
    3: "balanced",
    4: "refused",
    5: "aborted",
    6: "too_hot",
    7: "cell_low",
    8: "resting",
}

#: States in which bytes 5-6 are the lowest group and byte 7 the margin.
BALANCE_ROUND_STATES = ("discharging", "resting", "balanced")
BALANCE_NO_CELL = 0xFF

#: Balance test command, the only frame the monitor transmits
#: (``BMS_CAN_BALANCE_CMD_ID``). Byte 0: 1 = on, 0 = off. Byte 1: key.
#: While it's on, the controller balances the pack in rounds.
ID_BALANCE_CMD = 0x740
BALANCE_CMD_KEY = 0xA5

#: First cell-voltage frame.  ``0x700`` = cells 1-4, ``0x701`` = 5-8, ...
ID_CELLS_BASE = 0x700
#: Cells per frame: 8 bytes / 2 bytes per cell.
CELLS_PER_FRAME = 4
#: 90 cells / 4 = 23 frames, so 0x700 .. 0x716 inclusive.
CELL_FRAME_COUNT = 23
ID_CELLS_LAST = ID_CELLS_BASE + CELL_FRAME_COUNT - 1

#: Per-IC diagnostics, one frame per IC: ``0x720`` = IC1 ... ``0x729`` = IC10.
ID_IC_BASE = 0x720
IC_FRAME_COUNT = 10
ID_IC_LAST = ID_IC_BASE + IC_FRAME_COUNT - 1

#: Thermistor slots, 4 x s16 per frame: ``0x730`` = slots 1-4, ...
#: Each IC owns 10 slots (one per GPIO it has), so 2 ICs per segment board
#: need 5 frames. The F105 fills an IC's slots in the board's RT order
#: (BMSCMU: IC1 slots 1-5 = RT1-RT5, IC2 slots 1-4 = RT6-RT9).
ID_TEMPS_BASE = 0x730
TEMPS_PER_FRAME = 4
TEMP_FRAME_COUNT = 5
TEMP_SLOTS_PER_IC = 10
ID_TEMPS_LAST = ID_TEMPS_BASE + TEMP_FRAME_COUNT - 1

#: A slot with no thermistor fitted (``CAN_FRAMES_TEMP_NO_SENSOR``). The
#: other sentinel, INT16_MAX, is a fitted probe with no reading: its chip
#: did not answer, or the probe is open, shorted or off its rated range.
TEMP_NO_SENSOR_RAW = -32768

#: Per-IC status bits in byte 1 of a ``0x72n`` frame.
IC_PEC_FAIL = 1 << 0
IC_ANY_OV = 1 << 1
IC_ANY_UV = 1 << 2
IC_OPEN_WIRE = 1 << 3
IC_THSD = 1 << 4
IC_SPIFLT = 1 << 5

IC_STATUS_BITS = (
    (IC_PEC_FAIL, "pec_fail"),
    (IC_ANY_OV, "any_ov"),
    (IC_ANY_UV, "any_uv"),
    (IC_OPEN_WIRE, "open_wire"),
    (IC_THSD, "thsd"),
    (IC_SPIFLT, "spiflt"),
)


def cell_frame_id(frame_index: int) -> int:
    if not 0 <= frame_index < CELL_FRAME_COUNT:
        raise ValueError("cell frame index %d out of range 0..%d"
                         % (frame_index, CELL_FRAME_COUNT - 1))
    return ID_CELLS_BASE + frame_index


def encode_cells(frame_index: int, volts) -> Tuple[int, bytes]:
    """Four cell voltages, u16 little-endian, scale 0.0001 V.

    None and missing slots (e.g. the last two of a 90-cell pack) become
    CELL_NO_DATA, not 0, matching what the controller sends for a silent
    IC. Negative voltages clamp to 0.
    """
    can_id = cell_frame_id(frame_index)
    values = list(volts)[:CELLS_PER_FRAME]
    while len(values) < CELLS_PER_FRAME:
        values.append(None)
    raw = []
    for volt in values:
        if volt is None:
            raw.append(CELL_NO_DATA)
        elif volt < 0:
            raw.append(0)
        else:
            raw.append(min(_u16(volt, SCALE_CELL_V), CELL_NO_DATA - 1))
    return can_id, struct.pack("<4H", *raw)


def decode_cells(can_id: int, data: bytes) -> dict:
    """Return ``{"frame": n, "first_cell": 1-based, "volts": [...]}``.

    CELL_NO_DATA slots decode to None.
    """
    frame_index = can_id - ID_CELLS_BASE
    if not 0 <= frame_index < CELL_FRAME_COUNT:
        raise ValueError("id 0x%03X is not a cell-voltage frame" % can_id)
    if len(data) != 8:
        raise ValueError("cell frame payload must be 8 bytes, got %d" % len(data))
    raw = struct.unpack("<4H", data)
    return {
        "frame": frame_index,
        "first_cell": frame_index * CELLS_PER_FRAME + 1,
        "volts": [_cell_from_u16(value) for value in raw],
    }


def encode_ic_status(ic_index: int, status_bits: int, cmd_counter: int,
                     consecutive_pec: int, die_temp_c, balancing_bitmap: int
                     ) -> Tuple[int, bytes]:
    """``0x720 + ic_index``: IC | STATUS | CCOUNT | CONSEC_PEC | ITMP | BAL."""
    if not 0 <= ic_index < IC_FRAME_COUNT:
        raise ValueError("ic_index %d out of range 0..%d"
                         % (ic_index, IC_FRAME_COUNT - 1))
    return ID_IC_BASE + ic_index, struct.pack(
        "<BBBBhH",
        ic_index & 0xFF,
        int(status_bits) & 0xFF,
        min(63, max(0, int(cmd_counter or 0))),
        min(255, max(0, int(consecutive_pec or 0))),
        _s16(die_temp_c, SCALE_TEMP_C),
        int(balancing_bitmap) & 0xFFFF,
    )


def decode_ic_status(can_id: int, data: bytes) -> dict:
    frame_index = can_id - ID_IC_BASE
    if not 0 <= frame_index < IC_FRAME_COUNT:
        raise ValueError("id 0x%03X is not a per-IC frame" % can_id)
    if len(data) != 8:
        raise ValueError("per-IC payload must be 8 bytes, got %d" % len(data))
    ic, bits, counter, consec, temp, balancing = struct.unpack("<BBBBhH", data)
    if ic != frame_index:
        raise ValueError("payload IC index %d does not match arbitration frame %d"
                         % (ic, frame_index))
    return {
        "ic_index": ic,
        "frame": frame_index,
        "status_bits": bits,
        "flags": [name for mask, name in IC_STATUS_BITS if bits & mask],
        "pec_fail": bool(bits & IC_PEC_FAIL),
        "cmd_counter": counter,
        "consecutive_pec": consec,
        "die_temp_c": _from_s16(temp, SCALE_TEMP_C),
        "balancing_bitmap": balancing,
    }


def encode_temps(frame_index: int, celsius) -> Tuple[int, bytes]:
    """Four thermistor channels, s16 little-endian, scale 0.1 degC.

    None becomes the -32768 "no sensor" sentinel, not 0 degC.
    """
    if not 0 <= frame_index < TEMP_FRAME_COUNT:
        raise ValueError("temp frame index %d out of range 0..%d"
                         % (frame_index, TEMP_FRAME_COUNT - 1))
    values = list(celsius)[:TEMPS_PER_FRAME]
    while len(values) < TEMPS_PER_FRAME:
        values.append(None)
    return ID_TEMPS_BASE + frame_index, struct.pack(
        "<4h", *[_s16(v, SCALE_TEMP_C) for v in values])


def decode_temps(can_id: int, data: bytes) -> dict:
    frame_index = can_id - ID_TEMPS_BASE
    if not 0 <= frame_index < TEMP_FRAME_COUNT:
        raise ValueError("id 0x%03X is not a temperature frame" % can_id)
    if len(data) != 8:
        raise ValueError("temperature payload must be 8 bytes, got %d" % len(data))
    raw = struct.unpack("<4h", data)
    return {
        "frame": frame_index,
        "first_sensor": frame_index * TEMPS_PER_FRAME + 1,
        "celsius": [_from_s16(value, SCALE_TEMP_C) for value in raw],
        "fitted": [value != TEMP_NO_SENSOR_RAW for value in raw],
    }


# ---------------------------------------------------------------------------
# Index mapping
# ---------------------------------------------------------------------------

def cell_index(ic: int, cell: int, cells_per_ic: int) -> int:
    """``(IC 1-based, cell 1-based)`` -> flat 0-based pack index."""
    return (ic - 1) * cells_per_ic + (cell - 1)


def from_cell_index(index: int, cells_per_ic: int) -> Tuple[int, int]:
    """Flat 0-based pack index -> ``(IC 1-based, cell 1-based)``."""
    return index // cells_per_ic + 1, index % cells_per_ic + 1


def encode_chain_status(chain_state: int, cycle: int, invalid_ics: int,
                        balance_state: int = 0, balance_ic: int = 0,
                        balance_cell: int = 0,
                        balance_margin_0p1mv: int = 0) -> Tuple[int, bytes]:
    """``0x6FF``: STATE | CYCLE | INVALID_ICS(u16 LE) | BAL_STATE | BAL_IC |
    BAL_CELL | BAL_MARGIN. The balance IC and cell are 0-based, 0xFF = none;
    the margin is in 0.1 mV."""
    data = bytearray(8)
    data[0] = int(chain_state) & 0xFF
    data[1] = int(cycle) & 0xFF
    struct.pack_into("<H", data, 2, int(invalid_ics) & 0xFFFF)
    data[4] = int(balance_state) & 0xFF
    data[5] = int(balance_ic) & 0xFF
    data[6] = int(balance_cell) & 0xFF
    data[7] = int(balance_margin_0p1mv) & 0xFF
    return ID_CHAIN_STATUS, bytes(data)


def decode_chain_status(data: bytes) -> dict:
    """The controller's view of the chain.

    ``invalid_ics`` is a bitmap with bit 0 = first chip. ``not_responding_ics``
    is 1-based to match the UI (the firmware's SWO report calls that chip IC0).
    """
    data = _check(data, "0x6FF")
    state, cycle, invalid = struct.unpack("<BBH", data[:4])
    bal_state, bal_ic, bal_cell = data[4], data[5], data[6]
    name = BALANCE_STATES.get(bal_state, "unknown")
    # Only these states name an IC or cell, and 0xFF means none. During a
    # round it's the lowest group; "too_hot" names just the IC.
    named = name in BALANCE_ROUND_STATES + ("aborted", "too_hot", "cell_low")
    has_ic = named and bal_ic != BALANCE_NO_CELL
    has_cell = has_ic and bal_cell != BALANCE_NO_CELL
    return {
        "chain_state": "RUNNING" if state == CHAIN_STATE_RUNNING else "INIT",
        "chain_running": state == CHAIN_STATE_RUNNING,
        "cycle": cycle,
        "invalid_ics_bitmap": invalid,
        "not_responding_ics": [i + 1 for i in range(16) if (invalid >> i) & 1],
        "balance_state": name,
        "balance_ic": bal_ic + 1 if has_ic else None,
        "balance_cell": bal_cell + 1 if has_cell else None,
        "balance_margin_mv": (data[7] / 10.0
                              if name in BALANCE_ROUND_STATES else None),
    }


def encode_balance_cmd(enable: bool) -> Tuple[int, bytes]:
    """``0x740``: balance test on (1) or off (0), then the key byte."""
    if not isinstance(enable, bool):
        raise ValueError("enable must be a bool, not %r" % (enable,))
    return ID_BALANCE_CMD, bytes((1 if enable else 0, BALANCE_CMD_KEY))


def decode_balance_cmd(data: bytes) -> dict:
    if len(data) != 2 or data[1] != BALANCE_CMD_KEY or data[0] > 1:
        raise ValueError("not a balance test command: %s" % data.hex())
    return {"enable": data[0] == 1}


def decode(can_id: int, data: bytes) -> Optional[dict]:
    """Decode any known frame from either bus; ``None`` for a foreign ID.

    The result has a ``kind`` key to dispatch on.
    """
    try:
        if can_id == ID_PACK_SUMMARY:
            out = decode_pack_summary(data); out["kind"] = "summary"
        elif can_id == ID_TEMPS_EXTREMES:
            out = decode_temps_extremes(data); out["kind"] = "extremes"
        elif can_id == ID_STATUS:
            out = decode_status(data); out["kind"] = "status"
        elif can_id == ID_CHAIN_STATUS:
            out = decode_chain_status(data); out["kind"] = "status"
        elif ID_CELLS_BASE <= can_id <= ID_CELLS_LAST:
            out = decode_cells(can_id, data); out["kind"] = "cells"
        elif ID_IC_BASE <= can_id <= ID_IC_LAST:
            out = decode_ic_status(can_id, data); out["kind"] = "ic"
        elif ID_TEMPS_BASE <= can_id <= ID_TEMPS_LAST:
            out = decode_temps(can_id, data); out["kind"] = "temperatures"
        elif can_id == ID_BALANCE_CMD:
            # Another node (or a second monitor) sending the command.
            out = decode_balance_cmd(data); out["kind"] = "balance_cmd"
        else:
            return None
    except (ValueError, struct.error):
        # Wrong length at one of our IDs: someone else's frame or a
        # corrupted one. Ignore it.
        return None
    out["id"] = can_id
    return out


def owns_id(can_id: int) -> bool:
    """True if this identifier belongs to the BMS protocol on either bus."""
    return (can_id in CAN1_IDS
            or can_id == ID_CHAIN_STATUS
            or ID_CELLS_BASE <= can_id <= ID_CELLS_LAST
            or ID_IC_BASE <= can_id <= ID_IC_LAST
            or ID_TEMPS_BASE <= can_id <= ID_TEMPS_LAST
            or can_id == ID_BALANCE_CMD)
