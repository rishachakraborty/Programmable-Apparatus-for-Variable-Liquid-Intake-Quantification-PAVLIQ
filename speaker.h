#ifndef SPEAKER_H
#define SPEAKER_H

#include <Arduino.h>
#include "config.h"

enum SpkCh : uint8_t { SPK_L = 0, SPK_R = 1, SPK_COUNT = 2 };

void spkBegin();

// Start a speaker cue.
//   durMs      : 0 = run until spkStop(), else auto-stop after durMs
//   toneHz     : carrier frequency (SPK_FREQ_MIN_HZ..SPK_FREQ_MAX_HZ)
//   clickMode  : false = continuous tone, true = gated click train
//   clickHz    : click rate when clickMode (1..CLICK_FREQ_MAX_HZ).
//                This is the parameter that encodes reward amount.
//   volPct     : 0..SPK_MAX_DUTY_PCT duty cycle
//   clickOnUs  : 0 = on-time is 50% of the click period (default),
//                otherwise an explicit on-time in microseconds
//                (clamped to 90% of the period).
// Returns false and emits an error if any argument is out of range.
bool spkStart(uint8_t ch, uint32_t durMs, uint32_t toneHz,
              bool clickMode, uint16_t clickHz, uint8_t volPct,
              uint16_t clickOnUs);

void spkStop(uint8_t ch);
void spkStopAll();

// Must be called every pass through loop(). Non-blocking.
void spkUpdate();

bool spkIsActive(uint8_t ch);

// Map 'l'/'r' to a channel index; returns SPK_COUNT if invalid.
uint8_t spkChFromChar(char c);
const char* spkChName(uint8_t ch);

#endif // SPEAKER_H
