#pragma once

#include "greeter_form.hpp"
#include "greeter_model.hpp"

#include <gui_forms/component.hpp>
#include <gui_forms/rules.hpp>

namespace example_app {

// What the window does, attached from program code (a form names no
// handlers): Greet greets the typed name in the label and appends it to
// the list; Clear empties the list; Escape in the name box clears the name
// (a window rule). The list stays hidden until it has a name.
//
// The greeter borrows the window and the model, which must outlive it: the
// program declares the model, then the window, then the greeter, so the
// greeter ends first. It owns its command subscriptions (disposing it ends
// them); the window owns the rule, which the greeter's disposal revokes.
class Greeter final : public gui_forms::Component {
public:
    Greeter(GreeterWindow& window, GreeterModel& model);

    // The rule's named action: clears the name box.
    void clear_name();

private:
    // The commands' handlers.
    void on_greet();
    void on_clear();
    // Shows the list and enables Clear exactly when the list has names.
    void show_list_state();

    GreeterWindow& window_;
    GreeterModel& model_;
};

} // namespace example_app
