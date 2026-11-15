// flags: -fexceptions
// H3: a loop over a member container whose body runs handlers.
// A-gf-controls-01-F1: RadioButton cleared its group while iterating the
// parent's live child list; a checked-changed handler that adds or removes
// a sibling invalidates the iteration. The fix iterates a copy of the
// identities and looks each one up again.
#include "vocabulary.hpp"

class RadioButton;

class Panel final : public fixture::Control {
public:
    std::vector<RadioButton*> children;
};

class RadioButton final : public fixture::Control {
public:
    void set_checked(bool checked) {
        checked_ = checked;
        checked_changed_.emit(checked);
    }

    void clear_group_bad(Panel& parent) {
        for (RadioButton* sibling : parent.children) {  // expect: H3
            if (sibling != this) (*sibling).set_checked(false);
        }
    }

    void clear_group_good(Panel& parent) {
        const std::vector<RadioButton*> siblings = parent.children;
        for (RadioButton* sibling : siblings) {
            if (sibling == this || !contains(parent, sibling)) continue;
            (*sibling).set_checked(false);
        }
    }

private:
    static bool contains(const Panel& parent, const RadioButton* button) noexcept {
        for (const RadioButton* child : parent.children) {
            if (child == button) return true;
        }
        return false;
    }

    bool checked_{};
    fixture::Event<bool> checked_changed_;
};
