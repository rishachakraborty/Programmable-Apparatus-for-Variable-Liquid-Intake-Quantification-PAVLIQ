#ifndef STEPPER_H
#define STEPPER_H

#include <Arduino.h>
#include "config.h"

// =============================================================
//  STEPPER AXES (NEMA 11 + DRV8825), ONE PER SPOUT
//
//  Each spout has its own syringe providing negative pressure at
//  the tip, so axes are indexed exactly like servos and lick
//  sensors: 0 = L, 1 = C, 2 = R. A rig with two pumps simply
//  leaves the third axis unconfigured; it refuses motion rather
//  than pretending to move.
//
//  Vocabulary is ASPIRATE (pull vacuum, plunger withdraws) and
//  DISPENSE (push back). Which physical direction that is depends
//  on how the carriage is mounted, so it is a runtime setting
//  (STPDIR), not a wiring assumption.
//
//  Three things this module deliberately does NOT do:
//
//   1. It does not block. Steps are emitted from stepperUpdate()
//      against micros(), so lick sampling and solenoid timing keep
//      running while a plunger moves. All axes step from the same
//      pass, so two pumps can run at once.
//
//   2. It does not stay energised. EN is released HOLD ms after
//      the last step (default immediate). Holding current buzzes,
//      heats the motor, and injects noise into a resistive
//      lickometer centimetres away. Safe only because a T6x1 lead
//      screw is not back-drivable at plunger loads.
//
//   3. It has no feedback. Position is counted steps, exactly like
//      the servo's commanded angle. A stall drifts silently. Soft
//      limits and STPZERO are the only defence; re-zero every
//      session.
// =============================================================

enum StepCh : uint8_t { STP_L = 0, STP_C = 1, STP_R = 2, STP_COUNT = 3 };

void stepperBegin();
void stepperUpdate();

// Motion. All return false and emit a specific error on refusal.
bool stepperAspirate(uint8_t ch, uint32_t steps);
bool stepperDispense(uint8_t ch, uint32_t steps);
bool stepperMoveTo(uint8_t ch, long posSteps);      // absolute, needs zero
bool stepperAspirateNl(uint8_t ch, uint32_t nl);    // needs STPCAL
bool stepperDispenseNl(uint8_t ch, uint32_t nl);
void stepperHalt(uint8_t ch);
void stepperHaltAll();

// Configuration.
bool stepperSetSpeed(uint8_t ch, uint16_t stepsPerSec);
bool stepperSetAccel(uint8_t ch, uint16_t stepsPerSec2);   // 0 = no ramp
bool stepperSetDir(uint8_t ch, int8_t aspirateSign);       // +1 or -1
bool stepperSetLimits(uint8_t ch, long lo, long hi);
bool stepperSetCal(uint8_t ch, uint32_t nlPerStep);
bool stepperSetHold(uint8_t ch, long ms);   // <0 = hold forever

// An axis with no driver wired is "unconfigured": it accepts
// configuration but refuses motion, so a two-pump rig cannot
// silently believe it purged a third spout.
bool stepperSetPresent(uint8_t ch, bool present);
bool stepperPresent(uint8_t ch);

void stepperEnable(uint8_t ch, bool on);       // EN is active LOW
bool stepperIsEnabled(uint8_t ch);

void stepperZero(uint8_t ch);
bool stepperPosKnown(uint8_t ch);

bool stepperIsMoving(uint8_t ch);
bool stepperAnyMoving();
long stepperPosition(uint8_t ch);
long stepperRemaining(uint8_t ch);
uint32_t stepperNlPerStep(uint8_t ch);
void stepperReport(uint8_t ch);
void stepperReportAll();

uint8_t stepperChFromToken(const char* tok);
const char* stepperChName(uint8_t ch);

#endif // STEPPER_H
