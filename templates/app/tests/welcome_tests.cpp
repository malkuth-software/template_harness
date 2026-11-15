// The composite control and the form that places it, without a native
// window: GreetingCard is generated from controls/greeting-card.control and
// WelcomePanel from forms/welcome.form, both at build time.
#include "greeting_card_control.hpp"
#include "welcome_form.hpp"

#include <cstdlib>
#include <iostream>
#include <memory>

namespace {

void require(const bool condition, const char* message) {
    if (condition) return;
    std::cerr << "welcome_tests: " << message << '\n';
    std::exit(1);
}

} // namespace

int main() {
    const std::shared_ptr<{{namespace}}::WelcomePanel> panel = {{namespace}}::WelcomePanel::make();
    const std::shared_ptr<{{namespace}}::GreetingCard> card = (*panel).welcome_card;
    require(card != nullptr, "the form makes the card");
    require((*card).read_title() == "Welcome", "the form sets the card's title");
    const gui_forms::Control::Ptr content = (*card).slot();
    require(content != nullptr && (*(*panel).welcome_message).parent() == content,
            "the form's label is in the card's slot");
    require((*card).child("title") != nullptr, "the card makes its own title label");
    (*card).write_title("Hello");
    require((*card).read_title() == "Hello", "the title reads back");
    return 0;
}
