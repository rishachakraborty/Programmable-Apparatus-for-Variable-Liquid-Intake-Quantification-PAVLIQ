#include "lick.h"
#include "proto.h"
#include "solenoids.h"

// =============================================================
//  LICK DETECTION
//
//  A resistive lickometer has no absolute scale. The resting value
//  depends on electrode geometry, wiring, tubing, humidity, how much
//  saliva is on the spout, and the individual board's ADC. A number
//  measured on one rig is meaningless on another and drifts on the
//  same rig within a session. So NOTHING is hardcoded: the firmware
//  measures the resting distribution, derives thresholds from it,
//  and keeps tracking slow drift while the session runs.
//
//  Robustness comes from four independent mechanisms:
//
//   1. HYSTERESIS. Declaring a lick takes a larger excursion than
//      ending one, so a signal sitting near threshold cannot chatter.
//   2. MINIMUM DURATION. A crossing must persist to count, which
//      rejects electrical spikes. The reported timestamp is the
//      original crossing, not the confirmation, so no accuracy lost.
//   3. REFRACTORY PERIOD. Blocks re-triggering on contact bounce.
//   4. SOLENOID BLANKING. Samples taken while a gate is switching
//      are discarded outright - that transient is not a lick.
//
//  Baseline tracking runs only while idle AND only on samples close
//  to the current baseline, so a long tongue contact can never drag
//  the baseline along with it.
// =============================================================

enum LickState : uint8_t {
  L_IDLE = 0, L_CAND_ON, L_ON, L_CAND_OFF
};

enum CalMode : uint8_t {
  CAL_NONE = 0, CAL_BASELINE, CAL_TOUCH, CAL_STREAM
};

struct LickChan {
  uint8_t   pin;
  bool      enabled;
  bool      calibrated;

  float     baseline;      // resting level, ADC counts
  float     sd;            // resting noise
  float     onDelta;       // excursion needed to declare a lick
  float     offDelta;      // excursion below which the lick ends
  int8_t    polarity;      // +1 lick raises the value, -1 lick lowers it

  LickState state;
  uint32_t  candOnMs;
  uint32_t  candOffMs;
  uint32_t  onMs;
  uint32_t  refractoryUntilMs;
  uint32_t  count;
  int16_t   lastRaw;
  bool      present;

  // calibration accumulators
  CalMode   cal;
  uint32_t  calEndMs;
  uint32_t  calN;
  uint32_t  calSum;
  uint32_t  calSumSq;
  uint32_t  calNextPrintMs;
  int16_t   calMin;
  int16_t   calMax;
};

static LickChan g_lk[LICK_COUNT];
static uint8_t  g_cursor = 0;          // round-robin channel index
static uint32_t g_nextSampleUs = 0;
static uint32_t g_lastEventMs = 0;
static float    g_baselineAlpha = 0.0f;

// Timing windows are runtime-tunable so a rig can be adjusted without
// a reflash. Defaults come from config.h.
static uint16_t g_minOnMs     = LICK_MIN_ON_MS;
static uint16_t g_minOffMs    = LICK_MIN_OFF_MS;
static uint16_t g_refractMs   = LICK_REFRACTORY_MS;

static const char* const LK_NAMES[LICK_COUNT] = {"L", "C", "R"};

const char* lickChName(uint8_t ch) {
  if (ch >= LICK_COUNT) return "?";
  return LK_NAMES[ch];
}

uint8_t lickChFromChar(char c) {
  if (c >= 'A' && c <= 'Z') c += 32;
  switch (c) {
    case 'l': return LK_L;
    case 'c': return LK_C;
    case 'r': return LK_R;
    default:  return LICK_COUNT;
  }
}

static inline bool validCh(uint8_t ch) {
  if (ch >= LICK_COUNT) { emitErr(F("LICK_BAD_CHANNEL")); return false; }
  return true;
}

// ---------------------------------------------------------------

void lickBegin() {
  g_lk[LK_L].pin = PIN_TOUCH_L;
  g_lk[LK_C].pin = PIN_TOUCH_C;
  g_lk[LK_R].pin = PIN_TOUCH_R;

  for (uint8_t k = 0; k < LICK_COUNT; k++) {
    LickChan& C = g_lk[k];
    pinMode(C.pin, INPUT);
    C.enabled    = false;      // stays off until calibrated
    C.calibrated = false;
    C.baseline   = 0.0f;
    C.sd         = 0.0f;
    C.onDelta    = 0.0f;
    C.offDelta   = 0.0f;
    C.polarity   = -1;         // assume a lick pulls the value down
    C.state      = L_IDLE;
    C.count      = 0;
    C.lastRaw    = 0;
    C.cal        = CAL_NONE;
    C.refractoryUntilMs = 0;
    C.present    = LICK_PRESENT_DEFAULT[k];
  }

  // Default Arduino ADC prescaler is 128 (125 kHz, ~112 us per read).
  // Drop to 64 (250 kHz, ~52 us). Slightly noisier in the lowest bit,
  // which is irrelevant here: we measure relative excursions of tens
  // to hundreds of counts, and the calibration measures whatever
  // noise remains and sizes the thresholds around it.
  ADCSRA = (ADCSRA & ~0x07) | 0x06;

  // Per-sample smoothing factor for the slow baseline tracker.
  float dt = (float)LICK_SAMPLE_INTERVAL_US * 1e-6f * (float)LICK_COUNT;
  g_baselineAlpha = dt / LICK_BASELINE_TAU_S;

  g_nextSampleUs = micros();
}

// ---------------------------------------------------------------
//  Calibration
// ---------------------------------------------------------------

static bool startCal(uint8_t ch, CalMode mode, uint16_t ms) {
  if (!validCh(ch)) return false;
  if (!g_lk[ch].present) {
    emitErr(F("LICK_SENSOR_NOT_PRESENT_ON_THIS_RIG")); return false;
  }
  if (ms == 0) ms = LICK_CAL_DEFAULT_MS;

  LickChan& C = g_lk[ch];
  C.cal            = mode;
  C.calEndMs       = millis() + ms;
  C.calN           = 0;
  C.calSum         = 0;
  C.calSumSq       = 0;
  C.calNextPrintMs = 0;
  C.calMin         = 32767;
  C.calMax         = -32768;
  C.state          = L_IDLE;
  return true;
}

bool lickCalibrateBaseline(uint8_t ch, uint16_t ms) {
  if (!startCal(ch, CAL_BASELINE, ms)) return false;
  emitInfo(F("Baseline calibration running. DO NOT TOUCH the spout."));
  return true;
}

bool lickCalibrateTouch(uint8_t ch, uint16_t ms) {
  if (!validCh(ch)) return false;
  if (!g_lk[ch].calibrated && g_lk[ch].baseline == 0.0f) {
    emitErr(F("LICK_RUN_BASELINE_CALIBRATION_FIRST"));
    return false;
  }
  if (!startCal(ch, CAL_TOUCH, ms)) return false;
  emitInfo(F("Contact calibration running. HOLD CONTACT on the spout."));
  return true;
}

bool lickStreamRaw(uint8_t ch, uint16_t ms) {
  if (!startCal(ch, CAL_STREAM, ms)) return false;
  emitInfo(F("Streaming raw ADC. Touch and release to see the swing."));
  return true;
}

void lickStopStream() {
  for (uint8_t k = 0; k < LICK_COUNT; k++) {
    if (g_lk[k].cal == CAL_STREAM) g_lk[k].cal = CAL_NONE;
  }
}

static void finishBaseline(uint8_t ch) {
  LickChan& C = g_lk[ch];
  if (C.calN < 20) { emitErr(F("LICK_CAL_TOO_FEW_SAMPLES")); C.cal = CAL_NONE; return; }

  float n    = (float)C.calN;
  float mean = (float)C.calSum / n;
  float var  = ((float)C.calSumSq - ((float)C.calSum * (float)C.calSum) / n) / (n - 1.0f);
  if (var < 0.0f) var = 0.0f;

  C.baseline = mean;
  C.sd       = sqrt(var);

  C.onDelta  = LICK_K_ON  * C.sd;
  C.offDelta = LICK_K_OFF * C.sd;
  if (C.onDelta  < (float)LICK_MIN_DELTA_COUNTS) C.onDelta  = (float)LICK_MIN_DELTA_COUNTS;
  if (C.offDelta < C.onDelta * 0.4f)             C.offDelta = C.onDelta * 0.4f;

  C.calibrated = true;
  C.enabled    = true;
  C.cal        = CAL_NONE;
  C.state      = L_IDLE;

  emitInfo(F("Baseline done. Thresholds are from measured noise only;"));
  emitInfo(F("run a contact calibration to size them to a real lick."));
  lickReport(ch);
}

static void finishTouch(uint8_t ch) {
  LickChan& C = g_lk[ch];
  if (C.calN < 20) { emitErr(F("LICK_CAL_TOO_FEW_SAMPLES")); C.cal = CAL_NONE; return; }

  float n           = (float)C.calN;
  float contactMean = (float)C.calSum / n;
  float cvar = ((float)C.calSumSq - ((float)C.calSum * (float)C.calSum) / n) / (n - 1.0f);
  if (cvar < 0.0f) cvar = 0.0f;
  float contactSd = sqrt(cvar);

  float gap = contactMean - C.baseline;

  if (fabs(gap) < (float)LICK_MIN_DELTA_COUNTS ||
      fabs(gap) < 3.0f * C.sd) {
    // Contact is not distinguishable from resting noise. Reporting
    // success here would produce a detector that fires on nothing.
    emitErr(F("LICK_CONTACT_INDISTINGUISHABLE_FROM_BASELINE"));
    emitInfo(F("Check wiring, pull-up resistor, and that contact was held."));
    C.cal = CAL_NONE;
    return;
  }

  // Polarity is measured, not assumed.
  C.polarity = (gap > 0.0f) ? +1 : -1;

  // Contact strength is NOT constant. Sizing the release threshold to
  // a fixed fraction of the MEAN lets normal fluctuation dip below it,
  // which fragments one sustained contact into several licks. Size it
  // instead to the WEAKEST sustained contact, estimated as
  // mean - 2*sd of the contact distribution. Using the raw minimum
  // would be fragile: one instant of lost contact during calibration
  // would drag the estimate to nothing.
  float eMean = fabs(gap);
  float eWeak = eMean - 2.0f * contactSd;
  if (eWeak < 0.30f * eMean) eWeak = 0.30f * eMean;   // sanity floor

  C.onDelta  = 0.60f * eWeak;
  C.offDelta = 0.30f * eWeak;

  // Never let either threshold fall inside the baseline noise band.
  float floorOn  = LICK_K_ON  * C.sd;
  float floorOff = LICK_K_OFF * C.sd;
  if (C.onDelta  < floorOn)  C.onDelta  = floorOn;
  if (C.offDelta < floorOff) C.offDelta = floorOff;
  if (C.onDelta  < (float)LICK_MIN_DELTA_COUNTS) C.onDelta = (float)LICK_MIN_DELTA_COUNTS;

  // Guarantee a real hysteresis band. Without this the two thresholds
  // can collapse together and the detector chatters.
  if (C.offDelta > 0.60f * C.onDelta) C.offDelta = 0.60f * C.onDelta;

  C.calibrated = true;
  C.enabled    = true;
  C.cal        = CAL_NONE;
  C.state      = L_IDLE;

  Serial.print(F("#,contact mean "));  Serial.print(contactMean, 1);
  Serial.print(F("  sd "));            Serial.print(contactSd, 1);
  Serial.print(F("  range "));         Serial.print(C.calMin);
  Serial.print('-');                   Serial.print(C.calMax);
  Serial.print(F("  baseline "));      Serial.print(C.baseline, 1);
  Serial.print(F("  gap "));           Serial.println(gap, 1);

  if (contactSd > 0.35f * eMean) {
    emitInfo(F("NOTE: contact was very variable. Fingers are far noisier"));
    emitInfo(F("than a tongue; this usually settles with a real mouse."));
  }
  lickReport(ch);
}

// Manual threshold override, for when calibration cannot capture the
// situation and you need to set the numbers by eye from LKRAW.
bool lickSetThresholds(uint8_t ch, float onD, float offD, int8_t pol) {
  if (!validCh(ch)) return false;
  if (onD <= 0.0f || offD <= 0.0f) { emitErr(F("LICK_DELTA_MUST_BE_POSITIVE")); return false; }
  if (offD >= onD) { emitErr(F("LICK_OFFDELTA_MUST_BE_BELOW_ONDELTA")); return false; }
  LickChan& C = g_lk[ch];
  C.onDelta  = onD;
  C.offDelta = offD;
  if (pol == 1 || pol == -1) C.polarity = pol;
  C.calibrated = true;
  C.enabled    = true;
  C.state      = L_IDLE;
  return true;
}

bool lickSetTiming(uint16_t minOn, uint16_t minOff, uint16_t refract) {
  if (minOff > 80) { emitErr(F("LICK_MINOFF_TOO_LONG_WOULD_MERGE_LICKS")); return false; }
  g_minOnMs   = minOn;
  g_minOffMs  = minOff;
  g_refractMs = refract;
  return true;
}

void lickReportTiming() {
  Serial.print(F("R,LICKTIME,"));
  Serial.print(g_minOnMs);   Serial.print(',');
  Serial.print(g_minOffMs);  Serial.print(',');
  Serial.println(g_refractMs);
}

// ---------------------------------------------------------------

void lickReport(uint8_t ch) {
  if (!validCh(ch)) return;
  LickChan& C = g_lk[ch];
  // R,LICK,<ch>,<baseline>,<sd>,<onDelta>,<offDelta>,<polarity>,
  //        <calibrated>,<enabled>,<count>,<lastRaw>
  Serial.print(F("R,LICK,"));
  Serial.print(lickChName(ch));       Serial.print(',');
  Serial.print(C.baseline, 1);        Serial.print(',');
  Serial.print(C.sd, 2);              Serial.print(',');
  Serial.print(C.onDelta, 1);         Serial.print(',');
  Serial.print(C.offDelta, 1);        Serial.print(',');
  Serial.print(C.polarity);           Serial.print(',');
  Serial.print(C.calibrated ? 1 : 0); Serial.print(',');
  Serial.print(C.enabled ? 1 : 0);    Serial.print(',');
  Serial.print(C.count);              Serial.print(',');
  Serial.print(C.lastRaw);            Serial.print(',');
  Serial.println(C.present ? 1 : 0);
}

void lickReportAll() {
  for (uint8_t k = 0; k < LICK_COUNT; k++) lickReport(k);
}

bool lickSetPresent(uint8_t ch, bool present) {
  if (!validCh(ch)) return false;
  g_lk[ch].present = present;
  if (!present) { g_lk[ch].enabled = false; g_lk[ch].cal = CAL_NONE; }
  return true;
}

bool lickPresent(uint8_t ch) {
  return (ch < LICK_COUNT) && g_lk[ch].present;
}

bool lickIsCalibrated(uint8_t ch) {
  if (ch >= LICK_COUNT) return false;
  return g_lk[ch].calibrated;
}

void lickSetEnabled(uint8_t ch, bool en) {
  if (!validCh(ch)) return;
  if (en && !g_lk[ch].calibrated) { emitErr(F("LICK_NOT_CALIBRATED")); return; }
  g_lk[ch].enabled = en;
  g_lk[ch].state   = L_IDLE;
}

bool lickEnabled(uint8_t ch) {
  if (ch >= LICK_COUNT) return false;
  return g_lk[ch].enabled;
}

uint32_t lickCount(uint8_t ch) {
  if (ch >= LICK_COUNT) return 0;
  return g_lk[ch].count;
}

void lickResetCount(uint8_t ch) {
  if (ch >= LICK_COUNT) return;
  g_lk[ch].count = 0;
}

void lickResetAllCounts() {
  for (uint8_t k = 0; k < LICK_COUNT; k++) g_lk[k].count = 0;
}

bool lickActive(uint8_t ch) {
  if (ch >= LICK_COUNT) return false;
  return (g_lk[ch].state == L_ON || g_lk[ch].state == L_CAND_OFF);
}

uint32_t lickLastEventMs() { return g_lastEventMs; }

// ---------------------------------------------------------------
//  Sampling and detection
// ---------------------------------------------------------------

static void processSample(uint8_t ch, int16_t raw, uint32_t nowMs) {
  LickChan& C = g_lk[ch];
  C.lastRaw = raw;

  // ---- calibration modes ----
  if (C.cal != CAL_NONE) {
    if (C.cal == CAL_STREAM) {
      if ((int32_t)(nowMs - C.calNextPrintMs) >= 0) {
        Serial.print(F("#,RAW,"));
        Serial.print(lickChName(ch)); Serial.print(',');
        Serial.print(nowMs);          Serial.print(',');
        Serial.println(raw);
        C.calNextPrintMs = nowMs + (1000UL / LICK_RAW_STREAM_HZ);
      }
      if ((int32_t)(nowMs - C.calEndMs) >= 0) {
        C.cal = CAL_NONE;
        emitInfo(F("Raw stream finished."));
      }
      return;
    }

    if (C.calN < LICK_CAL_MAX_SAMPLES) {
      C.calSum   += (uint32_t)raw;
      C.calSumSq += (uint32_t)raw * (uint32_t)raw;
      C.calN++;
      if (raw < C.calMin) C.calMin = raw;
      if (raw > C.calMax) C.calMax = raw;
    }
    if ((int32_t)(nowMs - C.calEndMs) >= 0) {
      if (C.cal == CAL_BASELINE) finishBaseline(ch);
      else                       finishTouch(ch);
    }
    return;
  }

  if (!C.enabled || !C.calibrated) return;

  // A solenoid edge injects a transient into an analog lickometer
  // even with flyback diodes. Discard, do not interpret.
  if (solBlankingActive()) return;

  // Signed excursion in the direction a lick actually moves the
  // signal, so both wiring polarities use identical logic below.
  float dev = (float)C.polarity * ((float)raw - C.baseline);

  switch (C.state) {
    case L_IDLE:
      if ((int32_t)(nowMs - C.refractoryUntilMs) < 0) break;
      if (dev > C.onDelta) {
        C.candOnMs = nowMs;
        C.state    = L_CAND_ON;
      } else if (fabs(dev) < C.offDelta) {
        // Track slow drift, but only while idle and only on samples
        // already close to baseline. A long contact can never drag
        // the baseline with it.
        C.baseline += g_baselineAlpha * ((float)raw - C.baseline);
      }
      break;

    case L_CAND_ON:
      if (dev <= C.onDelta) {
        C.state = L_IDLE;                     // spike, not a lick
      } else if ((nowMs - C.candOnMs) >= g_minOnMs) {
        C.state = L_ON;
        C.onMs  = C.candOnMs;
        C.count++;
        g_lastEventMs = nowMs;
        // Timestamp is the ORIGINAL crossing, so the confirmation
        // window costs nothing in timing accuracy.
        emitEvent(C.candOnMs, "LICK_ON", lickChName(ch),
                  (long)dev, (long)C.count);
      }
      break;

    case L_ON:
      if (dev <= C.offDelta) {
        C.candOffMs = nowMs;
        C.state     = L_CAND_OFF;
      }
      break;

    case L_CAND_OFF:
      if (dev > C.offDelta) {
        C.state = L_ON;                       // dropout, still licking
      } else if ((nowMs - C.candOffMs) >= g_minOffMs) {
        C.state = L_IDLE;
        C.refractoryUntilMs = nowMs + g_refractMs;
        g_lastEventMs = nowMs;
        emitEvent(C.candOffMs, "LICK_OFF", lickChName(ch),
                  (long)(C.candOffMs - C.onMs), (long)C.count);
      }
      break;
  }
}

void lickUpdate() {
  uint32_t nowUs = micros();
  if ((int32_t)(nowUs - g_nextSampleUs) < 0) return;

  g_nextSampleUs += LICK_SAMPLE_INTERVAL_US;
  if ((int32_t)(nowUs - g_nextSampleUs) >= 0) {
    g_nextSampleUs = nowUs + LICK_SAMPLE_INTERVAL_US;   // resync
  }

  // One channel per tick. Each pass therefore blocks for a single
  // analogRead (~52 us), which keeps click-train gating accurate.
  uint8_t ch = g_cursor;
  g_cursor = (g_cursor + 1) % LICK_COUNT;
  if (!g_lk[ch].present) return;   // do not burn an ADC read on nothing

  int16_t raw = (int16_t)analogRead(g_lk[ch].pin);
  processSample(ch, raw, millis());
}
