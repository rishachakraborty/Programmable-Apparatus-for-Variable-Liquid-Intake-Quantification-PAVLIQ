#ifndef COMMANDS_H
#define COMMANDS_H

#include <Arduino.h>

// Parses and executes one command line. The buffer is modified in
// place. This is the single entry point used by BOTH the host
// (Python GUI) and the interactive debug menu, so the two can
// never drift apart: the debug menu literally assembles a command
// string and submits it here.
void dispatchCommand(char* line);

void printHelp();

// Fires everything currently staged with ARM. The trial state machine
// calls this at cue onset so a choice trial's two tones start together.
void armFireStaged();

#endif // COMMANDS_H
