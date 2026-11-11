int code(int m) {
    switch (m) { // expect: HS-switch-default
    case 0: return 0;
    case 1: return 1;
    }
    return -1;
}
