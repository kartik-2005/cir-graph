// A field written through a pointer in another TU and read back by reference.
#include "api.h"
int main() {
  Account account = {100, 50};
  deposit(&account, 25);
  return available(account) == 175 ? 0 : 1;
}
