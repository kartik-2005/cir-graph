// switch inside a for loop, with break and continue.
int classify(int value) {
  switch (value % 3) {
  case 0:
    return 10;
  case 1:
    return 20;
  default:
    return 30;
  }
}

int total(int limit) {
  int sum = 0;
  for (int i = 0; i < limit; ++i) {
    if (i == 2)
      continue;
    if (i == 5)
      break;
    sum += classify(i);
  }
  return sum;
}

int main() { return total(10) == 10 + 20 + 10 + 20 ? 0 : 1; }
