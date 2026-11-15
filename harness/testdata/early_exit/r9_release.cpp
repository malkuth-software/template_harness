// flags: -fexceptions
// R9 (rule 1 as reworded): an owner lets go of a resource before releasing
// it, so nothing is ever freed twice.
#include "host.hpp"
#include "std.hpp"

struct Block final {
    int size;
};

class Holder final {
public:
    ~Holder();
    void replace_while_held(Block* fresh);
    void replace_after_letting_go(Block* fresh);
    void clear_while_held();
    void clear_after_letting_go();

private:
    Block* block_{};
    void* raw_{};
    std::optional<int> cached_{};
    std::unique_ptr<Block> owned_{};
};

void Holder::replace_while_held(Block* fresh) {
    delete block_;  // expect: R9
    block_ = fresh;
}

void Holder::replace_after_letting_go(Block* fresh) {
    Block* old = std::exchange(block_, fresh);
    delete old;
}

void Holder::clear_while_held() {
    free(raw_);  // expect: R9
    raw_ = nullptr;
    cached_.reset();  // expect: R9
}

void Holder::clear_after_letting_go() {
    void* raw = raw_;
    raw_ = nullptr;
    free(raw);
    // std::unique_ptr replaces its pointer before deleting the old object.
    owned_.reset();
}

// A destructor is no exception: it lets go before it releases.
Holder::~Holder() {
    delete block_;  // expect: R9
}
