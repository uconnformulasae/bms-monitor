# Recording and replay

## Logging to CSV

Add `--csv` to any run:

```sh
python3 -m bms_monitor --source can --can-interface slcan \
  --can-channel /dev/cu.usbserial-XXXX --ics 2 --cells-per-ic 10 --csv run1.csv
```

- One row per poll cycle. The file is flushed once a second and when you stop
  with Ctrl-C, so a crash loses at most about a second.
- If the file already exists with the same `--ics` and `--cells-per-ic`, new
  rows go on the end. If the shape is different, it stops with an error, so
  use a new name.
- Size is roughly 300 bytes per row. The BMSCMU test segment over CAN came out
  around 4 MB per hour.
- Over CAN, thermistor readings go to a second file next to it,
  `run1_temps.csv`, one row per update. It's only created once a temperature
  arrives.

## Export CSV

The **Export CSV** button in the top bar downloads everything the monitor
still has in memory, in the same format as `--csv`. It keeps the last
`--history` samples (default 18000, about 30 minutes at 10 Hz).

## Replay

```sh
python3 -m bms_monitor --source replay --file run1.csv
```

This opens the same dashboard with a timeline bar: play/pause, a slider to
scrub, speed (0.25x, 1x, 4x, 16x) and **End** to jump to the end. No hardware
is needed. Files from `--csv` and from Export CSV both work.

By default the whole file is loaded at once. Add `--replay-stream` to play it
back in real time instead (`--replay-speed 4` for 4x). Long files are cut to
the last `--history` samples, with a message saying so.

Damaged rows (for example a half-written last line) are skipped and counted.

## CSV format

The first line is a comment with the format version and chain shape, for
example `# bms-monitor-csv/2 ics=2 cells_per_ic=10`. Then a header row, then
one row per cycle:

| Column | Meaning |
| --- | --- |
| `t_wall` | Unix time, seconds |
| `t_mono` | Monotonic clock, seconds. Only differences mean anything; use it for durations |
| `seq` | Cycle number |
| `ics_expected`, `ics_present` | ICs in the chain, and how many answered |
| `cells_per_ic` | Cells per IC |
| `complete`, `partial` | 1 if every IC answered, or only some did |
| `stale` | 1 during warm-up after connecting |
| `pec_ok` | 1 if every IC's data passed its checksum |
| `pack_v`, `min_v`, `max_v`, `spread_mv` | Pack totals. Blank unless every IC answered with good data |
| `min_ic`, `min_cell`, `max_ic`, `max_cell` | Where the lowest and highest cell are |
| `ic<n>_pec_ok`, `ic<n>_present` | Per IC: checksum ok, and answered this cycle |
| `ic<n>_ccount`, `ic<n>_jump` | Per IC: command counter, and 1 if it jumped unexpectedly |
| `ic<n>_c1` ... | Cell voltages in volts |

Rows with a failed checksum are kept, with `pec_ok=0` and the voltages left
blank, so a noisy connection shows up in the log instead of disappearing from
it. An IC that didn't answer has `ic<n>_present=0` and blank cells.

The temperatures file starts with `# bms-monitor-temps-csv/1`, then
`t_wall`, `t_mono` and one column per thermistor in °C, named like
`rt1_ic1_c`. A blank means no reading.
