#include <vector>
int width() { return 4; }
int area(const std::vector<int>& xs) {
    int w = width();
    const int& h = w;
    int* p = &w;
    for (int x : xs) { w += x; }
    return w * h * *p;
}
