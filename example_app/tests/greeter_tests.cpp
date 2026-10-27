// The form's behaviour without a native window: the test links
// GUIForms::Controls, builds the greeter inside a renderer-free Window model
// and drives it as a user would.
#include "greeter.hpp"

#include <gui_forms/window.hpp>

#include <cstdlib>
#include <iostream>
#include <span>
#include <string>

namespace {

void require(const bool condition, const char* message) {
    if (condition) return;
    std::cerr << "greeter_tests: " << message << '\n';
    std::exit(1);
}

// A user's click: the button runs its command, which a test requires.
void click(gui_forms::Button& button) {
    const bool clicked = button.perform_click();
    require(clicked, "the enabled button runs its click");
}

} // namespace

int main() {
    example_app::Greeter greeter{};
    gui_forms::Window window(greeter.root, gui_forms::Size{420.0, 360.0});
    window.perform_layout();
    require((*greeter.greeted).items().empty(), "the list starts empty");

    click(*greeter.greet);
    require((*greeter.greeting).text() == "Type a name first.", "an empty name asks for one");
    require((*greeter.greeted).items().empty(), "an empty name is not listed");

    (*greeter.name).set_text("Ada");
    click(*greeter.greet);
    (*greeter.name).set_text("Grace");
    click(*greeter.greet);
    require((*greeter.greeting).text() == "Hello, Grace!", "the label greets the latest name");
    const std::span<const std::string> listed = (*greeter.greeted).items();
    require(listed.size() == 2U && listed[0] == "Ada" && listed[1] == "Grace", "each greeted name is listed in order");

    greeter.dispose();
    (*greeter.name).set_text("Linus");
    click(*greeter.greet);
    require((*greeter.greeted).items().size() == 2U, "a disposed greeter no longer handles the button");
    std::cout << "greeter: empty name, greeting, list and disposal passed\n";
    return 0;
}
