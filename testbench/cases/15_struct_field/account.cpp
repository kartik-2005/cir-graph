#include "api.h"
void deposit(Account *account, int amount) { account->balance += amount; }
int available(const Account &account) { return account.balance + account.limit; }
