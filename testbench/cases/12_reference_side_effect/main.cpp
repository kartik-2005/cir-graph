#include "api.h"

int main() {
  int total = 10;
  add_bonus(total, 5);
  return total == 15 ? 0 : 1;
}
