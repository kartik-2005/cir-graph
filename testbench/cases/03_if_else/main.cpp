int choose(int value) {
  if (value > 0)
    return 1;
  else
    return 2;
}

int main() { return choose(1) == 1 && choose(0) == 2 ? 0 : 1; }
