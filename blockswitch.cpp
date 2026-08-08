#include "blockswitch.h"
#include "proto.h"
#include "servos.h"
#include "solenoids.h"
#include "stepper.h"
#include "trial.h"

// =============================================================
//  BLOCK SWITCH TIMELINE
//
//  Each spout has its own syringe pump, so the vacuum for spout L is
//  stepper axis L. Axis indices and spout indices are the same
//  numbers throughout this file. BSMODE,0 purges every configured
//  spout at once, which is the reason per-spout pumps are worth
//  having: a side switch inside a choice block has to fit in an ITI.
//
//   BSGO
//     |
//   [RETRACT]   every spout is driven to its retracted angle and
//     |         we wait for physical arrival. Purging over an
//     |         extended spout drips old solution on the animal.
//   [PRE]       settle
//     |
//     |   for each cycle, for each configured spout:
//     |
//   [VAC]       stepper aspirates <steps>; waits for STEP_DONE,
//     |         not for a timer, so the dwell begins when the
//     |         plunger has actually stopped
//   [VAC_DWELL]
//   [FILL]      the NEW solution's solenoid opens for <fill_ms>,
//     |         <pulses> times with <gap_ms> between
//   [FILL_DWELL]
//     |
//   [RETURN]    optional: plunger back to where it started, so
//     |         the syringe does not walk toward its end stop
//     |         over a session of switches
//   [POST]      settle; stepper releases on its own hold timer
//   BLOCK_END
// =============================================================

enum BsState : uint8_t {
  BS_IDLE = 0, BS_RETRACT, BS_PRE, BS_VAC, BS_VAC_DWELL,
  BS_FILL, BS_FILL_DWELL, BS_RETURN, BS_POST
};

struct BsSpout {
  bool     active;
  uint8_t  solIdx;          // 0-based, the NEW solution for this spout
  uint32_t fillMs;          // open time per pulse
  uint8_t  pulses;
  uint32_t gapMs;
};

static BsSpout  g_bs[SV_COUNT];
static BsState  g_state   = BS_IDLE;
static uint32_t g_id      = 0;
static bool     g_defined = false;
static uint8_t  g_outcome = BLK_OUT_NONE;

static uint32_t g_vacSteps    = BLK_VAC_STEPS_DEFAULT;
static uint16_t g_vacSps      = BLK_VAC_SPS_DEFAULT;
static uint32_t g_preMs       = 250;
static uint32_t g_vacDwellMs  = 500;
static uint32_t g_fillDwellMs = 500;
static uint32_t g_postMs      = 250;
// 1 = one spout at a time, 0 = every configured spout in parallel.
static uint8_t  g_sequential  = 1;
static bool     g_useStepper  = true;
static uint8_t  g_cycles      = 2;
// OFF by default. The return stroke pushes whatever was aspirated back
// down the line. That is correct if the syringe pulls into a waste trap
// or through a check valve, and is exactly wrong if it does not - it
// would drive the old solution back into the spout you just refilled.
// Turn it on with BSRETURN,1 once the fluidics justify it.
static bool     g_return      = false;

static uint32_t g_stateEnteredMs = 0;
static uint8_t  g_cycle          = 0;
static int8_t   g_cur            = -1;
static uint8_t  g_pulseIdx       = 0;
static bool     g_inGap          = false;
static uint32_t g_pulseStartMs   = 0;
static long     g_startPos[SV_COUNT];
static uint32_t g_aspirated[SV_COUNT];

static const char* chName(int8_t ch) {
  switch (ch) { case 0: return "L"; case 1: return "C"; case 2: return "R"; }
  return "-";
}

static void enter(BsState s) {
  g_state = s;
  g_stateEnteredMs = millis();
}

static uint32_t inState(uint32_t now) { return now - g_stateEnteredMs; }

bool blockRunning() { return g_state != BS_IDLE; }

// ---------------------------------------------------------------
//  Definition
// ---------------------------------------------------------------

void blockBegin() {
  g_state   = BS_IDLE;
  g_defined = false;
  for (uint8_t k = 0; k < SV_COUNT; k++) g_bs[k].active = false;
}

bool blockNew(uint32_t id) {
  if (blockRunning()) { emitErr(F("BLOCK_ALREADY_RUNNING")); return false; }
  g_id = id;
  for (uint8_t k = 0; k < SV_COUNT; k++) g_bs[k].active = false;
  g_defined = true;
  return true;
}

bool blockAddSpout(uint8_t ch, uint8_t solIdx, uint32_t fillMs,
                   uint8_t pulses, uint32_t gapMs) {
  if (!g_defined) { emitErr(F("BLOCK_CALL_BSNEW_FIRST")); return false; }
  if (ch >= SV_COUNT) { emitErr(F("BLOCK_BAD_SPOUT")); return false; }
  if (solIdx >= SOL_COUNT_MAX) { emitErr(F("BLOCK_BAD_SOLENOID")); return false; }
  if (fillMs == 0) { emitErr(F("BLOCK_FILL_MS_MUST_BE_POSITIVE")); return false; }
  if (fillMs > SOL_DISPENSE_MAX_MS) { emitErr(F("BLOCK_FILL_TOO_LONG")); return false; }
  if (pulses == 0) pulses = 1;

  g_bs[ch].active = true;
  g_bs[ch].solIdx = solIdx;
  g_bs[ch].fillMs = fillMs;
  g_bs[ch].pulses = pulses;
  g_bs[ch].gapMs  = gapMs;
  return true;
}

bool blockSetVac(uint32_t steps, uint16_t stepsPerSec) {
  g_vacSteps = steps;                 // 0 is legal: refill with no purge
  if (stepsPerSec > 0) g_vacSps = stepsPerSec;
  return true;
}

bool blockSetTiming(uint32_t preMs, uint32_t vacDwellMs,
                    uint32_t fillDwellMs, uint32_t postMs) {
  g_preMs       = preMs;
  g_vacDwellMs  = vacDwellMs;
  g_fillDwellMs = fillDwellMs;
  g_postMs      = postMs;
  return true;
}

bool blockSetMode(uint8_t sequential) { g_sequential = sequential ? 1 : 0; return true; }

bool blockSetUseStepper(bool use) { g_useStepper = use; return true; }

bool blockSetCycles(uint8_t n) {
  if (n == 0 || n > BLK_MAX_CYCLES) { emitErr(F("BLOCK_CYCLES_OUT_OF_RANGE")); return false; }
  g_cycles = n;
  return true;
}

bool blockSetReturn(bool on) { g_return = on; return true; }

// ---------------------------------------------------------------

static int8_t nextActive(int8_t from) {
  for (int8_t k = from; k < (int8_t)SV_COUNT; k++) if (g_bs[k].active) return k;
  return -1;
}

static uint8_t countActive() {
  uint8_t n = 0;
  for (uint8_t k = 0; k < SV_COUNT; k++) if (g_bs[k].active) n++;
  return n;
}

// Aspirate on one axis, or on every configured axis at once when the
// sequencer is in parallel mode.
// Aspiration is optional. When no syringe pump is fitted, or when the
// pump is deliberately excluded, the line is cleared by running the
// newly selected reinforcer through it in the retracted position: the
// displaced volume carries the previous solution out of the dead space.
// This is slower to reach a given purity and discards more liquid than
// aspiration, but requires no pump.
static void startVac() {
  if (g_vacSteps == 0 || !g_useStepper) { enter(BS_VAC_DWELL); return; }

  bool any = false;
  for (uint8_t k = 0; k < SV_COUNT; k++) {
    if (!g_bs[k].active) continue;
    if (g_sequential && (int8_t)k != g_cur) continue;

    emitEvent(millis(), "BLOCK_VAC", chName((int8_t)k),
              (long)g_vacSteps, (long)(g_cycle + 1));
    if (!stepperAspirate(k, g_vacSteps)) {
      // Soft limit, an unzeroed axis, or an absent pump. Do not
      // silently continue and refill a spout that still holds the old
      // solution - that is precisely the mislabelled trial this whole
      // sequence exists to prevent.
      emitErr(F("BLOCK_VACUUM_FAILED"));
      blockAbort();
      return;
    }
    g_aspirated[k] += g_vacSteps;
    any = true;
  }
  if (!any) { enter(BS_VAC_DWELL); return; }
  enter(BS_VAC);
}

static void startFill() {
  BsSpout& S = g_bs[g_cur];
  g_pulseIdx = 1;
  g_inGap    = false;
  g_pulseStartMs = millis();
  emitEvent(g_pulseStartMs, "BLOCK_FILL", chName(g_cur),
            (long)(S.solIdx + 1), (long)S.fillMs);
  solDispenseMs(S.solIdx, S.fillMs);
  enter(BS_FILL);
}

static void finishSequence() {
  if (g_return && g_useStepper) {
    bool started = false;
    for (uint8_t k = 0; k < SV_COUNT; k++) {
      if (!g_bs[k].active || g_aspirated[k] == 0) continue;
      emitEvent(millis(), "BLOCK_RETURN", chName((int8_t)k),
                (long)g_aspirated[k], 0);
      bool ok = stepperPosKnown(k) ? stepperMoveTo(k, g_startPos[k])
                                   : stepperDispense(k, g_aspirated[k]);
      if (ok) started = true;
      else    emitErr(F("BLOCK_RETURN_FAILED"));
    }
    if (started) { enter(BS_RETURN); return; }
  }
  enter(BS_POST);
}

bool blockStart() {
  if (blockRunning()) { emitErr(F("BLOCK_ALREADY_RUNNING")); return false; }
  if (!g_defined)     { emitErr(F("BLOCK_NOT_DEFINED")); return false; }
  if (trialRunning()) { emitErr(F("BLOCK_TRIAL_IN_PROGRESS")); return false; }
  if (countActive() == 0) { emitErr(F("BLOCK_NO_SPOUTS_CONFIGURED")); return false; }
  if (g_vacSteps > 0 && g_useStepper) {
    for (uint8_t k = 0; k < SV_COUNT; k++) {
      if (!g_bs[k].active) continue;
      if (!stepperPresent(k)) {
        emitErr(F("BLOCK_NO_PUMP_ON_THAT_SPOUT")); return false;
      }
      if (!stepperPosKnown(k)) {
        // Aspirating an unknown number of steps from an unknown
        // position is how a plunger ends up jammed against the end of
        // the barrel, with no way for the firmware to tell.
        emitErr(F("BLOCK_STEPPER_NOT_ZEROED_RUN_STPZERO")); return false;
      }
    }
  }

  g_outcome = BLK_OUT_NONE;
  g_cycle   = 0;
  g_cur     = nextActive(0);
  for (uint8_t k = 0; k < SV_COUNT; k++) {
    g_aspirated[k] = 0;
    g_startPos[k]  = stepperPosition(k);
    // The vacuum stroke speed is a property of the block switch, so it
    // is applied here rather than left to whatever STPSPS was last set
    // to by hand. It persists afterwards and STPREAD shows it: one
    // visible speed, not a hidden one.
    if (g_vacSps > 0 && g_bs[k].active) stepperSetSpeed(k, g_vacSps);
  }

  emitEvent(millis(), "BLOCK_START", "", (long)g_id, (long)countActive());

  for (uint8_t k = 0; k < SV_COUNT; k++) servoGoRetract(k);
  enter(BS_RETRACT);
  return true;
}

void blockAbort() {
  if (!blockRunning()) return;
  solCloseAll();
  stepperHaltAll();
  for (uint8_t k = 0; k < SV_COUNT; k++) servoGoRetract(k);
  g_outcome = BLK_OUT_ABORT;
  emitEvent(millis(), "BLOCK_END", "", (long)g_id, (long)BLK_OUT_ABORT);
  g_state   = BS_IDLE;
  g_defined = false;
}

void blockReport() {
  // R,BLOCK,<id>,<state>,<cycle>,<cur>,<outcome>,<vacSteps>,<sps>,
  //         <pre>,<vacDwell>,<fillDwell>,<post>,<mode>,<cycles>,<return>
  Serial.print(F("R,BLOCK,"));
  Serial.print(g_id);            Serial.print(',');
  Serial.print(g_state);         Serial.print(',');
  Serial.print(g_cycle + 1);     Serial.print(',');
  Serial.print(chName(g_cur));   Serial.print(',');
  Serial.print(g_outcome);       Serial.print(',');
  Serial.print(g_vacSteps);      Serial.print(',');
  Serial.print(g_vacSps);        Serial.print(',');
  Serial.print(g_preMs);         Serial.print(',');
  Serial.print(g_vacDwellMs);    Serial.print(',');
  Serial.print(g_fillDwellMs);   Serial.print(',');
  Serial.print(g_postMs);        Serial.print(',');
  Serial.print(g_sequential);    Serial.print(',');
  Serial.print(g_useStepper ? 1 : 0); Serial.print(',');
  Serial.print(g_cycles);        Serial.print(',');
  Serial.println(g_return ? 1 : 0);
}

// ---------------------------------------------------------------
//  Execution
// ---------------------------------------------------------------

void blockUpdate() {
  if (g_state == BS_IDLE) return;
  uint32_t now = millis();

  switch (g_state) {

    case BS_RETRACT:
      if (!servoAnyMoving()) enter(BS_PRE);
      break;

    case BS_PRE:
      if (inState(now) >= g_preMs) startVac();
      break;

    case BS_VAC:
      // Physical arrival, not a timer. A dwell started while a plunger
      // is still moving is not a dwell. In parallel mode this waits
      // for the slowest axis, which is what makes the mode safe.
      if (!stepperAnyMoving()) enter(BS_VAC_DWELL);
      break;

    case BS_VAC_DWELL:
      if (inState(now) >= g_vacDwellMs) startFill();
      break;

    case BS_FILL: {
      BsSpout& S = g_bs[g_cur];
      if (!g_inGap) {
        if ((now - g_pulseStartMs) >= S.fillMs) {
          if (g_pulseIdx >= S.pulses) { enter(BS_FILL_DWELL); break; }
          g_inGap = true;
          g_pulseStartMs = now;
        }
      } else {
        if ((now - g_pulseStartMs) >= S.gapMs) {
          g_pulseIdx++;
          g_inGap = false;
          g_pulseStartMs = now;
          solDispenseMs(S.solIdx, S.fillMs);
        }
      }
      break;
    }

    case BS_FILL_DWELL: {
      if (inState(now) < g_fillDwellMs) break;
      emitEvent(now, "BLOCK_SPOUT_DONE", chName(g_cur),
                (long)(g_cycle + 1), (long)g_bs[g_cur].pulses);

      int8_t nxt = nextActive(g_cur + 1);
      if (nxt >= 0) {
        g_cur = nxt;
        // In parallel mode every spout was already aspirated together,
        // so the remaining spouts only need their fill.
        if (g_sequential) startVac();
        else              startFill();
        break;
      }
      g_cycle++;
      if (g_cycle < g_cycles) {
        g_cur = nextActive(0);
        startVac();
        break;
      }
      finishSequence();
      break;
    }

    case BS_RETURN:
      if (!stepperAnyMoving()) enter(BS_POST);
      break;

    case BS_POST:
      if (inState(now) >= g_postMs) {
        g_outcome = BLK_OUT_OK;
        emitEvent(now, "BLOCK_END", "", (long)g_id, (long)BLK_OUT_OK);
        // The stepper releases itself once its hold window expires;
        // nothing here needs to force it, and forcing it would cut a
        // hold the user may have asked for deliberately.
        g_state   = BS_IDLE;
        g_defined = false;
      }
      break;

    default:
      break;
  }
}
