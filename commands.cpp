#include "commands.h"
#include "proto.h"
#include "led.h"
#include "speaker.h"
#include "servos.h"
#include "solenoids.h"
#include "lick.h"
#include "trial.h"
#include "stepper.h"
#include "blockswitch.h"
#include "debug.h"

static void toUpper(char* s) {
  for (; *s; s++) if (*s >= 'a' && *s <= 'z') *s -= 32;
}

static bool eq(const char* a, const char* b) {
  return strcmp(a, b) == 0;
}

void printHelp() {
  emitInfo(F("--- COMMAND REFERENCE ---"));
  emitInfo(F("PING                        -> R,PING,1"));
  emitInfo(F("ID                          -> firmware name/version"));
  emitInfo(F("SYNC                        -> R,SYNC,<millis>"));
  emitInfo(F("STATUS                      -> active cue summary"));
  emitInfo(F("LED,<w|b|g>,<dur_ms>,<mode>,<pulse_hz>,<bright>"));
  emitInfo(F("    mode 0=constant 1=pulsing; dur 0=until LEDSTOP"));
  emitInfo(F("    bright 0-255"));
  emitInfo(F("LEDSTOP,<w|b|g>"));
  emitInfo(F("SPK,<l|r>,<dur_ms>,<tone_hz>,<mode>,<click_hz>,<vol>[,<on_us>]"));
  emitInfo(F("    mode 0=constant 1=clicktrain; vol 0-50 (duty %)"));
  emitInfo(F("    on_us optional, 0/omitted = 50% of click period"));
  emitInfo(F("SPKSTOP,<l|r>"));
  emitInfo(F("--- servos: l=left c=center r=right ---"));
  emitInfo(F("SVINIT,<l|c|r|all>          zero = fully retracted"));
  emitInfo(F("SVREAD,<l|c|r>              -> R,SERVO,ch,cur,tgt,moving,slew,dir,att"));
  emitInfo(F("SVWRITE,<l|c|r>,<angle>[,<force>]"));
  emitInfo(F("    force=1 bypasses the 10 deg minimum (init page only)"));
  emitInfo(F("SVFWD,<l|c|r>,<deg>[,<force>]   extend toward mouse"));
  emitInfo(F("SVBACK,<l|c|r>,<deg>[,<force>]  retract"));
  emitInfo(F("    force=1 ignores soft limits and the 10 deg minimum;"));
  emitInfo(F("    the 0-180 hardware range is never bypassable."));
  emitInfo(F("SVSLEW,<l|c|r>,<deg_per_s>  20-2000, default 400"));
  emitInfo(F("SVDIR,<l|c|r>,<1|-1>        which way extends"));
  emitInfo(F("SVSTOP,<l|c|r>              halt in place"));
  emitInfo(F("SVATTACH / SVDETACH,<l|c|r>"));
  emitInfo(F("SVDETACH,all  or  SVOFF     PANIC: all servos limp"));
  emitInfo(F("SVLIMIT,<l|c|r>,<min>,<max> soft travel limits"));
  emitInfo(F("    Set these to the actuator's real mechanical stops."));
  emitInfo(F("    A command past the stop stalls the servo forever and"));
  emitInfo(F("    the firmware CANNOT detect it - there is no feedback."));
  emitInfo(F("SVIDLE,<l|c|r|all>,<ms>     auto-detach after idle, default 500"));
  emitInfo(F("    0 = hold position. Holding hums; detaching drifts."));
  emitInfo(F("SVZERO,<l|c|r>,<angle>[,<force>]  set RETRACTED position"));
  emitInfo(F("SVEXT,<l|c|r>,<angle>[,<force>]   set DRINKING position"));
  emitInfo(F("SVUS,<l|c|r>,<microsec>     RAW pulse, 544-2400, no angle math"));
  emitInfo(F("    Use SVUS to find real mechanical stops. Angle 0 and 180"));
  emitInfo(F("    jam many hobby servos against their internal limits."));
  emitInfo(F("--- steppers: one syringe pump per spout ---"));
  emitInfo(F("STPREAD,<l|c|r> / STPALL    position, limits, calibration"));
  emitInfo(F("STPZERO,<ch>                declare here to be zero"));
  emitInfo(F("STPASP,<ch>,<steps>         aspirate (pull vacuum)"));
  emitInfo(F("STPDIS,<ch>,<steps>         dispense (push back)"));
  emitInfo(F("STPGOTO,<ch>,<pos>          absolute, needs STPZERO"));
  emitInfo(F("STPVOL,<ch>,<nl>,<1=asp|0=dis>   by volume, needs STPCAL"));
  emitInfo(F("STPSPS/STPACC/STPDIR/STPLIM/STPCAL/STPHOLD,<ch>,..."));
  emitInfo(F("STPPRESENT,<ch>,<0|1>       does this spout have a pump"));
  emitInfo(F("STPSTOP/STPON/STPOFF,<ch>"));
  emitInfo(F("--- block switch: purge and refill the dead space ---"));
  emitInfo(F("BSNEW,<id>"));
  emitInfo(F("BSSPOUT,<ch>,<sol>,<fill_ms>[,<pulses>][,<gap_ms>]"));
  emitInfo(F("BSVAC,<steps>[,<steps_per_sec>]"));
  emitInfo(F("BSTIME,<pre>,<vac_dwell>,<fill_dwell>,<post>"));
  emitInfo(F("BSMODE,<0=parallel|1=one spout at a time>"));
  emitInfo(F("BSPUMP,<0|1>       use the syringe pump for aspiration"));
  emitInfo(F("    0 clears the line by dispensing through it instead"));
  emitInfo(F("BSCYCLES,<n>   BSRETURN,<0|1>   BSGO / BSABORT / BSSTATE"));
  emitInfo(F("--- trials ---"));
  emitInfo(F("ARM,<cue>...                stage cues first"));
  emitInfo(F("TRNEW,<id>,<mode>           mode 0=single spout 1=choice"));
  emitInfo(F("TRSPOUT,<ch>,<sol>,<ms>,<fr>,<rewarded>"));
  emitInfo(F("TRTIME,<cueReward>,<omission>,<retractDelay>,<iti>"));
  emitInfo(F("TRGATE,<ms>                 quiet period before trial ends"));
  emitInfo(F("TRGO / TRABORT / TRSTATE"));
  emitInfo(F("--- lick sensors: l=left c=center r=right ---"));
  emitInfo(F("LKRAW,<ch>,<ms>             stream raw ADC. START HERE."));
  emitInfo(F("LKCAL,<ch>,<ms>             baseline. DO NOT TOUCH spout."));
  emitInfo(F("LKTOUCH,<ch>,<ms>           contact level. HOLD the spout."));
  emitInfo(F("    Nothing is hardcoded: run LKCAL then LKTOUCH on each"));
  emitInfo(F("    sensor at the start of every session."));
  emitInfo(F("LKREAD,<ch> / LKALL         thresholds and counts"));
  emitInfo(F("LKON,<ch> / LKOFF,<ch>      enable / disable detection"));
  emitInfo(F("LKRESET,<ch> / LKZERO       reset counters"));
  emitInfo(F("LKSTOP                      stop any raw stream"));
  emitInfo(F("LKTIME,<minOn>,<minOff>,<refract>   ms, blank=report"));
  emitInfo(F("    Raise minOff if one sustained contact fragments into"));
  emitInfo(F("    several licks. 25 is the default, 40 is still safe."));
  emitInfo(F("LKSET,<ch>,<onDelta>,<offDelta>[,<pol>]  manual override"));
  emitInfo(F("--- solenoids: 1-8, unwired ones marked absent ---"));
  emitInfo(F("SOLID,<n>,<liquid>,<l|c|r>  set identity (no commas in name)"));
  emitInfo(F("SOLGET,<n>                  0 or blank = report all"));
  emitInfo(F("SOLPRESENT,<n>,<0|1>        is a driver wired here"));
  emitInfo(F("SVPRESENT,<l|c|r>,<0|1>     does this spout exist"));
  emitInfo(F("LKPRESENT,<l|c|r>,<0|1>     does this sensor exist"));
  emitInfo(F("SOLCAL,<n>,<nl_per_ms>      calibration, nanolitres per ms"));
  emitInfo(F("SOLOPEN,<n> / SOLCLOSE,<n>  manual flush"));
  emitInfo(F("SOLDISP,<n>,<ms>            timed dispense"));
  emitInfo(F("SOLVOL,<n>,<nl>             dispense by volume (needs SOLCAL)"));
  emitInfo(F("--- synchronised onset ---"));
  emitInfo(F("ARM,<any cue command>       stage it, do not fire yet"));
  emitInfo(F("GO                          fire everything staged together"));
  emitInfo(F("ARM                         list what is staged"));
  emitInfo(F("DISARM                      clear the staging buffer"));
  emitInfo(F("    Use this for choice trials: both tones must start at"));
  emitInfo(F("    the same instant, which two separate commands cannot do."));
  emitInfo(F("STOPALL                     cues off, gates closed, servos halt"));
  emitInfo(F("DEBUG                       -> interactive menu"));
  emitInfo(F("HELP"));
}

// ---------------------------------------------------------------
//  Cue staging
//
//  ARM copies a command verbatim into a slot; GO dispatches every
//  armed slot inside one pass of loop(). Onset skew between staged
//  cues is the cost of the dispatch itself - tens of microseconds -
//  so a choice trial's two tones begin together and carry the same
//  millisecond timestamp.
// ---------------------------------------------------------------

static char    g_armed[ARM_MAX_SLOTS][ARM_SLOT_LEN];
static uint8_t g_armCount = 0;
static bool    g_armFiring = false;

static void armClear() {
  g_armCount = 0;
}

static bool armPush(const char* cmd) {
  if (g_armFiring) { emitErr(F("ARM_BUSY_FIRING")); return false; }
  if (g_armCount >= ARM_MAX_SLOTS) { emitErr(F("ARM_BUFFER_FULL")); return false; }
  if (cmd == NULL || cmd[0] == '\0') { emitErr(F("ARM_EMPTY_COMMAND")); return false; }
  if (strlen(cmd) >= ARM_SLOT_LEN) { emitErr(F("ARM_COMMAND_TOO_LONG")); return false; }
  strncpy(g_armed[g_armCount], cmd, ARM_SLOT_LEN - 1);
  g_armed[g_armCount][ARM_SLOT_LEN - 1] = '\0';
  g_armCount++;
  return true;
}

void armFireStaged() {
  if (g_armCount == 0) { emitErr(F("ARM_NOTHING_STAGED")); return; }
  uint8_t n = g_armCount;
  // Dispatch straight out of the slots. dispatchCommand chews the
  // buffer up in place, but the slots are cleared immediately after,
  // so no copy is needed - and a 672-byte duplicate buffer on an
  // 8 KB part is not affordable. g_armFiring blocks re-entry.
  g_armFiring = true;
  emitEvent(millis(), "SYNC_GO", "", (long)n, 0);
  for (uint8_t k = 0; k < n; k++) dispatchCommand(g_armed[k]);
  g_armFiring = false;
  armClear();
}

static void armList() {
  emitReplyNum(F("ARMED"), (long)g_armCount);
  for (uint8_t k = 0; k < g_armCount; k++) {
    Serial.print(F("#,  slot "));
    Serial.print(k + 1);
    Serial.print(F(": "));
    Serial.println(g_armed[k]);
  }
}

void dispatchCommand(char* line) {
  if (line == NULL || line[0] == '\0') return;

  // Staging is handled BEFORE tokenising, because ARM must capture
  // the remainder of the line intact rather than a split copy.
  if ((line[0] == 'A' || line[0] == 'a') &&
      (line[1] == 'R' || line[1] == 'r') &&
      (line[2] == 'M' || line[2] == 'm')) {
    if (line[3] == ',') {
      if (armPush(line + 4)) emitAck(F("ARM"));
      return;
    }
    if (line[3] == '\0') { armList(); return; }
  }

  Args a;
  a.parse(line);
  if (a.n == 0) return;

  char verb[16];
  strncpy(verb, a.s(0), sizeof(verb) - 1);
  verb[sizeof(verb) - 1] = '\0';
  toUpper(verb);

  // ---------------- housekeeping ----------------
  if (eq(verb, "PING")) {
    emitReplyNum(F("PING"), 1);
    return;
  }
  if (eq(verb, "ID")) {
    Serial.print(F("R,ID,"));
    Serial.print(F(FW_NAME));
    Serial.print(' ');
    Serial.println(F(FW_VERSION));
    return;
  }
  if (eq(verb, "SYNC")) {
    // Read millis() as late as possible so the reported value is
    // as close as we can get to the instant of transmission.
    emitReplyNum(F("SYNC"), (long)millis());
    return;
  }
  if (eq(verb, "HELP") || eq(verb, "?")) {
    printHelp();
    return;
  }
  if (eq(verb, "STATUS")) {
    for (uint8_t k = 0; k < LED_COUNT; k++) {
      Serial.print(F("R,LED_"));
      Serial.print(ledChName(k));
      Serial.print(',');
      Serial.println(ledIsActive(k) ? 1 : 0);
    }
    for (uint8_t k = 0; k < SPK_COUNT; k++) {
      Serial.print(F("R,SPK_"));
      Serial.print(spkChName(k));
      Serial.print(',');
      Serial.println(spkIsActive(k) ? 1 : 0);
    }
    return;
  }
  if (eq(verb, "GO"))     { armFireStaged(); return; }
  if (eq(verb, "DISARM")) { armClear(); emitAck(F("DISARM")); return; }
  if (eq(verb, "DEBUG")) {
    debugEnter();
    return;
  }

  // ---------------- LEDs ----------------
  if (eq(verb, "LED")) {
    if (a.n < 6) { emitErr(F("LED_NEEDS_5_ARGS")); return; }
    uint8_t ch = ledChFromChar(a.c(1));
    if (ch >= LED_COUNT) { emitErr(F("LED_BAD_CHANNEL")); return; }

    long dur    = a.i(2, 0);
    long mode   = a.i(3, 0);
    long pulse  = a.i(4, 0);
    long bright = a.i(5, 255);

    if (dur < 0)    { emitErr(F("LED_DURATION_NEGATIVE")); return; }
    if (bright < 0 || bright > 255) { emitErr(F("LED_BRIGHTNESS_RANGE")); return; }

    if (ledStart(ch, (uint32_t)dur, mode != 0,
                 (uint16_t)pulse, (uint8_t)bright)) {
      emitAck(F("LED"));
    }
    return;
  }

  if (eq(verb, "LEDSTOP")) {
    if (a.n < 2) { emitErr(F("LEDSTOP_NEEDS_CHANNEL")); return; }
    uint8_t ch = ledChFromChar(a.c(1));
    if (ch >= LED_COUNT) { emitErr(F("LED_BAD_CHANNEL")); return; }
    ledStop(ch);
    emitAck(F("LEDSTOP"));
    return;
  }

  // ---------------- Speakers ----------------
  if (eq(verb, "SPK")) {
    if (a.n < 7) { emitErr(F("SPK_NEEDS_6_ARGS")); return; }
    uint8_t ch = spkChFromChar(a.c(1));
    if (ch >= SPK_COUNT) { emitErr(F("SPK_BAD_CHANNEL")); return; }

    long dur     = a.i(2, 0);
    long toneHz  = a.i(3, 0);
    long mode    = a.i(4, 0);
    long clickHz = a.i(5, 0);
    long vol     = a.i(6, SPK_MAX_DUTY_PCT);
    long onUs    = a.i(7, 0);          // optional

    if (dur < 0)     { emitErr(F("SPK_DURATION_NEGATIVE")); return; }
    if (toneHz < 0)  { emitErr(F("SPK_TONE_NEGATIVE")); return; }
    if (vol < 0 || vol > 100) { emitErr(F("SPK_VOLUME_RANGE")); return; }
    if (onUs < 0 || onUs > 65535L) { emitErr(F("SPK_ONTIME_RANGE")); return; }

    if (spkStart(ch, (uint32_t)dur, (uint32_t)toneHz, mode != 0,
                 (uint16_t)clickHz, (uint8_t)vol, (uint16_t)onUs)) {
      emitAck(F("SPK"));
    }
    return;
  }

  if (eq(verb, "SPKSTOP")) {
    if (a.n < 2) { emitErr(F("SPKSTOP_NEEDS_CHANNEL")); return; }
    uint8_t ch = spkChFromChar(a.c(1));
    if (ch >= SPK_COUNT) { emitErr(F("SPK_BAD_CHANNEL")); return; }
    spkStop(ch);
    emitAck(F("SPKSTOP"));
    return;
  }

  // ---------------- Servos ----------------
  if (verb[0] == 'S' && verb[1] == 'V') {

    if (eq(verb, "SVINIT")) {
      if (a.n < 2) { emitErr(F("SVINIT_NEEDS_CHANNEL")); return; }
      const char* t = a.s(1);
      if (t[0] == 'a' || t[0] == 'A') {         // "all"
        if (servoInitAll()) emitAck(F("SVINIT"));
        return;
      }
      uint8_t ch = servoChFromToken(t);
      if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return; }
      if (servoInit(ch)) emitAck(F("SVINIT"));
      return;
    }

    // Panic path: must work even when the channel token is junk.
    if (eq(verb, "SVDETACH") && (a.s(1)[0] == 'a' || a.s(1)[0] == 'A')) {
      servoDetachAll(); emitAck(F("SVDETACH")); return;
    }
    if (eq(verb, "SVOFF")) { servoDetachAll(); emitAck(F("SVOFF")); return; }

    if (eq(verb, "SVIDLE") && (a.s(1)[0] == 'a' || a.s(1)[0] == 'A')) {
      if (a.n < 3) { emitErr(F("SVIDLE_NEEDS_MS")); return; }
      long ms = a.i(2, 0);
      if (ms < 0) { emitErr(F("SVIDLE_NEGATIVE")); return; }
      for (uint8_t k = 0; k < SV_COUNT; k++) servoSetIdleDetach(k, (uint32_t)ms);
      emitAck(F("SVIDLE"));
      return;
    }

    uint8_t ch = servoChFromToken(a.s(1));
    if (ch >= SV_COUNT) { emitErr(F("SERVO_BAD_CHANNEL")); return; }

    if (eq(verb, "SVREAD"))   { servoReport(ch); return; }
    if (eq(verb, "SVSTOP"))   { servoHalt(ch); emitAck(F("SVSTOP")); return; }
    if (eq(verb, "SVATTACH")) { if (servoAttach(ch)) emitAck(F("SVATTACH")); return; }
    if (eq(verb, "SVDETACH")) { if (servoDetach(ch)) emitAck(F("SVDETACH")); return; }

    if (eq(verb, "SVWRITE")) {
      if (a.n < 3) { emitErr(F("SVWRITE_NEEDS_ANGLE")); return; }
      bool force = (a.i(3, 0) != 0);
      if (servoWrite(ch, (int)a.i(2, -1), force)) emitAck(F("SVWRITE"));
      return;
    }
    if (eq(verb, "SVFWD")) {
      if (a.n < 3) { emitErr(F("SVFWD_NEEDS_DEGREES")); return; }
      if (servoForward(ch, (int)a.i(2, 0), a.i(3, 0) != 0)) emitAck(F("SVFWD"));
      return;
    }
    if (eq(verb, "SVBACK")) {
      if (a.n < 3) { emitErr(F("SVBACK_NEEDS_DEGREES")); return; }
      if (servoBack(ch, (int)a.i(2, 0), a.i(3, 0) != 0)) emitAck(F("SVBACK"));
      return;
    }
    if (eq(verb, "SVSLEW")) {
      if (a.n < 3) { emitErr(F("SVSLEW_NEEDS_VALUE")); return; }
      if (servoSetSlew(ch, (uint16_t)a.i(2, 0))) emitAck(F("SVSLEW"));
      return;
    }
    if (eq(verb, "SVLIMIT")) {
      if (a.n < 4) { emitErr(F("SVLIMIT_NEEDS_MIN_AND_MAX")); return; }
      if (servoSetLimits(ch, (int)a.i(2, 0), (int)a.i(3, 180))) emitAck(F("SVLIMIT"));
      return;
    }
    if (eq(verb, "SVIDLE")) {
      if (a.n < 3) { emitErr(F("SVIDLE_NEEDS_MS")); return; }
      long ms = a.i(2, 0);
      if (ms < 0) { emitErr(F("SVIDLE_NEGATIVE")); return; }
      if (servoSetIdleDetach(ch, (uint32_t)ms)) emitAck(F("SVIDLE"));
      return;
    }
    if (eq(verb, "SVPRESENT")) {
      if (a.n < 3) { emitErr(F("SVPRESENT_NEEDS_0_OR_1")); return; }
      if (servoSetPresent(ch, a.i(2, 1) != 0)) { emitAck(F("SVPRESENT")); servoReport(ch); }
      return;
    }
    if (eq(verb, "SVEXT")) {
      if (a.n < 3) { emitErr(F("SVEXT_NEEDS_ANGLE")); return; }
      if (servoSetExtend(ch, (int)a.i(2, 0), a.i(3, 0) != 0)) {
        emitAck(F("SVEXT")); servoReport(ch); }
      return;
    }
    if (eq(verb, "SVZERO")) {
      if (a.n < 3) { emitErr(F("SVZERO_NEEDS_ANGLE")); return; }
      if (servoSetZero(ch, (int)a.i(2, 0), a.i(3, 0) != 0)) {
        emitAck(F("SVZERO")); servoReport(ch); }
      return;
    }
    if (eq(verb, "SVUS")) {
      if (a.n < 3) { emitErr(F("SVUS_NEEDS_MICROSECONDS")); return; }
      long us = a.i(2, 0);
      if (us < 0 || us > 65535L) { emitErr(F("SERVO_US_OUT_OF_RANGE")); return; }
      if (servoWriteUs(ch, (uint16_t)us)) emitAck(F("SVUS"));
      return;
    }
    if (eq(verb, "SVDIR")) {
      if (a.n < 3) { emitErr(F("SVDIR_NEEDS_VALUE")); return; }
      if (servoSetDir(ch, (int8_t)a.i(2, 0))) emitAck(F("SVDIR"));
      return;
    }
    emitErr(F("UNKNOWN_SERVO_COMMAND"));
    return;
  }

  // ---------------- Stepper axes ----------------
  if (verb[0] == 'S' && verb[1] == 'T' && verb[2] == 'P') {
    uint8_t ch = stepperChFromToken(a.s(1));
    if (eq(verb, "STPALL")) { stepperReportAll(); return; }
    if (ch >= STP_COUNT) { emitErr(F("STEP_BAD_AXIS")); return; }

    if (eq(verb, "STPREAD"))  { stepperReport(ch); return; }
    if (eq(verb, "STPZERO"))  { stepperZero(ch); emitAck(F("STPZERO")); return; }
    if (eq(verb, "STPSTOP"))  { stepperHalt(ch); emitAck(F("STPSTOP")); return; }
    if (eq(verb, "STPON"))    { stepperEnable(ch, true);  emitAck(F("STPON")); return; }
    if (eq(verb, "STPOFF"))   { stepperEnable(ch, false); emitAck(F("STPOFF")); return; }

    // Configuration is always allowed; motion is not, because a
    // plunger moving mid-trial changes the pressure at the spout the
    // animal is drinking from.
    if (eq(verb, "STPSPS"))   { if (stepperSetSpeed(ch, (uint16_t)a.i(2, 0))) emitAck(F("STPSPS")); return; }
    if (eq(verb, "STPACC"))   { if (stepperSetAccel(ch, (uint16_t)a.i(2, 0))) emitAck(F("STPACC")); return; }
    if (eq(verb, "STPDIR"))   { if (stepperSetDir(ch, (int8_t)a.i(2, 0))) emitAck(F("STPDIR")); return; }
    if (eq(verb, "STPLIM"))   { if (stepperSetLimits(ch, a.i(2, 0), a.i(3, 0))) emitAck(F("STPLIM")); return; }
    if (eq(verb, "STPCAL"))   { if (stepperSetCal(ch, (uint32_t)a.i(2, 0))) emitAck(F("STPCAL")); return; }
    if (eq(verb, "STPHOLD"))  { if (stepperSetHold(ch, a.i(2, 0))) emitAck(F("STPHOLD")); return; }
    if (eq(verb, "STPPRESENT")) {
      if (stepperSetPresent(ch, a.i(2, 0) != 0)) { emitAck(F("STPPRESENT")); stepperReport(ch); }
      return;
    }

    if (trialRunning()) { emitErr(F("STEP_TRIAL_IN_PROGRESS")); return; }
    if (blockRunning()) { emitErr(F("STEP_BLOCK_SWITCH_IN_PROGRESS")); return; }

    if (eq(verb, "STPASP")) { if (stepperAspirate(ch, (uint32_t)a.i(2, 0))) emitAck(F("STPASP")); return; }
    if (eq(verb, "STPDIS")) { if (stepperDispense(ch, (uint32_t)a.i(2, 0))) emitAck(F("STPDIS")); return; }
    if (eq(verb, "STPGOTO")) { if (stepperMoveTo(ch, a.i(2, 0))) emitAck(F("STPGOTO")); return; }
    if (eq(verb, "STPVOL")) {
      if (a.n < 4) { emitErr(F("STPVOL_NEEDS_NL_AND_DIRECTION")); return; }
      uint32_t nl = (uint32_t)a.i(2, 0);
      bool asp = a.i(3, 1) != 0;
      if (asp ? stepperAspirateNl(ch, nl) : stepperDispenseNl(ch, nl))
        emitAck(F("STPVOL"));
      return;
    }
    emitErr(F("UNKNOWN_STEPPER_COMMAND"));
    return;
  }

  // ---------------- Block switch ----------------
  if (verb[0] == 'B' && verb[1] == 'S') {
    if (eq(verb, "BSNEW")) {
      if (blockNew((uint32_t)a.i(1, 0))) emitAck(F("BSNEW"));
      return;
    }
    if (eq(verb, "BSSPOUT")) {
      if (a.n < 4) { emitErr(F("BSSPOUT_NEEDS_CH_SOL_FILLMS")); return; }
      uint8_t ch = servoChFromToken(a.s(1));
      if (ch >= SV_COUNT) { emitErr(F("BLOCK_BAD_SPOUT")); return; }
      long sol = a.i(2, 0);
      if (sol < 1 || sol > SOL_COUNT_MAX) { emitErr(F("SOL_BAD_INDEX_USE_1_TO_4")); return; }
      if (blockAddSpout(ch, (uint8_t)(sol - 1), (uint32_t)a.i(3, 0),
                        (uint8_t)a.i(4, 1), (uint32_t)a.i(5, 150)))
        emitAck(F("BSSPOUT"));
      return;
    }
    if (eq(verb, "BSVAC")) {
      if (blockSetVac((uint32_t)a.i(1, 0), (uint16_t)a.i(2, 0))) emitAck(F("BSVAC"));
      return;
    }
    if (eq(verb, "BSTIME")) {
      if (a.n < 5) { emitErr(F("BSTIME_NEEDS_FOUR_VALUES")); return; }
      if (blockSetTiming((uint32_t)a.i(1, 0), (uint32_t)a.i(2, 0),
                         (uint32_t)a.i(3, 0), (uint32_t)a.i(4, 0)))
        emitAck(F("BSTIME"));
      return;
    }
    if (eq(verb, "BSPUMP")) {
      if (blockSetUseStepper(a.i(1, 1) != 0)) emitAck(F("BSPUMP"));
      return;
    }
    if (eq(verb, "BSMODE"))   { if (blockSetMode((uint8_t)a.i(1, 1))) emitAck(F("BSMODE")); return; }
    if (eq(verb, "BSCYCLES")) { if (blockSetCycles((uint8_t)a.i(1, 2))) emitAck(F("BSCYCLES")); return; }
    if (eq(verb, "BSRETURN")) { if (blockSetReturn(a.i(1, 0) != 0)) emitAck(F("BSRETURN")); return; }
    if (eq(verb, "BSGO"))     { if (blockStart()) emitAck(F("BSGO")); return; }
    if (eq(verb, "BSABORT"))  { blockAbort(); emitAck(F("BSABORT")); return; }
    if (eq(verb, "BSSTATE"))  { blockReport(); return; }
    emitErr(F("UNKNOWN_BLOCKSWITCH_COMMAND"));
    return;
  }

  // ---------------- Trials ----------------
  if (verb[0] == 'T' && verb[1] == 'R') {
    if (eq(verb, "TRNEW")) {
      if (a.n < 3) { emitErr(F("TRNEW_NEEDS_ID_AND_MODE")); return; }
      if (trialNew((uint32_t)a.i(1, 0), (uint8_t)a.i(2, 0))) emitAck(F("TRNEW"));
      return;
    }
    if (eq(verb, "TRSPOUT")) {
      if (a.n < 6) { emitErr(F("TRSPOUT_NEEDS_CH_SOL_MS_FR_REW")); return; }
      uint8_t ch = servoChFromToken(a.s(1));
      if (ch >= SV_COUNT) { emitErr(F("TRIAL_BAD_SPOUT")); return; }
      long sol = a.i(2, 0);
      if (sol < 1 || sol > SOL_COUNT_MAX) { emitErr(F("SOL_BAD_INDEX_USE_1_TO_4")); return; }
      if (trialAddSpout(ch, (uint8_t)(sol - 1), (uint32_t)a.i(3, 0),
                        (uint16_t)a.i(4, 1), a.i(5, 1) != 0)) emitAck(F("TRSPOUT"));
      return;
    }
    if (eq(verb, "TRTIME")) {
      if (a.n < 5) { emitErr(F("TRTIME_NEEDS_FOUR_VALUES")); return; }
      if (trialSetTiming((uint32_t)a.i(1, 1000), (uint32_t)a.i(2, 5000),
                         (uint32_t)a.i(3, 1000), (uint32_t)a.i(4, 5000)))
        emitAck(F("TRTIME"));
      return;
    }
    if (eq(verb, "TRGATE")) {
      if (a.n < 2) { emitErr(F("TRGATE_NEEDS_MS")); return; }
      if (trialSetGate((uint16_t)a.i(1, 500))) emitAck(F("TRGATE"));
      return;
    }
    if (eq(verb, "TRGO"))    { if (trialStart()) emitAck(F("TRGO")); return; }
    if (eq(verb, "TRABORT")) { trialAbort(); emitAck(F("TRABORT")); return; }
    if (eq(verb, "TRSTATE")) { trialReport(); return; }
    emitErr(F("UNKNOWN_TRIAL_COMMAND"));
    return;
  }

  // ---------------- Lick sensors ----------------
  if (verb[0] == 'L' && verb[1] == 'K') {

    if (eq(verb, "LKALL")) { lickReportAll(); return; }
    if (eq(verb, "LKZERO")) { lickResetAllCounts(); emitAck(F("LKZERO")); return; }
    if (eq(verb, "LKSTOP")) { lickStopStream(); emitAck(F("LKSTOP")); return; }
    if (eq(verb, "LKTIME")) {
      if (a.n < 2) { lickReportTiming(); return; }
      if (a.n < 4) { emitErr(F("LKTIME_NEEDS_MINON_MINOFF_REFRACT")); return; }
      if (lickSetTiming((uint16_t)a.i(1, LICK_MIN_ON_MS),
                        (uint16_t)a.i(2, LICK_MIN_OFF_MS),
                        (uint16_t)a.i(3, LICK_REFRACTORY_MS))) {
        emitAck(F("LKTIME"));
        lickReportTiming();
      }
      return;
    }

    uint8_t ch = lickChFromChar(a.c(1));
    if (ch >= LICK_COUNT) { emitErr(F("LICK_BAD_CHANNEL")); return; }

    if (eq(verb, "LKREAD")) { lickReport(ch); return; }
    if (eq(verb, "LKRAW")) {
      if (lickStreamRaw(ch, (uint16_t)a.i(2, 10000))) emitAck(F("LKRAW"));
      return;
    }
    if (eq(verb, "LKCAL")) {
      if (lickCalibrateBaseline(ch, (uint16_t)a.i(2, LICK_CAL_DEFAULT_MS))) emitAck(F("LKCAL"));
      return;
    }
    if (eq(verb, "LKTOUCH")) {
      if (lickCalibrateTouch(ch, (uint16_t)a.i(2, LICK_CAL_DEFAULT_MS))) emitAck(F("LKTOUCH"));
      return;
    }
    if (eq(verb, "LKON"))  { lickSetEnabled(ch, true);  emitAck(F("LKON"));  return; }
    if (eq(verb, "LKOFF")) { lickSetEnabled(ch, false); emitAck(F("LKOFF")); return; }
    if (eq(verb, "LKRESET")) { lickResetCount(ch); emitAck(F("LKRESET")); return; }
    if (eq(verb, "LKPRESENT")) {
      if (a.n < 3) { emitErr(F("LKPRESENT_NEEDS_0_OR_1")); return; }
      if (lickSetPresent(ch, a.i(2, 1) != 0)) { emitAck(F("LKPRESENT")); lickReport(ch); }
      return;
    }
    if (eq(verb, "LKSET")) {
      if (a.n < 4) { emitErr(F("LKSET_NEEDS_ONDELTA_OFFDELTA")); return; }
      if (lickSetThresholds(ch, (float)a.i(2, 0), (float)a.i(3, 0),
                            (int8_t)a.i(4, 0))) {
        emitAck(F("LKSET"));
        lickReport(ch);
      }
      return;
    }
    emitErr(F("UNKNOWN_LICK_COMMAND"));
    return;
  }

  // ---------------- Solenoids ----------------
  if (verb[0] == 'S' && verb[1] == 'O' && verb[2] == 'L') {

    if (eq(verb, "SOLGET")) {
      long n = a.i(1, 0);
      if (n <= 0) { solReportAll(); }
      else        { solReportIdentity((uint8_t)(n - 1)); }
      return;
    }

    long n = a.i(1, 0);
    if (n < 1 || n > SOL_COUNT_MAX) { emitErr(F("SOL_BAD_INDEX")); return; }
    uint8_t idx = (uint8_t)(n - 1);

    if (eq(verb, "SOLID")) {
      if (a.n < 4) { emitErr(F("SOLID_NEEDS_LIQUID_AND_SPOUT")); return; }
      char sc = a.c(3);
      uint8_t spout = (sc == 'l') ? SPOUT_L :
                      (sc == 'c') ? SPOUT_C :
                      (sc == 'r') ? SPOUT_R : SPOUT_NONE;
      if (spout == SPOUT_NONE) { emitErr(F("SOL_BAD_SPOUT_USE_L_C_R")); return; }
      if (solSetIdentity(idx, a.s(2), spout)) {
        emitAck(F("SOLID"));
        solReportIdentity(idx);
      }
      return;
    }
    if (eq(verb, "SOLPRESENT")) {
      if (a.n < 3) { emitErr(F("SOLPRESENT_NEEDS_0_OR_1")); return; }
      if (solSetPresent(idx, a.i(2, 1) != 0)) { emitAck(F("SOLPRESENT")); solReportIdentity(idx); }
      return;
    }
    if (eq(verb, "SOLCAL")) {
      if (a.n < 3) { emitErr(F("SOLCAL_NEEDS_NL_PER_MS")); return; }
      long v = a.i(2, 0);
      if (v < 0) { emitErr(F("SOLCAL_NEGATIVE")); return; }
      if (solSetCalibration(idx, (uint32_t)v)) emitAck(F("SOLCAL"));
      return;
    }
    if (eq(verb, "SOLOPEN"))  { if (solOpen(idx))  emitAck(F("SOLOPEN"));  return; }
    if (eq(verb, "SOLCLOSE")) { if (solClose(idx)) emitAck(F("SOLCLOSE")); return; }
    if (eq(verb, "SOLDISP")) {
      if (a.n < 3) { emitErr(F("SOLDISP_NEEDS_MS")); return; }
      long ms = a.i(2, 0);
      if (ms < 0) { emitErr(F("SOL_DURATION_NEGATIVE")); return; }
      if (solDispenseMs(idx, (uint32_t)ms)) emitAck(F("SOLDISP"));
      return;
    }
    if (eq(verb, "SOLVOL")) {
      if (a.n < 3) { emitErr(F("SOLVOL_NEEDS_NL")); return; }
      long nl = a.i(2, 0);
      if (nl <= 0) { emitErr(F("SOLVOL_MUST_BE_POSITIVE")); return; }
      if (solDispenseNl(idx, (uint32_t)nl)) emitAck(F("SOLVOL"));
      return;
    }
    emitErr(F("UNKNOWN_SOLENOID_COMMAND"));
    return;
  }

  // ---------------- Global ----------------
  if (eq(verb, "STOPALL")) {
    // Safe state, in order of what can do damage: abort the switch
    // sequencer first so its queued retracts cannot outlive the halt,
    // then close gates, then de-energise the pumps, then cues, then
    // freeze the servos.
    blockAbort();
    solCloseAll();
    stepperHaltAll();
    for (uint8_t k = 0; k < STP_COUNT; k++) stepperEnable(k, false);
    ledStopAll();
    spkStopAll();
    for (uint8_t k = 0; k < SV_COUNT; k++) servoHalt(k);
    emitAck(F("STOPALL"));
    return;
  }

  emitErr(F("UNKNOWN_COMMAND"));
}
