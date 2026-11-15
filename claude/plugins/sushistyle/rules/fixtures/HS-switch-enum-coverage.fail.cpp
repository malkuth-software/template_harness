enum class Dock { top, bottom, fill };
int extent(Dock dock) {
    switch (dock) { // expect: HS-switch-enum-coverage
    case Dock::top: return 1;
    case Dock::bottom: return 2;
    }
    return 0;
}
