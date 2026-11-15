#include <vector>
int scale(int v);
struct History {
    std::vector<int> merges;
    int count;
    using Last = decltype(merges.back()); // expect: HS-decltype-expression
    using CountRef = decltype((count)); // expect: HS-decltype-expression
};
using Scaled = decltype(scale(1)); // expect: HS-decltype-expression
using Sum = decltype(1 + 2L); // expect: HS-decltype-expression
