#ifndef LICK_H
#define LICK_H

#include <Arduino.h>
#include "config.h"

enum LickCh : uint8_t { LK_L = 0, LK_C = 1, LK_R = 2 };

void lickBegin();

// Whether this sensor exists on the rig. Absent channels are not
// sampled and refuse calibration.
bool lickSetPresent(uint8_t ch, bool present);
bool lickPresent(uint8_t ch);
void lickUpdate();

// ---- calibration ----------------------------------------------
// Both calibrations are non-blocking: they accumulate in lickUpdate()
// and report when finished. Nothing else in the firmware stalls.

// Baseline only. Spout must be UNTOUCHED. Measures mean and standard
// deviation of the resting signal and sets thresholds at k*sd.
// Use when you cannot conveniently touch the spout.
bool lickCalibrateBaseline(uint8_t ch, uint16_t ms);

// Contact level. Spout must be TOUCHED for the whole window. Measures
// the contact level, derives polarity from which way the signal moved,
// and places thresholds inside the measured gap. More robust than
// baseline-only, because it knows how big a real lick actually is.
bool lickCalibrateTouch(uint8_t ch, uint16_t ms);

// Manual override when calibration cannot capture the situation.
bool lickSetThresholds(uint8_t ch, float onD, float offD, int8_t pol);

// Timing windows, tunable at runtime so a rig can be adjusted
// without a reflash. minOff is the important one: raise it if a
// single sustained contact fragments into several licks.
bool lickSetTiming(uint16_t minOn, uint16_t minOff, uint16_t refract);
void lickReportTiming();

bool lickIsCalibrated(uint8_t ch);
void lickReport(uint8_t ch);
void lickReportAll();

// ---- raw streaming --------------------------------------------
// Prints raw ADC values so you can watch what the sensor actually
// does. This is how you discover polarity and rough magnitude before
// committing to any calibration.
bool lickStreamRaw(uint8_t ch, uint16_t ms);
void lickStopStream();

// ---- detection ------------------------------------------------
void lickSetEnabled(uint8_t ch, bool en);
bool lickEnabled(uint8_t ch);

// Lick counters, used by the operant ratio logic in Step 4.
uint32_t lickCount(uint8_t ch);
void     lickResetCount(uint8_t ch);
void     lickResetAllCounts();

// True while a tongue is in contact. Step 4 uses this for the
// "no lick for 500 ms" gate before starting a new trial.
bool     lickActive(uint8_t ch);
uint32_t lickLastEventMs();

uint8_t lickChFromChar(char c);
const char* lickChName(uint8_t ch);

#endif // LICK_H
