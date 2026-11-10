// Direct recursion (fact) and mutual recursion (is_even / is_odd).
int fact(int n) { return n <= 1 ? 1 : n * fact(n - 1); }

bool is_odd(unsigned n);
bool is_even(unsigned n) { return n == 0 ? true : is_odd(n - 1); }
bool is_odd(unsigned n) { return n == 0 ? false : is_even(n - 1); }

int main() { return fact(5) == 120 && is_even(10) && is_odd(7) ? 0 : 1; }
