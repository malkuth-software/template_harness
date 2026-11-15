// flags: -fexceptions
// H2: the object itself used after a handler-running call with nothing
// keeping it alive. A-gf-controls-06-F1: ContextMenu::activate ran the
// item's command and the item_invoked handlers, which may destroy the
// menu's owner and so the menu, then read its own fields and closed itself.
// Asking is_alive() reads the possibly freed object, so it is no proof. The
// fix holds a strong reference across the calls.
#include "vocabulary.hpp"

class ContextMenu final : public fixture::Control {
public:
    void activate_bad(std::size_t item) {
        command_.emit(item);
        if (!is_alive()) return;  // expect: H2
        item_invoked_.emit(item);  // expect: H2
        close();  // expect: H2
    }

    void activate_good(std::size_t item) {
        const fixture::ControlPtr hold = shared_from_this();
        command_.emit(item);
        item_invoked_.emit(item);
        close();
    }

    void close() noexcept { open_ = false; }

private:
    fixture::Event<std::size_t> command_;
    fixture::Event<std::size_t> item_invoked_;
    bool open_{true};
};
