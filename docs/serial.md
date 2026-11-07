# USB serial (Nucleo bench board)

The bench setup is a NUCLEO-F303RE running ADI's ADBMS6830 test-menu
firmware. It talks over the ST-LINK USB port at 115200 baud. The firmware has
a numbered menu and only measures when it's asked, so the monitor sends menu
numbers in a loop and reads the text that comes back. It only ever sends read
and measure commands.

## Running

```sh
python3 -m bms_monitor --source serial --port auto --ics 10 --cells-per-ic 9
```

`--port auto` looks for the ST-LINK (USB ID `0483:374B`, or a `usbmodem` or
`ttyACM` device) and looks again on every reconnect, so unplugging and
replugging is fine. To pick the port yourself, pass it directly:
`--port /dev/cu.usbmodem1103`, or `--port COM4` on Windows.

Close anything else that has the port open first, like `screen` or the
STM32CubeIDE console.

## What it polls

`--poll` is a comma-separated list of menu commands that run in a loop. The
default is `4`.

| Command | What it does |
| --- | --- |
| 4 | Starts a cell conversion and reads every cell voltage |
| 11, 12 | Starts an aux conversion and reads it (temperatures) |
| 15 | Reads the status registers (faults) |
| 22 | Reads each chip's serial ID, shown on the Chain tab |

For example, `--poll 4,11,12,22` reads cells, temperatures and serial IDs.

Command 15 writes the configuration registers before it reads, because
that's how the firmware implements it. It's the configuration the firmware
already has, but it's still a write, so it's off by default. Add it with
`--status-poll` if you want the fault panel.

The poll loop never sends 1, 16 or 17 to 21: those write configuration or
clear registers. `--list-commands` prints the full list.

The first two readings after connecting are marked stale and not plotted
(`--warmup`, default 2). The firmware reads right after starting a conversion,
so the first result can be old.

## Temperatures

Over serial the monitor converts the aux voltages to °C itself. These flags
set the thermistor curve. You can also change it in the Settings panel.

| Flag | Default |
| --- | --- |
| `--ntc-beta` | `3435` |
| `--ntc-r25` | `10000` (ohms at 25 °C) |
| `--ntc-series` | `10000` (divider resistor, ohms) |
| `--ntc-vref` | `3.0` (VREF2) |
| `--ntc-pulldown` | off (NTC on the low side by default) |

Without `--poll 11,12` the Temperatures tab says "not polled". Without
`--status-poll` the fault panel does too.

## Things to know

- Expect a few samples per second. Each full read of the pack spends about
  240 ms on wake-up delays in the firmware.
- `VMV` and `V+` in the firmware's output are printed with the same scale
  factor when they should differ. The monitor shows them exactly as printed,
  so don't trust those two.

## Troubleshooting

**Every cell reads exactly 1.500000 V or 1.499850 V.** Nothing is answering.
Those are the readings for all zeros and all ones. Usually the BMS board has
no power: it runs off the cells it measures, so without real cells or the
resistor rig nothing replies.

**A red banner says the firmware is stuck.** Press the black RESET button on
the Nucleo. The firmware's input loop gets stuck if it receives something
that isn't a number (from another terminal, for example). The monitor only
sends digits.

**LINK says RECONNECTING.** The banner says why. Usually another program has
the port open, or it's a charge-only USB cable.

**A red banner names an IC that isn't answering.** Check the isoSPI daisy
chain, starting at the board before the first missing IC.

**`pyserial is required`.** Run `pip install -r requirements.txt`.
