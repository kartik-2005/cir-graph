#include "api.h"

static int offset(int value) { return value + 1; }

int from_left(int value) { return offset(value); }
