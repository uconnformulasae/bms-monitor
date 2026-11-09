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
| `--ics` | `1` | Must match `BMS_IC_COUNT` in the firmware's `App/Inc/board.h`. |
| `--cells-per-ic` | `16` | Must match the number of bits set in `BMS_CHANNEL_MASK` (`0x03FF` = 10). |
| `--uv`, `--ov` | `3.0`, `4.2` | Where the charts draw the limit lines. The firmware uses 2.7 V and 4.2 V. |

If `--ics` or `--cells-per-ic` don't match the firmware, cells show up in the
wrong place or not at all.

## What the controller sends

Everything is little-endian. The controller runs one cycle every 100 ms.

| ID | Rate | Contents |
| --- | --- | --- |
| `0x6FE` | 1 Hz | Build version, DLC 5. Bytes 0-3: build hash, u32, the first 8 hex digits of the firmware's flash-log commit. Byte 4: flags, `0x01` = built with uncommitted changes, `0x02` = built without git (hash 0). Shown as **Firmware** in the source popover; run `tools/find_build.sh <hash>` in the firmware repo to find the build. |
| `0x6FF` | 10 Hz | Chain status. Byte 0: state (0 = starting up, 1 = running). Byte 1: cycle counter. Bytes 2-3: bitmap of ICs with no valid data this cycle (bit 0 = IC 1). Bytes 4-6: balancing state, IC and cell. Byte 7: `0x01` while turning discharge off hasn't read back as off (see [Balancing](#balancing)). |
| `0x700` + n | 10 Hz | Cell voltages, 4 per frame, u16 in 0.1 mV. Packed IC by IC, only the channels in `BMS_CHANNEL_MASK`. `0xFFFF` = no data. |
| `0x720` + i | 2 Hz | One frame per IC. Byte 0: IC index (from 0). Byte 1: status flags. Byte 2: command counter. Byte 3: cycles in a row with no valid cell data. Bytes 4-5: die temperature, s16 in 0.1 °C (`32767` = no reading). Bytes 6-7: DCC (discharge) bits, bit 0 = C1, as last read back from the chip with a good PEC. Sent even when bit 0 of byte 1 is set. |
| `0x730` + n | 2 Hz | Thermistors, 4 per frame, s16 in 0.1 °C. 10 slots per IC, in RT order. `-32768` = no probe in that slot, `32767` = probe fitted but no reading. |

Status flags (byte 1 of `0x720` + i):

| Bit | Meaning |
| --- | --- |
| 0 | no status this cycle (cells invalid or the status read failed PEC); the other flags and the die temperature are then unknown |
| 1 | a cell is over voltage |
| 2 | a cell is under voltage |
| 3 | open wire (not set by the firmware yet) |
| 4 | thermal shutdown |
| 5 | SPI fault |

With 2 ICs and 10 cells each, that's `0x6FE`, `0x6FF`, `0x700`-`0x704`,
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

## Balancing

Start the monitor with `--allow-tx` and a **Balancing** panel appears on
the Live tab. It runs the firmware's balance test: while the toggle is on,
the controller discharges every wired cell on every IC. It is a test of the
discharge path, not a balancing algorithm. Turning the toggle off stops it
straight away.

A discharge only starts if the chain is running, every IC sent valid cells
that cycle, no cell is at or below the undervoltage threshold, no earlier
discharge clear is still unconfirmed, and the DCC bits read back from every
chip as written.

The controller also stops by itself, and stays stopped until the toggle
goes off and on again, if:

- it hears no ON for over 1 s (`BMS_BALANCE_CMD_TIMEOUT_MS`). See
  [the heartbeat](#the-heartbeat) below.
- the chain drops back to starting up.
- an IC gives no valid cells for 3 cycles in a row.
- any IC's die temperature is 80 °C or more for over 5 s. A die it can't
  read counts as hot, so a chip that stops answering can't keep discharging
  unwatched; a single missed reading doesn't trip it. Die temperatures are
  read every 500 ms.
- any cell reaches the undervoltage threshold (`BMS_UV_THRESHOLD_V`, 2.7 V).

```sh
python3 -m bms_monitor --source can --can-interface slcan \
  --can-channel /dev/cu.usbserial-XXXX --ics 2 --cells-per-ic 10 --allow-tx
```

Without `--allow-tx` the monitor never transmits, and the panel and the
POST endpoint don't exist.

The command:

| ID | Contents |
| --- | --- |
| `0x740` | DLC 2. Byte 0: 1 = on, 0 = off. Byte 1: `0xA5`. Anything else is ignored by the firmware. |

### The heartbeat

The firmware never needs the monitor to run, but balancing is only ever
started from it, so ON works as a heartbeat. While the toggle is on, the
monitor sends ON again every 250 ms (`REPEAT_S` in `can_command_gate.py`).
A repeat can wait up to 500 ms for a quiet gap on the bus, so ON always
arrives inside the firmware's 1 s timeout. Only the first ON and the OFF
are written to the Raw console.

The repeat stops, and the firmware times the discharge out within a second,
when:

- the toggle goes off (OFF is sent too);
- the CAN link drops or the adapter reconnects;
- an ON couldn't be sent within 1 s. Sending it later would start a
  discharge long after the click;
- the monitor exits.

The repeat runs in the monitor, not the browser, because browsers slow
timers in background tabs to about once a second, which would trip the
timeout. Closing the tab leaves the test on until the toggle is turned off
or the monitor stops.

### What the controller reports

The controller reports what it did in `0x6FF`, so the panel shows the
controller's state, not just what was asked for:

| Byte 4 | State | Meaning |
| --- | --- | --- |
| 0 | off | Toggle is off. Firmware without balancing also reads as off. |
| 1 | pending | On; the discharge starts on the next measurement cycle. |
| 2 | discharging | Every wired cell has its DCC bit set, confirmed by readback. |
| 4 | refused | Not every IC had valid cell data, the DCC readback didn't match, or an earlier clear was still unconfirmed. Nothing is discharging. |
| 5 | aborted | The chain reset while discharging; DCC was cleared. |
| 6 | too_hot | A die was 80 °C or more (or unreadable) for over 5 s; DCC was cleared. Byte 5 is that IC. |
| 7 | cell_low | A cell was at or below the undervoltage threshold; DCC was cleared. Bytes 5-6 are that IC and cell. |
| 8 | timed_out | No ON for over 1 s; DCC was cleared. |
| 9 | no_data | An IC gave no valid cells for 3 cycles in a row; DCC was cleared. Byte 5 is that IC. |

States 4 to 9 stay until OFF, so the toggle has to go off and on again.
Byte 5 is an IC (from 0) and byte 6 a cell within that IC (from 0, in the
same order as the `0x700` frames), or `0xFF` for none.

Byte 7 bit 0 means the controller turned discharge off but some chip
hasn't read back as off. It retries every cycle until one does. The panel
shows a warning while it's set, in any state.

Which cells are discharging comes from the DCC bits in each IC's `0x720`
frame, which are the chip's own readback. The dashboard shows them whatever
the state, so a chip still discharging after a stop stays marked.

The firmware sets and clears DCC and decides when to stop. The monitor
only sends the toggle and its heartbeat.

Two CANdapter quirks used to cost a received frame per toggle, which showed
up as an IC "left out of this cycle" (often IC2, whose frames come last):

- It acknowledges every transmit with one ACK byte (`0x06`) and no line
  end, which python-can read as the start of the next frame and threw that
  frame away. With `--can-interface slcan` the monitor strips it
  (`bms_monitor/sources/candapter.py`).
- If a command reaches it while it is forwarding a frame, it corrupts that
  frame (bits cleared in a few characters: `0x703` arrives as `0x403`). The
  monitor now sends only after 25 ms with no frames, which is the gap between
  the controller's 10 Hz bursts (`TX_GAP_S` in `can_source.py`).

Lines python-can can't parse and frames with unknown IDs are logged to the
Raw console, up to 20 per run. Requests that can't be sent within 1 s (bus
down, CANdapter reconnecting) are dropped rather than sent late.

The POST endpoint (`/api/balance-test`) only accepts requests from the
dashboard itself: it needs a per-run token from `/api/snapshot`, a
`127.0.0.1` or `localhost` Host header, and a same-origin `Origin`. Another
web page open in the same browser can't trigger it.

## Known limits

- Per-IC frames are only decoded for `0x720`-`0x729` (10 ICs), and
  thermistor frames only for `0x730`-`0x734` (the first 2 ICs). The
  thermistor range will need to grow when the firmware goes to the full
  chain.
- The controller doesn't send pack current or state of charge yet, so
  those show as n/a. The balance test above is controller-side only and
  isn't reported on CAN1.

## Troubleshooting

**Nothing shows up.** Check that the bitrate is 250000, the CANdapter port is
right, the controller is powered, and CAN2 is terminated.

**`python-can is required`.** Run `pip install -r requirements.txt`.

**Cells are missing or in the wrong place.** `--ics` or `--cells-per-ic`
doesn't match the firmware build.
