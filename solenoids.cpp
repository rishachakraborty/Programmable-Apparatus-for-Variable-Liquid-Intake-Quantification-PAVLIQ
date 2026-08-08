#include <EEPROM.h>
#include "solenoids.h"
#include "proto.h"

// =============================================================
//  SOLENOIDS
//
//  Four gates: two feed the left spout, two feed the right, in the
//  default configuration. Which liquid and which spout each one
//  serves is CONFIGURATION, not something the firmware assumes, so
//  that the rig can be repurposed without a reflash.
//
//  Every open is watchdogged. If the host crashes with a gate open,
//  the firmware closes it and reports the fault rather than
//  emptying a reservoir into the rig.
// =============================================================

struct SolState {
  uint8_t  pin;
  char     liquid[SOL_LIQUID_NAME_LEN];
  uint8_t  spout;
  uint32_t nlPerMs;
  bool     open;
  bool     timed;
  uint32_t openedMs;
  uint32_t plannedMs;
};

static SolState g_sol[SOL_COUNT_MAX];
static uint32_t g_blankUntilMs = 0;

static const char* const SPOUT_NAMES[4] = {"NONE", "L", "C", "R"};

// ---------------------------------------------------------------

static inline bool validIdx(uint8_t idx) {
  if (idx >= SOL_COUNT_MAX) { emitErr(F("SOL_BAD_INDEX_USE_1_TO_4")); return false; }
  return true;
}

static inline void markBlanking() {
  g_blankUntilMs = millis() + SOL_BLANKING_MS;
}

bool solBlankingActive() {
  return (int32_t)(millis() - g_blankUntilMs) < 0;
}

// ---------------------------------------------------------------

void solBegin() {
  g_sol[0].pin = PIN_SOL1;
  g_sol[1].pin = PIN_SOL2;
  g_sol[2].pin = PIN_SOL3;
  g_sol[3].pin = PIN_SOL4;

  for (uint8_t k = 0; k < SOL_COUNT_MAX; k++) {
    pinMode(g_sol[k].pin, OUTPUT);
    digitalWrite(g_sol[k].pin, LOW);      // fail closed
    g_sol[k].liquid[0] = '\0';
    g_sol[k].spout     = SPOUT_NONE;
    g_sol[k].nlPerMs   = 0;
    g_sol[k].open      = false;
    g_sol[k].timed     = false;
  }
  solLoadFromEeprom();
}

// ---- identity --------------------------------------------------

bool solSetIdentity(uint8_t idx, const char* liquid, uint8_t spout) {
  if (!validIdx(idx)) return false;
  if (spout > SPOUT_R) { emitErr(F("SOL_BAD_SPOUT_USE_L_C_R")); return false; }

  strncpy(g_sol[idx].liquid, liquid, SOL_LIQUID_NAME_LEN - 1);
  g_sol[idx].liquid[SOL_LIQUID_NAME_LEN - 1] = '\0';
  g_sol[idx].spout = spout;
  solSaveToEeprom();
  return true;
}

const char* solLiquid(uint8_t idx) {
  if (idx >= SOL_COUNT_MAX) return "";
  return g_sol[idx].liquid;
}

uint8_t solSpout(uint8_t idx) {
  if (idx >= SOL_COUNT_MAX) return SPOUT_NONE;
  return g_sol[idx].spout;
}

void solReportIdentity(uint8_t idx) {
  if (!validIdx(idx)) return;
  // R,SOL,<n>,<liquid>,<spout>,<nl_per_ms>,<open>
  Serial.print(F("R,SOL,"));
  Serial.print(idx + 1);                     Serial.print(',');
  Serial.print(g_sol[idx].liquid[0] ? g_sol[idx].liquid : "UNSET");
  Serial.print(',');
  Serial.print(SPOUT_NAMES[g_sol[idx].spout]); Serial.print(',');
  Serial.print(g_sol[idx].nlPerMs);          Serial.print(',');
  Serial.println(g_sol[idx].open ? 1 : 0);
}

void solReportAll() {
  for (uint8_t k = 0; k < SOL_COUNT_MAX; k++) solReportIdentity(k);
}

// ---- calibration -----------------------------------------------

bool solSetCalibration(uint8_t idx, uint32_t nlPerMs) {
  if (!validIdx(idx)) return false;
  g_sol[idx].nlPerMs = nlPerMs;
  solSaveToEeprom();
  return true;
}

uint32_t solCalibration(uint8_t idx) {
  if (idx >= SOL_COUNT_MAX) return 0;
  return g_sol[idx].nlPerMs;
}

// ---- actuation -------------------------------------------------

static void rawOpen(uint8_t idx, bool timed, uint32_t plannedMs) {
  SolState& S = g_sol[idx];
  markBlanking();
  uint32_t now = millis();
  digitalWrite(S.pin, HIGH);
  S.open      = true;
  S.timed     = timed;
  S.openedMs  = now;
  S.plannedMs = plannedMs;
  emitEvent(now, "SOL_OPEN", "", (long)(idx + 1), (long)plannedMs);
}

static void rawClose(uint8_t idx, const __FlashStringHelper* reason) {
  SolState& S = g_sol[idx];
  if (!S.open) return;
  markBlanking();
  uint32_t now = millis();
  digitalWrite(S.pin, LOW);
  uint32_t actual = now - S.openedMs;
  S.open  = false;
  S.timed = false;
  // d1 = solenoid number, d2 = actual open time in ms
  emitEvent(now, "SOL_CLOSE", "", (long)(idx + 1), (long)actual);
  if (reason != NULL) emitErr(reason);
}

bool solOpen(uint8_t idx) {
  if (!validIdx(idx)) return false;
  if (g_sol[idx].open) { emitErr(F("SOL_ALREADY_OPEN")); return false; }
  rawOpen(idx, false, 0);
  return true;
}

bool solClose(uint8_t idx) {
  if (!validIdx(idx)) return false;
  if (!g_sol[idx].open) { emitErr(F("SOL_ALREADY_CLOSED")); return false; }
  rawClose(idx, NULL);
  return true;
}

bool solDispenseMs(uint8_t idx, uint32_t ms) {
  if (!validIdx(idx)) return false;
  if (ms == 0)                     { emitErr(F("SOL_DURATION_ZERO")); return false; }
  if (ms > SOL_DISPENSE_MAX_MS)    { emitErr(F("SOL_DURATION_ABOVE_MAX")); return false; }
  if (g_sol[idx].open)             { emitErr(F("SOL_ALREADY_OPEN")); return false; }
  rawOpen(idx, true, ms);
  return true;
}

bool solDispenseNl(uint8_t idx, uint32_t nl) {
  if (!validIdx(idx)) return false;
  uint32_t cal = g_sol[idx].nlPerMs;
  if (cal == 0) { emitErr(F("SOL_NOT_CALIBRATED")); return false; }
  uint32_t ms = (nl + cal / 2UL) / cal;      // rounded
  if (ms == 0) ms = 1;
  return solDispenseMs(idx, ms);
}

void solCloseAll() {
  for (uint8_t k = 0; k < SOL_COUNT_MAX; k++) {
    if (g_sol[k].open) rawClose(k, NULL);
  }
}

bool solIsOpen(uint8_t idx) {
  if (idx >= SOL_COUNT_MAX) return false;
  return g_sol[idx].open;
}

void solUpdate() {
  uint32_t now = millis();
  for (uint8_t k = 0; k < SOL_COUNT_MAX; k++) {
    SolState& S = g_sol[k];
    if (!S.open) continue;

    if (S.timed) {
      if ((now - S.openedMs) >= S.plannedMs) rawClose(k, NULL);
    } else {
      // Manual flush watchdog: never leave a gate open indefinitely.
      if ((now - S.openedMs) >= SOL_MANUAL_MAX_MS) {
        rawClose(k, F("SOL_WATCHDOG_FORCED_CLOSE"));
      }
    }
  }
}

// ---- EEPROM ----------------------------------------------------
//  layout: [magic][ per solenoid: liquid[16], spout, nlPerMs(4) ]

struct SolPersist {
  char     liquid[SOL_LIQUID_NAME_LEN];
  uint8_t  spout;
  uint32_t nlPerMs;
};

void solSaveToEeprom() {
  int addr = EEPROM_BASE_ADDR;
  EEPROM.update(addr++, EEPROM_MAGIC);
  for (uint8_t k = 0; k < SOL_COUNT_MAX; k++) {
    SolPersist p;
    memcpy(p.liquid, g_sol[k].liquid, SOL_LIQUID_NAME_LEN);
    p.spout   = g_sol[k].spout;
    p.nlPerMs = g_sol[k].nlPerMs;
    EEPROM.put(addr, p);
    addr += sizeof(SolPersist);
  }
}

void solLoadFromEeprom() {
  int addr = EEPROM_BASE_ADDR;
  if (EEPROM.read(addr++) != EEPROM_MAGIC) return;   // never written
  for (uint8_t k = 0; k < SOL_COUNT_MAX; k++) {
    SolPersist p;
    EEPROM.get(addr, p);
    addr += sizeof(SolPersist);
    p.liquid[SOL_LIQUID_NAME_LEN - 1] = '\0';
    memcpy(g_sol[k].liquid, p.liquid, SOL_LIQUID_NAME_LEN);
    g_sol[k].spout   = (p.spout <= SPOUT_R) ? p.spout : SPOUT_NONE;
    g_sol[k].nlPerMs = p.nlPerMs;
  }
}
