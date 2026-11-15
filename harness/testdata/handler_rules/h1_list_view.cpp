// flags: -fexceptions
// H1: an index taken from member state before a handler-running call and
// used after it. A-gf-controls-08-F1: ListView hit-tested a press, requested
// focus (whose validated and lost-focus handlers may reload the list), then
// wrote selected_[hit.index]. The fix requests focus first and hit-tests
// after.
#include "vocabulary.hpp"

struct Hit final {
    std::size_t index;
    bool on_row;
};

class ListView final : public fixture::Control {
public:
    Hit hit_test(int y) const noexcept {
        Hit hit{};
        hit.index = static_cast<std::size_t>(y / 20);
        hit.on_row = hit.index < selected_.size();
        return hit;
    }

    void on_press_bad(fixture::Window& window, int y) {
        const Hit hit = hit_test(y);
        if (!hit.on_row) return;
        const bool focused = window.request_focus(*this);
        if (!focused) return;
        selected_[hit.index] = 1U;  // expect: H1
    }

    void on_press_good(fixture::Window& window, int y) {
        const bool focused = window.request_focus(*this);
        if (!focused) return;
        const Hit hit = hit_test(y);
        if (!hit.on_row) return;
        selected_[hit.index] = 1U;
    }

private:
    std::vector<unsigned char> selected_;
};
