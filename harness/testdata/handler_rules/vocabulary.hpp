// The handler-rules vocabulary as a project spells it
// (docs/handler_methodology.md, section 3.1): the declaration marks as
// macros, the site markers in the project's own namespace (matched by their
// last two name components), and the small stand-ins the fixtures share for
// GUI.Forms' event, focus and fail-fast seams.
#pragma once

#include <cstddef>
#include <stdexcept>
#include <vector>

#define FIXTURE_DISPATCH_ENTRY [[clang::annotate("handler.dispatch_entry")]]
#define FIXTURE_RUNS_HANDLERS [[clang::annotate("handler.runs_handlers")]]
#define FIXTURE_APPLICATION [[clang::annotate("handler.application")]]
#define FIXTURE_NO_HANDLERS [[clang::annotate("handler.no_handlers")]]
#define FIXTURE_REVALIDATES [[clang::annotate("handler.revalidates")]]
#define FIXTURE_KEEP_ALIVE [[clang::annotate("handler.keep_alive")]]
#define FIXTURE_LIFETIME [[clang::annotate("handler.lifetime")]]
#define FIXTURE_REFUSAL_THROW [[clang::annotate("handler.refusal_throw")]]
#define FIXTURE_SELF_STABLE [[clang::annotate("handler.self_stable")]]

namespace fixture::handler {

// The next statement may use `value` after a handler-running call.
template <typename Value>
struct Revalidated final {
    constexpr Revalidated(const Value& value, const char* reason) noexcept : value_(&value), reason_(reason) {}
    const Value* value_;
    const char* reason_;
};

// The next statement's throw or throwing call on a dispatch path is accepted.
struct ThrowAccepted final {
    constexpr explicit ThrowAccepted(const char* reason) noexcept : reason_(reason) {}
    const char* reason_;
};

}  // namespace fixture::handler

namespace fixture {

// The fail-fast gate: its target is a dispatch root (the project's `gates`).
template <typename Owner, typename Argument>
void call_failing_fast(const char* site, void (Owner::*target)(Argument), Owner& owner, Argument argument) {
    static_cast<void>(site);
    (owner.*target)(argument);
}

// An application event: emit runs application handlers.
template <typename Argument>
class Event final {
public:
    FIXTURE_RUNS_HANDLERS FIXTURE_APPLICATION void emit(Argument argument);
};

class Control;

// A strong reference to a control; held across a call it keeps the control alive.
class FIXTURE_KEEP_ALIVE ControlPtr final {
public:
    explicit ControlPtr(Control* control) noexcept;
    ~ControlPtr();
    Control* get() const noexcept;

private:
    Control* control_;
};

// A record that outlives its window; `window` is null once the window is gone.
struct WindowLifetime final {
    void* window;
};

// A local copy of a lifetime record, tested after a call.
class FIXTURE_LIFETIME LifetimeHold final {
public:
    explicit LifetimeHold(WindowLifetime* record) noexcept;
    bool window_alive() const noexcept;

private:
    WindowLifetime* record_;
};

class Window final {
public:
    // Runs validation and focus handlers of the old and the new control.
    FIXTURE_RUNS_HANDLERS bool request_focus(Control& control);
    Control* focused_control() const noexcept;
    bool active() const noexcept;
};

class Control {
public:
    virtual ~Control();
    Window* attached_window() const noexcept;
    ControlPtr shared_from_this() noexcept;
    bool is_alive() const noexcept;
};

}  // namespace fixture
