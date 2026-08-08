#ifndef DEBUG_H
#define DEBUG_H

#include <Arduino.h>

// Interactive Serial Monitor menu. Enter with the command DEBUG.
//
// The menu never talks to the hardware directly. It asks the user
// one question per argument, assembles the corresponding machine
// command string, echoes it, and hands it to dispatchCommand().
// That guarantees the debug path and the Python path exercise
// identical code, and it teaches the user the command grammar.

void debugBegin();
void debugEnter();
void debugExit();
bool debugIsActive();

// Called by the main loop with a complete input line whenever
// debug mode is active.
void debugFeedLine(char* line);

#endif // DEBUG_H
