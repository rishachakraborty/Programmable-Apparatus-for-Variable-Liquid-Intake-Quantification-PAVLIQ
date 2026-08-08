#include "led.h"
#include "proto.h"

// =============================================================
//  SOFTWARE PWM
//
//  Timer3 is owned by the right speaker and Timer0 by millis(),
//  so pins 2, 3 and 4 cannot use analogWrite(). Instead Timer2
//  fires a compare interrupt at 31.25 kHz; an 8-bit counter wraps
//  every 256 interrupts, giving a 122 Hz PWM carrier with 256
//  brightness steps. 122 Hz is above the mouse flicker-fusion
//  threshold, so a "steady" LED is perceived as steady.
//
//  The ISR uses direct port writes because digitalWrite() (~5 us)
//  would consume most of the 32 us interrupt budget. As written
//  the ISR is ~2 us, roughly 6% CPU.
// =============================================================

static volatile uint8_t g_duty[LED_COUNT] = {0, 0, 0};
static volatile uint8_t g_pwmCount = 0;

ISR(TIMER2_COMPA_vect) {
  uint8_t c = g_pwmCount++;
  uint8_t setE = 0, clrE = 0;

  if (c == 0) {
    if (g_duty[LED_W]) setE |= LED_W_MASK;
    if (g_duty[LED_B]) setE |= LED_B_MASK;
    if (g_duty[LED_G]) LED_G_PORT |= LED_G_MASK;
  }
  if (c == g_duty[LED_W]) clrE |= LED_W_MASK;
  if (c == g_duty[LED_B]) clrE |= LED_B_MASK;
  if (c == g_duty[LED_G]) LED_G_PORT &= (uint8_t)~LED_G_MASK;

  if (setE) LED_W_PORT |= setE;      // LED_W_PORT and LED_B_PORT
  if (clrE) LED_W_PORT &= (uint8_t)~clrE;  // are both PORTE
}

static inline void setDuty(uint8_t ch, uint8_t d) {
  uint8_t sreg = SREG;
  cli();
  g_duty[ch] = d;
  SREG = sreg;
}

// =============================================================
//  Per-channel cue state
// =============================================================

struct LedState {
  bool     active;
  uint8_t  brightness;
  bool     pulsing;
  uint16_t pulseHz;
  uint32_t startMs;
  uint32_t durationMs;   // 0 = indefinite
  uint32_t halfPeriodUs;
  uint32_t nextToggleUs;
  bool     phaseOn;
};

static LedState g_led[LED_COUNT];

static const char* const LED_NAMES[LED_COUNT] = {"W", "B", "G"};

const char* ledChName(uint8_t ch) {
  if (ch >= LED_COUNT) return "?";
  return LED_NAMES[ch];
}

uint8_t ledChFromChar(char c) {
  if (c >= 'A' && c <= 'Z') c += 32;
  switch (c) {
    case 'w': return LED_W;
    case 'b': return LED_B;
    case 'g': return LED_G;
    default:  return LED_COUNT;
  }
}

// =============================================================

void ledBegin() {
  pinMode(PIN_LED_W, OUTPUT);
  pinMode(PIN_LED_B, OUTPUT);
  pinMode(PIN_LED_G, OUTPUT);
  digitalWrite(PIN_LED_W, LOW);
  digitalWrite(PIN_LED_B, LOW);
  digitalWrite(PIN_LED_G, LOW);

  for (uint8_t k = 0; k < LED_COUNT; k++) {
    g_led[k].active = false;
    g_duty[k] = 0;
  }

  // Timer2: CTC mode, prescaler 8, OCR2A = 63
  //   f = 16 MHz / (8 * (63 + 1)) = 31250 Hz
  //   PWM carrier = 31250 / 256 = 122.07 Hz
  uint8_t sreg = SREG;
  cli();
  TCCR2A = _BV(WGM21);              // CTC
  TCCR2B = _BV(CS21);               // prescaler 8
  OCR2A  = 63;
  TCNT2  = 0;
  TIMSK2 = _BV(OCIE2A);             // enable compare-A interrupt
  SREG = sreg;
}

bool ledStart(uint8_t ch, uint32_t durMs, bool pulsing,
              uint16_t pulseHz, uint8_t brightness) {
  if (ch >= LED_COUNT) { emitErr(F("LED_BAD_CHANNEL")); return false; }
  if (durMs > CUE_MAX_DURATION_MS) { emitErr(F("LED_DURATION_RANGE")); return false; }
  if (pulsing && (pulseHz == 0 || pulseHz > PULSE_FREQ_MAX_HZ)) {
    emitErr(F("LED_PULSE_HZ_RANGE")); return false;
  }

  LedState& L = g_led[ch];
  uint32_t now = millis();

  L.active       = true;
  L.brightness   = brightness;
  L.pulsing      = pulsing;
  L.pulseHz      = pulseHz;
  L.startMs      = now;
  L.durationMs   = durMs;
  L.phaseOn      = true;

  if (pulsing) {
    L.halfPeriodUs = 500000UL / (uint32_t)pulseHz;
    L.nextToggleUs = micros() + L.halfPeriodUs;
  } else {
    L.halfPeriodUs = 0;
  }

  setDuty(ch, brightness);
  emitEvent(now, "LED_ON", ledChName(ch), brightness,
            pulsing ? (long)pulseHz : 0L);
  return true;
}

void ledStop(uint8_t ch) {
  if (ch >= LED_COUNT) return;
  if (!g_led[ch].active) return;
  uint32_t now = millis();
  g_led[ch].active = false;
  setDuty(ch, 0);
  emitEvent(now, "LED_OFF", ledChName(ch), 0, 0);
}

void ledStopAll() {
  for (uint8_t k = 0; k < LED_COUNT; k++) ledStop(k);
}

bool ledIsActive(uint8_t ch) {
  if (ch >= LED_COUNT) return false;
  return g_led[ch].active;
}

void ledUpdate() {
  uint32_t nowMs = millis();
  uint32_t nowUs = micros();

  for (uint8_t k = 0; k < LED_COUNT; k++) {
    LedState& L = g_led[k];
    if (!L.active) continue;

    if (L.durationMs > 0 && (nowMs - L.startMs) >= L.durationMs) {
      ledStop(k);
      continue;
    }

    if (L.pulsing && (int32_t)(nowUs - L.nextToggleUs) >= 0) {
      L.phaseOn = !L.phaseOn;
      setDuty(k, L.phaseOn ? L.brightness : 0);
      L.nextToggleUs += L.halfPeriodUs;
      // Resynchronise if we fell far behind (should not happen).
      if ((int32_t)(nowUs - L.nextToggleUs) >= 0) {
        L.nextToggleUs = nowUs + L.halfPeriodUs;
      }
    }
  }
}
