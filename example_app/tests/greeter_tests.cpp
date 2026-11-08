// The example's behaviour without a native window: the test links
// GUIForms::Controls, makes the application as the program does (its theme,
// the example's package onto GUI.Forms' own, compiled once by
// Application::create), makes the generated GreeterWindow joined to it (a
// renderer-free Window model), and drives the window as a user would.
#include "example_theme.hpp"
#include "greeter.hpp"
#include "greeter_form.hpp"
#include "greeter_model.hpp"

#include <gui_forms/application.hpp>
#include <gui_forms/events.hpp>
#include <gui_forms/theme.hpp>
#include <gui_forms/window.hpp>

#include <cstdlib>
#include <iostream>
#include <memory>
#include <span>
#include <utility>
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

// The application as the program makes it: the package compiles onto
// GUI.Forms' own theme with no finding at all.
std::unique_ptr<gui_forms::Application> make_application() {
    gui_forms::ApplicationConfig config{};
    config.theme.base = gui_forms::ThemeBase::gui_forms;
    config.theme.packages.push_back(example_app::example_theme());
    gui_forms::ApplicationCreateResult created = gui_forms::Application::create(std::move(config));
    require(static_cast<bool>(created), "the application and its theme are accepted");
    require(created.findings.empty(), "the theme package compiles without warnings");
    return std::move(created.application);
}

} // namespace

int main() {
    const std::unique_ptr<gui_forms::Application> application = make_application();
    example_app::GreeterModel model{};
    example_app::GreeterWindow window(*application, model);
    example_app::Greeter greeter(window, model);
    window.perform_layout();
    require(window.resources() == (*application).resources(), "the window joins the application and its theme");

    // The form: the title, the bound buttons and the default button.
    require(example_app::GreeterWindow::window_title == "GUI.Forms example", "the form names the window's title");
    require(window.accept_button() == window.example_greet, "Greet is the window's default button");

    // The list is hidden, and Clear disabled, until a name is greeted.
    require(!(*window.example_greeted).visible.read(), "the empty list is hidden");
    require(!model.clear.state().enabled && !(*window.example_clear).enabled.read(),
            "Clear follows its disabled command");

    click(*window.example_greet);
    require((*window.example_greeting).text.read() == "Type a name first.", "an empty name asks for one");
    require((*window.example_greeted).items().empty(), "an empty name is not listed");

    (*window.example_name).text = "Ada";
    click(*window.example_greet);
    require((*window.example_greeted).visible.read(), "the list shows once it has a name");
    require(model.clear.state().enabled, "Clear is enabled once the list has a name");

    // The check box is bound two way to the model's Value<bool>.
    (*window.example_shout).checked = true;
    require(model.shout.get(), "checking Shout reaches the model");
    (*window.example_name).text = "Grace";
    click(*window.example_greet);
    require((*window.example_greeting).text.read() == "HELLO, GRACE!", "a shouted greeting is in capitals");
    const std::span<const std::string> listed = (*window.example_greeted).items();
    require(listed.size() == 2U && listed[0] == "Ada" && listed[1] == "Grace", "each greeted name is listed in order");

    // The rule: Escape in the name box clears the name, and only there.
    require(window.request_focus(window.example_name), "the name box takes focus");
    (*window.example_name).text = "Linus";
    press(window, gui_forms::PhysicalKey::escape);
    require((*window.example_name).text.read().empty(), "Escape in the name box clears the name");
    require(window.rules().statistics().consumed == 1U, "the rule consumed the key");

    // Clear empties the list and hides it again.
    click(*window.example_clear);
    require((*window.example_greeted).items().empty() && !(*window.example_greeted).visible.read(),
            "Clear empties and hides the list");

    // Disposed, the greeter neither handles the commands nor keeps its rule.
    greeter.dispose();
    require(window.rules().armed_count() == 0U, "disposing the greeter disarms its rule");
    (*window.example_name).text = "Linus";
    click(*window.example_greet);
    require((*window.example_greeted).items().empty(), "a disposed greeter no longer greets");
    std::cout << "greeter: form, theme, bindings, cells, rule and disposal passed\n";
    return 0;
}
