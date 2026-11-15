int apply(int (^op)(int), int v) { return op(v); }
int twice(int v) {
    return apply(^(int x) { return x * 2; }, v); // expect: HS-objc-block
}
