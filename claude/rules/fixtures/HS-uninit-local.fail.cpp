int sum(int a, int b) {
    int total; // expect: HS-uninit-local
    total = a + b;
    return total;
}
