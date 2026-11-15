#include <ranges>
#include <vector>
int first_even(const std::vector<int>& xs) {
    for (int x : xs | std::views::filter(is_even)) { return x; } // expect: HS-ranges
    return 0;
}
