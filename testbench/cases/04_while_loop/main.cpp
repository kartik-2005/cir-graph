int sum_to_three() {
  int sum = 0;
  int i = 1;
  while (i <= 3) {
    sum += i;
    ++i;
  }
  return sum;
}

int main() { return sum_to_three() == 6 ? 0 : 1; }
