"""Command line entry point.

All settings are read once at startup. The poll plan is parsed and checked
here and does not change while the tool runs; nothing over HTTP can change
which commands get sent.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import sys
import threading
import time
import webbrowser

from . import model
from .hub import Hub
from .logging_csv import CsvLogger, TempCsvLogger, temps_path_for
from .sources import command_gate
from .web.server import MonitorServer

DEFAULT_HTTP_PORT = 8765

# Optional local development tools (not included in this repo). When they
# are present they add extra --source options.
_dev = None
if importlib.util.find_spec("bms_monitor.simfw") is not None:
    from .simfw import devhooks as _dev


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m bms_monitor",
        description="Read-only monitor for the ADBMS6830 BMS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 -m bms_monitor --source can --can-interface slcan \\\n"
            "      --can-channel /dev/cu.usbserial-XXXX --ics 2 --cells-per-ic 10\n"
            "  python3 -m bms_monitor --source serial --port auto\n"
            "  python3 -m bms_monitor --source replay --file run.csv\n"
            + (_dev.EPILOG if _dev else "")
        ),
    )
    sources = ["serial", "can", "replay"]
    if _dev:
        sources = list(_dev.SOURCES) + sources
    parser.add_argument(
        "--source", default=sources[0], choices=tuple(sources),
        help="data source (default: %s)" % sources[0],
    )
    if _dev:
        _dev.add_arguments(parser)

    grp = parser.add_argument_group("replay")
    grp.add_argument("--file", default=None, metavar="PATH",
                     help="CSV to replay (required for --source replay)")
    grp.add_argument("--replay-stream", action="store_true",
                     help="stream at wall-clock speed instead of loading the "
                          "whole file at once (scrubbing works either way)")
    grp.add_argument("--replay-speed", type=float, default=1.0,
                     help="playback speed for --replay-stream (default: 1.0)")

    grp = parser.add_argument_group("serial")
    grp.add_argument("--port", default="auto",
                     help="'auto' to scan for a Nucleo VCP, a device path, or any "
                          "pyserial URL such as socket://127.0.0.1:5555 or loop://")
    grp.add_argument("--baud", type=int, default=115200, help="baud rate (default: 115200)")
    grp.add_argument("--cmd-timeout", type=float, default=1.5,
                     help="seconds to wait for a response before re-issuing (default: 1.5)")
    grp.add_argument("--max-rate", type=float, default=50.0,
                     help="ceiling on command re-issue rate in Hz (default: 50, "
                          "above what 115200 can deliver; 0 = unlimited)")

    grp = parser.add_argument_group("can")
    grp.add_argument("--can-interface", default="socketcan",
                     help="python-can interface: socketcan, slcan, pcan, "
                          "virtual, ... (default: socketcan)")
    grp.add_argument("--can-channel", default="can0",
                     help="python-can channel (default: can0)")
    grp.add_argument("--can-bitrate", type=int, default=250000,
                     help="bus bitrate (default: 250000, the F105 controller's CAN2)")
    grp.add_argument("--allow-tx", action="store_true",
                     help="allow transmitting the balance test toggle (0x740) from "
                          "the dashboard; without it the tool never transmits")

    grp = parser.add_argument_group("acquisition")
    grp.add_argument("--poll", default="4",
                     help="comma-separated menu commands to cycle (default: 4). "
                          "Only read/measure commands are accepted; see --list-commands.")
    grp.add_argument("--status-poll", action="store_true",
                     help="also poll command 15 for the fault panel. Off by default "
                          "because command 15 writes WRCFGA/WRCFGB before it reads "
                          "(see docs/serial.md).")
    grp.add_argument("--warmup", type=int, default=2,
                     help="poll cycles after link-up to mark stale and not plot "
                          "(default: 2)")
    grp.add_argument("--ics", type=int, default=1, metavar="N",
                     help="number of ADBMS6830s in the daisy chain (default: 1)")
    grp.add_argument("--cells-per-ic", type=int, default=model.CELL_COUNT,
                     metavar="N",
                     help="cells wired to each IC (default: 16). Unused channels "
                          "read garbage and latch UV flags, so set this to what is "
                          "actually wired. Over CAN it must match BMS_CHANNEL_MASK "
                          "in the F105 firmware (0x03FF = 10).")
    grp.add_argument("--ics-per-segment", type=int, default=2, metavar="N",
                     help="ADBMS6830s on one segment board (default: 2). Used to "
                          "group cells by board in the display.")

    grp = parser.add_argument_group("display only (never sent to the BMS)")
    grp.add_argument("--uv", type=float, default=model.DEFAULT_UV_THRESHOLD,
                     help="under-voltage line on the charts (default: 3.0)")
    grp.add_argument("--ov", type=float, default=model.DEFAULT_OV_THRESHOLD,
                     help="over-voltage line on the charts (default: 4.2)")
    grp.add_argument("--warn-spread", type=float, default=30.0, metavar="MV",
                     help="cell spread in mV that colours the tile warning "
                          "(default: 30)")
    grp.add_argument("--crit-spread", type=float, default=60.0, metavar="MV",
                     help="cell spread in mV that colours the tile critical "
                          "(default: 60)")
    grp.add_argument("--ntc-beta", type=float, default=3435.0,
                     help="thermistor beta / B25-85 (default: 3435)")
    grp.add_argument("--ntc-r25", type=float, default=10000.0,
                     help="thermistor resistance at 25 C in ohms (default: 10000)")
    grp.add_argument("--ntc-series", type=float, default=10000.0,
                     help="divider series resistor in ohms (default: 10000)")
    grp.add_argument("--ntc-vref", type=float, default=3.0,
                     help="divider reference, i.e. VREF2 (default: 3.0)")
    grp.add_argument("--ntc-pulldown", action="store_true",
                     help="NTC to VREF2 and series resistor to ground "
                          "(default is the other way round)")

    grp = parser.add_argument_group("output")
    grp.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                     help="local HTTP port (default: %d)" % DEFAULT_HTTP_PORT)
    grp.add_argument("--csv", default=None, metavar="PATH",
                     help="append samples to a CSV file (~5 MB/hour at 10 Hz); CAN "
                          "thermistor readings go to <name>_temps.csv beside it")
    grp.add_argument("--history", type=int, default=18000,
                     help="samples kept in memory (default: 18000, ~30 min at 10 Hz)")
    grp.add_argument("--no-browser", action="store_true", help="do not open a browser")
    grp.add_argument("--duration", type=float, default=0.0,
                     help="exit after N seconds (0 = run until Ctrl-C)")

    parser.add_argument("--console", action="store_true",
                        help="interactive debug console (serial only): send a "
                             "menu command and see the raw bytes, the SPI opcodes "
                             "with their PEC15, and the decoded values")
    parser.add_argument("--list-commands", action="store_true",
                        help="print the command allow-list and exit")
    parser.add_argument("--can-channels", action="store_true",
                        help="print the CAN channel table for Race Studio and exit")
    return parser


def _print_commands() -> None:
    print("Allowed (read / measure only):")
    for cmd in sorted(command_gate.ALLOWED_READ_COMMANDS):
        note = command_gate.COMMAND_NAMES.get(cmd, "")
        star = " *" if cmd in command_gate.MEASUREMENT_COMMANDS else "  "
        print("  %2d%s %s" % (cmd, star, note))
    print("\nDenied (this tool will not send these):")
    for cmd in sorted(command_gate.DENIED_COMMANDS):
        print("  %2d   %s" % (cmd, command_gate.DENIED_COMMANDS[cmd]))
    print("\n  * starts an ADC conversion. That is a measurement, not a")
    print("    configuration change, but the firmware only measures when asked.")
    print("    See docs/serial.md.")
    print()
    print("The interactive console (--console) additionally allows 1, 16 and 21")
    print("after confirming. 17-20 are blocked everywhere: they clear registers")
    print("and can erase the fault you are diagnosing.")


def _build_source(args, poll_plan, tx_control=None):
    if _dev and args.source in _dev.SOURCES:
        return _dev.build_source(args, poll_plan)
    if args.source == "serial":
        from .sources.serial_source import SerialSource
        return SerialSource(port=args.port, baud=args.baud, poll_plan=poll_plan,
                            cmd_timeout=args.cmd_timeout, warmup=args.warmup,
                            max_rate_hz=args.max_rate, total_ic=args.ics,
                            cells_per_ic=args.cells_per_ic)
    if args.source == "replay":
        from .sources.replay import ReplaySource
        if not args.file:
            raise SystemExit("--source replay needs --file PATH")
        return ReplaySource(args.file, load_all=not args.replay_stream,
                            speed=args.replay_speed,
                            limit=max(100, args.history))
    if args.source == "can":
        from .sources.can_source import CanSource
        return CanSource(channel=args.can_channel, interface=args.can_interface,
                         bitrate=args.can_bitrate, total_ic=args.ics,
                         cells_per_ic=args.cells_per_ic, warmup=args.warmup,
                         tx_control=tx_control)
    raise SystemExit("unknown source %r" % args.source)


def _ui_config(args, poll_plan) -> dict:
    """Fixed settings for the browser. Display only; none of it is sent to the BMS."""
    return {
        "uv": args.uv,
        "ov": args.ov,
        "warn_spread_mv": args.warn_spread,
        "crit_spread_mv": args.crit_spread,
        "ics": args.ics,
        "cells_per_ic": args.cells_per_ic,
        "ics_per_segment": args.ics_per_segment,
        "poll_plan": list(poll_plan),
        "status_polled": 15 in poll_plan,
        "aux_polled": bool({11, 12} & set(poll_plan)),
        "ntc": {
            "beta": args.ntc_beta,
            "r25": args.ntc_r25,
            "series": args.ntc_series,
            "vref": args.ntc_vref,
            "pullup": not args.ntc_pulldown,
        },
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_commands:
        _print_commands()
        return 0
    if args.can_channels:
        from .sources.can_frames import race_studio_rows
        for line in race_studio_rows():
            print(line)
        return 0
    if args.console:
        from . import console as console_mod
        link = None
        if _dev and args.source in _dev.SOURCES:
            link = _dev.console_link(args)
        return console_mod.main(args, link=link)
    if _dev:
        code = _dev.early_exit(args)
        if code is not None:
            return code

    # The poll plan is checked once here and then fixed for the whole run.
    try:
        poll_plan = list(command_gate.parse_poll_plan(args.poll))
        if args.status_poll and 15 not in poll_plan:
            poll_plan.append(15)
        poll_plan = list(command_gate.validate_poll_plan(poll_plan))
    except command_gate.CommandRejected as exc:
        print("error: %s" % exc, file=sys.stderr)
        print("run with --list-commands to see what is allowed", file=sys.stderr)
        return 2

    if args.source == "replay" and not args.file:
        print("error: --source replay requires --file PATH", file=sys.stderr)
        return 2
    if not (0 <= args.http_port <= 65535):
        print("error: --http-port must be between 0 and 65535", file=sys.stderr)
        return 2
    if args.ics < 1 or args.ics > 16:
        print("error: --ics must be between 1 and 16", file=sys.stderr)
        return 2
    if args.cells_per_ic < 1 or args.cells_per_ic > model.CELL_COUNT:
        print("error: --cells-per-ic must be between 1 and %d" % model.CELL_COUNT,
              file=sys.stderr)
        return 2
    if args.ics_per_segment < 1 or args.ics_per_segment > 16:
        print("error: --ics-per-segment must be between 1 and 16",
              file=sys.stderr)
        return 2
    if args.allow_tx and args.source != "can":
        print("error: --allow-tx only works with --source can", file=sys.stderr)
        return 2

    csv_logger = None
    temp_csv_logger = None
    if args.csv:
        try:
            csv_logger = CsvLogger(args.csv)
        except OSError as exc:
            print("error: cannot open CSV %s: %s" % (args.csv, exc), file=sys.stderr)
            return 2
        temp_csv_logger = TempCsvLogger(temps_path_for(args.csv))

    def _close_loggers():
        if csv_logger:
            csv_logger.close()
        if temp_csv_logger:
            temp_csv_logger.close()
    tx_control = None
    if args.allow_tx:
        from .sources.can_command_gate import BalanceTestControl
        tx_control = BalanceTestControl()

    hub = Hub(history=max(100, args.history), csv_logger=csv_logger,
              ui_config=_ui_config(args, poll_plan), temp_csv_logger=temp_csv_logger)

    try:
        source = _build_source(args, poll_plan, tx_control)
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        _close_loggers()
        return 2

    hub.attach(source)

    try:
        server = MonitorServer(hub, host="127.0.0.1", port=args.http_port,
                               tx_control=tx_control)
    except OSError as exc:
        print("error: cannot bind 127.0.0.1:%d (%s)" % (args.http_port, exc), file=sys.stderr)
        print("       another copy may already be running; try --http-port 8766",
              file=sys.stderr)
        _close_loggers()
        return 2

    stopping = threading.Event()

    def shutdown(signum=None, frame=None):
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):        # not the main thread / unsupported
            pass

    server.start()
    try:
        source.start()
    except Exception as exc:
        # No pyserial, no CAN backend, no port: print why and exit.
        print("error: %s" % exc, file=sys.stderr)
        server.stop()
        _close_loggers()
        return 2

    print("bms-monitor")
    if _dev and args.source in _dev.SOURCES:
        print("  source     : %s" % _dev.describe(args))
    else:
        print("  source     : %s" % args.source)
    if args.source == "replay":
        info = source.status()
        print("  file       : %s" % info["file"])
        print("  loaded     : %d cycles, %.1f s, %d IC(s), %d cells/IC"
              % (info["cycles"], info["duration_s"], info["ics"], info["cells_per_ic"]))
        if info["bad_rows"]:
            print("               %d damaged row(s) skipped" % info["bad_rows"])
        if info["last_error"]:
            print("               %s" % info["last_error"])
    elif args.source == "can":
        print("  bus        : %s:%s @ %d" % (args.can_interface, args.can_channel,
                                             args.can_bitrate))
        print("  chain      : %d IC(s) x %d populated cells = %d cells"
              % (args.ics, args.cells_per_ic, args.ics * args.cells_per_ic))
        print("  transmit   : %s" % ("balance test toggle (0x740), from the dashboard"
                                     if tx_control else "off (read-only)"))
    else:
        print("  chain      : %d IC(s) x %d populated cells = %d cells"
              % (args.ics, args.cells_per_ic, args.ics * args.cells_per_ic))
        print("  poll plan  : %s  (read/measure only)"
              % ",".join(str(c) for c in poll_plan))
    if 15 in poll_plan and args.source != "replay":
        print("               note: 15 writes WRCFGA/WRCFGB before reading, see docs/serial.md")
    print("  csv        : %s" % (csv_logger.path if csv_logger else "off"))
    if temp_csv_logger:
        print("  temps csv  : %s  (once the controller sends 0x730)" % temp_csv_logger.path)
    print("  open       : %s" % server.url)
    print("  stop       : Ctrl-C")
    sys.stdout.flush()

    if not args.no_browser:
        # The URL is printed above, so a browser that fails to open is fine.
        threading.Thread(
            target=lambda: _try_open(server.url), name="browser", daemon=True
        ).start()

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        while not stopping.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            stopping.wait(0.25)
    except KeyboardInterrupt:
        pass

    print("\nshutting down ...")
    source.stop()
    server.stop()
    if csv_logger:
        csv_logger.close()
        print("  csv written: %s (%d rows)" % (csv_logger.path, csv_logger.rows))
    if temp_csv_logger:
        temp_csv_logger.close()
        if temp_csv_logger.rows:
            print("  temps csv  : %s (%d rows)" % (temp_csv_logger.path, temp_csv_logger.rows))
    stats = hub.stats()
    print("  samples: %d   pec errors: %d   ccount jumps: %d"
          % (stats["total_samples"], stats["pec_total"], stats["ccount_jumps"]))
    return 0


def _try_open(url: str) -> None:
    time.sleep(0.4)
    try:
        webbrowser.open(url)
    except Exception:
        pass
