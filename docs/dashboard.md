# Reading the dashboard

The page is served at http://127.0.0.1:8765 and only on your own machine.
There's no password and it won't listen on the network. Tested in Chrome.

## Top bar

The pill at the top shows the overall state: `LIVE`, `STALE`,
`RECONNECTING`, `LINK DOWN` or `OFFLINE`, plus how many ICs are healthy and
the sample rate. Click it for the details: source, port, poll plan, uptime,
PEC error totals and where the CSV log is going.

**Export CSV** downloads everything the monitor has in memory. See
[recording.md](recording.md).

## Live tab

- **Pack voltage**: the total. It's blank if any IC isn't answering, since a
  total from part of the chain would look right and be wrong.
- **Lowest / highest cell**: with which IC and which cell.
- **Spread**: highest minus lowest, in mV. Green under 30 mV, amber above,
  red above 60 mV. You can change the limits in **Settings** or with
  `--warn-spread` and `--crit-spread`.
- **Chain health**: one box per IC. `ok`, `PEC ERR` (its data failed the
  checksum), `flaky` (answering some cycles) or `SILENT` (not answering).
- **Cell view**: one panel per segment board (`--ics-per-segment`, default
  2). Each dot is a cell and the bar shows how far it is from the pack
  average. Click a dot to follow that cell in the time series. Pick a board
  (S1, S2, ...) to zoom in: the voltage axis narrows to that board's cells,
  and blue and orange mark the lowest and highest cell on that board. The
  dashed line is still the whole pack's average. **Whole pack** zooms back
  out.
- **Time series**: every cell over time. The lowest cell is blue, the highest
  is orange, the rest are grey. It follows the cell view's zoom.
- **Cells**: every cell at once, one row per chip, with a line between
  boards. The colour is how far the cell is from the pack average (blue low,
  orange high) and the number is always printed. A UV/OV flag adds a heavy
  border and an arrow. A chip that didn't answer this cycle shows a blank
  row, not its last reading. Click a tile to follow that cell. **Table**
  switches to a sortable table.

## Charge tab

For charging or balancing. The spread over time is the main chart, and it
should trend down. Cells are ranked worst first. **Start session** records
the spread from that point so you can export it.

Pack current, state of charge and balancing show n/a. The ADBMS6830 has no
current input, and the controller doesn't report these yet. They'll fill in
if it ever sends them.

## Temperatures tab

Thermistor channels in °C. Over CAN they arrive already converted and named
RT1, RT2, ... like on the board. Over serial you need `--poll 11,12`, and the
thermistor curve is in **Settings**. See [serial.md](serial.md).

## Chain tab

Per-IC detail: how often each chip answered, its command counter, flags and
die temperature over CAN, and its factory serial number over serial if you
add `22` to `--poll`. The serial numbers confirm the chain is in the order
you expect and that nobody swapped a board.

## Raw console tab

Every line the BMS sent over serial, exactly as it came in. Useful when
something on the other tabs doesn't make sense.

## Missing data shows as a gap

Every frame from the BMS carries a checksum. When one fails, it isn't
plotted: the line breaks and a red tick shows along the bottom of the chart.
A gap means nothing was measured, not that nothing happened. The same goes
for a silent IC: the pack voltage goes blank instead of adding up the chips
that did answer.
