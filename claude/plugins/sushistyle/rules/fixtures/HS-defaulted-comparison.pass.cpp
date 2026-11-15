struct Point {
    int x;
    bool operator==(const Point& other) const { return x == other.x; }
};
