# Hardware Build

* [Build of Materials](#Build-of-Materials)
* [Wiring](#Wiring)
* [Flashing Firmware](#Flashing_Firmware)
* [More Pictures](#More-Pictures)
* [Usage](./README.md)

![rad-qrng-assembled](./docs/rad-qrng-assembled.webp)

[Video overview](https://youtu.be/PPCGsez5wok)



## Build of Materials

* **Geiger Counter Kit** ~ $40 USD
  * It is labeled “**RadiationD-v1.1 (CAJOE) kit**” and comes with a few different tubes, I used the **M4011 tube** that cane with mine. Is frequently available on AliExpress, [ebay](https://ebay.io/m/KHoTNR), and [Amazon](https://a.co/d/02xt5Qd4). The kit is frequently pre-assembled. ![Assembled geiger counter “Radiation D-v1.1(CAJOE)”](./docs/RadiationD-v1.1(CAJOE).webp)
  * I didn’t attach the cover on mine since it prevents access to the pins. 
* **Arduino Mega 2560** ~ $50 USD
  * Probably any Arduino would work, like the cheaper Uno, but the MEGA was what I had on hand.
  * The important thing is the board needs a **5V OUT** to power the Geiger Counter. So if you have a *Bill and Ted’s Excellent* micro-controller with 5V OUT and you know how to program it, then that will probably work too.
  * I frequently buy mine from Adafruit, Sparkfun, Digi-Key, ELEGOO, or Mouser, but I’m sure most brands/resellers should work.
  * An ESP32, Pi, or Pico would need a voltage divider or level shifter, e.g. 2 kΩ + 3.3 kΩ.
  * You will also need a USB connector to your computer based on whatever your board and computer requires.
* 3x **Male-Female Jumper Wires**![Male-Female Jumper Wires](./docs/jumper-wires.webp)
  * These usually come in a 40 wire bundles. My kit came with 3, but they were the wrong style. I frequently buy ELEGOO brand, and that was what I had on hand, but I’m sure most brands work fine.
* **Radioactive Source**
  * I tested both an ionizing smoke detector, and uranium glass. I believe the ionizing ones are the most popular smoke detectors so you probably have one in your house.  They usually have a warning on the back.
  * I got a small pouch of [uranium glass beads](https://a.co/d/0eXi3L45) on Amazon for $10. They were sold as *“Bohemia Crystal Valley Czech 50-Piece Glass Beads, 6x9mm, Teardrop Transparent Yellow Uranium Glass (Glows Under UV), Small”*
    * The indication that it *glows under UV* is a good sign that it is *real* uranium glass. I frequently encounter listings that claim to be “uranium glass” but really mean “uranium colored” and they  will not glow and will not work.
  * Feel free to try other sources.
* *(Optional)* **Tube Upgrade** ~$50
  * I read that the SBM-20 Geiger-Muller Tube might be an upgrade, but it is a longer length, so it would require modification to the board.
  * I also read that the M4011 tube is the better of the two tubes that frequently come with the kit.
  * If you plan to upgrade, then you can sometimes find the [kit without a tube](https://a.co/d/0aGlxjXK) for less money.
  * Here is a [comparison of different tube options](https://radmon.org/index.php/forum/geiger-muller-tubes/1463-glass-tube-shootout).
  * Your millage may vary, but I just wanted to mention it as an option.  

The stronger the radiation source and more sensitive the tube, the faster your system will gather entropy. 

## Wiring

This powers the Geiger-counter via the Arduino and captures the events with timestamps.

| CAJOE header                         | Arduino header  |
| ------------------------------------ | --------------- |
| `GND`                                | `GND`           |
| 5V                                   | `5V`            |
| `VIN` (this is the **pulse output**) | `pin 49 (ICP4)` |

The pulse is 5V logic which the Mega handles directly. 

**Silencing the buzzer:** pull the jumper cap on `J1` (next to `R20`/`C66`). The buzzer stops clicking; the pulse output and the pulse LED (`D23`) are unaffected. Put the cap back to re-enable it.

## Flashing Firmware

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

## More Pictures

Arduino End - Pin 49 goes to VIN on Cajoe ![arduino-end.avif](docs/arduino-end.avif) 

 Arduino Side - 5V and GND goes to 5V and GND on Cajoe![arduino side](docs/arduino-side.avif) 

 Cajoe![cajoe](docs/cajoe.avif) 
