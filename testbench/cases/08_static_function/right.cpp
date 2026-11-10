#include "api.h"

static int offset(int value) { return value + 2; }

int from_right(int value) { return offset(value); }
