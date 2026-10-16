# Geiger-Counter Based QRNG

Uses the commonly available RadiationD-v1.1 (CAJOE) kit with the M4011 tube  → Arduino Mega (hardware time-stamping) → Python (analysis + extraction).

![Sample capture of 1714 events](./data/geiger-20261013-005942.png)

## Wiring

This powers the Geiger-counter via the Arduino and captures the events with timestamps.

| CAJOE header | Mega |
|---|---|
| `GND` | `GND` |
| 5V | `5V` |
| `VIN` (this is the **pulse output**) | `pin 49 (ICP4)` |

The pulse is 5 V logic which the Mega handles directly. (On an ESP32, Pi or Pico it needs a voltage divider or level shifter, e.g. 2 kΩ + 3.3 kΩ.)

**Silencing the buzzer:** pull the jumper cap on `J1` (next to `R20`/`C66`). The buzzer stops clicking; the pulse output and the pulse LED (`D23`) are unaffected. Put the cap back to re-enable it.

## Setup

### 1. Flash the firmware

The sketch `firmware/geiger_capture/geiger_capture.ino` timestamps every pulse on Timer4 input capture (pin 49) and streams `seq,ticks` over USB serial at 115200 baud.

With [`arduino-cli`](https://arduino.github.io/arduino-cli/latest/installation/) (single binary; or `brew install arduino-cli`, or the install script on that page):

```
arduino-cli core update-index
arduino-cli core install arduino:avr            # one-time: AVR toolchain for the Mega
arduino-cli board list                          # find the port, e.g. /dev/ttyACM0 (shows "Arduino Mega or Mega 2560")
arduino-cli compile --fqbn arduino:avr:mega firmware/geiger_capture
arduino-cli upload  --fqbn arduino:avr:mega -p /dev/ttyACM0 firmware/geiger_capture
```

Or with the [Arduino IDE](https://www.arduino.cc/en/software): open the `.ino`, choose *Tools → Board → Arduino Mega or Mega 2560* and the port, then Upload. No extra libraries are needed.

Notes:
- Elegoo and other clones usually work with the stock `arduino:avr:mega` target. Clones with a CH340 USB chip show up as `/dev/ttyUSB0` instead of `ttyACM0`.
- **Linux serial permission:** your user must be in the `dialout` group (Fedora/Debian: `sudo usermod -aG dialout $USER`, then log out and in). Fedora with ModemManager may briefly grab new ports; if uploads fail, retry or `sudo systemctl stop ModemManager`.
- Check it works: `arduino-cli monitor -p /dev/ttyACM0 -c baudrate=115200` should print `# qrng-geiger tick_hz=2000000 edge=falling`, then `seq,ticks` lines as pulses arrive (and `# alive` every 10 s). Close the monitor before running `capture`.
- Falling edge is captured by default; flip `CAPTURE_RISING` at the top of the sketch if you prefer the other edge (either gives valid timing).

### 2. Install the host tools

Python 3.9+ (developed on 3.14) with three packages from PyPI: [numpy](https://pypi.org/project/numpy/) (maths), [matplotlib](https://pypi.org/project/matplotlib/) (dashboard and figures), [pyserial](https://pypi.org/project/pyserial/) (talking to the Mega). No SciPy needed.

```
python3 -m venv .venv && . .venv/bin/activate     # optional but tidy
pip install -r requirements.txt
```

If you'd rather use distro packages on Fedora: `sudo dnf install python3-numpy python3-matplotlib python3-pyserial`.
The live `dashboard` needs a GUI backend for matplotlib; if it complains, install Tk (`sudo dnf install python3-tkinter`, or `pip install PyQt6`). `analyze --png` needs no display.

## Use

`capture` finds the Mega automatically (matching Arduino / CH340 / FTDI USB IDs, falling back to the first `/dev/ttyACM*` or `ttyUSB*`) and tells you which port it picked. If you have several boards attached, pass `--port /dev/ttyACM0`. Opening the port resets the Mega, so the first couple of seconds are lost on purpose; the file only contains post-reset data. It shows a rolling 60 s CPM while running.

```
./qrng.py capture                         # real hardware -> data/geiger-<time>.csv  (Ctrl-C to stop)
./qrng.py dashboard data/geiger-*.csv     # live view in a second terminal
./qrng.py analyze FILE --png out.png      # text report + figure
./qrng.py generate FILE -n 64             # 64 random BYTES (-n/--bytes, default 32) as hex; -o file.bin for binary
./qrng.py analyze FILE --full             # extra checks: Poisson fit, per-bit bias, autocorrelation, output tests
./qrng.py generate FILE --raw -o raw.bin  # unconditioned symbols for NIST ea_non_iid
```

See [Caveats](#Caveats) for instructions on keeping your machine awake. 

### Reports: png, hex, raw and markdown

When a `capture` ends (Ctrl-C) it writes four files next to the CSV, with the same base name:

| File | Contents |
|---|---|
| `NAME.png` | the analysis figure |
| `NAME.hex` | the conditioned random output, one 32-byte block (64 hex characters) per line |
| `NAME.raw` | the unconditioned symbols (low `k` bits of each gap, one per byte) |
| `NAME.md` | analysis report: flags, summary, detailed checks, SP 800-22 results, embedded figure |

`--no-report` skips this; `-k` and `--nist22 {none,out,raw,both}` control it (default `out`: SP 800-22 on the conditioned output). The CSV is closed and safe before the report starts, and a failure in the report never loses the capture.

The same thing can be run by hand, and across several captures:

```
./qrng.py report data/geiger-20261012-193342.csv --force             # regenerate (existing files are never overwritten without --force)
./qrng.py report data/geiger-2026101*.csv -o data/combined --nist22 both
```

The individual pieces are still available: `analyze --png/--full`, `generate`, and `nist22` below.

### NIST SP 800-22 test suite

[sp800_22_tests-py](https://github.com/jimmckeeth/sp800_22_tests-py) (a fork of [dj-on-github/sp800_22_tests](https://github.com/dj-on-github/sp800_22_tests); pure Python, GPL-2, no dependencies) is included as a git submodule:

```
git submodule update --init sp800_22_tests-py     # once, after cloning
./qrng.py nist22 data/geiger-*.csv                # conditioned output
./qrng.py nist22 data/geiger-*.csv --raw          # unconditioned low-k-bit symbols
```

`--suite PATH` or `$SP800_22_DIR` point at a different copy. Reports note when the suite is missing instead of failing.

It needs 1,028,016 bits (about 129 KB) for all 15 tests. `nist22` reports tests it can't run validly as `SKIPPED` (the suite itself calls some of them FAIL, and returns bogus P-values for the rank test below 38,912 bits), and skips the two random-excursion tests when the walk has fewer than 500 cycles. Its non-overlapping template test picks a single random template per call, so it is repeated 20 times and judged by proportion.
Roughly 177,000 events (~7.5 hours at 390 CPM) give that much conditioned output. The suite is pure Python but quick: about 30 s for 1 Mbit (plus ~1 min for the 20 repeated template draws).

### Combining captures

Each `capture` run writes its own file and never overwrites an existing one (`--force` to override). `analyze`, `generate` and `dashboard` accept any number of files and pool them:

```
./qrng.py analyze data/geiger-2026101*.csv --png all.png
./qrng.py generate data/geiger-*.csv -n 1024 -o random.bin
```

Runs are placed on one timeline by the `start=` time in each file's header, so breaks show as real gaps in the rate plot. Every run is a separate segment and no gap is ever measured across a break (a stopped run costs you one interval, nothing more), so pooling is statistically sound. Files from different tick rates are refused, and mixing in simulated data is flagged in red in the report.

### Simulation

No hardware handy (or a demo that must not stall)?

```
./qrng.py capture --simulate 30 --speed 100 -o data/sim.csv   # PRNG Poisson events, labelled SIMULATED, NOT quantum
./qrng.py capture --replay data/real-overnight.csv --speed 200 -o data/live.csv   # real data, sped up
```

## How it works

1. Each pulse is a decay event; gaps between pulses are exponentially distributed (the dashboard overlays the fit).
2. Low `k` bits (default 8) of each gap in 0.5 µs ticks are essentially uniform: the gap is seconds long, the low bits span ~128 µs.
3. Conditioning: SHA-256 over enough symbols to carry 256 + 64 bits of **measured** SP 800-90B MCV min-entropy per 256-bit block.
4. Also shown for teaching: the classic "compare two gaps" bit method (~1 bit per 3 events).

## Caveats

- **Rate is the limit.** M4011 background is ~20–40 CPM → ~0.5 events/s → one 32-byte block per ~90 s. For a live demo, put a source near the tube (thoriated lantern mantle, uranium glass, old Am-241 smoke detector element, KCl salt substitute) to get hundreds of CPM. Record a long background run beforehand and use `--replay` as a fallback.
- The MCV estimate is a conservative single estimator; for serious claims run NIST's `ea_non_iid` on the `--raw` output, and `dieharder`/`rngtest` on the conditioned output.
- **Lost events are detected, not hidden.** The firmware numbers every pulse in the interrupt handler, so an event lost anywhere (ring overflow, serial, USB) leaves a gap in `seq`, and no interval is ever measured across it. For files without that guarantee, any gap longer than 20x the typical gap is also split out and reported as an `OUTAGE`.
- **Don't let the laptop sleep during a capture.** USB serial data is lost while it's suspended (a 168 s sleep once silently removed ~1,000 events). Inhibit sleep for the run, e.g. `systemd-inhibit --what=sleep:idle ./qrng.py capture`. Re-flash the firmware after pulling this change so `seq` comes from the Mega.

## More Information

* [ESPHome Devices page on RadiationD v1.1(CAJOE)](https://devices.esphome.io/devices/geiger-counter/)
* [GitHub repo on ESP32 based Geiger counter](https://github.com/SensorsIot/Geiger-Counter-RadiationD-v1.1-CAJOE-) (includes [video tutorial](https://youtu.be/K28Az3-gV7E)) with ESP32
* [Arduino fork of above](https://github.com/rassware/Geiger-Counter-RadiationD-v1.1-CAJOE-)
* [Blog post on assembling the kit](https://www.richardmudhar.com/blog/2023/10/cajoe-ebay-geiger-counter-kit/)
