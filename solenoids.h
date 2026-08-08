#ifndef SOLENOIDS_H
#define SOLENOIDS_H

#include <Arduino.h>
#include "config.h"

// Solenoids are addressed 1..4 by the user, 0..3 internally.
enum SolSpout : uint8_t { SPOUT_NONE = 0, SPOUT_L = 1, SPOUT_C = 2, SPOUT_R = 3 };

void solBegin();

// ---- identity --------------------------------------------------
// Each solenoid's identity is (liquid name, spout it feeds). The
// Python task-setup page writes these; they persist in EEPROM so
// the debug menu is usable standalone.
bool solSetIdentity(uint8_t idx, const char* liquid, uint8_t spout);
void solReportIdentity(uint8_t idx);
void solReportAll();
const char* solLiquid(uint8_t idx);
uint8_t     solSpout(uint8_t idx);

// ---- calibration -----------------------------------------------
// Stored as nanolitres per millisecond of open time (integer, so no
// float parsing on the wire). Measure by weighing the output of a
// known number of long opens. Lets the GUI take microlitres and
// convert, instead of making the user think in milliseconds.
bool solSetCalibration(uint8_t idx, uint32_t nlPerMs);
uint32_t solCalibration(uint8_t idx);

// ---- actuation -------------------------------------------------
bool solOpen(uint8_t idx);                    // manual flush, watchdog capped
bool solClose(uint8_t idx);
bool solDispenseMs(uint8_t idx, uint32_t ms); // timed open
bool solDispenseNl(uint8_t idx, uint32_t nl); // volume, uses calibration
void solCloseAll();

bool solIsOpen(uint8_t idx);

void solUpdate();

// ---- lick blanking ---------------------------------------------
// True for SOL_BLANKING_MS around every solenoid edge. Step 3's
// lick detector discards samples while this is asserted so that
// switching transients are not logged as licks.
bool solBlankingActive();

void solSaveToEeprom();
void solLoadFromEeprom();

#endif // SOLENOIDS_H
