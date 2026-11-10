/* C translation units and an indirect call through a function pointer. */
#include "ops.h"
int main(void) { return apply(add, 2, 3) == 5 && apply(mul, 2, 3) == 6 ? 0 : 1; }
