// flags: -fexceptions
// D2: a standard library member that throws on ordinary input, reached
// from dispatch. A-gf-controls-08-F1's second half: ensure_visible indexed
// the rows with at(), which threw std::out_of_range out of the mouse
// handler for a stale index.
#include "vocabulary.hpp"

class ListView final : public fixture::Control {
public:
    void ensure_visible_bad(std::size_t index) {
        scroll_ = rows_.at(index);  // expect: D2
    }

    void ensure_visible_good(std::size_t index) noexcept {
        if (index >= rows_.size()) return;
        scroll_ = rows_[index];
    }

    FIXTURE_DISPATCH_ENTRY void on_pointer(std::size_t index) {
        ensure_visible_bad(index);
        ensure_visible_good(index);
    }

private:
    std::vector<int> rows_;
    int scroll_{};
};
