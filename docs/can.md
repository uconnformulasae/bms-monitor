# CAN

The F105 BMS controller sends all of its data on CAN2 at 250 kbit/s. The
monitor listens through a CAN adapter using python-can. On the bench we use
the Orion CANdapter, which shows up as a USB serial port and runs through
python-can's `slcan` interface.

This has been run on the BMSCMU test segment (2 ICs, 10 cells each).

## Running

```sh
python3 -m bms_monitor --source can --can-interface slcan \
  --can-channel /dev/cu.usbserial-XXXX --ics 2 --cells-per-ic 10 --uv 2.7
```

To find the CANdapter's port, run `ls /dev/cu.usbserial*` on macOS. On
Windows, use the COM port from Device Manager (for example
`--can-channel COM5`). With a SocketCAN adapter on Linux, use
`--can-interface socketcan --can-channel can0`.

| Flag | Default | Notes |
| --- | --- | --- |
| `--can-interface` | `socketcan` | python-can interface. Use `slcan` for the CANdapter. |
| `--can-channel` | `can0` | Port or channel name. |
| `--can-bitrate` | `250000` | CAN2 on the F105 runs at 250k. |
| `--ics` | `1` | Must match `TOTAL_IC` in the firmware's `bms_config.h`. |
| `--cells-per-ic` | `16` | Must match the number of bits set in `BMS_CHANNEL_MASK` (`0x03FF` = 10). |
| `--uv`, `--ov` | `3.0`, `4.2` | Where the charts draw the limit lines. The firmware uses 2.7 V and 4.2 V. |

If `--ics` or `--cells-per-ic` don't match the firmware, cells show up in the
wrong place or not at all.

## What the controller sends

Everything is little-endian. The controller runs one cycle every 100 ms.

| ID | Rate | Contents |
| --- | --- | --- |
| `0x6FF` | 10 Hz | Chain status. Byte 0: state (0 = starting up, 1 = running). Byte 1: cycle counter. Bytes 2-3: bitmap of ICs with no valid data this cycle (bit 0 = IC 1). |
| `0x700` + n | 10 Hz | Cell voltages, 4 per frame, u16 in 0.1 mV. Packed IC by IC, only the channels in `BMS_CHANNEL_MASK`. `0xFFFF` = no data. |
| `0x720` + i | 2 Hz | One frame per IC. Byte 0: IC index (from 0). Byte 1: status flags. Byte 2: command counter. Byte 3: cycles in a row with no valid cell data. Bytes 4-5: die temperature, s16 in 0.1 °C (`32767` = no reading). Bytes 6-7: balancing mask (always 0 for now). |
| `0x730` + n | 2 Hz | Thermistors, 4 per frame, s16 in 0.1 °C. 10 slots per IC, in RT order. `-32768` = no probe in that slot, `32767` = probe fitted but no reading. |

Status flags (byte 1 of `0x720` + i):

| Bit | Meaning |
| --- | --- |
| 0 | PEC failure |
| 1 | a cell is over voltage |
| 2 | a cell is under voltage |
| 3 | open wire (not set by the firmware yet) |
| 4 | thermal shutdown |
| 5 | SPI fault |

With 2 ICs and 10 cells each, that's `0x6FF`, `0x700`-`0x704`,
`0x720`-`0x721` and `0x730`-`0x734`.

The thermistor readings come from the conversion started on the previous
status cycle, so they're about 500 ms old.

## How the monitor uses them

- Each `0x6FF` ends a round. An IC's cells are only shown if `0x6FF` doesn't
  flag it and none of its cells are `0xFFFF`. While the chain is starting up,
  nothing is shown.
- Over CAN, under and over voltage are reported per chip, not per cell. The
  Chain tab and the chip boxes on the Live tab show which IC has a flag,
  along with its die temperature.
- Thermistors show up on the Temperatures tab as RT1, RT2, and so on, the
  same names as on the board. A probe that's unplugged, shorted, outside
  -30 to 120 °C, or on a chip that isn't answering shows no reading instead
  of a number.
- With `--csv run.csv`, temperatures are also logged to `run_temps.csv`.
  See [recording.md](recording.md).

## Known limits

- Per-IC frames are only decoded for `0x720`-`0x729` (10 ICs), and
  thermistor frames only for `0x730`-`0x734` (the first 2 ICs). The
  thermistor range will need to grow when the firmware goes to the full
  chain.
- The controller doesn't send pack current, state of charge or balancing
  yet, so those show as n/a.

## Troubleshooting

**Nothing shows up.** Check that the bitrate is 250000, the CANdapter port is
right, the controller is powered, and CAN2 is terminated.

**`python-can is required`.** Run `pip install -r requirements.txt`.

**Cells are missing or in the wrong place.** `--ics` or `--cells-per-ic`
doesn't match the firmware build.
