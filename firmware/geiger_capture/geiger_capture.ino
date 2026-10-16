/*
  geiger_capture.ino  -  hardware-timestamp every pulse from a RadiationD-v1.1 (CAJOE) board.

  Targets: Arduino Mega 2560 and Arduino UNO (5 V logic, so the CAJOE pulse output connects
  directly).

  Wiring (CAJOE 3-pin header -> Arduino):
    GND            -> GND
    5V             -> 5V
    VIN (= pulse)  -> Mega pin 49 (ICP4) or UNO pin 8 (ICP1)

  The board's 16-bit timer runs at 2 MHz (16 MHz / 8) -> 0.5 us resolution. The capture hardware
  latches the timer the instant the edge arrives, so interrupt latency adds no jitter. The timer
  is extended to 48 bits in software (~4.5 years before wrap).

  Output, one line per event:   <seq>,<ticks>
  seq numbers every pulse the hardware saw, so any event lost anywhere (full ring, serial, USB)
  leaves a visible gap in seq and the host never measures an interval across it.
  Lines starting with '#' are comments (header, heartbeat, drop count).
*/

// ------------------------------------------------------------------------- User settings

static const uint32_t SERIAL_BAUD = 115200;
static const uint32_t HEARTBEAT_MS = 10000;
static const uint16_t TIMER_PRESCALER = 8;
static const uint8_t RING_SIZE = 64;  // Must be a power of two and no greater than 128.
static const bool CAPTURE_RISING = false;

// ------------------------------------------------------------------------- Board mapping

#if defined(__AVR_ATmega2560__)
  static const uint8_t CAPTURE_PIN = 49;  // ICP4
  #define CAPTURE_TIMER_OVF_VECTOR TIMER4_OVF_vect
  #define CAPTURE_TIMER_CAPT_VECTOR TIMER4_CAPT_vect
  #define CAPTURE_TIMER_CONTROL_A TCCR4A
  #define CAPTURE_TIMER_CONTROL_B TCCR4B
  #define CAPTURE_TIMER_COUNTER TCNT4
  #define CAPTURE_TIMER_INPUT ICR4
  #define CAPTURE_TIMER_FLAGS TIFR4
  #define CAPTURE_TIMER_MASK TIMSK4
  #define CAPTURE_TIMER_CLOCK_1 CS40
  #define CAPTURE_TIMER_CLOCK_8 CS41
  #define CAPTURE_TIMER_CLOCK_64 (_BV(CS41) | _BV(CS40))
  #define CAPTURE_TIMER_CLOCK_256 CS42
  #define CAPTURE_TIMER_CLOCK_1024 (_BV(CS42) | _BV(CS40))
  #define CAPTURE_TIMER_EDGE_BIT ICES4
  #define CAPTURE_TIMER_INPUT_FLAG ICF4
  #define CAPTURE_TIMER_OVERFLOW_FLAG TOV4
  #define CAPTURE_TIMER_INPUT_ENABLE ICIE4
  #define CAPTURE_TIMER_OVERFLOW_ENABLE TOIE4
  #define BOARD_NAME "mega2560"
#elif defined(__AVR_ATmega328P__)
  static const uint8_t CAPTURE_PIN = 8;   // ICP1 (Arduino UNO)
  #define CAPTURE_TIMER_OVF_VECTOR TIMER1_OVF_vect
  #define CAPTURE_TIMER_CAPT_VECTOR TIMER1_CAPT_vect
  #define CAPTURE_TIMER_CONTROL_A TCCR1A
  #define CAPTURE_TIMER_CONTROL_B TCCR1B
  #define CAPTURE_TIMER_COUNTER TCNT1
  #define CAPTURE_TIMER_INPUT ICR1
  #define CAPTURE_TIMER_FLAGS TIFR1
  #define CAPTURE_TIMER_MASK TIMSK1
  #define CAPTURE_TIMER_CLOCK_1 CS10
  #define CAPTURE_TIMER_CLOCK_8 CS11
  #define CAPTURE_TIMER_CLOCK_64 (_BV(CS11) | _BV(CS10))
  #define CAPTURE_TIMER_CLOCK_256 CS12
  #define CAPTURE_TIMER_CLOCK_1024 (_BV(CS12) | _BV(CS10))
  #define CAPTURE_TIMER_EDGE_BIT ICES1
  #define CAPTURE_TIMER_INPUT_FLAG ICF1
  #define CAPTURE_TIMER_OVERFLOW_FLAG TOV1
  #define CAPTURE_TIMER_INPUT_ENABLE ICIE1
  #define CAPTURE_TIMER_OVERFLOW_ENABLE TOIE1
  #define BOARD_NAME "uno"
#else
  #error "Unsupported board: select Arduino Mega 2560 or Arduino UNO"
#endif

static const uint32_t TICK_HZ = F_CPU / TIMER_PRESCALER;
static_assert(TIMER_PRESCALER == 1 || TIMER_PRESCALER == 8 || TIMER_PRESCALER == 64
              || TIMER_PRESCALER == 256 || TIMER_PRESCALER == 1024,
              "TIMER_PRESCALER must be 1, 8, 64, 256, or 1024");
static_assert(RING_SIZE && !(RING_SIZE & (RING_SIZE - 1)), "RING_SIZE must be a power of two");
static_assert(RING_SIZE <= 128, "RING_SIZE must fit the 8-bit ring indexes");

static const uint8_t TIMER_CLOCK_BITS =
    TIMER_PRESCALER == 1 ? _BV(CAPTURE_TIMER_CLOCK_1) :
    TIMER_PRESCALER == 8 ? _BV(CAPTURE_TIMER_CLOCK_8) :
    TIMER_PRESCALER == 64 ? CAPTURE_TIMER_CLOCK_64 :
    TIMER_PRESCALER == 256 ? _BV(CAPTURE_TIMER_CLOCK_256) : CAPTURE_TIMER_CLOCK_1024;

struct Ev { uint64_t t; uint32_t n; };
volatile Ev ring[RING_SIZE];
volatile uint32_t evCount = 0;                  // numbers EVERY pulse, including ones that get dropped
volatile uint8_t head = 0;                      // written by ISR
uint8_t tail = 0;                               // read by loop()
volatile uint32_t ovf = 0;                      // hardware timer overflow count (upper bits)
volatile uint32_t dropped = 0;

ISR(CAPTURE_TIMER_OVF_VECTOR) {
  ovf++;
}

ISR(CAPTURE_TIMER_CAPT_VECTOR) {
  uint16_t lo = CAPTURE_TIMER_INPUT;
  uint32_t hi = ovf;
  // An overflow may have happened just before the capture with its ISR still pending
  // (interrupts are off in here). A small capture value means it was after the wrap.
  if ((CAPTURE_TIMER_FLAGS & _BV(CAPTURE_TIMER_OVERFLOW_FLAG)) && lo < 0x8000) hi++;
  uint32_t n = evCount++;
  uint8_t next = (head + 1) & (RING_SIZE - 1);
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
  Serial.begin(SERIAL_BAUD);
  pinMode(CAPTURE_PIN, INPUT);

  Serial.print(F("# qrng-geiger tick_hz="));
  Serial.print(TICK_HZ);
  Serial.print(F(" board=" BOARD_NAME " capture_pin="));
  Serial.print(CAPTURE_PIN);
  Serial.println(CAPTURE_RISING ? F(" edge=rising") : F(" edge=falling"));

  noInterrupts();
  CAPTURE_TIMER_CONTROL_A = 0;
  CAPTURE_TIMER_CONTROL_B = TIMER_CLOCK_BITS | (CAPTURE_RISING ? _BV(CAPTURE_TIMER_EDGE_BIT) : 0);
  CAPTURE_TIMER_COUNTER = 0;
  CAPTURE_TIMER_FLAGS = _BV(CAPTURE_TIMER_INPUT_FLAG) | _BV(CAPTURE_TIMER_OVERFLOW_FLAG);
  CAPTURE_TIMER_MASK = _BV(CAPTURE_TIMER_INPUT_ENABLE) | _BV(CAPTURE_TIMER_OVERFLOW_ENABLE);
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
    tail = (tail + 1) & (RING_SIZE - 1);
    Serial.print(n);
    Serial.print(',');
    printU64(t);
    Serial.print('\n');
  }

  uint32_t now = millis();
  if (now - lastBeat >= HEARTBEAT_MS) {
    lastBeat = now;
    Serial.print(F("# alive s="));
    Serial.print(now / 1000);
    Serial.print(F(" dropped="));
    Serial.println(dropped);
  }
}
