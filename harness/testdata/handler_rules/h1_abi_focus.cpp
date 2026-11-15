// flags: -fexceptions
// H1 on a raw pointer obtained from an object's state. A-gf-abi-03-F3: the
// C ABI's controls.focus kept attached_window() in a local, requested focus
// (whose callbacks may dispose the window), then read the window through
// it. The control is held by a strong reference; the window is not. The fix
// takes the window again after the call.
#include "vocabulary.hpp"

bool focus_bad(fixture::Control& control) {
    fixture::Window* const window = control.attached_window();
    if (window == nullptr) return false;
    const bool moved = (*window).request_focus(control);
    if (!moved) return false;
    const bool keyboard = (*window).active();  // expect: H1
    return keyboard;
}

bool focus_good(fixture::Control& control) {
    fixture::Window* const window = control.attached_window();
    if (window == nullptr) return false;
    const bool moved = (*window).request_focus(control);
    if (!moved) return false;
    fixture::Window* const after = control.attached_window();
    if (after == nullptr) return false;
    const bool keyboard = (*after).active();
    return keyboard;
}
