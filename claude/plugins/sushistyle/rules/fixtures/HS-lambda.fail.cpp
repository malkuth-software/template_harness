#include <algorithm>
#include <vector>
int count_large(const std::vector<int>& xs) {
    return static_cast<int>(std::count_if(xs.begin(), xs.end(), [](int x) { return x > 9; })); // expect: HS-lambda
}
