#pragma once
typedef int (*binary_op)(int, int);
int add(int a, int b);
int mul(int a, int b);
int apply(binary_op op, int a, int b);
