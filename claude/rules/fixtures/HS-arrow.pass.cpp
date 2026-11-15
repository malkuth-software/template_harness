struct Node { int value; int get() { return this->value; } };
int read(Node* p) {
    return (*p).value;
}
struct Base { int cursor; };
struct Derived : Base { int read_cursor() { return cursor + this->cursor; } };
