int width() { return 4; }
int area() {
    auto w = width(); // expect: HS-auto
    const auto& h = w; // expect: HS-auto
    auto* p = &w; // expect: HS-auto
    return w * h * *p;
}
