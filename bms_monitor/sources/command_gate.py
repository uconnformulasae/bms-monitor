"""Every byte this tool sends to the board is encoded here.

tests/test_readonly.py checks that nothing else writes to a port. Commands
must be a plain allow-listed int and are sent as exactly ``b"<n>\\r"``.

Only digits and one CR are ever sent because adBms_Application.c reads
commands with ``scanf("%d")``: a non-digit stays in the buffer and the loop
spins on ``Enter cmd:`` until the board is reset.

The firmware has no free-running stream, so reading cells means starting
conversions (3/5/7/9/11/13; command 4 does ADCV + SNAP/UNSNAP). The tool
never writes config, touches balancing, clears registers or sends 16.
Command 15 is the exception (see WRITES_CONFIG_SIDE_EFFECT).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Two allow-lists:
#
#   ALLOWED_READ_COMMANDS  the automatic polling loop. Read/measure only.
#                          Every TelemetrySource uses this one.
#   CONSOLE_COMMANDS       the interactive debug console. A person runs one
#                          command at a time and gets a warning before
#                          anything with a side effect.
#
# encode_command() only checks ALLOWED_READ_COMMANDS. The wider set needs
# encode_console_command(), which tests/test_readonly.py checks is only used
# from bms_monitor/console.py.
# ---------------------------------------------------------------------------

#: Opcodes the polling loop may send. Checked against run_command() in
#: adBms_Application.c.
ALLOWED_READ_COMMANDS = frozenset(
    {0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 22}
)

#: Denied commands, with the reason shown in the error.
DENIED_COMMANDS = {
    1: "writes configuration (WRCFGA/WRCFGB) then reads it back",
    16: "LOOP MEASUREMENT -- writes WRCFGA and WRCFGB before looping; "
        "it looks like a read command and is not",
    17: "clears cell registers",
    18: "clears aux registers",
    19: "clears spin registers",
    20: "clears fcell registers",
    21: "writes configuration",
}

#: Commands that start an ADC conversion. Not configuration, but not
#: passive either.
MEASUREMENT_COMMANDS = frozenset({3, 4, 5, 7, 9, 11, 13, 15})

#: Command 15 (read status registers) calls adBmsWriteData(WRCFGA) and
#: adBmsWriteData(WRCFGB) before reading (adBms_Application.c:503-518). It
#: rewrites the config the firmware already holds, so no setting changes,
#: but it is a SPI write. It's allowed because the fault panel needs it, and
#: it's off unless you pass --status-poll.
WRITES_CONFIG_SIDE_EFFECT = frozenset({15})

#: Opcodes the interactive console may send. Includes the config commands
#: so you can check what config the firmware is actually asserting.
#:
#: 17-20 are blocked in both sets: they clear the cell, aux, spin and fcell
#: registers and can erase the fault you're trying to diagnose.
CONSOLE_COMMANDS = frozenset(
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 21, 22}
)

#: Blocked in the console too, with the reason.
CONSOLE_DENIED = {
    17: "clears the cell registers -- it can erase the fault you are diagnosing",
    18: "clears the aux registers -- it can erase the fault you are diagnosing",
    19: "clears the spin registers -- it can erase the fault you are diagnosing",
    20: "clears the fcell registers -- it can erase the fault you are diagnosing",
}

#: Console commands that make the firmware write configuration. They
#: re-assert the config compiled into adBms_Application.c and can't change
#: it (that needs a reflash), but they are still writes to the chip.
CONSOLE_WRITES_CONFIG = frozenset({1, 15, 16, 21})

#: Console commands that produce a lot of output.
CONSOLE_FLOODS = frozenset({16})


def console_warning(command: int):
    """The warning to show before sending ``command``, or None."""
    notes = []
    if command in CONSOLE_WRITES_CONFIG:
        notes.append(
            "command %d WRITES CONFIGURATION to the chip (WRCFGA/WRCFGB). "
            "It re-asserts the configuration the firmware was compiled with "
            "and cannot change it -- nothing in ADI's menu lets a PC-side "
            "tool choose a config value; that only changes by reflashing. "
            "It is still an SPI write." % command
        )
    if command in CONSOLE_FLOODS:
        notes.append(
            "command %d is LOOP MEASUREMENT: it runs four measurement "
            "cycles and reprints the whole menu, so expect a wall of output."
            % command
        )
    if command == 15:
        notes.append(
            "command 15 also issues WRCFGA/WRCFGB before reading the status "
            "registers (verified in adBms_Application.c:503-518)."
        )
    return " ".join(notes) if notes else None


#: Human-readable menu, used by --list-commands and the web UI's About box.
COMMAND_NAMES = {
    0: "Print menu",
    2: "Read configuration",
    3: "Start cell voltage conversion",
    4: "Read cell voltages (self-sufficient: ADCV + SNAP + 6 reads + UNSNAP)",
    5: "Start S-voltage conversion",
    6: "Read S-voltages",
    7: "Start avg cell voltage conversion",
    8: "Read avg cell voltages",
    9: "Start F-cell voltage conversion",
    10: "Read F-cell voltages",
    11: "Start aux voltage conversion",
    12: "Read aux voltages",
    13: "Start RAux voltage conversion",
    14: "Read RAux voltages",
    15: "Read status registers (see WRITES_CONFIG_SIDE_EFFECT)",
    # RDSID is a pure read. The factory serial confirms the daisy-chain
    # order, e.g. that no segment boards were swapped.
    22: "Read device SID (6-byte factory serial; confirms chain order)",
}

#: The firmware's _read() returns on '\r' or '\n', so one CR is enough
#: (and it's what a terminal sends).
TERMINATOR = b"\r"


class CommandRejected(ValueError):
    """Raised for anything the gate won't emit. The poll plan is validated
    at startup, so this shouldn't happen at runtime."""


def is_allowed(command) -> bool:
    """Non-raising membership test used by the CLI for friendly errors."""
    return type(command) is int and command in ALLOWED_READ_COMMANDS


def encode_command(command) -> bytes:
    """Return the exact bytes for ``command`` or raise CommandRejected."""
    # `type(x) is int` rather than isinstance(): bool is a subclass of int,
    # and True would otherwise encode as command 1 (write config).
    if type(command) is not int:
        raise CommandRejected(
            f"command must be a plain int, got {type(command).__name__!r}: {command!r}"
        )
    if command not in ALLOWED_READ_COMMANDS:
        reason = DENIED_COMMANDS.get(command)
        if reason:
            raise CommandRejected(
                f"command {command} is denied: {reason}. This tool is read-only."
            )
        raise CommandRejected(
            f"command {command} is not in the allow-list {sorted(ALLOWED_READ_COMMANDS)}"
        )
    payload = f"{command}".encode("ascii") + TERMINATOR
    # Sanity check: the firmware needs digits followed by one CR.
    assert payload.rstrip(b"\r").isdigit(), payload
    assert payload.count(b"\r") == 1, payload
    return payload


def encode_console_command(command) -> bytes:
    """Encode a command for the interactive console only.

    Same checks as encode_command() but against CONSOLE_COMMANDS. Only
    bms_monitor/console.py may call this (a test checks it), so the polling
    loop can't reach the wider set.
    """
    if type(command) is not int:
        raise CommandRejected(
            f"command must be a plain int, got {type(command).__name__!r}: {command!r}"
        )
    if command not in CONSOLE_COMMANDS:
        reason = CONSOLE_DENIED.get(command)
        if reason:
            raise CommandRejected(
                f"command {command} is blocked even in the console: {reason}"
            )
        raise CommandRejected(
            f"command {command} is not an ADBMS6830 menu command "
            f"(valid: {sorted(CONSOLE_COMMANDS)})"
        )
    payload = f"{command}".encode("ascii") + TERMINATOR
    assert payload.rstrip(b"\r").isdigit(), payload
    assert payload.count(b"\r") == 1, payload
    return payload


def validate_poll_plan(commands) -> tuple:
    """Validate the poll plan once at startup. It can't be changed while
    running, so any error shows up immediately."""
    plan = tuple(commands)
    if not plan:
        raise CommandRejected("poll plan is empty")
    if len(plan) > 16:
        raise CommandRejected("poll plan is unreasonably long (>16 commands)")
    for command in plan:
        encode_command(command)   # raises on anything not allowed
    return plan


def parse_poll_plan(spec: str) -> tuple:
    """Parse a ``--poll`` string such as ``"4"`` or ``"3,4"``.

    Non-integers are rejected here so the error points at the CLI argument.
    """
    parts = [p.strip() for p in str(spec).split(",") if p.strip()]
    plan = []
    for part in parts:
        if not part.isdigit():          # no signs, no spaces, no "4\r1"
            raise CommandRejected(f"poll plan entry {part!r} is not a plain number")
        plan.append(int(part))
    return validate_poll_plan(plan)
