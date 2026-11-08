#include "greeter.hpp"

#include <gui_forms/event.hpp>
#include <gui_forms/events.hpp>

#include <cctype>
#include <stdexcept>
#include <string>
#include <string_view>

namespace example_app {
namespace {

// The Escape rule's action: a named function with its explicit context.
void clear_name_action(Greeter& greeter, const gui_forms::RuleFiring&) { greeter.clear_name(); }

// `text` in capitals, for the shouted greeting (ASCII letters only).
std::string shouted(std::string_view text) {
    std::string loud(text);
    for (char& letter : loud) letter = static_cast<char>(std::toupper(static_cast<unsigned char>(letter)));
    return loud;
}

} // namespace

Greeter::Greeter(GreeterWindow& window, GreeterModel& model) : window_(window), model_(model) {
    gui_forms::on(model_.greet.invoked(), *this, &Greeter::on_greet);
    gui_forms::on(model_.clear.invoked(), *this, &Greeter::on_clear);

    // Escape while the name box has focus clears the name. The rule runs
    // before the control route and consumes the key, so the text box and
    // the window's cancel handling never see it.
    gui_forms::Rule escape{};
    escape.trigger = gui_forms::trigger::key_pressed(gui_forms::PhysicalKey::escape);
    escape.conditions.push_back(gui_forms::condition::focused(*window_.example_name));
    escape.actions.push_back(gui_forms::action(*this, &clear_name_action));
    escape.consume = true;
    const gui_forms::RuleAddResult added = window_.rules().add(escape);
    if (added.refusal != gui_forms::RuleRefusal::none) throw std::logic_error("the Escape rule was refused");

    show_list_state();
}

void Greeter::clear_name() { (*window_.example_name).set_text(""); }

void Greeter::on_greet() {
    const std::string_view typed = (*window_.example_name).text();
    if (typed.empty()) {
        (*window_.example_greeting).set_text("Type a name first.");
        return;
    }
    const std::string person(typed);
    std::string text = "Hello, " + person + "!";
    if (model_.shout.get()) text = shouted(text);
    (*window_.example_greeting).set_text(text);
    (*window_.example_greeted).add_item(person);
    show_list_state();
}

void Greeter::on_clear() {
    (*window_.example_greeted).clear_items();
    (*window_.example_greeting).set_text("Type a name, then press Greet.");
    show_list_state();
}

void Greeter::show_list_state() {
    const bool has_names = !(*window_.example_greeted).items().empty();
    // A property cell: an assignment writes it (an equal value does
    // nothing), `.read()` reads it.
    (*window_.example_greeted).visible = has_names;
    // The Clear button follows its bound command's enabled state.
    model_.clear.set_enabled(has_names);
}

} // namespace example_app
