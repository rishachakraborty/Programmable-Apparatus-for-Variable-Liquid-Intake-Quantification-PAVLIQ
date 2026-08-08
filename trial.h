#ifndef TRIAL_H
#define TRIAL_H

#include <Arduino.h>
#include "config.h"

// =============================================================
//  TRIAL STATE MACHINE
//
//  Python owns the SEQUENCE: which trial comes next, all
//  randomization, block structure, ratio schedules, logging.
//  The Arduino owns the EXECUTION of a single trial.
//
//  The split is deliberate. If Python decided "lick detected ->
//  retract the other spout -> open the solenoid", every reward
//  would be at the mercy of USB scheduling, which is 5-30 ms and
//  occasionally far worse. Here the whole responsive loop runs on
//  the microcontroller and Python receives timestamped events.
//
//  Defining and running a trial:
//      ARM,SPK,l,500,10000,1,50,50     stage cues (fire together)
//      ARM,SPK,r,500,5000,1,200,50
//      TRNEW,<id>,<mode>               0 = single spout, 1 = choice
//      TRSPOUT,<ch>,<sol>,<ms>,<fr>,<rewarded>
//      TRSPOUT,<ch>,<sol>,<ms>,<fr>,<rewarded>   (choice: twice)
//      TRTIME,<cueReward>,<omission>,<retractDelay>,<iti>
//      TRGO
// =============================================================

enum TrialOutcome : uint8_t {
  TR_OUT_NONE     = 0,
  TR_OUT_REWARD   = 1,   // requirement met, reward delivered
  TR_OUT_NOREWARD = 2,   // requirement met, contingency withheld reward
  TR_OUT_OMISSION = 3,   // requirement not met within the response window
  TR_OUT_ABORT    = 4
};

void trialBegin();
void trialUpdate();

bool trialNew(uint32_t id, uint8_t mode);       // mode 0 single, 1 choice
bool trialAddSpout(uint8_t ch, uint8_t solIdx, uint32_t dispenseMs,
                   uint16_t fr, bool rewarded);
bool trialSetTiming(uint32_t cueRewardMs, uint32_t omissionMs,
                    uint32_t retractDelayMs, uint32_t itiMs);
bool trialSetGate(uint16_t ms);                 // quiet period before next trial
bool trialStart();
void trialAbort();
void trialReport();

bool trialRunning();

#endif // TRIAL_H
