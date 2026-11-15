// flags: -fexceptions
// R8: our code calls no exit or quick_exit; fail-fast uses abort.
#include "host.hpp"
#include "std.hpp"
#include "vocabulary.hpp"

void leave() {
    exit(1);  // expect: R8
}

void leave_through_std() {
    std::exit(2);  // expect: R8
}

void leave_quickly() {
    quick_exit(3);  // expect: R8
}

void register_quick_exit_handler() {
    [[maybe_unused]] const fixture::early_exit::NoExit site{"registration only"};  // expect-report: R1
    at_quick_exit(nullptr);  // expect: R8
}

void leave_without_handlers() {
    _Exit(4);  // expect-report: R8
}
