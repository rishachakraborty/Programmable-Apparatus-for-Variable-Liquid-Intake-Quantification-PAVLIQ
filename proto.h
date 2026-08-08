#ifndef PROTO_H
#define PROTO_H

#include <Arduino.h>
#include "config.h"

// =============================================================
//  SERIAL PROTOCOL
//
//  HOST -> ARDUINO : one ASCII command per line, comma separated,
//                    terminated by '\n'. Case-insensitive verb.
//
//  ARDUINO -> HOST : one ASCII record per line, first field is a
//                    single-character record type:
//
//    E,<t_ms>,<TYPE>,<CH>,<d1>,<d2>   timestamped event  (log this)
//    R,<KEY>,<VALUE>                  reply to a query
//    A,<VERB>                         command accepted
//    X,<REASON>                       command rejected / error
//    #,<text>                         human-readable comment
//
//  <t_ms> is Arduino millis() at the moment the event occurred,
//  captured before printing, so serial latency never contaminates
//  a timestamp. Python treats Arduino millis() as the master clock
//  and records a host-clock offset via the SYNC command.
// =============================================================

#define MAX_TOKENS 12
#define MAX_LINE   96

struct Args {
  char*   tok[MAX_TOKENS];
  uint8_t n;

  // Splits `line` in place on commas. Trims surrounding whitespace.
  void parse(char* line);

  // Token accessor; returns "" if the index does not exist.
  const char* s(uint8_t idx) const;

  // Integer accessor; returns defv if missing or empty.
  long i(uint8_t idx, long defv = 0) const;

  // First character of a token, lowercased; 0 if missing.
  char c(uint8_t idx) const;
};

// IMPORTANT: the text arguments below are __FlashStringHelper*, not
// const char*. On AVR a string literal handed to a const char*
// parameter is copied into SRAM at boot, and this firmware has
// enough diagnostic text to exhaust the Mega's 8 KB on its own.
// Every call site must therefore wrap its literal in F().
void emitEvent(uint32_t t, const char* type, const char* ch,
               long d1, long d2);
void emitAck(const __FlashStringHelper* verb);
void emitErr(const __FlashStringHelper* reason);
void emitReplyStr(const __FlashStringHelper* key, const char* val);
void emitReplyNum(const __FlashStringHelper* key, long val);
void emitInfo(const __FlashStringHelper* txt);
void emitInfoNum(const __FlashStringHelper* txt, long val);

#endif // PROTO_H
