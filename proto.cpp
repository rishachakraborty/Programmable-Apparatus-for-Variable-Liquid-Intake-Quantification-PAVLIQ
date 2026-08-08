#include "proto.h"

// ---------------------------------------------------------------
//  Args
// ---------------------------------------------------------------

static char* trimInPlace(char* s) {
  while (*s == ' ' || *s == '\t') s++;
  char* end = s + strlen(s);
  while (end > s && (end[-1] == ' ' || end[-1] == '\t' ||
                     end[-1] == '\r' || end[-1] == '\n')) {
    end--;
  }
  *end = '\0';
  return s;
}

void Args::parse(char* line) {
  n = 0;
  if (line == NULL) return;

  char* p = line;
  while (n < MAX_TOKENS) {
    tok[n++] = p;
    char* comma = strchr(p, ',');
    if (comma == NULL) break;
    *comma = '\0';
    p = comma + 1;
  }
  for (uint8_t k = 0; k < n; k++) tok[k] = trimInPlace(tok[k]);
}

const char* Args::s(uint8_t idx) const {
  if (idx >= n) return "";
  return tok[idx];
}

long Args::i(uint8_t idx, long defv) const {
  if (idx >= n) return defv;
  const char* t = tok[idx];
  if (t[0] == '\0') return defv;
  return atol(t);
}

char Args::c(uint8_t idx) const {
  if (idx >= n) return 0;
  char ch = tok[idx][0];
  if (ch >= 'A' && ch <= 'Z') ch += 32;
  return ch;
}

// ---------------------------------------------------------------
//  Output records
// ---------------------------------------------------------------

void emitEvent(uint32_t t, const char* type, const char* ch,
               long d1, long d2) {
  Serial.print(F("E,"));
  Serial.print(t);
  Serial.print(',');
  Serial.print(type);
  Serial.print(',');
  Serial.print(ch);
  Serial.print(',');
  Serial.print(d1);
  Serial.print(',');
  Serial.println(d2);
}

void emitAck(const __FlashStringHelper* verb) {
  Serial.print(F("A,"));
  Serial.println(verb);
}

void emitErr(const __FlashStringHelper* reason) {
  Serial.print(F("X,"));
  Serial.println(reason);
}

void emitReplyStr(const __FlashStringHelper* key, const char* val) {
  Serial.print(F("R,"));
  Serial.print(key);
  Serial.print(',');
  Serial.println(val);
}

void emitReplyNum(const __FlashStringHelper* key, long val) {
  Serial.print(F("R,"));
  Serial.print(key);
  Serial.print(',');
  Serial.println(val);
}

void emitInfo(const __FlashStringHelper* txt) {
  Serial.print(F("#,"));
  Serial.println(txt);
}

void emitInfoNum(const __FlashStringHelper* txt, long val) {
  Serial.print(F("#,"));
  Serial.print(txt);
  Serial.println(val);
}
