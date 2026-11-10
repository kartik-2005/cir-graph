#pragma once
struct Account {
  int balance;
  int limit;
};
void deposit(Account *account, int amount);
int available(const Account &account);
