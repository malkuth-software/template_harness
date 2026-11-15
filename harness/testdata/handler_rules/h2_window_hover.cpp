// flags: -fexceptions
// H2 with a lifetime token. A-gf-core-17-F1: the window's hover block raised
// mouse-leave and mouse-enter (whose handlers may close and destroy the
// window) and then wrote hovered_ and the tool-tip state. The click raisers
// already held the lifetime record and stopped when it named no window; the
// fix does the same for hover.
#include "vocabulary.hpp"

class Window final {
public:
    void hover_bad(fixture::Control* next) {
        mouse_leave_.emit(hovered_);
        hovered_ = next;  // expect: H2
        mouse_enter_.emit(next);  // expect: H2
        tool_tip_wake_ = true;  // expect: H2
    }

    void hover_good(fixture::Control* next) {
        const fixture::LifetimeHold hold(lifetime_);
        mouse_leave_.emit(hovered_);
        if (!hold.window_alive()) return;
        hovered_ = next;
        mouse_enter_.emit(next);
        if (!hold.window_alive()) return;
        tool_tip_wake_ = true;
    }

private:
    fixture::WindowLifetime* lifetime_{};
    fixture::Control* hovered_{};
    bool tool_tip_wake_{};
    fixture::Event<fixture::Control*> mouse_leave_;
    fixture::Event<fixture::Control*> mouse_enter_;
};
