#ifndef LED_H
#define LED_H

#include <Arduino.h>
#include "config.h"

enum LedCh : uint8_t { LED_W = 0, LED_B = 1, LED_G = 2, LED_COUNT = 3 };

void ledBegin();

// Start an LED cue.
//   durMs      : 0 = run until ledStop(), else auto-stop after durMs
//   pulsing    : false = steady, true = square-wave flicker
//   pulseHz    : flicker rate when pulsing (1..PULSE_FREQ_MAX_HZ)
//   brightness : 0..255 software-PWM duty
// Returns false and emits an error if any argument is out of range.
bool ledStart(uint8_t ch, uint32_t durMs, bool pulsing,
              uint16_t pulseHz, uint8_t brightness);

void ledStop(uint8_t ch);
void ledStopAll();

// Must be called every pass through loop(). Non-blocking.
void ledUpdate();

bool ledIsActive(uint8_t ch);

// Map 'w'/'b'/'g' to a channel index; returns LED_COUNT if invalid.
uint8_t ledChFromChar(char c);
const char* ledChName(uint8_t ch);

#endif // LED_H
