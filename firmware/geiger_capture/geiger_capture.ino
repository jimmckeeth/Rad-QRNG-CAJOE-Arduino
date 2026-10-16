/*
  geiger_capture.ino  -  hardware-timestamp every pulse from a RadiationD-v1.1 (CAJOE) board.

  Target: Arduino Mega 2560 (5 V logic, so the CAJOE pulse output connects directly).

  Wiring (CAJOE 3-pin header -> Mega):
    GND            -> GND
    5V             -> 5V
    VIN (= pulse)  -> pin 49  (ICP4, Timer4 input capture)

  Timer4 runs free at 2 MHz (16 MHz / 8) -> 0.5 us resolution. The capture hardware latches the
  timer the instant the edge arrives, so interrupt latency adds no jitter. The 16-bit timer is
  extended to 48 bits in software (~4.5 years before wrap).

  Output, 115200 baud, one line per event:   <seq>,<ticks>
  seq numbers every pulse the hardware saw, so any event lost anywhere (full ring, serial, USB)
  leaves a visible gap in seq and the host never measures an interval across it.
  Lines starting with '#' are comments (header, heartbeat, drop count).
*/

#define TICK_HZ 2000000UL
#define CAPTURE_RISING 0   // 0 = falling edge (as in the SensorsIot sketch), 1 = rising edge

static const uint8_t RING = 64;                 // power of two
struct Ev { uint64_t t; uint32_t n; };
volatile Ev ring[RING];
volatile uint32_t evCount = 0;                  // numbers EVERY pulse, including ones that get dropped
volatile uint8_t head = 0;                      // written by ISR
uint8_t tail = 0;                               // read by loop()
volatile uint32_t ovf = 0;                      // Timer4 overflow count (upper bits)
volatile uint32_t dropped = 0;

ISR(TIMER4_OVF_vect) {
  ovf++;
}

ISR(TIMER4_CAPT_vect) {
  uint16_t lo = ICR4;
  uint32_t hi = ovf;
  // An overflow may have happened just before the capture with its ISR still pending
  // (interrupts are off in here). A small capture value means it was after the wrap.
  if ((TIFR4 & _BV(TOV4)) && lo < 0x8000) hi++;
  uint32_t n = evCount++;
  uint8_t next = (head + 1) & (RING - 1);
  if (next == tail) { dropped++; return; }      // ring full: the missing n shows up as a seq gap on the host
  ring[head].t = ((uint64_t)hi << 16) | lo;
  ring[head].n = n;
  head = next;
}

static void printU64(uint64_t v) {
  char buf[21];
  uint8_t i = sizeof(buf);
  buf[--i] = 0;
  do { buf[--i] = '0' + (uint8_t)(v % 10); v /= 10; } while (v);
  Serial.print(&buf[i]);
}

void setup() {
  Serial.begin(115200);
  pinMode(49, INPUT);

  Serial.print(F("# qrng-geiger tick_hz="));
  Serial.print(TICK_HZ);
  Serial.println(CAPTURE_RISING ? F(" edge=rising") : F(" edge=falling"));

  noInterrupts();
  TCCR4A = 0;
  TCCR4B = _BV(CS41) | (CAPTURE_RISING ? _BV(ICES4) : 0);   // clk/8, edge select
  TCNT4 = 0;
  TIFR4 = _BV(ICF4) | _BV(TOV4);
  TIMSK4 = _BV(ICIE4) | _BV(TOIE4);
  interrupts();
}

void loop() {
  static uint32_t lastBeat = 0;

  while (tail != head) {
    uint64_t t;
    uint32_t n;
    noInterrupts();
    t = ring[tail].t;
    n = ring[tail].n;
    interrupts();
    tail = (tail + 1) & (RING - 1);
    Serial.print(n);
    Serial.print(',');
    printU64(t);
    Serial.print('\n');
  }

  uint32_t now = millis();
  if (now - lastBeat >= 10000) {
    lastBeat = now;
    Serial.print(F("# alive s="));
    Serial.print(now / 1000);
    Serial.print(F(" dropped="));
    Serial.println(dropped);
  }
}
