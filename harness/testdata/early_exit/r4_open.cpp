// flags: -fexceptions
// R4: no foreign call of class cancel or any while an invariant is open,
// unless a valid Guarded marker covers it; no Whole claim while one is.
#include "host.hpp"
#include "vocabulary.hpp"

void open_without_guard() {
    const Publishing publishing{};
    [[maybe_unused]] const fixture::early_exit::NoExit site{"claims the poll cannot exit"};  // expect-report: R1
    host_poll_events();  // expect: R4
}

void open_with_guard(int& depth) {
    const Publishing publishing{};
    const DepthGuard guard(depth);
    [[maybe_unused]] const fixture::early_exit::Guarded site{guard, "the guard withdraws the record"};
    host_poll_events();
}

void whole_while_open() {
    const Publishing publishing{};
    [[maybe_unused]] const fixture::early_exit::Whole site{"wrong: a record is half published"};
    host_poll_events();  // expect: R4
}
