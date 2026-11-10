#include "stack.h"

static int is_full(const struct stack *s) { return s->size >= STACK_CAPACITY; }

void stack_init(struct stack *s) { s->size = 0; }

int stack_push(struct stack *s, int value) {
  if (is_full(s))
    return -1;
  s->items[s->size++] = value;
  return 0;
}

int stack_pop(struct stack *s, int *out) {
  if (s->size == 0)
    return -1;
  *out = s->items[--s->size];
  return 0;
}
