#include <Servo.h>
#include "servos.h"
#include "proto.h"

// =============================================================
//  STEPPED SERVO MOTION
//
//  A bare write() makes the servo slew at maximum speed, which is
//  abrupt and mechanically loud immediately beside a resistive
//  lickometer. Instead we hold a floating-point commanded angle and
//  advance it toward the target at a fixed degrees/second rate,
//  refreshing every SERVO_UPDATE_MS (20 ms, one servo frame).
//
//  writeMicroseconds() is used rather than write() because write()
//  quantises to whole degrees; at 400 deg/s and a 20 ms frame each
//  step is 8 degrees, which would reintroduce exactly the jerk we
//  are trying to remove.
//
//  POSITION FEEDBACK: none exists. A hobby servo has no feedback
//  wire, so the "current angle" reported here is the commanded
//  angle. It is correct unless the actuator stalls or is physically
//  pushed, in which case the firmware cannot know.
// =============================================================

struct ServoState {
  Servo    dev;
  uint8_t  pin;
  int      zeroAngle;
  int8_t   extendDir;      // +1 or -1: which way extends the spout
  float    current;        // commanded angle, sub-degree precision
  float    target;
  uint16_t slewDegPerSec;
  bool     moving;
  bool     attached;
  uint32_t lastStepMs;
  uint32_t moveStartMs;
  int      softMin;        // mechanical travel limit, tighter than 0-180
  int      softMax;
  uint32_t idleDetachMs;   // 0 = hold position forever
  uint32_t idleSinceMs;
  bool     posKnown;       // false until SVINIT has actually driven there
  int      extendAngle;    // dispense position, captured on the init page
  bool     extendSet;
  bool     present;
};

static ServoState g_sv[SV_COUNT];

// Deadline until which the idle release is suppressed on every channel.
//
// A de-energised servo must be re-energised and allowed to settle before
// it can move, which would add a variable delay to any withdrawal
// commanded in the interim. That matters only during the interval in
// which a withdrawal can actually be triggered by the subject's
// behaviour - from stimulus onset until the response is resolved.
// Outside that interval the normal idle release applies, so the holding
// current is present for the response window rather than for the whole
// trial. This distinction is significant when trials are long: holding
// throughout would keep the motors energised, and warm, for the
// consumption, retraction and inter-trial phases, during which no
// latency-critical movement occurs.
static uint32_t g_holdUntilMs = 0;
static bool     g_holdActive  = false;

void servoHoldUntil(uint32_t deadlineMs) {
  g_holdUntilMs = deadlineMs;
  g_holdActive  = true;
}

void servoHoldRelease() {
  g_holdActive = false;
  // The idle timers restart now, so channels release after their normal
  // interval rather than immediately.
  uint32_t now = millis();
  for (uint8_t k = 0; k < SV_COUNT; k++) g_sv[k].idleSinceMs = now;
}

bool servoHoldActive() {
  if (!g_holdActive) return false;
  // Expire on the deadline as well as on explicit release, so a trial
  // that ends abnormally cannot leave the motors energised indefinitely.
  if ((int32_t)(millis() - g_holdUntilMs) >= 0) {
    servoHoldRelease();
    return false;
  }
  return true;
}

static const char* const SV_NAMES[SV_COUNT] = {"L", "C", "R"};

const char* servoChName(uint8_t ch) {
  if (ch >= SV_COUNT) return "?";
  return SV_NAMES[ch];
}

uint8_t servoChFromToken(const char* tok) {
  if (tok == NULL || tok[0] == '\0') return SV_COUNT;
  char c = tok[0];
  if (c >= 'A' && c <= 'Z') c += 32;
  switch (c) {
    case 'l': return SV_L;
    case 'c': return SV_C;
    case 'r': return SV_R;
    default:  return SV_COUNT;
  }
}

static inline uint16_t angleToUs(float a) {
  if (a < SERVO_ANGLE_MIN) a = SERVO_ANGLE_MIN;
  if (a > SERVO_ANGLE_MAX) a = SERVO_ANGLE_MAX;
  float span = (float)(SERVO_US_MAX - SERVO_US_MIN);
  return (uint16_t)(SERVO_US_MIN + (a / 180.0f) * span + 0.5f);
}

// ---------------------------------------------------------------

void servoBegin() {
  g_sv[SV_L].pin = PIN_SERVO_L;
  g_sv[SV_C].pin = PIN_SERVO_C;
  g_sv[SV_R].pin = PIN_SERVO_R;

  g_sv[SV_L].zeroAngle = SERVO_ZERO_L;
  g_sv[SV_C].zeroAngle = SERVO_ZERO_C;
  g_sv[SV_R].zeroAngle = SERVO_ZERO_R;

  g_sv[SV_L].extendDir = SERVO_EXTEND_DIR_L;
  g_sv[SV_C].extendDir = SERVO_EXTEND_DIR_C;
  g_sv[SV_R].extendDir = SERVO_EXTEND_DIR_R;

  for (uint8_t k = 0; k < SV_COUNT; k++) {
    ServoState& S = g_sv[k];
    S.current       = (float)S.zeroAngle;
    S.target        = (float)S.zeroAngle;
    S.slewDegPerSec = SERVO_SLEW_DEFAULT;
    S.moving        = false;
    S.attached      = false;
    S.lastStepMs    = 0;
    S.moveStartMs   = 0;
    S.softMin       = SERVO_ANGLE_MIN;
    S.softMax       = SERVO_ANGLE_MAX;
    S.idleDetachMs  = SERVO_IDLE_DETACH_MS;
    S.idleSinceMs   = 0;
    // The software position is a GUESS until SVINIT physically drives
    // the actuator to a known reference. Absolute writes computed
    // against a guess silently do the wrong thing - including nothing
    // at all, when the guess happens to equal the requested angle.
    S.posKnown      = false;
    S.extendAngle   = S.zeroAngle;
    S.extendSet     = false;
    S.present       = SERVO_PRESENT_DEFAULT[k];
  }
  // Deliberately NOT attached or driven here. Nothing moves until
  // the host or the debug menu explicitly calls SVINIT, so a reset
  // mid-experiment does not fling the spouts at the animal.
}

bool servoSetPresent(uint8_t ch, bool present) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (!present && g_sv[ch].attached) servoDetach(ch);
  g_sv[ch].present = present;
  return true;
}

bool servoPresent(uint8_t ch) {
  return (ch < SV_COUNT) && g_sv[ch].present;
}

bool servoAttach(uint8_t ch) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (!g_sv[ch].present) {
    // A spout that is not on the rig must not be driven. Refusing here
    // is what lets the readiness check skip it instead of demanding a
    // calibration for hardware that does not exist.
    emitErr(F("SERVO_NOT_PRESENT_ON_THIS_RIG")); return false;
  }
  ServoState& S = g_sv[ch];
  if (!S.attached) {
    S.dev.attach(S.pin, SERVO_US_MIN, SERVO_US_MAX);
    S.dev.writeMicroseconds(angleToUs(S.current));
    S.attached    = true;
    S.idleSinceMs = millis();
  }
  return true;
}

bool servoDetach(uint8_t ch) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  ServoState& S = g_sv[ch];
  if (S.attached) {
    S.dev.detach();
    S.attached = false;
    S.moving   = false;
  }
  return true;
}

bool servoIsAttached(uint8_t ch) {
  if (ch >= SV_COUNT) return false;
  return g_sv[ch].attached;
}

// ---------------------------------------------------------------

static bool startMove(uint8_t ch, float target, const char* tag) {
  ServoState& S = g_sv[ch];
  uint32_t now = millis();

  if (!S.attached) servoAttach(ch);

  S.target      = target;
  S.moving      = (fabs(S.target - S.current) > 0.05f);
  S.lastStepMs  = now;
  S.moveStartMs = now;

  emitEvent(now, tag, servoChName(ch),
            (long)(S.current + 0.5f), (long)(S.target + 0.5f));

  if (!S.moving) {
    // Target equals present position. This is the failure mode that
    // looks like a broken command: an ack, a DONE, and no motion.
    // Say so explicitly.
    S.idleSinceMs = now;
    emitEvent(now, "SERVO_NOOP", servoChName(ch),
              (long)(S.current + 0.5f), 0);
    emitInfo(F("Servo already at that angle - nothing to do."));
  }
  return true;
}

bool servoInit(uint8_t ch) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (!g_sv[ch].present) { emitErr(F("SERVO_NOT_PRESENT_ON_THIS_RIG")); return false; }
  ServoState& S = g_sv[ch];
  servoAttach(ch);
  // Zeroing is always permitted: it is the reference move, and the
  // minimum-step guard must not be able to block a retract.
  bool ok = startMove(ch, (float)S.zeroAngle, "SERVO_ZERO");
  if (ok) S.posKnown = true;
  return ok;
}

bool servoInitAll() {
  bool ok = true;
  for (uint8_t k = 0; k < SV_COUNT; k++) {
    if (g_sv[k].present) ok &= servoInit(k);
  }
  return ok;
}

bool servoWrite(uint8_t ch, int angle, bool force) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (angle < SERVO_ANGLE_MIN || angle > SERVO_ANGLE_MAX) {
    emitErr(F("SERVO_ANGLE_OUT_OF_RANGE_0_180")); return false;
  }
  ServoState& S = g_sv[ch];
  if (!force && (angle < S.softMin || angle > S.softMax)) {
    // Beyond the actuator's mechanical travel. Commanding this would
    // drive the servo into a hard stop, where it stalls, draws its
    // full stall current and buzzes - and because a hobby servo has
    // no feedback wire, the firmware would report SERVO_DONE while
    // it happened. Soft limits are the only defence during a session.
    //
    // force bypasses them because the initialization page IS the tool
    // for finding where the limits should be, and a limit you cannot
    // cross is a limit you cannot measure. The 0-180 check above is
    // NOT bypassable: outside it the pulse width is meaningless.
    emitErr(F("SERVO_BEYOND_SOFT_LIMIT")); return false;
  }

  if (!S.posKnown) {
    emitInfo(F("WARNING: position not verified. Run SVINIT first, or"));
    emitInfo(F("use SVFWD/SVBACK, which do not depend on it."));
  }

  float delta = fabs((float)angle - S.current);
  if (!force && delta > 0.05f && delta < (float)SERVO_MIN_STEP) {
    emitErr(F("SERVO_STEP_BELOW_MINIMUM_10DEG"));
    return false;
  }
  return startMove(ch, (float)angle, "SERVO_MOVE");
}

static bool relativeMove(uint8_t ch, int delta, int8_t sign, bool force) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (!force && delta < SERVO_MIN_STEP) {
    emitErr(F("SERVO_STEP_BELOW_MINIMUM_10DEG")); return false;
  }
  if (delta < 1) { emitErr(F("SERVO_STEP_MUST_BE_POSITIVE")); return false; }
  ServoState& S = g_sv[ch];
  float target = S.current + (float)(delta * sign * S.extendDir);

  // Hard range: never bypassable, force or not.
  if (target < (float)SERVO_ANGLE_MIN || target > (float)SERVO_ANGLE_MAX) {
    emitErr(F("SERVO_MOVE_WOULD_EXCEED_0_180")); return false;
  }
  if (!force && (target < (float)S.softMin || target > (float)S.softMax)) {
    emitErr(F("SERVO_MOVE_WOULD_EXCEED_SOFT_LIMIT")); return false;
  }
  return startMove(ch, target, "SERVO_MOVE");
}

bool servoForward(uint8_t ch, int delta, bool force) {
  return relativeMove(ch, delta, +1, force);
}
bool servoBack(uint8_t ch, int delta, bool force) {
  return relativeMove(ch, delta, -1, force);
}

void servoHalt(uint8_t ch) {
  if (ch >= SV_COUNT) return;
  ServoState& S = g_sv[ch];
  if (!S.moving) return;
  S.target = S.current;
  S.moving = false;
  emitEvent(millis(), "SERVO_HALT", servoChName(ch),
            (long)(S.current + 0.5f), 0);
}

bool servoSetSlew(uint8_t ch, uint16_t degPerSec) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (degPerSec < SERVO_SLEW_MIN || degPerSec > SERVO_SLEW_MAX) {
    emitErr(F("SERVO_SLEW_OUT_OF_RANGE")); return false;
  }
  g_sv[ch].slewDegPerSec = degPerSec;
  return true;
}

bool servoSetDir(uint8_t ch, int8_t dir) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (dir != 1 && dir != -1) { emitErr(F("SERVO_DIR_MUST_BE_1_OR_-1")); return false; }
  g_sv[ch].extendDir = dir;
  return true;
}

bool servoSetLimits(uint8_t ch, int lo, int hi) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (lo < SERVO_ANGLE_MIN || hi > SERVO_ANGLE_MAX || lo >= hi) {
    emitErr(F("SERVO_LIMITS_INVALID")); return false;
  }
  ServoState& S = g_sv[ch];
  if (S.zeroAngle < lo || S.zeroAngle > hi) {
    emitErr(F("SERVO_LIMITS_EXCLUDE_ZERO_ANGLE")); return false;
  }
  S.softMin = lo;
  S.softMax = hi;
  return true;
}

bool servoSetIdleDetach(uint8_t ch, uint32_t ms) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  g_sv[ch].idleDetachMs = ms;
  g_sv[ch].idleSinceMs  = millis();
  return true;
}

bool servoSetZero(uint8_t ch, int angle, bool force) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (angle < SERVO_ANGLE_MIN || angle > SERVO_ANGLE_MAX) {
    emitErr(F("SERVO_ANGLE_OUT_OF_RANGE_0_180")); return false;
  }
  ServoState& S = g_sv[ch];
  if (!force && (angle < S.softMin || angle > S.softMax)) {
    emitErr(F("SERVO_ZERO_OUTSIDE_SOFT_LIMITS")); return false;
  }
  S.zeroAngle = angle;
  S.posKnown  = false;      // reference changed; re-init to re-establish
  return true;
}

// Raw pulse width, bypassing the angle model entirely. This is the
// tool for finding a servo's REAL mechanical limits: creep outward
// in 25 us steps and stop the moment you hear strain. Angles 0 and
// 180 correspond to 544 and 2400 us, and many hobby servos are
// already jammed against an internal stop at those extremes.
bool servoWriteUs(uint8_t ch, uint16_t us) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (us < SERVO_US_MIN || us > SERVO_US_MAX) {
    emitErr(F("SERVO_US_OUT_OF_RANGE")); return false;
  }
  ServoState& S = g_sv[ch];
  if (!S.attached) servoAttach(ch);
  S.moving = false;
  S.dev.writeMicroseconds(us);
  // Keep the angle model in step with what we just did.
  S.current = ((float)(us - SERVO_US_MIN) /
               (float)(SERVO_US_MAX - SERVO_US_MIN)) * 180.0f;
  S.target      = S.current;
  S.idleSinceMs = millis();
  emitEvent(millis(), "SERVO_US", servoChName(ch),
            (long)us, (long)(S.current + 0.5f));
  return true;
}

bool servoSetExtend(uint8_t ch, int angle, bool force) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return false; }
  if (angle < SERVO_ANGLE_MIN || angle > SERVO_ANGLE_MAX) {
    emitErr(F("SERVO_ANGLE_OUT_OF_RANGE_0_180")); return false;
  }
  ServoState& S = g_sv[ch];
  if (!force && (angle < S.softMin || angle > S.softMax)) {
    emitErr(F("SERVO_EXTEND_OUTSIDE_SOFT_LIMITS")); return false;
  }
  S.extendAngle = angle;
  S.extendSet   = true;
  return true;
}

int  servoExtendAngle(uint8_t ch) {
  if (ch >= SV_COUNT) return -1;
  return g_sv[ch].extendAngle;
}

bool servoExtendSet(uint8_t ch) {
  if (ch >= SV_COUNT) return false;
  return g_sv[ch].extendSet;
}

// Trial-time moves between the two captured positions. These bypass
// the minimum-step guard deliberately: both endpoints were validated
// against the soft limits when they were captured, and a retract must
// never be blocked by a guard meant to catch typos on the init page.
bool servoGoExtend(uint8_t ch) {
  if (ch >= SV_COUNT) return false;
  return servoWrite(ch, g_sv[ch].extendAngle, true);
}

bool servoGoRetract(uint8_t ch) {
  if (ch >= SV_COUNT) return false;
  return servoWrite(ch, g_sv[ch].zeroAngle, true);
}

void servoDetachAll() {
  for (uint8_t k = 0; k < SV_COUNT; k++) {
    if (g_sv[k].attached) {
      g_sv[k].moving = false;
      g_sv[k].dev.detach();
      g_sv[k].attached = false;
      emitEvent(millis(), "SERVO_DETACH", servoChName(k),
                (long)(g_sv[k].current + 0.5f), 0);
    }
  }
}

int  servoCurrentAngle(uint8_t ch) {
  if (ch >= SV_COUNT) return -1;
  return (int)(g_sv[ch].current + 0.5f);
}

int  servoTargetAngle(uint8_t ch) {
  if (ch >= SV_COUNT) return -1;
  return (int)(g_sv[ch].target + 0.5f);
}

bool servoIsMoving(uint8_t ch) {
  if (ch >= SV_COUNT) return false;
  return g_sv[ch].moving;
}

bool servoAnyMoving() {
  for (uint8_t k = 0; k < SV_COUNT; k++) if (g_sv[k].moving) return true;
  return false;
}

void servoReport(uint8_t ch) {
  if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return; }
  ServoState& S = g_sv[ch];
  // R,SERVO,<ch>,<current>,<target>,<moving>,<slew>,<dir>,<attached>
  Serial.print(F("R,SERVO,"));
  Serial.print(servoChName(ch));   Serial.print(',');
  Serial.print(servoCurrentAngle(ch)); Serial.print(',');
  Serial.print(servoTargetAngle(ch));  Serial.print(',');
  Serial.print(S.moving ? 1 : 0);  Serial.print(',');
  Serial.print(S.slewDegPerSec);   Serial.print(',');
  Serial.print(S.extendDir);       Serial.print(',');
  Serial.print(S.attached ? 1 : 0); Serial.print(',');
  Serial.print(S.softMin);         Serial.print(',');
  Serial.print(S.softMax);         Serial.print(',');
  Serial.print(S.idleDetachMs);    Serial.print(',');
  Serial.print(S.zeroAngle);       Serial.print(',');
  Serial.print(S.posKnown ? 1 : 0); Serial.print(',');
  Serial.print(S.extendAngle);     Serial.print(',');
  Serial.print(S.extendSet ? 1 : 0); Serial.print(',');
  Serial.println(S.present ? 1 : 0);
}

// ---------------------------------------------------------------

void servoUpdate() {
  uint32_t now = millis();
  // Evaluated once per pass rather than per channel: the deadline check
  // can release the hold, and every channel must see the same answer.
  const bool holding = servoHoldActive();

  for (uint8_t k = 0; k < SV_COUNT; k++) {
    ServoState& S = g_sv[k];

    // Idle auto-detach. A servo holding position against any load
    // hums continuously, which is mechanical noise a few centimetres
    // from a resistive lickometer, and it is what a stalled servo
    // does forever. Off by default because a detached actuator can
    // drift under gravity - enable it per channel once you know
    // your linkage holds.
    if (!holding && !S.moving && S.attached && S.idleDetachMs > 0 &&
        (now - S.idleSinceMs) >= S.idleDetachMs) {
      S.dev.detach();
      S.attached = false;
      emitEvent(now, "SERVO_IDLE_DETACH", servoChName(k),
                (long)(S.current + 0.5f), 0);
      continue;
    }

    if (!S.moving || !S.attached) continue;
    if ((now - S.lastStepMs) < SERVO_UPDATE_MS) continue;

    uint32_t elapsed = now - S.lastStepMs;
    S.lastStepMs = now;

    float maxStep = ((float)S.slewDegPerSec * (float)elapsed) / 1000.0f;
    float remain  = S.target - S.current;

    if (fabs(remain) <= maxStep) {
      S.current = S.target;
      S.dev.writeMicroseconds(angleToUs(S.current));
      S.moving  = false;
      S.idleSinceMs = now;
      emitEvent(now, "SERVO_DONE", servoChName(k),
                (long)(S.current + 0.5f), (long)(now - S.moveStartMs));
    } else {
      S.current += (remain > 0) ? maxStep : -maxStep;
      S.dev.writeMicroseconds(angleToUs(S.current));
    }
  }
}
