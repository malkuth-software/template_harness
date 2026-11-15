enum class Dock { top, bottom, fill };
int extent(Dock dock) {
    switch (dock) {
    case Dock::top: return 1;
    case Dock::bottom: return 2;
    case Dock::fill: return 3;
    }
    return 0;
}
