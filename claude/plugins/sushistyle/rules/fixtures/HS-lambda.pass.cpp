#include <algorithm>
#include <vector>
struct IsLarge { bool operator()(int x) const { return x > 9; } };
int count_large(const std::vector<int>& xs) {
    return static_cast<int>(std::count_if(xs.begin(), xs.end(), IsLarge{}));
}
