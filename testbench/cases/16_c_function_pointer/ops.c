#include "ops.h"
int add(int a, int b) { return a + b; }
int mul(int a, int b) { return a * b; }
int apply(binary_op op, int a, int b) { return op(a, b); }
