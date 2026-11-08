// The example's behaviour without a native window: the test links
// GUIForms::Controls, makes the generated GreeterWindow (a renderer-free
// Window model, joined to no application), compiles the example's theme
// package against its resources, and drives the window as a user would.
#include "example_theme.hpp"
#include "greeter.hpp"
#include "greeter_form.hpp"
#include "greeter_model.hpp"

#include <gui_forms/events.hpp>
#include <gui_forms/theme.hpp>
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

// A user's click: the button runs its bound command, which a test requires.
void click(gui_forms::Button& button) {
    const bool clicked = button.perform_click();
    require(clicked, "the enabled button runs its click");
}

// One key press on the window, as the host delivers it.
void press(gui_forms::Window& window, std::uint32_t physical_key) {
    gui_forms::KeyEvent down{};
    down.action = gui_forms::KeyAction::down;
    down.physical_key = physical_key;
    static_cast<void>(window.dispatch_key(down));
}

// The package compiles onto GUI.Forms' own theme with no finding at all.
void test_theme(example_app::GreeterWindow& window) {
    gui_forms::ThemeCompileOptions options{};
    options.base = gui_forms::ThemeBase::gui_forms;
    options.controls = gui_forms::stock_control_anatomies();
    const gui_forms::ThemeCompileResult compiled =
        (*window.resources()).compile_theme_package(example_app::example_theme(), gui_forms::ThemeLibrary{}, options);
    require(compiled.succeeded(), "the theme package compiles");
    require(compiled.diagnostics().empty(), "the theme package compiles without warnings");
    window.set_theme(compiled.theme());
}

} // namespace

int main() {
    example_app::GreeterModel model{};
    example_app::GreeterWindow window(model);
    example_app::Greeter greeter(window, model);
    window.perform_layout();
    test_theme(window);

    // The form: the title, the bound buttons and the default button.
    require(example_app::GreeterWindow::window_title == "GUI.Forms example", "the form names the window's title");
    require(window.accept_button() == window.example_greet, "Greet is the window's default button");

    // The list is hidden, and Clear disabled, until a name is greeted.
    require(!(*window.example_greeted).visible.read(), "the empty list is hidden");
    require(!model.clear.state().enabled && !(*window.example_clear).enabled.read(),
            "Clear follows its disabled command");

    click(*window.example_greet);
    require((*window.example_greeting).text() == "Type a name first.", "an empty name asks for one");
    require((*window.example_greeted).items().empty(), "an empty name is not listed");

    (*window.example_name).set_text("Ada");
    click(*window.example_greet);
    require((*window.example_greeted).visible.read(), "the list shows once it has a name");
    require(model.clear.state().enabled, "Clear is enabled once the list has a name");

    // The check box is bound two way to the model's Value<bool>.
    (*window.example_shout).set_checked(true);
    require(model.shout.get(), "checking Shout reaches the model");
    (*window.example_name).set_text("Grace");
    click(*window.example_greet);
    require((*window.example_greeting).text() == "HELLO, GRACE!", "a shouted greeting is in capitals");
    const std::span<const std::string> listed = (*window.example_greeted).items();
    require(listed.size() == 2U && listed[0] == "Ada" && listed[1] == "Grace", "each greeted name is listed in order");

    // The rule: Escape in the name box clears the name, and only there.
    require(window.request_focus(window.example_name), "the name box takes focus");
    (*window.example_name).set_text("Linus");
    press(window, gui_forms::PhysicalKey::escape);
    require((*window.example_name).text().empty(), "Escape in the name box clears the name");
    require(window.rules().statistics().consumed == 1U, "the rule consumed the key");

    // Clear empties the list and hides it again.
    click(*window.example_clear);
    require((*window.example_greeted).items().empty() && !(*window.example_greeted).visible.read(),
            "Clear empties and hides the list");

    // Disposed, the greeter neither handles the commands nor keeps its rule.
    greeter.dispose();
    require(window.rules().armed_count() == 0U, "disposing the greeter disarms its rule");
    (*window.example_name).set_text("Linus");
    click(*window.example_greet);
    require((*window.example_greeted).items().empty(), "a disposed greeter no longer greets");
    std::cout << "greeter: form, theme, bindings, cells, rule and disposal passed\n";
    return 0;
}
