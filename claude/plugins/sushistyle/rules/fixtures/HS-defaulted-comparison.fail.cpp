#include <compare>
struct Point {
    int x;
    bool operator==(const Point&) const = default; // expect: HS-defaulted-comparison
};
