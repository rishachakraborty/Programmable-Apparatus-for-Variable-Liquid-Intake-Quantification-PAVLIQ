#ifndef BLOCKSWITCH_H
#define BLOCKSWITCH_H

#include <Arduino.h>
#include "config.h"

// =============================================================
//  BLOCK SWITCH
//
//  At a block reversal the fluid sitting in the spout dead space
//  is the OLD solution. Whatever the animal licks first after
//  the switch is that old fluid, at the moment the cue says it
//  should be the new one. On an alcohol/water preference task
//  that is not a rounding error - it is a mislabelled trial at
//  exactly the point in the session where the animal is being
//  asked to re-learn the mapping.
//
//  So a block switch is a physical operation, not a bookkeeping
//  one, and it gets its own state machine for the same reason
//  a trial does: the sequence is timed, has to survive USB
//  latency, and has to be logged with real timestamps.
//
//  Each spout has its OWN syringe pump, so its own vacuum. That
//  changes the sequencing from the shared-pump case: spouts can be
//  purged in parallel (BSMODE,0), which matters because a side
//  switch inside a choice block triggers this between trials and
//  the ITI is what has to absorb it.
//
//  Per spout, per cycle:
//      [VAC]    stepper aspirates, pulling the old solution back
//               out of the dead space
//      [DWELL]  let the line settle before opening a valve into it
//      [FILL]   the NEW solution's solenoid opens - one pulse, or
//               a train of them - refilling the dead space and
//               re-forming the bead at the tip
//      [DWELL]
//
//  Spouts are retracted first and stay retracted throughout, so
//  nothing is dripped on a head-fixed animal, and the whole thing
//  is refused outright while a trial is running.
//
//  Two or more cycles is the conservative setting: the first
//  clears the old solution, the second washes the line with the
//  new one and clears that too.
//
//  Definition:
//      BSNEW,<id>
//      BSSPOUT,<ch>,<sol>,<fill_ms>[,<pulses>][,<gap_ms>]
//      BSSPOUT,<ch>,<sol>,<fill_ms>[,<pulses>][,<gap_ms>]
//      BSVAC,<steps>[,<steps_per_sec>]
//      BSTIME,<pre_ms>,<vac_dwell_ms>,<fill_dwell_ms>,<post_ms>
//      BSMODE,<0|1>        0 = all spouts purge in parallel (fast),
//                          1 = one spout at a time (default, quieter)
//      BSCYCLES,<n>
//      BSRETURN,<0|1>      return the plunger at the end; default 0,
//                          safe only with a waste trap or check valve
//                          between the spout and the syringe
//      BSGO
// =============================================================

enum BlockOutcome : uint8_t {
  BLK_OUT_NONE  = 0,
  BLK_OUT_OK    = 1,
  BLK_OUT_ABORT = 2
};

void blockBegin();
void blockUpdate();

bool blockNew(uint32_t id);
bool blockAddSpout(uint8_t ch, uint8_t solIdx, uint32_t fillMs,
                   uint8_t pulses, uint32_t gapMs);
bool blockSetVac(uint32_t steps, uint16_t stepsPerSec);
bool blockSetTiming(uint32_t preMs, uint32_t vacDwellMs,
                    uint32_t fillDwellMs, uint32_t postMs);
bool blockSetMode(uint8_t sequential);   // 0 parallel, 1 one at a time

// Whether the syringe pump participates. With aspiration disabled the
// line is cleared by dispensing the new reinforcer through it in the
// retracted position, which needs no pump but discards more liquid.
bool blockSetUseStepper(bool use);
bool blockSetCycles(uint8_t n);
bool blockSetReturn(bool on);
bool blockStart();
void blockAbort();
void blockReport();

bool blockRunning();

#endif // BLOCKSWITCH_H
