enum class Mode { idle, run };
int code(Mode m) {
    switch (m) {
    case Mode::idle: return 0;
    case Mode::run: return 1;
    default: return -1;
    }
}
int named_all(Mode m) {
    switch (m) {
    case Mode::idle: return 0;
    case Mode::run: return 1;
    }
    return -1;
}
