#include "speaker.h"
#include "proto.h"

// =============================================================
//  TONE GENERATION
//
//  tone() drives only one pin at a time, but choice trials need
//  the alcohol tone on one side and the water tone on the other
//  simultaneously. So each speaker gets its own 16-bit timer run
//  in Fast PWM mode 14 (TOP = ICRn):
//
//      frequency = F_CPU / (prescaler * (1 + ICRn))
//      duty      = OCRnA / (1 + ICRn)      -> "volume"
//
//  Pin 6 = OC4A (Timer4) = LEFT
//  Pin 5 = OC3A (Timer3) = RIGHT
//
//  Only channel A of each timer is connected, so OC3B/OC3C
//  (pins 2 and 3) stay disconnected and remain under normal
//  PORTE control for the LED software PWM.
//
//  Click trains are produced by gating the timer output on and
//  off in spkUpdate() using micros(). The gate operation itself
//  is a single register write; timing accuracy is therefore set
//  by how often loop() runs. With a non-blocking main loop that
//  is well under 100 us, which is <4% of a 2.5 ms half-period at
//  the 200 Hz worst case. If you later need sample-accurate
//  clicks, move the gating into a Timer1 compare ISR.
// =============================================================

struct SpkState {
  bool     active;
  uint32_t startMs;
  uint32_t durationMs;    // 0 = indefinite
  uint32_t toneHz;
  bool     clickMode;
  uint16_t clickHz;
  uint8_t  volPct;
  uint32_t onUs;
  uint32_t offUs;
  uint32_t nextEdgeUs;
  bool     gateOpen;
};

static SpkState g_spk[SPK_COUNT];

static const char* const SPK_NAMES[SPK_COUNT] = {"L", "R"};

const char* spkChName(uint8_t ch) {
  if (ch >= SPK_COUNT) return "?";
  return SPK_NAMES[ch];
}

uint8_t spkChFromChar(char c) {
  if (c >= 'A' && c <= 'Z') c += 32;
  switch (c) {
    case 'l': return SPK_L;
    case 'r': return SPK_R;
    default:  return SPK_COUNT;
  }
}

// -------------------------------------------------------------
//  Low-level timer control
// -------------------------------------------------------------

static const uint16_t PRESCALE_DIV[5]  = {1, 8, 64, 256, 1024};
static const uint8_t  PRESCALE_BITS[5] = {1, 2, 3, 4, 5};

// Picks the smallest prescaler that keeps TOP within 16 bits,
// which maximises duty-cycle resolution.
static bool computeTimerSettings(uint32_t freqHz, uint8_t volPct,
                                 uint16_t* topOut, uint16_t* ocrOut,
                                 uint8_t* csOut) {
  for (uint8_t k = 0; k < 5; k++) {
    uint32_t top = (F_CPU / ((uint32_t)PRESCALE_DIV[k] * freqHz));
    if (top == 0) continue;
    top -= 1;
    if (top <= 65535UL) {
      if (top < 3) return false;          // too little duty resolution
      uint32_t ocr = ((top + 1UL) * (uint32_t)volPct) / 100UL;
      if (ocr == 0 && volPct > 0) ocr = 1;
      if (ocr > top) ocr = top;
      *topOut = (uint16_t)top;
      *ocrOut = (uint16_t)ocr;
      *csOut  = PRESCALE_BITS[k];
      return true;
    }
  }
  return false;
}

static void timerConfigure(uint8_t ch, uint16_t top, uint16_t ocr,
                           uint8_t cs) {
  uint8_t sreg = SREG;
  cli();
  if (ch == SPK_R) {                       // Timer3, pin 5, OC3A
    TCCR3A = _BV(WGM31);                   // COM cleared: gate closed
    TCCR3B = _BV(WGM33) | _BV(WGM32) | cs;
    ICR3   = top;
    OCR3A  = ocr;
    TCNT3  = 0;
  } else {                                 // Timer4, pin 6, OC4A
    TCCR4A = _BV(WGM41);
    TCCR4B = _BV(WGM43) | _BV(WGM42) | cs;
    ICR4   = top;
    OCR4A  = ocr;
    TCNT4  = 0;
  }
  SREG = sreg;
}

static void timerRelease(uint8_t ch) {
  uint8_t sreg = SREG;
  cli();
  if (ch == SPK_R) {
    TCCR3A = 0;
    TCCR3B = 0;
    PORTE &= (uint8_t)~_BV(3);             // pin 5 = PE3 low
  } else {
    TCCR4A = 0;
    TCCR4B = 0;
    PORTH &= (uint8_t)~_BV(3);             // pin 6 = PH3 low
  }
  SREG = sreg;
}

// Connect / disconnect the compare output. Used for click gating.
static inline void spkGate(uint8_t ch, bool open) {
  uint8_t sreg = SREG;
  cli();
  if (ch == SPK_R) {
    if (open) {
      TCCR3A |= _BV(COM3A1);
    } else {
      TCCR3A &= (uint8_t)~_BV(COM3A1);
      PORTE  &= (uint8_t)~_BV(3);
    }
  } else {
    if (open) {
      TCCR4A |= _BV(COM4A1);
    } else {
      TCCR4A &= (uint8_t)~_BV(COM4A1);
      PORTH  &= (uint8_t)~_BV(3);
    }
  }
  SREG = sreg;
}

// -------------------------------------------------------------

void spkBegin() {
  pinMode(PIN_SPK_L, OUTPUT);
  pinMode(PIN_SPK_R, OUTPUT);
  digitalWrite(PIN_SPK_L, LOW);
  digitalWrite(PIN_SPK_R, LOW);
  for (uint8_t k = 0; k < SPK_COUNT; k++) {
    g_spk[k].active = false;
    timerRelease(k);
  }
}

bool spkStart(uint8_t ch, uint32_t durMs, uint32_t toneHz,
              bool clickMode, uint16_t clickHz, uint8_t volPct,
              uint16_t clickOnUs) {
  if (ch >= SPK_COUNT) { emitErr(F("SPK_BAD_CHANNEL")); return false; }
  if (durMs > CUE_MAX_DURATION_MS) { emitErr(F("SPK_DURATION_RANGE")); return false; }
  if (toneHz < SPK_FREQ_MIN_HZ || toneHz > SPK_FREQ_MAX_HZ) {
    emitErr(F("SPK_TONE_HZ_RANGE")); return false;
  }
  if (clickMode && (clickHz == 0 || clickHz > CLICK_FREQ_MAX_HZ)) {
    emitErr(F("SPK_CLICK_HZ_RANGE")); return false;
  }
  if (volPct > SPK_MAX_DUTY_PCT) volPct = SPK_MAX_DUTY_PCT;

  // A click train must contain whole cycles of the carrier.
  if (clickMode && (uint32_t)clickHz * 4UL > toneHz) {
    emitErr(F("SPK_CLICK_HZ_TOO_HIGH_FOR_TONE")); return false;
  }

  uint16_t top, ocr; uint8_t cs;
  if (!computeTimerSettings(toneHz, volPct, &top, &ocr, &cs)) {
    emitErr(F("SPK_FREQ_UNREACHABLE")); return false;
  }

  SpkState& S = g_spk[ch];
  uint32_t now = millis();

  S.active     = true;
  S.startMs    = now;
  S.durationMs = durMs;
  S.toneHz     = toneHz;
  S.clickMode  = clickMode;
  S.clickHz    = clickHz;
  S.volPct     = volPct;

  timerConfigure(ch, top, ocr, cs);

  if (clickMode) {
    uint32_t periodUs = 1000000UL / (uint32_t)clickHz;
    uint32_t onUs = (clickOnUs > 0) ? (uint32_t)clickOnUs : (periodUs / 2UL);
    uint32_t maxOn = (periodUs * 9UL) / 10UL;
    if (onUs > maxOn) onUs = maxOn;
    if (onUs < 1) onUs = 1;
    S.onUs  = onUs;
    S.offUs = periodUs - onUs;
    S.gateOpen   = true;
    S.nextEdgeUs = micros() + S.onUs;
  } else {
    S.onUs = S.offUs = 0;
    S.gateOpen = true;
  }

  spkGate(ch, true);

  emitEvent(now, "SPK_ON", spkChName(ch), (long)toneHz,
            clickMode ? (long)clickHz : 0L);
  return true;
}

void spkStop(uint8_t ch) {
  if (ch >= SPK_COUNT) return;
  if (!g_spk[ch].active) return;
  uint32_t now = millis();
  g_spk[ch].active = false;
  spkGate(ch, false);
  timerRelease(ch);
  emitEvent(now, "SPK_OFF", spkChName(ch), 0, 0);
}

void spkStopAll() {
  for (uint8_t k = 0; k < SPK_COUNT; k++) spkStop(k);
}

bool spkIsActive(uint8_t ch) {
  if (ch >= SPK_COUNT) return false;
  return g_spk[ch].active;
}

void spkUpdate() {
  uint32_t nowMs = millis();
  uint32_t nowUs = micros();

  for (uint8_t k = 0; k < SPK_COUNT; k++) {
    SpkState& S = g_spk[k];
    if (!S.active) continue;

    if (S.durationMs > 0 && (nowMs - S.startMs) >= S.durationMs) {
      spkStop(k);
      continue;
    }

    if (S.clickMode && (int32_t)(nowUs - S.nextEdgeUs) >= 0) {
      S.gateOpen = !S.gateOpen;
      spkGate(k, S.gateOpen);
      S.nextEdgeUs += S.gateOpen ? S.onUs : S.offUs;
      if ((int32_t)(nowUs - S.nextEdgeUs) >= 0) {
        S.nextEdgeUs = nowUs + (S.gateOpen ? S.onUs : S.offUs);
      }
    }
  }
}
