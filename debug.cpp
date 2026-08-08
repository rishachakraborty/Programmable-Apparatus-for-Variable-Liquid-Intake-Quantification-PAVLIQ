#include "debug.h"
#include "proto.h"
#include "commands.h"
#include "led.h"
#include "speaker.h"
#include "servos.h"
#include "solenoids.h"
#include "lick.h"

#define DBG_MAX_Q    6
#define DBG_ANS_LEN  16

enum DbgMode : uint8_t { DBG_OFF = 0, DBG_MENU = 1, DBG_ASK = 2 };
enum DbgFlow : uint8_t { FLOW_NONE = 0, FLOW_LED, FLOW_SPK, FLOW_SERVO, FLOW_SOL, FLOW_LICK };

static DbgMode g_mode = DBG_OFF;
static DbgFlow g_flow = FLOW_NONE;

// When armed, cue flows stage the command instead of firing it, so
// several cues can be released together with "go". This is how you
// test a choice trial: two tones, one onset.
static bool g_armMode = false;

static char        g_chan;                 // 'w'/'b'/'g'/'l'/'r'/'1'..'4'
static uint8_t     g_nQ;
static uint8_t     g_qi;
static const char* g_defaults[DBG_MAX_Q];
static char        g_ans[DBG_MAX_Q][DBG_ANS_LEN];

// ---- question sets --------------------------------------------
//
// Prompt text lives in flash and is printed from a switch rather
// than held in an array of const char*. An array of string literals
// puts BOTH the pointers and the characters in SRAM, and these
// prompts alone were costing the better part of a kilobyte on a
// part that only has eight.
//
// The default answers stay in SRAM: they are a few characters each,
// and finishFlow needs them as plain char* to build command strings.

static void printPromptText(DbgFlow f, uint8_t q) {
  switch (f) {
    case FLOW_LED:
      switch (q) {
        case 0: Serial.print(F("Duration of flash in ms (0 = stay on until stopped)")); break;
        case 1: Serial.print(F("Mode: 0 = constant, 1 = pulsing")); break;
        case 2: Serial.print(F("Pulse frequency in Hz, 1-100 (ignored if constant)")); break;
        case 3: Serial.print(F("Brightness, 0-255")); break;
      }
      break;
    case FLOW_SPK:
      switch (q) {
        case 0: Serial.print(F("Duration of tone in ms (0 = play until stopped)")); break;
        case 1: Serial.print(F("Tone (carrier) frequency in Hz, 20-40000")); break;
        case 2: Serial.print(F("Mode: 0 = constant tone, 1 = click train")); break;
        case 3: Serial.print(F("Click train frequency in Hz, 1-1000 (ignored if constant)")); break;
        case 4: Serial.print(F("Volume as duty cycle percent, 0-50")); break;
      }
      break;
    case FLOW_SERVO:
      switch (q) {
        case 0: Serial.print(F("Action: i=init  r=read  w=write  f=fwd  b=back  s=stop  d=detach  e=attach")); break;
        case 1: Serial.print(F("Value in degrees (write = absolute 0-180; fwd/back = step >= 10)")); break;
      }
      break;
    case FLOW_SOL:
      switch (q) {
        case 0: Serial.print(F("Action: o=open  c=close  d=dispense ms  v=dispense nL  g=get identity")); break;
        case 1: Serial.print(F("Value (d = milliseconds, v = nanolitres)")); break;
      }
      break;
    case FLOW_LICK:
      switch (q) {
        case 0: Serial.print(F("Action: w=watch raw  c=calibrate baseline  t=calibrate touch  r=read  e=enable  d=disable  z=zero count")); break;
        case 1: Serial.print(F("Duration in ms")); break;
      }
      break;
    default: break;
  }
}

static const char* const LED_DEFAULTS[4]   = {"500", "0", "10", "255"};
static const char* const SPK_DEFAULTS[5]   = {"500", "10000", "0", "50", "50"};
static const char* const SERVO_DEFAULTS[2] = {"r", "10"};
static const char* const SOL_DEFAULTS[2]   = {"g", "50"};
static const char* const LICK_DEFAULTS[2] = {"w", "10000"};

// ---------------------------------------------------------------

static void printMenu() {
  emitInfo(F("=============== DEBUG MODE ==============="));
  emitInfo(F(" CUES    w / b / g   white, blue, green LED"));
  emitInfo(F("         l / r       left, right speaker"));
  emitInfo(F(" SERVOS  ls / cs / rs  left, center, right"));
  emitInfo(F(" GATES   s1 / s2 / s3 / s4   solenoids 1-4"));
  emitInfo(F(" LICKS   kl / kc / kr  left, center, right sensors"));
  emitInfo(F("         Start with action w to watch raw values."));
  emitInfo(F(" PANIC   sd  ALL SERVOS LIMP - use if one is buzzing"));
  emitInfo(F(" OTHER   x  stop everything, close all gates"));
  emitInfo(F("         st status    ?  this menu    q  quit"));
  emitInfo(F(" SYNC    a  toggle arm mode   go  fire staged cues"));
  emitInfo(F("         In arm mode a cue is staged, not played. Build"));
  emitInfo(F("         both tones of a choice trial, then press go."));
  emitInfo(F(" Blank answer accepts the [default]. q cancels a flow."));
  emitInfo(F(" Servo writes from this menu bypass the 10 deg minimum,"));
  emitInfo(F(" so you can dial in spout positions precisely."));
  emitInfo(F("=========================================="));
}

static void askCurrent() {
  Serial.print(F("#,Q"));
  Serial.print(g_qi + 1);
  Serial.print('/');
  Serial.print(g_nQ);
  Serial.print(F(": "));
  printPromptText(g_flow, g_qi);
  Serial.print(F("  ["));
  Serial.print(g_defaults[g_qi]);
  Serial.println(']');
}

static void beginFlow(DbgFlow flow, char chan,
                      const char* const* defaults, uint8_t nQ) {
  g_flow = flow;
  g_chan = chan;
  g_nQ   = nQ;
  g_qi   = 0;
  for (uint8_t k = 0; k < nQ; k++) {
    g_defaults[k] = defaults[k];
    g_ans[k][0]   = '\0';
  }
  g_mode = DBG_ASK;
  askCurrent();
}

// Actions that need no numeric argument, so we skip question 2.
static bool actionNeedsValue(DbgFlow flow, char act) {
  if (flow == FLOW_SERVO) return (act == 'w' || act == 'f' || act == 'b');
  if (flow == FLOW_SOL)   return (act == 'd' || act == 'v');
  if (flow == FLOW_LICK)  return (act == 'w' || act == 'c' || act == 't');
  return true;
}

// Assemble the machine command and run it through the same
// dispatcher the Python host uses. The echoed string is exactly
// what Python would send.
static void finishFlow() {
  char cmd[MAX_LINE];
  cmd[0] = '\0';

  switch (g_flow) {
    case FLOW_LED:
      snprintf(cmd, sizeof(cmd), "LED,%c,%s,%s,%s,%s",
               g_chan, g_ans[0], g_ans[1], g_ans[2], g_ans[3]);
      break;

    case FLOW_SPK:
      snprintf(cmd, sizeof(cmd), "SPK,%c,%s,%s,%s,%s,%s",
               g_chan, g_ans[0], g_ans[1], g_ans[2], g_ans[3], g_ans[4]);
      break;

    case FLOW_SERVO: {
      char act = g_ans[0][0];
      if (act >= 'A' && act <= 'Z') act += 32;
      switch (act) {
        case 'i': snprintf(cmd, sizeof(cmd), "SVINIT,%c", g_chan); break;
        case 'r': snprintf(cmd, sizeof(cmd), "SVREAD,%c", g_chan); break;
        case 's': snprintf(cmd, sizeof(cmd), "SVSTOP,%c", g_chan); break;
        case 'd': snprintf(cmd, sizeof(cmd), "SVDETACH,%c", g_chan); break;
        case 'e': snprintf(cmd, sizeof(cmd), "SVATTACH,%c", g_chan); break;
        // force=1: the debug menu IS the manual positioning tool,
        // so fine adjustment must be possible here.
        case 'w': snprintf(cmd, sizeof(cmd), "SVWRITE,%c,%s,1", g_chan, g_ans[1]); break;
        case 'f': snprintf(cmd, sizeof(cmd), "SVFWD,%c,%s",  g_chan, g_ans[1]); break;
        case 'b': snprintf(cmd, sizeof(cmd), "SVBACK,%c,%s", g_chan, g_ans[1]); break;
        default:
          emitInfo(F("Unknown servo action. Use i r w f b s d e."));
          g_mode = DBG_MENU;
          return;
      }
      break;
    }

    case FLOW_SOL: {
      char act = g_ans[0][0];
      if (act >= 'A' && act <= 'Z') act += 32;
      switch (act) {
        case 'o': snprintf(cmd, sizeof(cmd), "SOLOPEN,%c",  g_chan); break;
        case 'c': snprintf(cmd, sizeof(cmd), "SOLCLOSE,%c", g_chan); break;
        case 'g': snprintf(cmd, sizeof(cmd), "SOLGET,%c",   g_chan); break;
        case 'd': snprintf(cmd, sizeof(cmd), "SOLDISP,%c,%s", g_chan, g_ans[1]); break;
        case 'v': snprintf(cmd, sizeof(cmd), "SOLVOL,%c,%s",  g_chan, g_ans[1]); break;
        default:
          emitInfo(F("Unknown solenoid action. Use o, c, d, v or g."));
          g_mode = DBG_MENU;
          return;
      }
      break;
    }

    case FLOW_LICK: {
      char act = g_ans[0][0];
      if (act >= 'A' && act <= 'Z') act += 32;
      switch (act) {
        case 'w': snprintf(cmd, sizeof(cmd), "LKRAW,%c,%s",   g_chan, g_ans[1]); break;
        case 'c': snprintf(cmd, sizeof(cmd), "LKCAL,%c,%s",   g_chan, g_ans[1]); break;
        case 't': snprintf(cmd, sizeof(cmd), "LKTOUCH,%c,%s", g_chan, g_ans[1]); break;
        case 'r': snprintf(cmd, sizeof(cmd), "LKREAD,%c",     g_chan); break;
        case 'e': snprintf(cmd, sizeof(cmd), "LKON,%c",       g_chan); break;
        case 'd': snprintf(cmd, sizeof(cmd), "LKOFF,%c",      g_chan); break;
        case 'z': snprintf(cmd, sizeof(cmd), "LKRESET,%c",    g_chan); break;
        default:
          emitInfo(F("Unknown lick action. Use w c t r e d z."));
          g_mode = DBG_MENU;
          return;
      }
      break;
    }

    default:
      g_mode = DBG_MENU;
      return;
  }

  // In arm mode, cue commands are staged rather than fired so that
  // several can be released with a single synchronised onset.
  bool stageable = (g_flow == FLOW_LED || g_flow == FLOW_SPK);
  if (g_armMode && stageable) {
    char staged[MAX_LINE];
    snprintf(staged, sizeof(staged), "ARM,%s", cmd);
    Serial.print(F("#,CMD: "));
    Serial.println(staged);
    dispatchCommand(staged);
    g_flow = FLOW_NONE;
    g_mode = DBG_MENU;
    emitInfo(F("Staged. Add more, then press go."));
    return;
  }

  Serial.print(F("#,CMD: "));
  Serial.println(cmd);

  dispatchCommand(cmd);          // modified in place by the parser

  g_flow = FLOW_NONE;
  g_mode = DBG_MENU;
  emitInfo(F("Ready. ? for menu."));
}

// ---------------------------------------------------------------

void debugBegin() { g_mode = DBG_OFF; g_flow = FLOW_NONE; g_armMode = false; }
bool debugIsActive() { return g_mode != DBG_OFF; }

void debugEnter() { g_mode = DBG_MENU; printMenu(); }

void debugExit() {
  g_mode = DBG_OFF;
  g_flow = FLOW_NONE;
  emitInfo(F("Debug mode off. Machine command mode active."));
}

static void runLiteral(const char* text) {
  char buf[MAX_LINE];
  strncpy(buf, text, sizeof(buf) - 1);
  buf[sizeof(buf) - 1] = '\0';
  dispatchCommand(buf);
}

static void handleMenuKey(char* line) {
  // lowercase the token in place
  for (char* p = line; *p; p++) if (*p >= 'A' && *p <= 'Z') *p += 32;

  if (line[0] == '\0') return;

  // single-character keys
  if (line[1] == '\0') {
    switch (line[0]) {
      case 'w': case 'b': case 'g':
        beginFlow(FLOW_LED, line[0], LED_DEFAULTS, 4); return;
      case 'l': case 'r':
        beginFlow(FLOW_SPK, line[0], SPK_DEFAULTS, 5); return;
      case 'x': runLiteral("STOPALL"); return;
      case 'a':
        g_armMode = !g_armMode;
        if (g_armMode) {
          emitInfo(F("Arm mode ON. Cues are staged, not played. Press go to fire."));
        } else {
          emitInfo(F("Arm mode OFF. Cues play immediately."));
        }
        if (!g_armMode) runLiteral("DISARM");
        return;
      case '?': printMenu(); printHelp(); return;
      case 'q': debugExit(); return;
      default:
        emitInfo(F("Unrecognised key. Press ? for the menu."));
        return;
    }
  }

  // two-character keys
  if (line[2] == '\0') {
    if (line[1] == 's' &&
        (line[0] == 'l' || line[0] == 'c' || line[0] == 'r')) {
      beginFlow(FLOW_SERVO, line[0], SERVO_DEFAULTS, 2);
      return;
    }
    if (line[0] == 's' && line[1] >= '1' && line[1] <= '4') {
      beginFlow(FLOW_SOL, line[1], SOL_DEFAULTS, 2);
      return;
    }
    if (line[0] == 'k' &&
        (line[1] == 'l' || line[1] == 'c' || line[1] == 'r')) {
      beginFlow(FLOW_LICK, line[1], LICK_DEFAULTS, 2);
      return;
    }
    if (line[0] == 'g' && line[1] == 'o') { runLiteral("GO"); return; }
    // Panic: every servo limp, immediately. Use this if one is
    // buzzing against a stop.
    if (line[0] == 's' && line[1] == 'd') { runLiteral("SVOFF"); return; }
    if (line[0] == 's' && line[1] == 't') { runLiteral("STATUS"); solReportAll();
      for (uint8_t k = 0; k < SV_COUNT; k++) servoReport(k); return; }
  }

  emitInfo(F("Unrecognised key. Press ? for the menu."));
}

void debugFeedLine(char* line) {
  if (g_mode == DBG_MENU) { handleMenuKey(line); return; }

  if (g_mode == DBG_ASK) {
    if (line[0] == 'q' && line[1] == '\0') {
      g_mode = DBG_MENU;
      g_flow = FLOW_NONE;
      emitInfo(F("Cancelled. ? for menu."));
      return;
    }

    const char* src = (line[0] == '\0') ? g_defaults[g_qi] : line;
    strncpy(g_ans[g_qi], src, DBG_ANS_LEN - 1);
    g_ans[g_qi][DBG_ANS_LEN - 1] = '\0';

    // Branching flows skip the value question for actions that
    // take no argument.
    if (g_qi == 0 && (g_flow == FLOW_SERVO || g_flow == FLOW_SOL)) {
      char act = g_ans[0][0];
      if (act >= 'A' && act <= 'Z') act += 32;
      if (!actionNeedsValue(g_flow, act)) { finishFlow(); return; }
    }

    g_qi++;
    if (g_qi >= g_nQ) finishFlow();
    else              askCurrent();
  }
}
