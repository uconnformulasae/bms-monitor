"""Interactive debug console: raw bytes and the decode, side by side.

ADI's demo firmware prints text, so the SPI bytes and PEC10 are not
observable over serial. The TX opcodes shown are computed locally, and
``decode``/``rx`` are for bytes captured some other way (e.g. a logic
analyser).

This is the only module allowed to call ``encode_console_command`` (a test
checks this). It opens its own link instead of using ``TelemetrySource``, so
the polling path stays read-only. Commands 17-20 are blocked here too.
"""

from __future__ import annotations

import math
import sys
import time
from typing import List, Optional

from .parsing.adi_pec import (
    check_rx_group,
    decode_cell_group,
    describe_menu_command,
    pec10,
    pec15,
)
from .parsing.adi_text import parse_line
from .parsing.blocks import BlockAssembler
from .parsing.lines import LineAssembler
from .model import CellRecord, EventRecord, StatusRecord, is_plausible_voltage
from .sources import command_gate

#: How long to keep reading after the last byte before calling a response
#: finished.  The firmware answers in one burst.
QUIET_S = 0.35
#: Give up on a response after this long.
RESPONSE_TIMEOUT_S = 4.0
#: Hard cap on one response. Firmware stuck in the scanf loop reprints
#: "Enter cmd:" nonstop, so a time limit alone can collect megabytes.
MAX_RESPONSE_BYTES = 64 * 1024
#: How much of a response to hex-dump before summarising the rest.
MAX_HEXDUMP_BYTES = 1024
#: How many decoded lines to print before summarising the rest.
MAX_DECODE_LINES = 400


# ---------------------------------------------------------------------------
# Link to the board. Anything with write(), read() and close() works.
# ---------------------------------------------------------------------------

class SerialLink:
    """A real board over pyserial (including ``socket://``)."""

    def __init__(self, port: str, baud: int = 115200):
        import serial

        from .sources.serial_source import list_candidate_ports

        if port == "auto":
            candidates = list_candidate_ports()
            if not candidates:
                raise RuntimeError(
                    "no Nucleo serial port found (looked for usbmodem/ttyACM and "
                    "ST-LINK VID:PID 0483:374B)")
            port = candidates[0]["device"]
        self.name = port
        self._serial = serial.serial_for_url(port, baudrate=baud, timeout=0.1,
                                             write_timeout=1.0)
        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def read(self) -> bytes:
        try:
            waiting = self._serial.in_waiting
        except Exception:
            waiting = 0
        return self._serial.read(max(1, min(waiting, 8192)))

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def hexdump(data: bytes, width: int = 16, indent: str = "    ") -> List[str]:
    """Offset / hex / printable-ASCII dump."""
    out = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hexpart = " ".join("%02x" % b for b in chunk)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append("%s%04x  %-*s  |%s|"
                   % (indent, offset, width * 3 - 1, hexpart, asciipart))
    return out


def parse_hex(tokens: List[str]) -> Optional[bytes]:
    """Parse pasted hex like ``70 0E 6C``, ``700E6C`` or ``0x70,0x0e``."""
    text = "".join(tokens).replace("0x", "").replace("0X", "")
    for junk in (",", ":", "-", "_", " ", "\t"):
        text = text.replace(junk, "")
    if not text or len(text) % 2 or any(c not in "0123456789abcdefABCDEF" for c in text):
        return None
    return bytes.fromhex(text)


def format_tx(command: int, payload: bytes) -> List[str]:
    out = ["TX  %s   %s   menu command %d"
           % (payload.hex(" ").upper(),
              "".join(chr(b) if 32 <= b < 127 else "." for b in payload),
              command)]
    opcodes = describe_menu_command(command)
    if opcodes:
        out.append("    SPI commands the firmware will issue for this "
                   "(computed locally, NOT observed):")
        for name, frame in opcodes:
            if frame is None:
                out.append("      %-10s built at runtime from configuration bits"
                           % name)
            else:
                out.append("      %-10s %s        opcode + PEC15"
                           % (name, frame.hex(" ").upper()))
    return out


def format_decode(records, cells_per_ic: int = 0) -> List[str]:
    """The DECODE block: voltage, raw code in decimal and hex, flags."""
    out = []
    for record in records:
        if isinstance(record, CellRecord):
            flag = "OK" if record.pec_ok else "PEC ERROR"
            out.append("  IC%-3d %-6s ccount=%-4s PECError=%s   [%s]"
                       % (record.ic, record.kind,
                          record.ccount if record.ccount is not None else "?",
                          record.pec_error if record.pec_error is not None else "?",
                          flag))
            if not record.complete:
                out.append("        line incomplete -- not a measurement")
            for index, volts in enumerate(record.values):
                if cells_per_ic and index >= cells_per_ic:
                    break
                if volts is None:
                    out.append("        c%-3d  (did not parse)" % (index + 1))
                    continue
                if not math.isfinite(volts):
                    out.append("        c%-3d  (non-finite voltage)" % (index + 1))
                    continue
                code = int(round(volts / 0.000150)) - 10000
                warn = "" if is_plausible_voltage(volts) else "   <-- IMPLAUSIBLE"
                out.append("        c%-3d  %10.6f V   code %6d = 0x%04X   "
                           "wire %s%s"
                           % (index + 1, volts, code, code & 0xFFFF,
                              (code & 0xFFFF).to_bytes(2, "little").hex(" ").upper(),
                              warn))
        elif isinstance(record, StatusRecord):
            if record.group:
                out.append("  Status %s:" % record.group)
            elif record.fields:
                out.append("        " + "  ".join(
                    "%s=%s" % (k, v) for k, v in sorted(record.fields.items())))
        elif isinstance(record, EventRecord):
            if record.kind in ("blank", "menu"):
                continue
            out.append("  [%s] %s" % (record.kind, record.text))
        else:
            out.append("  [unparsed] %r" % (getattr(record, "text", record),))
    return out or ["  (nothing decodable in this response)"]


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

HELP = """
commands
  <n>            send ADBMS6830 menu command n and show TX / RX / DECODE
  menu           list the menu commands and which ones are blocked
  decode <hex>   read bytes as int16 cell codes, LITTLE vs BIG endian
  rx <hex>       verify an 8-byte register group (6 data + CC/PEC10)
  pec15 <hex>    command PEC, as Pec15_Calc() computes it
  pec10 <hex>    data PEC (transmit variant), as pec10_calc() computes it
  hex            toggle the raw RX hex dump on/off
  quit           leave
"""


class DebugConsole:
    def __init__(self, link, cells_per_ic: int = 0, show_hex: bool = True,
                 out=None, assume_yes: bool = False, ask=None, reader=None):
        self.link = link
        self.cells_per_ic = cells_per_ic
        self.show_hex = show_hex
        self.out = out or sys.stdout
        self.assume_yes = assume_yes
        # ask/reader are injectable so tests never block on a real input().
        self.truncated = False
        self.ask = ask or (lambda prompt: input(prompt))
        self.reader = reader or (lambda: input("> "))
        self._assembler = LineAssembler()
        # Fold the reformatted firmware's titled blocks the same way the
        # telemetry pipeline does, or DECODE would miss them.
        self._blocks = BlockAssembler()

    # -- plumbing ----------------------------------------------------------

    def say(self, *lines) -> None:
        for line in lines:
            self.out.write(str(line) + "\n")
        self.out.flush()

    def banner(self) -> None:
        self.say(
            "bms-monitor debug console",
            "  link : %s" % getattr(self.link, "name", "?"),
            "",
            "Shows the bytes and the arithmetic, which is what a dashboard",
            "cannot: a swapped pair still reads as a plausible voltage.",
            "",
            "Note on what is observable: ADI's demo firmware prints formatted",
            "TEXT, not raw register bytes, so the SPI payload and the chip's",
            "PEC10 are not visible from the PC over this interface -- the",
            "firmware checks the PEC internally and prints a 0/1 flag. The TX",
            "opcode frames below are computed locally and labelled as such.",
            "Use 'decode' / 'rx' for bytes captured another way.",
            "",
            "Type 'help' for commands, 'quit' to leave.",
            "",
        )

    # -- transport ---------------------------------------------------------

    def drain(self, quiet: float = QUIET_S, timeout: float = RESPONSE_TIMEOUT_S,
              max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
        """Read until the link goes quiet, times out, or hits the byte cap.

        The byte cap is for a stuck firmware that never stops printing.
        """
        buf = bytearray()
        started = time.monotonic()
        last = started
        self.truncated = False
        while True:
            chunk = self.link.read()
            now = time.monotonic()
            if chunk:
                buf += chunk
                last = now
            if len(buf) >= max_bytes:
                self.truncated = True
                break
            if (now - last) >= quiet and buf:
                break
            if (now - started) >= timeout:
                break
            if not chunk:
                time.sleep(0.01)
        return bytes(buf)

    def dump(self, data: bytes, limit: int = MAX_HEXDUMP_BYTES) -> None:
        for line in hexdump(data[:limit]):
            self.say(line)
        if len(data) > limit:
            self.say("    ... %d more bytes not shown" % (len(data) - limit))

    # -- command handling --------------------------------------------------

    def send_menu_command(self, command: int) -> None:
        try:
            payload = command_gate.encode_console_command(command)
        except command_gate.CommandRejected as exc:
            self.say("refused: %s" % exc)
            return

        warning = command_gate.console_warning(command)
        if warning:
            self.say("", "!! %s" % warning)
            if not self.assume_yes:
                try:
                    answer = self.ask("   send command %d anyway? [y/N] " % command)
                except (EOFError, KeyboardInterrupt):
                    self.say("")
                    return
                if answer.strip().lower() not in ("y", "yes"):
                    self.say("   not sent.")
                    return

        # Clear anything already queued so the response is unambiguous.
        self.drain(quiet=0.05, timeout=0.2)
        self._assembler.reset()
        self._blocks.reset()

        started = time.monotonic()
        self.link.write(payload)
        data = self.drain()
        elapsed = time.monotonic() - started

        self.say("")
        for line in format_tx(command, payload):
            self.say(line)
        self.say("RX  %d bytes in %.3f s%s"
                 % (len(data), elapsed,
                    "   (capped -- the board is still talking; a wedged "
                    "firmware does this)" if getattr(self, "truncated", False) else ""))
        if not data:
            self.say("    (nothing came back within %.1f s)" % RESPONSE_TIMEOUT_S)
            return
        if self.show_hex:
            self.dump(data)
        records = []
        for text, trunc in self._assembler.feed(data):
            if trunc:
                records.append(parse_line(text, truncated=True))
                continue
            for line in self._blocks.feed_line(text):
                records.append(parse_line(line))
        for line in self._blocks.flush():
            records.append(parse_line(line))
        self.say("DECODE")
        lines = format_decode(records, self.cells_per_ic)
        for line in lines[:MAX_DECODE_LINES]:
            self.say(line)
        if len(lines) > MAX_DECODE_LINES:
            self.say("  ... %d more decoded line(s) not shown"
                     % (len(lines) - MAX_DECODE_LINES))

    def cmd_decode(self, tokens) -> None:
        data = parse_hex(tokens)
        if not data or len(data) < 2:
            self.say("usage: decode 70 0E 6C 0E 68 0E   (an even number of bytes)")
            return
        little = decode_cell_group(data, little_endian=True)
        big = decode_cell_group(data, little_endian=False)
        self.say("",
                 "%d bytes, %d cell code(s).  The ADBMS6830 sends cell codes"
                 % (len(data), len(little)),
                 "LITTLE-endian, so that column is the correct reading.",
                 "",
                 "  cell  bytes    little-endian (correct)          "
                 "big-endian (swapped)",
                 "  ----  -------  -------------------------------  "
                 "-------------------------------")
        for lo, hi in zip(little, big):
            pair = data[(lo["index"] - 1) * 2:(lo["index"] - 1) * 2 + 2]
            self.say("  c%-3d  %s    %s = %7d = %9.6f V   %s = %7d = %9.6f V%s"
                     % (lo["index"], pair.hex(" ").upper(),
                        lo["hex"], lo["raw"], lo["volts"],
                        hi["hex"], hi["raw"], hi["volts"],
                        "" if is_plausible_voltage(hi["volts"]) else "  <-- impossible"))

        self.say("",
                 "volts = (code + 10000) * 0.000150   [getVoltage(), "
                 "serialPrintResult.c]")

        # A byte swap doesn't always give an out-of-range voltage (3.70 V is
        # 0x394B, and 0x4B39 is a valid 4.3886 V). What it does break is the
        # clustering: real cells sit within tens of mV of each other, swapped
        # ones scatter across volts. So compare the spread of both readings.
        if len(little) >= 2:
            lo_spread = (max(c["volts"] for c in little)
                         - min(c["volts"] for c in little)) * 1000
            hi_spread = (max(c["volts"] for c in big)
                         - min(c["volts"] for c in big)) * 1000
            self.say("",
                     "  spread, little-endian : %9.1f mV" % lo_spread,
                     "  spread, big-endian    : %9.1f mV" % hi_spread)
            if hi_spread > max(lo_spread * 4.0, 50.0):
                self.say("  -> the little-endian reading is coherent and the "
                         "swapped one is not.")
            elif lo_spread > max(hi_spread * 4.0, 50.0):
                self.say("  -> WARNING: the SWAPPED reading is the coherent one. "
                         "These bytes look byte-order-reversed.")
            else:
                self.say("  -> inconclusive from spread alone with this few "
                         "bytes; compare against the rest of the pack.")
        self.say("",
                 "A swap does NOT always give an out-of-range value -- 3.70 V is",
                 "code 0x394B and 0x4B39 decodes to a representable 4.3886 V. What",
                 "it always destroys is coherence: real cells cluster within tens",
                 "of millivolts, swapped ones scatter across volts.",
                 "")

    def cmd_rx(self, tokens) -> None:
        data = parse_hex(tokens)
        if not data:
            self.say("usage: rx 70 0E 6C 0E 68 0E 5C FC   (8 bytes)")
            return
        result = check_rx_group(data)
        if not result["ok"] and "error" in result:
            self.say(result["error"])
            return
        self.say("",
                 "  bytes        %s" % data.hex(" ").upper(),
                 "  payload      %s" % result["payload"].hex(" ").upper(),
                 "  cmd counter  %d" % result["cmd_counter"],
                 "  PEC10 rx     0x%03X" % result["pec_rx"],
                 "  PEC10 calc   0x%03X" % result["pec_calc"],
                 "  verdict      %s" % ("OK" if result["ok"] else "MISMATCH -- "
                                        "corrupted frame, or the wrong bytes, "
                                        "or a PEC implementation bug"))
        for cell in result["cells"]:
            self.say("    c%-3d %s = %7d = %9.6f V"
                     % (cell["index"], cell["hex"], cell["raw"], cell["volts"]))
        self.say("")

    def cmd_pec(self, which: str, tokens) -> None:
        data = parse_hex(tokens)
        if not data:
            self.say("usage: %s 00 04" % which)
            return
        if which == "pec15":
            value = pec15(data)
            self.say("  pec15(%s) = 0x%04X   wire: %s %s"
                     % (data.hex(" ").upper(), value,
                        data.hex(" ").upper(),
                        bytes(((value >> 8) & 0xFF, value & 0xFF)).hex(" ").upper()))
        else:
            value = pec10(data, is_rx=False)
            self.say("  pec10(%s) = 0x%03X   (transmit variant)"
                     % (data.hex(" ").upper(), value))

    def cmd_menu(self) -> None:
        self.say("", "ADBMS6830 menu commands available in this console:")
        for command in sorted(command_gate.CONSOLE_COMMANDS):
            name = command_gate.COMMAND_NAMES.get(command)
            if name is None:
                name = {1: "Write and read configuration",
                        16: "Loop measurements (4 cycles, then reprints the menu)",
                        21: "Write configuration"}.get(command, "")
            mark = " [WRITES CONFIG]" if command in command_gate.CONSOLE_WRITES_CONFIG else ""
            self.say("  %2d  %s%s" % (command, name, mark))
        self.say("", "Blocked here and in the polling loop:")
        for command in sorted(command_gate.CONSOLE_DENIED):
            self.say("  %2d  %s" % (command, command_gate.CONSOLE_DENIED[command]))
        self.say("")

    # -- REPL --------------------------------------------------------------

    def handle(self, text: str) -> bool:
        """Process one input line.  Returns False to exit."""
        text = text.strip()
        if not text:
            return True
        parts = text.split()
        word = parts[0].lower()

        if word in ("quit", "exit", "q"):
            return False
        if word in ("help", "?", "h"):
            self.say(HELP)
            return True
        if word == "menu":
            self.cmd_menu()
            return True
        if word == "hex":
            self.show_hex = not self.show_hex
            self.say("raw RX hex dump %s" % ("on" if self.show_hex else "off"))
            return True
        if word == "decode":
            self.cmd_decode(parts[1:])
            return True
        if word == "rx":
            self.cmd_rx(parts[1:])
            return True
        if word in ("pec15", "pec10"):
            self.cmd_pec(word, parts[1:])
            return True
        if word.isdigit():
            self.send_menu_command(int(word))
            return True

        self.say("unknown: %r  (type 'help')" % text)
        return True

    def run(self) -> int:
        self.banner()
        # Show whatever the board printed on connect, e.g. the boot menu.
        initial = self.drain(quiet=0.3, timeout=1.5)
        if initial:
            self.say("RX on connect: %d bytes" % len(initial))
            if self.show_hex:
                self.dump(initial, limit=256)
            for text, _trunc in self._assembler.feed(initial):
                self._blocks.feed_line(text)
            self.say("")
        while True:
            try:
                text = self.reader()
            except (EOFError, KeyboardInterrupt):
                self.say("")
                break
            try:
                if not self.handle(text):
                    break
            except Exception as exc:
                # Don't let one bad command kill the console.
                self.say("error: %r" % (exc,))
        return 0


def build_link(args):
    """Open the link the console should talk over."""
    if args.source == "serial":
        return SerialLink(args.port, args.baud)
    raise RuntimeError(
        "the debug console only works with --source serial "
        "(try --console --source serial --port auto)")


def main(args, link=None) -> int:
    try:
        if link is None:
            link = build_link(args)
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    console = DebugConsole(link, cells_per_ic=args.cells_per_ic)
    try:
        return console.run()
    finally:
        link.close()
