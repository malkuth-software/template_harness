#pragma once

#include <gui_forms/basic_controls.hpp>
#include <gui_forms/component.hpp>
#include <gui_forms/input_controls.hpp>

#include <memory>

namespace example_app {

// The app's one form: a text box for a name, a button that greets that name
// in the label, and a list of every name greeted so far.
//
// The root panel owns the four controls (a parent owns its children); the
// greeter keeps shared references to them and owns the button's click
// subscription, which ends when the greeter is disposed or destroyed. The
// controls are public so that the app can hand `root` to a window and a test
// can drive the form as a user would; replacing one would detach it from
// `root`, so callers only read them.
class Greeter final : public gui_forms::Component {
public:
    Greeter();

    std::shared_ptr<gui_forms::Panel> root;
    std::shared_ptr<gui_forms::Label> greeting;
    std::shared_ptr<gui_forms::TextBox> name;
    std::shared_ptr<gui_forms::Button> greet;
    std::shared_ptr<gui_forms::ListBox> greeted;

private:
    // The button's click: an empty name changes only the label; any other
    // name is greeted and appended to the list.
    void on_greet();
};

} // namespace example_app
