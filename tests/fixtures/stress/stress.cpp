extern int ext_fn(int);
struct P { int x; int y; };
static int sq(int v) { return v * v; }
int fact(int n) { return n <= 1 ? 1 : n * fact(n - 1); }
int is_even(int n);
int is_odd(int n) { return n == 0 ? 0 : is_even(n - 1); }
int is_even(int n) { return n == 0 ? 1 : is_odd(n - 1); }
void bump(int &r) { r = r + 1; }
int loops(int n) {
  int s = 0;
  for (int i = 0; i < n; ++i) {
    if (i == 2) continue;
    if (i == 7) break;
    s += sq(i);
  }
  do { s--; } while (s > 100);
  switch (n) {
  case 1: s += 1; break;
  case 2: s += 2; [[fallthrough]];
  case 3: { s += 3; break; }
  default: s = 0;
  }
  return s;
}
int unused_helper() { return 42; }
int main() {
  int a = 3;
  bump(a);
  int (*fp)(int) = sq;
  P p{1, 2};
  p.y = fp(a) + p.x;
  int r = (a > 2 || ext_fn(a)) ? loops(a) : fact(p.y);
  return is_even(r) + r;
}
