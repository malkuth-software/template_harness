int doubled(int x) { return x * 2; }
int apply(int (*op)(int), int v) { return op(v); }
int twice(int v) { return apply(&doubled, v); }
