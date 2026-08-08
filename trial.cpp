#include "trial.h"
#include "proto.h"
#include "commands.h"
#include "servos.h"
#include "solenoids.h"
#include "lick.h"
#include "led.h"
#include "speaker.h"
#include "blockswitch.h"

// =============================================================
//  TRIAL TIMELINE
//
//   TRGO
//     |
//   [EXTEND]      active spouts driven to their captured extended
//     |           position; waits for SERVO_DONE on all of them, so
//     |           the cue never fires at a spout still in motion
//   [CUE]         staged cues fire together; lick counters zeroed;
//     |           response window opens
//     |
//     |  choice trial: the FIRST lick on any active spout is the
//     |  choice. The other spout retracts immediately, which is what
//     |  stops the animal oscillating between the two.
//     |
//   [RESPOND]     licks on the chosen spout count toward the ratio
//     |           requirement
//     |
//     +-- requirement met -----> reward is delivered at whichever is
//     |                          LATER: the end of the cue-reward
//     |                          delay, or the moment the requirement
//     |                          was met. With FR 8 at ~50 ms per lick
//     |                          the animal finishes around 400 ms, so
//     |                          a 1000 ms delay normally governs.
//     |
//     +-- window expires -----> OMISSION: all spouts retract, straight
//                               to ITI, scored as an omission
//   [CONSUME]     retraction delay after reward
//   [RETRACT]     chosen spout retracts
//   [ITI]         inter-trial interval (Python supplies the value it
//                 drew from the exponential distribution)
//   [GATE]        requires a quiet period with no licking before the
//                 trial is allowed to end, so the next trial never
//                 starts mid-bout
//   TRIAL_END
// =============================================================

enum TrialState : uint8_t {
  TS_IDLE = 0, TS_EXTEND, TS_CUE, TS_RESPOND, TS_REWARD,
  TS_CONSUME, TS_RETRACT, TS_ITI, TS_GATE
};

struct SpoutCfg {
  bool     active;
  uint8_t  solIdx;         // 0-based
  uint32_t dispenseMs;
  uint16_t fr;             // licks required
  bool     rewarded;       // contingency decided by Python
};

static SpoutCfg   g_sp[SV_COUNT];
static TrialState g_state = TS_IDLE;
static uint32_t   g_id = 0;
static uint8_t    g_mode = 0;            // 0 single, 1 choice
static bool       g_defined = false;

static uint32_t g_cueRewardMs   = 1000;
static uint32_t g_omissionMs    = 5000;
static uint32_t g_retractDelay  = 1000;
static uint32_t g_itiMs         = 5000;
static uint16_t g_gateMs        = 500;

static uint32_t g_stateEnteredMs = 0;
static uint32_t g_cueMs          = 0;
static uint32_t g_reqMetMs       = 0;
static bool     g_reqMet         = false;
static int8_t   g_chosen         = -1;
static uint8_t  g_outcome        = TR_OUT_NONE;

static const char* chName(int8_t ch) {
  switch (ch) { case 0: return "L"; case 1: return "C"; case 2: return "R"; }
  return "-";
}

static void enter(TrialState s) {
  g_state = s;
  g_stateEnteredMs = millis();
}

static uint32_t inState(uint32_t now) { return now - g_stateEnteredMs; }

// ---------------------------------------------------------------
//  Definition
// ---------------------------------------------------------------

void trialBegin() {
  g_state = TS_IDLE;
  g_defined = false;
  for (uint8_t k = 0; k < SV_COUNT; k++) g_sp[k].active = false;
}

bool trialRunning() { return g_state != TS_IDLE; }

bool trialNew(uint32_t id, uint8_t mode) {
  if (trialRunning()) { emitErr(F("TRIAL_ALREADY_RUNNING")); return false; }
  g_id   = id;
  g_mode = (mode != 0) ? 1 : 0;
  for (uint8_t k = 0; k < SV_COUNT; k++) g_sp[k].active = false;
  g_defined = true;
  return true;
}

bool trialAddSpout(uint8_t ch, uint8_t solIdx, uint32_t dispenseMs,
                   uint16_t fr, bool rewarded) {
  if (!g_defined)  { emitErr(F("TRIAL_CALL_TRNEW_FIRST")); return false; }
  if (ch >= SV_COUNT) { emitErr(F("TRIAL_BAD_SPOUT")); return false; }
  if (solIdx >= SOL_COUNT_MAX) { emitErr(F("TRIAL_BAD_SOLENOID")); return false; }
  if (dispenseMs > SOL_DISPENSE_MAX_MS) { emitErr(F("TRIAL_DISPENSE_TOO_LONG")); return false; }
  if (fr == 0) { emitErr(F("TRIAL_FR_MUST_BE_AT_LEAST_1")); return false; }

  // Refuse rather than guess. A spout whose extended position was
  // never captured would be driven to its retracted angle and the
  // animal would face a trial with nothing in reach.
  if (!servoPresent(ch)) { emitErr(F("TRIAL_SPOUT_NOT_PRESENT_ON_THIS_RIG")); return false; }
  if (!solPresent(solIdx)) { emitErr(F("TRIAL_SOLENOID_NOT_PRESENT")); return false; }
  if (!servoExtendSet(ch)) { emitErr(F("TRIAL_EXTEND_POSITION_NOT_SET")); return false; }
  if (!lickIsCalibrated(ch)) { emitErr(F("TRIAL_LICK_SENSOR_NOT_CALIBRATED")); return false; }

  g_sp[ch].active     = true;
  g_sp[ch].solIdx     = solIdx;
  g_sp[ch].dispenseMs = dispenseMs;
  g_sp[ch].fr         = fr;
  g_sp[ch].rewarded   = rewarded;
  return true;
}

bool trialSetTiming(uint32_t cueRewardMs, uint32_t omissionMs,
                    uint32_t retractDelayMs, uint32_t itiMs) {
  if (omissionMs < cueRewardMs) {
    // The response window has to outlast the delay, or a trial could
    // be scored an omission before reward was ever possible.
    emitErr(F("TRIAL_OMISSION_WINDOW_SHORTER_THAN_CUE_REWARD_DELAY"));
    return false;
  }
  g_cueRewardMs  = cueRewardMs;
  g_omissionMs   = omissionMs;
  g_retractDelay = retractDelayMs;
  g_itiMs        = itiMs;
  return true;
}

bool trialSetGate(uint16_t ms) { g_gateMs = ms; return true; }

// ---------------------------------------------------------------

static uint8_t countActive() {
  uint8_t n = 0;
  for (uint8_t k = 0; k < SV_COUNT; k++) if (g_sp[k].active) n++;
  return n;
}

bool trialStart() {
  if (trialRunning()) { emitErr(F("TRIAL_ALREADY_RUNNING")); return false; }
  if (!g_defined)     { emitErr(F("TRIAL_NOT_DEFINED")); return false; }
  // A purge retracts spouts and runs pumps. Starting a trial into that
  // would cue an animal at a spout that is moving away from it.
  if (blockRunning()) { emitErr(F("TRIAL_BLOCK_SWITCH_IN_PROGRESS")); return false; }

  uint8_t n = countActive();
  if (n == 0) { emitErr(F("TRIAL_NO_ACTIVE_SPOUTS")); return false; }
  if (g_mode == 1 && n < 2) { emitErr(F("TRIAL_CHOICE_NEEDS_TWO_SPOUTS")); return false; }

  g_chosen   = -1;
  g_reqMet   = false;
  g_reqMetMs = 0;
  g_outcome  = TR_OUT_NONE;

  uint32_t now = millis();
  emitEvent(now, "TRIAL_START", "", (long)g_id, (long)g_mode);

  // Extend only the spouts in play. On a single-spout trial the other
  // spouts stay retracted, so the animal has no alternative to choose.
  for (uint8_t k = 0; k < SV_COUNT; k++) {
    if (!servoPresent(k)) continue;
    if (g_sp[k].active) servoGoExtend(k);
    else                servoGoRetract(k);
  }
  enter(TS_EXTEND);
  return true;
}

void trialAbort() {
  if (!trialRunning()) return;
  uint32_t now = millis();
  solCloseAll();
  ledStopAll();
  spkStopAll();
  for (uint8_t k = 0; k < SV_COUNT; k++) servoGoRetract(k);
  g_outcome = TR_OUT_ABORT;
  emitEvent(now, "TRIAL_END", "", (long)g_id, (long)TR_OUT_ABORT);
  enter(TS_IDLE);
  g_state = TS_IDLE;
}

void trialReport() {
  Serial.print(F("R,TRIAL,"));
  Serial.print(g_id);            Serial.print(',');
  Serial.print(g_state);         Serial.print(',');
  Serial.print(g_mode);          Serial.print(',');
  Serial.print(chName(g_chosen));Serial.print(',');
  Serial.print(g_reqMet ? 1 : 0);Serial.print(',');
  Serial.print(g_outcome);       Serial.print(',');
  Serial.print(g_cueRewardMs);   Serial.print(',');
  Serial.print(g_omissionMs);    Serial.print(',');
  Serial.print(g_retractDelay);  Serial.print(',');
  Serial.print(g_itiMs);         Serial.print(',');
  Serial.println(g_gateMs);
}

// ---------------------------------------------------------------
//  Execution
// ---------------------------------------------------------------

static void deliverReward(uint32_t now) {
  SpoutCfg& S = g_sp[g_chosen];
  if (S.rewarded) {
    solDispenseMs(S.solIdx, S.dispenseMs);
    g_outcome = TR_OUT_REWARD;
    emitEvent(now, "TRIAL_REWARD", chName(g_chosen),
              (long)(S.solIdx + 1), (long)S.dispenseMs);
  } else {
    // Contingency withheld the reward. Everything else about the
    // trial is identical, which is the point: the animal cannot
    // predict omission of reward from anything other than its absence.
    g_outcome = TR_OUT_NOREWARD;
    emitEvent(now, "TRIAL_NOREWARD", chName(g_chosen),
              (long)(S.solIdx + 1), 0);
  }
  enter(TS_CONSUME);
}

void trialUpdate() {
  if (g_state == TS_IDLE) return;
  uint32_t now = millis();

  switch (g_state) {

    case TS_EXTEND:
      // Wait for physical arrival, not for a timer. Cueing at a spout
      // still in motion would make reward latency depend on servo
      // speed instead of on the animal.
      if (!servoAnyMoving()) {
        for (uint8_t k = 0; k < SV_COUNT; k++) {
          if (g_sp[k].active) lickResetCount(k);
        }
        armFireStaged();          // all cues start in one loop pass
        g_cueMs = millis();
        emitEvent(g_cueMs, "TRIAL_CUE", "", (long)g_id, (long)g_mode);
        enter(TS_RESPOND);
      }
      break;

    case TS_RESPOND: {
      // --- choice resolution: first lick wins ---
      if (g_chosen < 0) {
        for (uint8_t k = 0; k < SV_COUNT; k++) {
          if (g_sp[k].active && lickCount(k) > 0) {
            g_chosen = (int8_t)k;
            emitEvent(now, "TRIAL_CHOICE", chName(g_chosen),
                      (long)(now - g_cueMs), (long)lickCount(k));
            if (g_mode == 1) {
              // Retract the alternative immediately so the animal
              // cannot sample both and count licks on each.
              for (uint8_t j = 0; j < SV_COUNT; j++) {
                if (j != (uint8_t)g_chosen && g_sp[j].active) servoGoRetract(j);
              }
            }
            break;
          }
        }
      }

      // --- ratio requirement ---
      if (g_chosen >= 0 && !g_reqMet) {
        if (lickCount(g_chosen) >= g_sp[g_chosen].fr) {
          g_reqMet   = true;
          g_reqMetMs = now;
          emitEvent(now, "TRIAL_FR_MET", chName(g_chosen),
                    (long)g_sp[g_chosen].fr, (long)(now - g_cueMs));
        }
      }

      // --- reward timing: the later of the delay and requirement ---
      if (g_reqMet && (now - g_cueMs) >= g_cueRewardMs) {
        deliverReward(now);
        break;
      }

      // --- omission ---
      if ((now - g_cueMs) >= g_omissionMs) {
        g_outcome = TR_OUT_OMISSION;
        emitEvent(now, "TRIAL_OMISSION", chName(g_chosen),
                  (long)(g_chosen >= 0 ? lickCount(g_chosen) : 0), 0);
        for (uint8_t k = 0; k < SV_COUNT; k++) servoGoRetract(k);
        enter(TS_ITI);
      }
      break;
    }

    case TS_CONSUME:
      if (inState(now) >= g_retractDelay) {
        if (g_chosen >= 0) servoGoRetract((uint8_t)g_chosen);
        enter(TS_RETRACT);
      }
      break;

    case TS_RETRACT:
      if (!servoAnyMoving()) enter(TS_ITI);
      break;

    case TS_ITI:
      if (inState(now) >= g_itiMs) {
        emitEvent(now, "TRIAL_ITI_END", "", (long)g_id, 0);
        enter(TS_GATE);
      }
      break;

    case TS_GATE: {
      // The next trial must not begin in the middle of a licking bout.
      // Require a quiet period: no contact anywhere, and nothing
      // logged recently.
      bool quiet = true;
      for (uint8_t k = 0; k < SV_COUNT; k++) if (lickActive(k)) quiet = false;
      if (quiet && (now - lickLastEventMs()) >= g_gateMs &&
          inState(now) >= g_gateMs) {
        emitEvent(now, "TRIAL_END", "", (long)g_id, (long)g_outcome);
        g_state   = TS_IDLE;
        g_defined = false;
      }
      break;
    }

    default:
      break;
  }
}
