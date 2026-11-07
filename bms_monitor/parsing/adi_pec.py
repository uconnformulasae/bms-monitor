"""ADI PEC15/PEC10 and the ADBMS6830 opcode table, for the debug console.

Used to show the raw bytes and arithmetic so a byte swap is obvious
(0x0E70 = 3696 is a sensible cell code, 0x700E = 28686 is not).

Both PECs follow ``ADBMS6830/lib/src/adBms6830GenericType.c``. CRC15_TABLE
is generated from the polynomial instead of copied; tests/test_console.py
checks it against ADI's table. No I/O.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

#: CRC15 polynomial used by the ADBMS6830 (x^15+x^14+x^10+x^8+x^7+x^4+x^3+1).
CRC15_POLY = 0x4599
#: Both PECs seed with 16.
PEC_SEED = 16
#: CRC10 polynomial: x^10+x^7+x^3+x^2+x+1, low ten bits.
CRC10_POLY = 0x8F


def _build_crc15_table() -> Tuple[int, ...]:
    table = []
    for index in range(256):
        remainder = index << 7
        for _ in range(8):
            if remainder & 0x4000:
                remainder = ((remainder << 1) ^ CRC15_POLY) & 0xFFFF
            else:
                remainder = (remainder << 1) & 0xFFFF
        table.append(remainder & 0xFFFF)
    return tuple(table)


CRC15_TABLE = _build_crc15_table()


def pec15(data: bytes) -> int:
    """Command PEC, exactly as ``Pec15_Calc()`` computes it.

    Returns the 16-bit value that goes on the wire (already shifted left
    one bit, so the LSB is always 0).
    """
    remainder = PEC_SEED
    for byte in bytes(data):
        addr = ((remainder >> 7) ^ byte) & 0xFF
        remainder = ((remainder << 8) ^ CRC15_TABLE[addr]) & 0xFFFF
    return (remainder * 2) & 0xFFFF


def pec10(data: bytes, is_rx: bool = False, command_counter: int = 0) -> int:
    """Data PEC, exactly as ``pec10_calc()`` computes it.

    ``is_rx`` selects the received-data variant, which folds the top six
    bits of the command-counter byte into the remainder before the final
    six shifts.  ``command_counter`` is that byte; on the wire it is the
    byte immediately after the payload.
    """
    remainder = PEC_SEED
    for byte in bytes(data):
        remainder ^= (byte << 2) & 0xFFFF
        for _ in range(8):
            if remainder & 0x200:
                remainder = ((remainder << 1) ^ CRC10_POLY) & 0xFFFF
            else:
                remainder = (remainder << 1) & 0xFFFF
    if is_rx:
        remainder ^= ((command_counter & 0xFC) << 2) & 0xFFFF
    for _ in range(6):
        if remainder & 0x200:
            remainder = ((remainder << 1) ^ CRC10_POLY) & 0xFFFF
        else:
            remainder = (remainder << 1) & 0xFFFF
    return remainder & 0x3FF


# ---------------------------------------------------------------------------
# Command opcodes, transcribed from adBms6830CmdList.h
# ---------------------------------------------------------------------------

OPCODES: Dict[str, Tuple[int, int]] = {
    "WRCFGA": (0x00, 0x01), "WRCFGB": (0x00, 0x24),
    "RDCFGA": (0x00, 0x02), "RDCFGB": (0x00, 0x26),
    "RDCVA": (0x00, 0x04), "RDCVB": (0x00, 0x06), "RDCVC": (0x00, 0x08),
    "RDCVD": (0x00, 0x0A), "RDCVE": (0x00, 0x09), "RDCVF": (0x00, 0x0B),
    "RDCVALL": (0x00, 0x0C),
    "RDACA": (0x00, 0x44), "RDACB": (0x00, 0x46), "RDACC": (0x00, 0x48),
    "RDACD": (0x00, 0x4A), "RDACE": (0x00, 0x49), "RDACF": (0x00, 0x4B),
    "RDACALL": (0x00, 0x4C),
    "RDSVA": (0x00, 0x03), "RDSVB": (0x00, 0x05), "RDSVC": (0x00, 0x07),
    "RDSVD": (0x00, 0x0D), "RDSVE": (0x00, 0x0E), "RDSVF": (0x00, 0x0F),
    "RDSALL": (0x00, 0x10), "RDCSALL": (0x00, 0x11), "RDACSALL": (0x00, 0x51),
    "RDASALL": (0x00, 0x35),
    "RDFCA": (0x00, 0x12), "RDFCB": (0x00, 0x13), "RDFCC": (0x00, 0x14),
    "RDFCD": (0x00, 0x15), "RDFCE": (0x00, 0x16), "RDFCF": (0x00, 0x17),
    "RDFCALL": (0x00, 0x18),
    "RDAUXA": (0x00, 0x19), "RDAUXB": (0x00, 0x1A), "RDAUXC": (0x00, 0x1B),
    "RDAUXD": (0x00, 0x1F),
    "RDRAXA": (0x00, 0x1C), "RDRAXB": (0x00, 0x1D), "RDRAXC": (0x00, 0x1E),
    "RDRAXD": (0x00, 0x25),
    "RDSTATA": (0x00, 0x30), "RDSTATB": (0x00, 0x31), "RDSTATC": (0x00, 0x32),
    "RDSTATD": (0x00, 0x33), "RDSTATE": (0x00, 0x34),
    "SNAP": (0x00, 0x2D), "UNSNAP": (0x00, 0x2F), "RDSID": (0x00, 0x2C),
    "CLRCELL": (0x07, 0x11), "CLRFC": (0x07, 0x14), "CLOVUV": (0x07, 0x15),
    "CLRSPIN": (0x07, 0x16), "CLRFLAG": (0x07, 0x17),
    "PLADC": (0x07, 0x18), "PLAUT": (0x07, 0x19),
}

#: SPI opcodes each menu command makes the firmware send, in order (from
#: ``adBms_Application.c``). ADCV/ADAX are built at runtime from config
#: bits, so they're marked with * and have no fixed opcode.
MENU_OPCODES: Dict[int, List[str]] = {
    0: [],
    1: ["WRCFGA", "WRCFGB", "RDCFGA", "RDCFGB"],
    2: ["RDCFGA", "RDCFGB"],
    3: ["ADCV*"],
    4: ["ADCV*", "SNAP", "RDCVA", "RDCVB", "RDCVC", "RDCVD", "RDCVE", "RDCVF", "UNSNAP"],
    5: ["ADSV*"],
    6: ["RDSVA", "RDSVB", "RDSVC", "RDSVD", "RDSVE", "RDSVF"],
    7: ["ADCV*"],
    8: ["RDACA", "RDACB", "RDACC", "RDACD", "RDACE", "RDACF"],
    9: ["ADCV*"],
    10: ["RDFCA", "RDFCB", "RDFCC", "RDFCD", "RDFCE", "RDFCF"],
    11: ["ADAX*"],
    12: ["RDAUXA", "RDAUXB", "RDAUXC", "RDAUXD"],
    13: ["ADAX2*"],
    14: ["RDRAXA", "RDRAXB", "RDRAXC", "RDRAXD"],
    15: ["WRCFGA", "WRCFGB", "ADAX*", "ADCV*",
         "RDSTATA", "RDSTATB", "RDSTATC", "RDSTATD", "RDSTATE"],
    16: ["WRCFGA", "WRCFGB", "ADCV*", "(loop x4)"],
    17: ["CLRCELL"], 18: ["CLRFLAG"], 19: ["CLRSPIN"], 20: ["CLRFC"],
    21: ["WRCFGA", "WRCFGB"],
    22: ["RDSID"],
}


def opcode_bytes(name: str) -> Optional[bytes]:
    """``"RDCVA"`` -> ``b"\\x00\\x04"``, or None for a runtime-built command."""
    entry = OPCODES.get(name.upper().rstrip("*"))
    if entry is None:
        return None
    return bytes(entry)


def command_frame(name: str) -> Optional[bytes]:
    """The four bytes the firmware clocks out for a command: the two
    opcode bytes followed by their PEC15, big-endian on the wire."""
    opcode = opcode_bytes(name)
    if opcode is None:
        return None
    pec = pec15(opcode)
    return opcode + bytes(((pec >> 8) & 0xFF, pec & 0xFF))


def describe_menu_command(command: int) -> List[Tuple[str, Optional[bytes]]]:
    """``[(opcode_name, four_wire_bytes_or_None), ...]`` for a menu command."""
    out = []
    for name in MENU_OPCODES.get(command, []):
        out.append((name, command_frame(name)))
    return out


# ---------------------------------------------------------------------------
# Register-group decoding, for the "are the bytes backwards?" question
# ---------------------------------------------------------------------------

def decode_cell_group(data: bytes, little_endian: bool = True) -> List[dict]:
    """Interpret a register-group payload as consecutive int16 cell codes.

    The ADBMS6830 sends cell codes little-endian (low byte first), so
    ``70 0E`` is 0x0E70 = 3696 -> 3.6960 V. ``little_endian=False`` shows
    the swapped reading, which the console's ``decode`` command prints
    alongside for comparison.
    """
    out = []
    order = "little" if little_endian else "big"
    for index in range(0, len(data) - 1, 2):
        raw = int.from_bytes(data[index:index + 2], order, signed=True)
        out.append({
            "index": index // 2 + 1,
            "raw": raw,
            "hex": "0x%04X" % (raw & 0xFFFF),
            "volts": (raw + 10000) * 0.000150,
        })
    return out


def check_rx_group(data: bytes) -> dict:
    """Verify a full received register group: 6 payload bytes, then a
    2-byte word carrying the command counter and the 10-bit PEC.

    Wire layout per the datasheet and ``adBmsReadData()``: the last two
    bytes are ``[cc(6) | pec(9:8)] [pec(7:0)]``.
    """
    if len(data) != 8:
        return {"ok": False, "error": "a register group is 8 bytes (6 data + 2 PEC/CC), got %d"
                                      % len(data)}
    payload = bytes(data[:6])
    counter_byte = data[6]
    received = ((data[6] & 0x03) << 8) | data[7]
    calculated = pec10(payload, is_rx=True, command_counter=counter_byte)
    return {
        "ok": received == calculated,
        "payload": payload,
        "cmd_counter": (counter_byte >> 2) & 0x3F,
        "pec_rx": received,
        "pec_calc": calculated,
        "cells": decode_cell_group(payload, little_endian=True),
        "cells_swapped": decode_cell_group(payload, little_endian=False),
    }
