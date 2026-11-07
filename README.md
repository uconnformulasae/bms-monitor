# BMS Monitor

A read-only display for the BMS. It runs on your laptop, connects to the BMS
over CAN or USB, and shows the pack in your browser: every cell voltage, the
pack voltage, the spread between the highest and lowest cell, temperatures,
and which ICs are responding. It never sends configuration to the BMS.

## Install

You need Python 3.9 or newer.

```sh
git clone https://github.com/uconnformulasae/bms-monitor.git
cd bms-monitor
pip install -r requirements.txt
```

## Run

F105 controller over CAN (needs CANdapter):

```sh
python3 -m bms_monitor --source can --can-interface slcan \
  --can-channel /dev/cu.usbserial-XXXX --ics 2 --cells-per-ic 10
```

Replace `/dev/cu.usbserial-XXXX` with the CANdapter's port (`ls /dev/cu.usbserial*`).
On Windows it's a COM port, like `COM5`.

Nucleo bench board over USB:

```sh
python3 -m bms_monitor --source serial --port auto --ics 10 --cells-per-ic 9
```

A browser tab opens at http://127.0.0.1:8765. Press Ctrl-C to stop.

Make sure `--ics` and `--cells-per-ic` match the firmware for either method 

To save a session add `--csv run.csv`, and to watch it again:

```sh
python3 -m bms_monitor --source replay --file run.csv
```

## Docs

- [CAN](docs/can.md): connecting to the F105 controller and the frames it sends
- [USB serial](docs/serial.md): the Nucleo bench firmware, `--poll`, temperatures
- [Reading the dashboard](docs/dashboard.md): what each tab and number means
- [Recording and replay](docs/recording.md): CSV logs, Export CSV, replay
- [Debug console](docs/debug-console.md): raw bytes for when a number looks wrong

## Tests

```sh
python3 -m unittest discover
```
