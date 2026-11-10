#include "api.h"

// Same name as the static helper in main.cpp: must not collide.
static int clamp(int v) { return v > 100 ? 100 : v; }

int fact(int n) { return n <= 1 ? 1 : n * fact(n - 1); }

int checked_sum(int limit) {
  int sum = 0;
  for (int i = 1; i <= limit; ++i) {
    if (i % 2 == 0)
      continue;
    sum += i;
  }
  return clamp(sum);
}

void scale(int &value, int factor) { value = value * factor; }

int never_called(int x) { return x + 1; }
