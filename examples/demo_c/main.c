/* C demo: a fixed-size stack in its own translation unit, driven from main. */
#include "stack.h"

static int sum_all(struct stack *s) {
  int total = 0;
  int value;
  while (stack_pop(s, &value) == 0)
    total += value;
  return total;
}

int main(void) {
  struct stack s;
  stack_init(&s);
  for (int i = 1; i <= 4; ++i)
    stack_push(&s, i * 10);
  return sum_all(&s) == 100 ? 0 : 1;
}
