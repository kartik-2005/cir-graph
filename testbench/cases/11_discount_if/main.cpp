int apply_discount(int price, int discount) {
  int result = price - discount;
  if (result < 0)
    result = 0;
  return result;
}

int demo(void) {
  int price = 100;
  int discount = 20;
  int final_price = apply_discount(price, discount);
  return final_price;
}

int main() {
  int normal = demo();
  int clamped = apply_discount(10, 20);
  return normal == 80 && clamped == 0 ? 0 : 1;
}
