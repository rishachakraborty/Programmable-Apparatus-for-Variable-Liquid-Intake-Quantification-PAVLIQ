#ifndef SERVOS_H
#define SERVOS_H

#include <Arduino.h>
#include "config.h"

enum ServoCh : uint8_t { SV_L = 0, SV_C = 1, SV_R = 2, SV_COUNT = 3 };

void servoBegin();

// Attach and drive to the fully-retracted zero angle for this
// channel. Always allowed regardless of step size.
bool servoInit(uint8_t ch);
bool servoInitAll();

// Move to an absolute angle.
//   force = true bypasses the SERVO_MIN_STEP guard. Intended ONLY
//   for the manual initialization page, where the user dials the
//   spout in by 1-2 degrees to find the dispense position.
bool servoWrite(uint8_t ch, int angle, bool force);

// Relative moves. delta must be >= SERVO_MIN_STEP.
// Forward = extend toward the mouse, back = retract away from it,
// resolved through the per-channel extend direction.
// force also bypasses the SERVO_MIN_STEP guard and the soft limits.
// The 0-180 hardware range is never bypassable.
bool servoForward(uint8_t ch, int delta, bool force = false);
bool servoBack(uint8_t ch, int delta, bool force = false);

// Halt in place mid-move.
void servoHalt(uint8_t ch);

bool servoSetSlew(uint8_t ch, uint16_t degPerSec);
bool servoSetDir(uint8_t ch, int8_t dir);
bool servoAttach(uint8_t ch);
bool servoDetach(uint8_t ch);
void servoDetachAll();

// Soft travel limits, tighter than the 0-180 hardware range. Set
// these to your actuator's real mechanical stops. Without them a
// command past the end of travel stalls the servo indefinitely, and
// the firmware has no way to detect that it happened.
bool servoSetLimits(uint8_t ch, int lo, int hi);

// Detach automatically after this many milliseconds without motion.
// 0 = never detach (default). Detaching silences holding-torque hum
// but lets the actuator drift under load.
bool servoSetIdleDetach(uint8_t ch, uint32_t ms);

// Change the retracted reference angle at runtime. Clears posKnown,
// so SVINIT must be run again to re-establish the reference.
bool servoSetZero(uint8_t ch, int angle, bool force = false);

// Raw pulse width in microseconds, bypassing the angle model. Use
// this to find each actuator's true mechanical limits.
bool servoWriteUs(uint8_t ch, uint16_t us);

// Extended (dispense) position, captured once on the initialization
// page. The retracted position is the zero angle. The trial state
// machine only ever moves between these two.
bool servoSetExtend(uint8_t ch, int angle, bool force = false);
int  servoExtendAngle(uint8_t ch);
bool servoExtendSet(uint8_t ch);
bool servoGoExtend(uint8_t ch);
bool servoGoRetract(uint8_t ch);

// Reported position is the commanded angle, tracked internally with
// sub-degree precision. Hobby servos have no feedback wire, so this
// is trustworthy only while the actuator is not stalled or forced.
int  servoCurrentAngle(uint8_t ch);
int  servoTargetAngle(uint8_t ch);
bool servoIsMoving(uint8_t ch);
bool servoIsAttached(uint8_t ch);
void servoReport(uint8_t ch);

// True while ANY servo is mid-move. Step 4 uses this to hold the
// trial state machine until spouts have physically arrived.
bool servoAnyMoving();

void servoUpdate();

uint8_t servoChFromToken(const char* tok);
const char* servoChName(uint8_t ch);

#endif // SERVOS_H
