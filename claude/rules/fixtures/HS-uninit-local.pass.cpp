#include <new>
#include <string>
int sum(int a, int b) {
    int total = a + b;
    std::string label{};
    for (int i = 0; i < 2; ++i) { total += i; }
    try { total += 1; } catch (const std::bad_alloc&) { return 0; } catch (const std::exception& error) { return 1; }
    return total + static_cast<int>(label.size());
}
