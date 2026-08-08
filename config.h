#ifndef CONFIG_H
#define CONFIG_H

#include <Arduino.h>

// =============================================================
//  MouseTaskFirmware - configuration
//  Board: Arduino Mega 2560
//
//  STEP 1 (this version): protocol, LEDs, speakers, debug menu
//  Later steps add: servos, solenoids, lick detection, trial FSM
// =============================================================

#define FW_NAME    "MouseTaskFirmware"
#define FW_VERSION "0.6.0"

static const uint32_t SERIAL_BAUD = 115200;

// ----------------------- SOLENOIDS (Step 2) ------------------
// Digital on/off only. Pins 9/10 are Timer2 PWM pins and 11/12 are
// Timer1 PWM pins, but we never analogWrite them, so both timers
// stay free for other use (Timer2 = LED software PWM).
static const uint8_t PIN_SOL1 = 12;
static const uint8_t PIN_SOL2 = 11;
static const uint8_t PIN_SOL3 = 10;
static const uint8_t PIN_SOL4 = 7;

// ----------------------- SPEAKERS ----------------------------
// Pin 6 = OC4A -> Timer4  (LEFT speaker)
// Pin 5 = OC3A -> Timer3  (RIGHT speaker)
// Each speaker owns a dedicated 16-bit timer so BOTH can sound
// simultaneously (required for choice trials). This is why we do
// NOT use tone(), which supports only one pin at a time.
static const uint8_t PIN_SPK_L = 6;   // PH3
static const uint8_t PIN_SPK_R = 5;   // PE3

// Square wave from an I/O pin has fixed amplitude, so "volume" is
// implemented as PWM duty cycle. 50% duty = maximum RMS output;
// values below that reduce output but also change harmonic content.
static const uint8_t SPK_MAX_DUTY_PCT = 50;

// ----------------------- LEDs --------------------------------
// Pin 2 = PE4 (Timer3 OC3B), Pin 3 = PE5 (Timer3 OC3C),
// Pin 4 = PG5 (Timer0 OC0B).
// Timer3 is claimed by the right speaker and Timer0 by millis(),
// so hardware analogWrite() is unavailable on all three LED pins.
// Brightness is therefore produced by software PWM driven from a
// Timer2 compare interrupt (see led.cpp).
static const uint8_t PIN_LED_W = 2;
static const uint8_t PIN_LED_B = 3;
static const uint8_t PIN_LED_G = 4;

// Direct port access used inside the PWM ISR (digitalWrite is far
// too slow to call at 31.25 kHz).
#define LED_W_PORT PORTE
#define LED_W_MASK _BV(4)
#define LED_B_PORT PORTE
#define LED_B_MASK _BV(5)
#define LED_G_PORT PORTG
#define LED_G_MASK _BV(5)

// ----------------------- TOUCH SENSORS (Step 3) --------------
static const uint8_t PIN_TOUCH_L = A13;
static const uint8_t PIN_TOUCH_C = A14;
static const uint8_t PIN_TOUCH_R = A15;

// ----------------------- LICK DETECTION ----------------------
// NOTHING here is a threshold. Every threshold is derived at runtime
// from measurements on this particular rig, because electrode
// resistance varies with wiring, tubing, humidity, saliva and the
// individual board. The constants below are only sampling rates,
// timing windows and safety floors.

static const uint8_t  LICK_COUNT = 3;

// One channel per tick, round-robin, so each analogRead (~52 us with
// the faster ADC prescaler) is the longest the main loop ever blocks.
// 333 us x 3 channels = 1 kHz per channel, well above the 7-10 Hz
// lick rhythm and fine enough for millisecond onset timestamps.
static const uint16_t LICK_SAMPLE_INTERVAL_US = 333;

static const uint16_t LICK_CAL_DEFAULT_MS  = 2000;
static const uint16_t LICK_CAL_MAX_SAMPLES = 2000;  // keeps sumsq in uint32

// Threshold = baseline +/- k * standard deviation, when calibrating
// from baseline noise alone. Hysteresis: it takes a bigger excursion
// to declare a lick than to end one, so a signal hovering near
// threshold cannot chatter.
static const float    LICK_K_ON  = 6.0f;
static const float    LICK_K_OFF = 3.0f;

// Floor on the threshold in ADC counts. If the baseline is extremely
// quiet, k*sd can collapse to almost nothing and every bit of noise
// becomes a lick.
static const uint16_t LICK_MIN_DELTA_COUNTS = 4;

// A crossing must persist this long to count, which rejects
// electrical spikes. The reported timestamp is the ORIGINAL crossing,
// not the moment of confirmation, so this costs no timing accuracy.
static const uint16_t LICK_MIN_ON_MS     = 5;

// A lick ENDS only after the signal has stayed released this long.
// Contact resistance fluctuates during a single sustained contact,
// and a short window lets one contact fragment into several licks.
// Bridging dropouts is safe: mice lick at 7-10 Hz, so genuine
// inter-lick gaps are 50-100 ms - far longer than this.
static const uint16_t LICK_MIN_OFF_MS    = 25;
static const uint16_t LICK_REFRACTORY_MS = 15;

// Baseline drifts over a session. Track it slowly, and only while
// idle, so a long tongue contact can never drag the baseline with it.
static const float    LICK_BASELINE_TAU_S = 10.0f;

static const uint16_t LICK_RAW_STREAM_HZ = 50;

// ----------------------- SERVOS (Step 2) ---------------------
// NOTE: the Servo library claims Timer5, which disables PWM on
// pins 44/45/46. None of our pins are affected.
static const uint8_t PIN_SERVO_L = 23;
static const uint8_t PIN_SERVO_C = 22;
static const uint8_t PIN_SERVO_R = 25;

// Fully-retracted ("zero") angle for each linear actuator.
static const int SERVO_ZERO_L = 120;
// WARNING: 0 degrees maps to a 544 us pulse, which is at or beyond
// the internal mechanical stop of many hobby servos - the servo jams
// against its own limit and buzzes at full stall current. If the
// center servo hums at its zero, raise this (10-15 is usually safe)
// or find the true limit with SVUS and set SVLIMIT accordingly.
static const int SERVO_ZERO_C = 0;
static const int SERVO_ZERO_R = 60;

static const int SERVO_ANGLE_MIN  = 0;
static const int SERVO_ANGLE_MAX  = 180;
static const int SERVO_MIN_STEP   = 10;   // reject moves smaller than this

// Direction that counts as "forward" = EXTEND the spout toward the
// mouse. Derived from the retracted angle plus the mechanics.
//   Center retracted at 0, so it can only extend upward: +1.
//   Left and right are GUESSES - verify on the bench and change
//   here, or at runtime with SVDIR,<ch>,<+1|-1>.
static const int8_t SERVO_EXTEND_DIR_L = -1;   // 120 -> lower angle
static const int8_t SERVO_EXTEND_DIR_C = +1;   // 0   -> higher angle
static const int8_t SERVO_EXTEND_DIR_R = +1;   // 60  -> higher angle

// Stepped (interpolated) motion. Hobby servos slew at full speed on
// a bare write(), which is abrupt and mechanically noisy right next
// to a resistive lickometer. We interpolate instead.
static const uint16_t SERVO_UPDATE_MS      = 20;   // servo frame rate
static const uint16_t SERVO_SLEW_DEFAULT   = 400;  // degrees / second
static const uint16_t SERVO_SLEW_MIN       = 20;
static const uint16_t SERVO_SLEW_MAX       = 2000; // >= this is ~instant
static const uint16_t SERVO_US_MIN         = 544;  // matches Servo lib
static const uint16_t SERVO_US_MAX         = 2400;

// Milliseconds of stillness after which a servo is detached.
// An attached servo is continuously fed pulses and continuously
// corrects, which is audible hum and visible jitter - mechanical
// noise a few centimetres from a resistive lickometer. Detaching
// stops the pulses and the servo goes silent.
//
// TRADE-OFF: a detached servo has no holding torque. If a spout can
// be back-driven by gravity or by the mouse pushing on it, it will
// drift, and the firmware will not know. If you see drift, set
// SVIDLE,<ch>,0 for that channel to hold position instead.
// 0 = never auto-detach.
static const uint32_t SERVO_IDLE_DETACH_MS = 500;

// ----------------------- SYNCHRONISED CUES -------------------
// A choice trial must start the alcohol tone and the water tone at
// the same instant. Two separate commands cannot do that: they are
// milliseconds apart at best, and worse over USB. Cues are instead
// staged with ARM and fired together with GO, so all of them start
// inside a single pass of loop() - tens of microseconds apart, and
// sharing one millisecond timestamp.
static const uint8_t ARM_MAX_SLOTS = 6;
// Slots hold cue commands only, which are far shorter than MAX_LINE.
static const uint8_t ARM_SLOT_LEN  = 56;

// ----------------------- SOLENOID LIMITS ---------------------
static const uint8_t  SOL_COUNT_MAX        = 4;
static const uint32_t SOL_DISPENSE_MAX_MS  = 5000UL;   // per timed open
static const uint32_t SOL_MANUAL_MAX_MS    = 60000UL;  // flush watchdog
static const uint8_t  SOL_LIQUID_NAME_LEN  = 16;

// Solenoid switching injects a transient into the analog lickometer
// even with flyback diodes and a single-point ground. Lick detection
// (Step 3) ignores samples inside this window around every edge.
static const uint16_t SOL_BLANKING_MS      = 3;

// EEPROM persistence of solenoid identity + calibration, so the
// debug menu is usable standalone. Python overwrites these on connect.
static const int      EEPROM_BASE_ADDR     = 0;
static const uint8_t  EEPROM_MAGIC         = 0xA7;

// ----------------------- SAFETY LIMITS -----------------------
static const uint32_t CUE_MAX_DURATION_MS = 60000UL;
static const uint32_t SPK_FREQ_MIN_HZ     = 20UL;
static const uint32_t SPK_FREQ_MAX_HZ     = 40000UL;
static const uint16_t CLICK_FREQ_MAX_HZ   = 1000;
static const uint16_t PULSE_FREQ_MAX_HZ   = 100;

// ----------------------- STEPPER AXES ------------------------
//  DRV8825 per spout: one syringe pump each.
//
//  PIN CHOICE MATTERS HERE. The reference wiring suggested 6/7/8,
//  but pin 6 is PIN_SPK_L (Timer4 OC4A). Sharing it would leave the
//  left speaker silent and the step pulses unreliable, and neither
//  failure announces itself. These are plain digital pins on the
//  Mega with no timer attached; change them here if the rig differs.
#define PIN_STEP_L_STEP   51
#define PIN_STEP_L_DIR    52
#define PIN_STEP_L_EN     53      // active LOW (nENBL)

#define PIN_STEP_R_STEP   29
#define PIN_STEP_R_DIR    30
#define PIN_STEP_R_EN     31

// Centre axis: placeholder pins for a third pump that does not exist
// yet. STEPPER_C_PRESENT is 0, so this axis refuses motion instead of
// silently pretending a spout was purged.
#define PIN_STEP_C_STEP   32
#define PIN_STEP_C_DIR    33
#define PIN_STEP_C_EN     34

#define STEPPER_L_PRESENT  true
#define STEPPER_C_PRESENT  false
#define STEPPER_R_PRESENT  false

// Bookkeeping only - nothing reads it - but it is what makes
// STEPPER_NL_PER_STEP interpretable six months from now.
//   M2 M1 M0 : 000 full, 001 1/2, 010 1/4, 011 1/8, 100 1/16
#define STEPPER_MICROSTEP        8

// Which sign of counted travel pulls vacuum. Flip this, or send
// STPDIR, rather than swapping motor wires.
#define STEPPER_ASPIRATE_SIGN    (+1)

// Soft limits in steps, relative to STPZERO at the plunger home stop.
// Deliberately short of the theoretical 80000 until real travel has
// been measured on the rig.
#define STEPPER_SOFT_MIN         0L
#define STEPPER_SOFT_MAX         40000L

#define STEPPER_SPS_DEFAULT      600
#define STEPPER_SPS_MIN          20
#define STEPPER_SPS_MAX          4000
#define STEPPER_ACCEL_DEFAULT    3000   // steps/s^2, 0 = constant rate
#define STEPPER_RAMP_MS          5

// DRV8825 datasheet minimums are 1.9 us STEP high, 1.9 us low,
// 650 ns DIR setup. These clear all three comfortably.
#define STEPPER_PULSE_US         4
#define STEPPER_DIR_SETUP_US     10
#define STEPPER_ENABLE_SETTLE_US 2000
#define STEPPER_MAX_BURST        8      // steps per axis per update pass

// Milliseconds energised after the last step. 0 releases at once:
// silent, cool, no chopper noise into the lickometer between
// switches. Safe only because a T6x1 screw is not back-drivable.
#define STEPPER_HOLD_MS          0

// Nanolitres of plunger displacement per step. Geometry for a 20 mL
// syringe on T6x1 at 1/8 microstepping gives ~180; confirm
// gravimetrically the way the solenoid table was built.
#define STEPPER_NL_PER_STEP      180UL

// ----------------------- BLOCK SWITCH ------------------------
#define BLK_VAC_STEPS_DEFAULT    300UL
#define BLK_VAC_SPS_DEFAULT      400
#define BLK_MAX_CYCLES           10

#endif // CONFIG_H
