#pragma once
#include <stddef.h>

#define STACK_CAPACITY 8

struct stack {
  int items[STACK_CAPACITY];
  size_t size;
};

void stack_init(struct stack *s);
int stack_push(struct stack *s, int value);
int stack_pop(struct stack *s, int *out);
