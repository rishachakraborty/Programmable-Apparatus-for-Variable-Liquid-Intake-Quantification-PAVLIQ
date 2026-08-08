#include "stepper.h"
#include "proto.h"

// =============================================================
//  STEP GENERATION
//
//  Every axis keeps its own next-step deadline in micros(). Each
//  pass of stepperUpdate() emits any step that has come due, up to
//  STEPPER_MAX_BURST per axis, so a slow loop iteration cannot make
//  the plunger stall but also cannot monopolise the CPU and starve
//  lick sampling.
//
//  The STEP pulse itself is a few microseconds of busy-wait. That is
//  the one place blocking is acceptable: the DRV8825 needs 1.9 us
//  minimum high time and there is no cheaper way to guarantee it.
//  At 4 us per pulse and 600 steps/s that is 0.24% of the CPU.
// =============================================================

struct StepAxis {
  uint8_t  pinStep, pinDir, pinEn;
  bool     present;

  long     pos;
  bool     posKnown;
  long     target;          // signed steps remaining, sign = direction
  int8_t   moveSign;        // +1 or -1 in POSITION space
  int8_t   aspirateSign;    // which position direction pulls vacuum

  uint16_t spsTarget;
  uint16_t spsNow;
  uint16_t accel;
  uint32_t nextStepUs;
  uint32_t lastRampMs;

  long     softMin, softMax;
  uint32_t nlPerStep;
  long     holdMs;

  bool     enabled;
  uint32_t lastStepMs;
  bool     moving;
  uint32_t moveStartMs;
  long     moveTotal;
};

static StepAxis g_ax[STP_COUNT];
static const char* const NAMES[STP_COUNT] = {"L", "C", "R"};

const char* stepperChName(uint8_t ch) {
  return (ch < STP_COUNT) ? NAMES[ch] : "?";
}

uint8_t stepperChFromToken(const char* t) {
  if (t == NULL || !t[0]) return STP_COUNT;
  char c = t[0];
  if (c >= 'A' && c <= 'Z') c += 32;
  switch (c) {
    case 'l': return STP_L;
    case 'c': return STP_C;
    case 'r': return STP_R;
    default:  return STP_COUNT;
  }
}

static bool valid(uint8_t ch) {
  if (ch >= STP_COUNT) { emitErr(F("STEP_BAD_AXIS")); return false; }
  return true;
}

static bool usable(uint8_t ch) {
  if (!valid(ch)) return false;
  if (!g_ax[ch].present) {
    // Refuse rather than pretend. A rig with two pumps must not
    // believe it purged a third spout.
    emitErr(F("STEP_AXIS_NOT_PRESENT_SET_STPPRESENT"));
    return false;
  }
  return true;
}

// ---------------------------------------------------------------

void stepperBegin() {
  const uint8_t sp[STP_COUNT] = {PIN_STEP_L_STEP, PIN_STEP_C_STEP, PIN_STEP_R_STEP};
  const uint8_t dp[STP_COUNT] = {PIN_STEP_L_DIR,  PIN_STEP_C_DIR,  PIN_STEP_R_DIR};
  const uint8_t ep[STP_COUNT] = {PIN_STEP_L_EN,   PIN_STEP_C_EN,   PIN_STEP_R_EN};
  const bool    pr[STP_COUNT] = {STEPPER_L_PRESENT, STEPPER_C_PRESENT,
                                 STEPPER_R_PRESENT};

  for (uint8_t k = 0; k < STP_COUNT; k++) {
    StepAxis& A = g_ax[k];
    A.pinStep = sp[k]; A.pinDir = dp[k]; A.pinEn = ep[k];
    A.present = pr[k];

    pinMode(A.pinStep, OUTPUT); digitalWrite(A.pinStep, LOW);
    pinMode(A.pinDir,  OUTPUT); digitalWrite(A.pinDir,  LOW);
    pinMode(A.pinEn,   OUTPUT);
    digitalWrite(A.pinEn, HIGH);      // active LOW: parked disabled

    A.pos = 0; A.posKnown = false; A.target = 0; A.moveSign = 1;
    A.aspirateSign = STEPPER_ASPIRATE_SIGN;
    A.spsTarget = STEPPER_SPS_DEFAULT; A.spsNow = 0;
    A.accel = STEPPER_ACCEL_DEFAULT;
    A.softMin = STEPPER_SOFT_MIN; A.softMax = STEPPER_SOFT_MAX;
    A.nlPerStep = STEPPER_NL_PER_STEP;
    A.holdMs = STEPPER_HOLD_MS;
    A.enabled = false; A.moving = false;
    A.nextStepUs = 0; A.lastRampMs = 0; A.moveTotal = 0;
  }
  // Nothing moves at boot, matching the servo module's refusal to
  // attach until told to.
}

bool stepperSetPresent(uint8_t ch, bool present) {
  if (!valid(ch)) return false;
  g_ax[ch].present = present;
  return true;
}

bool stepperPresent(uint8_t ch) {
  return (ch < STP_COUNT) && g_ax[ch].present;
}

void stepperEnable(uint8_t ch, bool on) {
  if (ch >= STP_COUNT) return;
  StepAxis& A = g_ax[ch];
  if (A.enabled == on) return;
  digitalWrite(A.pinEn, on ? LOW : HIGH);     // nENBL
  A.enabled = on;
  if (on) delayMicroseconds(STEPPER_ENABLE_SETTLE_US);
  emitEvent(millis(), on ? "STEP_ON" : "STEP_OFF", stepperChName(ch), 0, 0);
}

bool stepperIsEnabled(uint8_t ch) {
  return (ch < STP_COUNT) && g_ax[ch].enabled;
}

void stepperZero(uint8_t ch) {
  if (!valid(ch)) return;
  g_ax[ch].pos = 0;
  g_ax[ch].posKnown = true;
  emitEvent(millis(), "STEP_ZERO", stepperChName(ch), 0, 0);
}

bool stepperPosKnown(uint8_t ch) {
  return (ch < STP_COUNT) && g_ax[ch].posKnown;
}

bool stepperIsMoving(uint8_t ch) {
  return (ch < STP_COUNT) && g_ax[ch].moving;
}

bool stepperAnyMoving() {
  for (uint8_t k = 0; k < STP_COUNT; k++) if (g_ax[k].moving) return true;
  return false;
}

long stepperPosition(uint8_t ch)  { return (ch < STP_COUNT) ? g_ax[ch].pos : 0; }
long stepperRemaining(uint8_t ch) { return (ch < STP_COUNT) ? g_ax[ch].target : 0; }
uint32_t stepperNlPerStep(uint8_t ch) {
  return (ch < STP_COUNT) ? g_ax[ch].nlPerStep : 0;
}

// ---------------------------------------------------------------
//  Configuration
// ---------------------------------------------------------------

bool stepperSetSpeed(uint8_t ch, uint16_t sps) {
  if (!valid(ch)) return false;
  if (sps < STEPPER_SPS_MIN || sps > STEPPER_SPS_MAX) {
    emitErr(F("STEP_SPEED_OUT_OF_RANGE")); return false;
  }
  g_ax[ch].spsTarget = sps;
  return true;
}

bool stepperSetAccel(uint8_t ch, uint16_t a) {
  if (!valid(ch)) return false;
  g_ax[ch].accel = a;
  return true;
}

bool stepperSetDir(uint8_t ch, int8_t sign) {
  if (!valid(ch)) return false;
  if (sign != 1 && sign != -1) {
    emitErr(F("STEP_DIR_MUST_BE_1_OR_-1")); return false;
  }
  g_ax[ch].aspirateSign = sign;
  return true;
}

bool stepperSetLimits(uint8_t ch, long lo, long hi) {
  if (!valid(ch)) return false;
  if (lo >= hi) { emitErr(F("STEP_LIMITS_INVALID")); return false; }
  g_ax[ch].softMin = lo;
  g_ax[ch].softMax = hi;
  return true;
}

bool stepperSetCal(uint8_t ch, uint32_t nlPerStep) {
  if (!valid(ch)) return false;
  if (nlPerStep == 0) { emitErr(F("STEP_CAL_MUST_BE_POSITIVE")); return false; }
  g_ax[ch].nlPerStep = nlPerStep;
  return true;
}

bool stepperSetHold(uint8_t ch, long ms) {
  if (!valid(ch)) return false;
  g_ax[ch].holdMs = ms;
  return true;
}

// ---------------------------------------------------------------
//  Motion
// ---------------------------------------------------------------

static bool startMove(uint8_t ch, long deltaSteps, const char* tag) {
  StepAxis& A = g_ax[ch];
  if (deltaSteps == 0) {
    emitEvent(millis(), "STEP_NOOP", stepperChName(ch), (long)A.pos, 0);
    return true;
  }
  if (A.moving) { emitErr(F("STEP_ALREADY_MOVING")); return false; }

  long dest = A.pos + deltaSteps;
  if (A.posKnown && (dest < A.softMin || dest > A.softMax)) {
    // Without this an aspirate past the end of the barrel stalls the
    // motor while the firmware happily reports STEP_DONE.
    emitErr(F("STEP_MOVE_WOULD_EXCEED_SOFT_LIMIT"));
    return false;
  }

  A.moveSign = (deltaSteps > 0) ? +1 : -1;
  A.target   = (deltaSteps > 0) ? deltaSteps : -deltaSteps;
  A.moveTotal = A.target;

  stepperEnable(ch, true);
  digitalWrite(A.pinDir, (A.moveSign > 0) ? HIGH : LOW);
  delayMicroseconds(STEPPER_DIR_SETUP_US);

  A.spsNow = (A.accel > 0) ? (uint16_t)max(20, (int)STEPPER_SPS_MIN)
                           : A.spsTarget;
  A.nextStepUs = micros();
  A.lastRampMs = millis();
  A.moveStartMs = A.lastRampMs;
  A.moving = true;

  emitEvent(A.moveStartMs, tag, stepperChName(ch),
            (long)(deltaSteps * A.moveSign), (long)A.pos);
  return true;
}

bool stepperAspirate(uint8_t ch, uint32_t steps) {
  if (!usable(ch)) return false;
  return startMove(ch, (long)steps * g_ax[ch].aspirateSign, "STEP_ASP");
}

bool stepperDispense(uint8_t ch, uint32_t steps) {
  if (!usable(ch)) return false;
  return startMove(ch, -(long)steps * g_ax[ch].aspirateSign, "STEP_DIS");
}

bool stepperMoveTo(uint8_t ch, long posSteps) {
  if (!usable(ch)) return false;
  if (!g_ax[ch].posKnown) {
    emitErr(F("STEP_POSITION_UNKNOWN_RUN_STPZERO")); return false;
  }
  return startMove(ch, posSteps - g_ax[ch].pos, "STEP_GOTO");
}

static bool nlMove(uint8_t ch, uint32_t nl, bool aspirate) {
  if (!usable(ch)) return false;
  uint32_t cal = g_ax[ch].nlPerStep;
  if (cal == 0) { emitErr(F("STEP_NOT_CALIBRATED")); return false; }
  uint32_t steps = (nl + cal / 2UL) / cal;
  if (steps == 0) steps = 1;
  return aspirate ? stepperAspirate(ch, steps) : stepperDispense(ch, steps);
}

bool stepperAspirateNl(uint8_t ch, uint32_t nl) { return nlMove(ch, nl, true); }
bool stepperDispenseNl(uint8_t ch, uint32_t nl) { return nlMove(ch, nl, false); }

void stepperHalt(uint8_t ch) {
  if (ch >= STP_COUNT) return;
  StepAxis& A = g_ax[ch];
  if (!A.moving) return;
  long done = A.moveTotal - A.target;
  A.moving = false;
  A.target = 0;
  A.lastStepMs = millis();
  emitEvent(A.lastStepMs, "STEP_HALT", stepperChName(ch), (long)A.pos, done);
}

void stepperHaltAll() {
  for (uint8_t k = 0; k < STP_COUNT; k++) stepperHalt(k);
}

void stepperReport(uint8_t ch) {
  if (!valid(ch)) return;
  StepAxis& A = g_ax[ch];
  // R,STEP,<ch>,<pos>,<remaining>,<moving>,<sps>,<accel>,<dir>,
  //        <min>,<max>,<nl_per_step>,<hold>,<enabled>,<known>,<present>
  Serial.print(F("R,STEP,"));
  Serial.print(stepperChName(ch));  Serial.print(',');
  Serial.print(A.pos);              Serial.print(',');
  Serial.print(A.target);           Serial.print(',');
  Serial.print(A.moving ? 1 : 0);   Serial.print(',');
  Serial.print(A.spsTarget);        Serial.print(',');
  Serial.print(A.accel);            Serial.print(',');
  Serial.print(A.aspirateSign);     Serial.print(',');
  Serial.print(A.softMin);          Serial.print(',');
  Serial.print(A.softMax);          Serial.print(',');
  Serial.print(A.nlPerStep);        Serial.print(',');
  Serial.print(A.holdMs);           Serial.print(',');
  Serial.print(A.enabled ? 1 : 0);  Serial.print(',');
  Serial.print(A.posKnown ? 1 : 0); Serial.print(',');
  Serial.println(A.present ? 1 : 0);
}

void stepperReportAll() {
  for (uint8_t k = 0; k < STP_COUNT; k++) stepperReport(k);
}

// ---------------------------------------------------------------
//  Update
// ---------------------------------------------------------------

static inline void pulse(StepAxis& A) {
  digitalWrite(A.pinStep, HIGH);
  delayMicroseconds(STEPPER_PULSE_US);
  digitalWrite(A.pinStep, LOW);
}

void stepperUpdate() {
  uint32_t nowUs = micros();
  uint32_t nowMs = millis();

  for (uint8_t k = 0; k < STP_COUNT; k++) {
    StepAxis& A = g_ax[k];

    if (!A.moving) {
      // Release the driver once the hold window expires. A negative
      // hold means hold forever, which is only wanted if something
      // can back-drive the plunger.
      if (A.enabled && A.holdMs >= 0 &&
          (nowMs - A.lastStepMs) >= (uint32_t)A.holdMs) {
        stepperEnable(k, false);
      }
      continue;
    }

    // Trapezoidal ramp on a coarse grid. Fine enough for a syringe and
    // far cheaper than recomputing an interval every step.
    if (A.accel > 0 && (nowMs - A.lastRampMs) >= STEPPER_RAMP_MS) {
      uint32_t dt = nowMs - A.lastRampMs;
      A.lastRampMs = nowMs;
      long dv = ((long)A.accel * (long)dt) / 1000L;

      // Distance needed to stop from the current rate, so deceleration
      // starts late enough to keep the move short but early enough that
      // the plunger is not still at speed when it arrives.
      long stopDist = ((long)A.spsNow * (long)A.spsNow) / (2L * (long)A.accel);
      if (A.target <= stopDist) {
        A.spsNow = (uint16_t)max((long)STEPPER_SPS_MIN, (long)A.spsNow - dv);
      } else if (A.spsNow < A.spsTarget) {
        A.spsNow = (uint16_t)min((long)A.spsTarget, (long)A.spsNow + dv);
      }
    }

    uint16_t sps = (A.spsNow > 0) ? A.spsNow : A.spsTarget;
    if (sps < STEPPER_SPS_MIN) sps = STEPPER_SPS_MIN;
    uint32_t interval = 1000000UL / (uint32_t)sps;

    uint8_t burst = 0;
    while (A.moving && (int32_t)(nowUs - A.nextStepUs) >= 0 &&
           burst < STEPPER_MAX_BURST) {
      pulse(A);
      A.pos += A.moveSign;
      A.target--;
      A.lastStepMs = nowMs;
      A.nextStepUs += interval;
      burst++;

      if (A.target <= 0) {
        A.moving = false;
        emitEvent(nowMs, "STEP_DONE", stepperChName(k), (long)A.pos,
                  (long)(nowMs - A.moveStartMs));
        break;
      }
    }

    // If the loop fell far behind, resynchronise instead of trying to
    // catch up in one burst, which would briefly overspeed the motor.
    if (A.moving && (int32_t)(nowUs - A.nextStepUs) > (int32_t)(interval * 4)) {
      A.nextStepUs = nowUs + interval;
    }
  }
}
