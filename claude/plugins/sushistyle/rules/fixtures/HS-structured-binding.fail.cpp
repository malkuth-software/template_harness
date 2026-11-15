struct Size { int w; int h; };
int area(Size s) {
    auto [w, h] = s; // expect: HS-structured-binding
    return w * h;
}
