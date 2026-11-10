#include "api.h"

static int clamp(int v) { return v < 0 ? 0 : v; }

static int twice(int v) { return 2 * v; }

int main() {
  int total = checked_sum(5);           // 1 + 3 + 5 = 9
  scale(total, 2);                      // 18, written through a reference
  int (*op)(int) = twice;               // indirect call target
  int r = (total > 10 && op(total) > 30) ? fact(3) : clamp(-1);
  return r == 6 ? 0 : 1;
}
