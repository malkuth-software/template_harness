#include "greeter.hpp"

#include <gui_forms/event.hpp>

#include <string>

namespace example_app {

Greeter::Greeter()
    : root(gui_forms::make_control<gui_forms::Panel>(gui_forms::StableId("example.root"))),
      greeting(gui_forms::make_control<gui_forms::Label>(gui_forms::StableId("example.greeting"),
                                                         "Type a name, then press Greet.")),
      name(gui_forms::make_control<gui_forms::TextBox>(gui_forms::StableId("example.name"))),
      greet(gui_forms::make_control<gui_forms::Button>(gui_forms::StableId("example.greet"), "Greet")),
      greeted(gui_forms::make_control<gui_forms::ListBox>(gui_forms::StableId("example.greeted"))) {
    // One column, top to bottom, in logical units of the 420 by 360 window.
    (*greeting).set_bounds({20.0, 20.0, 380.0, 28.0});
    (*name).set_bounds({20.0, 60.0, 260.0, 30.0});
    (*greet).set_bounds({292.0, 60.0, 108.0, 30.0});
    (*greeted).set_bounds({20.0, 104.0, 380.0, 236.0});
    (*root).add_child(greeting);
    (*root).add_child(name);
    (*root).add_child(greet);
    (*root).add_child(greeted);
    gui_forms::on((*greet).clicked(), *this, &Greeter::on_greet);
}

void Greeter::on_greet() {
    // Properties are cells: `.read()` reads, assignment writes (and raises
    // the change), and the list's items are edited in place.
    const std::string person = (*name).text.read();
    if (person.empty()) {
        (*greeting).text = "Type a name first.";
        return;
    }
    (*greeting).text = "Hello, " + person + "!";
    (*greeted).items.append(person);
}

} // namespace example_app
