struct Node { int value; int get() { return this->value; } };
int read(Node* p) {
    return p->value; // expect: HS-arrow
}
