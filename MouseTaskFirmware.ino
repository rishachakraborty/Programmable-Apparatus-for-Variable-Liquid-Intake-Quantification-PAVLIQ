// =============================================================
//  MouseTaskFirmware
//  Two-choice head-fixed preference task - Arduino Mega 2560
//
//  STEP 4 of 9: firmware feature-complete. Protocol, LED and
//  speaker cues with synchronised onset, servos with stepped motion
//  and captured positions, solenoids with identity + calibration,
//  adaptive lick detection, trial state machine, debug menu.
//  Python GUI is next; the stepper/vacuum lands last.
//
//  DESIGN RULES for everything added from here on:
//    1. loop() must never block. No delay(), no while-waiting.
//       Lick detection will run at ~1 kHz and cannot tolerate it.
//    2. Timestamps are captured with millis() at the moment the
//       event happens, never at the moment it is printed.
//    3. Every hardware action is reachable through exactly one
//       function, called by exactly one dispatcher, used by both
//       the debug menu and the Python host.
//
//  TIMER ALLOCATION (Mega 2560)
//    Timer0 : millis()/micros()                  - do not touch
//    Timer1 : free (reserved for future use)
//    Timer2 : LED software PWM, 31.25 kHz ISR
//    Timer3 : RIGHT speaker, pin 5  (OC3A)
//    Timer4 : LEFT speaker,  pin 6  (OC4A)
//    Timer5 : Servo library (claimed by servos.cpp)
//
//  Steppers use plain digital pins (26-34), NOT 6/7/8: pin 6 is the
//  left speaker and sharing it would silence one tone of every choice
//  cue while making the step pulses unreliable.
// =============================================================

#include "config.h"
#include "proto.h"
#include "led.h"
#include "speaker.h"
#include "servos.h"
#include "solenoids.h"
#include "lick.h"
#include "trial.h"
#include "stepper.h"
#include "blockswitch.h"
#include "commands.h"
#include "debug.h"

static char    g_line[MAX_LINE];
static uint8_t g_lineLen = 0;
static bool    g_overflow = false;

void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial) { ; }   // safe: runs before the task, not during

  ledBegin();
  spkBegin();
  servoBegin();   // configures state only; nothing moves until SVINIT
  solBegin();     // all gates driven LOW first, then EEPROM identities
  lickBegin();    // detection stays OFF until each channel is calibrated
  stepperBegin(); // EN parked HIGH; nothing moves until commanded
  blockBegin();
  trialBegin();
  debugBegin();

  Serial.print(F("#,"));
  Serial.print(F(FW_NAME));
  Serial.print(' ');
  Serial.print(F(FW_VERSION));
  Serial.println(F(" ready"));
  emitInfo(F("Send HELP for the command list, or DEBUG for the menu."));
  emitReplyNum(F("BOOT"), (long)millis());
}

static void handleLine(char* line) {
  if (debugIsActive()) {
    debugFeedLine(line);
  } else {
    dispatchCommand(line);
  }
}

static void pollSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\r') continue;

    if (c == '\n') {
      if (g_overflow) {
        emitErr(F("LINE_TOO_LONG"));
        g_overflow = false;
      } else {
        g_line[g_lineLen] = '\0';
        handleLine(g_line);
      }
      g_lineLen = 0;
      return;   // one line per pass keeps loop latency bounded
    }

    if (g_lineLen < MAX_LINE - 1) {
      g_line[g_lineLen++] = c;
    } else {
      g_overflow = true;
    }
  }
}

void loop() {
  pollSerial();
  // Early: its timing resolution is the loop period, and the burst
  // limit only helps if it is called often.
  stepperUpdate();
  ledUpdate();
  spkUpdate();
  servoUpdate();
  solUpdate();
  lickUpdate();
  trialUpdate();
  blockUpdate();
}
