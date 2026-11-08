#pragma once

#include <gui_forms/commands.hpp>
#include <gui_forms/value.hpp>

namespace example_app {

// The state the window binds to (forms/greeter.form names these members):
// whether to shout the greeting, and the two commands its buttons run. The
// model owns them; the program declares it before the window, so the
// window and its bindings end first. Not copyable: Value and Command are
// components with identities.
struct GreeterModel final {
    gui_forms::Value<bool> shout{false};
    gui_forms::Command greet{"example.greet", "Greet"};
    gui_forms::Command clear{"example.clear", "Clear"};
};

} // namespace example_app
