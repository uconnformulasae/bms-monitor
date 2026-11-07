# Debug console

A terminal tool for when a number on the dashboard looks wrong and you want
to see the raw data behind it. You type a menu command and it shows what was
sent, the raw bytes that came back, and the decoded values side by side.

It only works over USB serial with the Nucleo bench firmware (see
[serial.md](serial.md)). There's no console over CAN.

```sh
python3 -m bms_monitor --console --source serial --port auto --cells-per-ic 9
```

## Example

```
> 4
TX  34 0D   4.   menu command 4
    SPI commands the firmware will issue for this (computed locally, NOT observed):
      SNAP       00 2D D2 A2        opcode + PEC15
      RDCVA      00 04 07 C2        opcode + PEC15
      ...
RX  409 bytes in 0.358 s
    0000  45 6e 74 65 72 20 63 6d 64 3a 34 0d 0a 3d 3d 3d  |Enter cmd:4..===|
    ...
DECODE
  IC1   cell   ccount=9    PECError=0   [OK]
        c1      3.689250 V   code  14595 = 0x3903   wire 03 39
```

- **TX** is the bytes sent to the board, plus the SPI commands the firmware
  will send for that menu item, with their PEC15 worked out on the PC.
- **RX** is exactly what came back, in hex and ASCII.
- **DECODE** is each cell's voltage next to its raw chip code, so a byte
  order problem shows up instead of hiding behind a believable voltage.

The firmware prints text, not raw register bytes, so the actual SPI traffic
and the chip's PEC10 can't be seen from the PC. The TX opcodes are marked
"computed" for that reason.

## Commands

| Command | What it does |
| --- | --- |
| `<n>` | Send menu command n and show TX, RX and DECODE |
| `menu` | List the menu commands and which ones are blocked |
| `decode <hex>` | Read bytes as cell codes both little-endian and big-endian, and say which reading makes sense |
| `rx <hex>` | Check an 8-byte register group (6 data bytes plus command counter and PEC10) |
| `pec15 <hex>`, `pec10 <hex>` | Work out the PEC for some bytes |
| `hex` | Turn the raw hex dump on or off |
| `help` | List these |
| `quit` | Leave |

`decode`, `rx` and the PEC commands are for bytes you got another way, like
from a logic analyser.

A byte swap doesn't always give an impossible voltage. For example, 3.70 V is
code `0x394B`, and swapped (`0x4B39`) it's 4.3886 V, which looks fine. What
gives it away is that real cells sit within tens of millivolts of each other
and swapped ones are spread over volts, which is what `decode` checks.

## What it won't send

- 17 to 20 are always blocked. They clear the cell, aux, S-pin and filtered
  cell registers, which can erase the fault you're trying to find.
- 1, 15, 16 and 21 write configuration to the chip, so it asks before sending
  them. They can only re-send the configuration the firmware was built with.
  Changing a setting means reflashing the firmware.
