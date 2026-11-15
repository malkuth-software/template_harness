#include <vector>
int first_even(const std::vector<int>& xs) {
    for (int x : xs) { if (x % 2 == 0) { return x; } }
    return 0;
}
