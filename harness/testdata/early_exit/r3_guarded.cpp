// flags: -fexceptions
// R3: a Guarded marker names a live object of an early_exit.guard class.
#include "host.hpp"
#include "vocabulary.hpp"

void guarded(int& depth) {
    const DepthGuard guard(depth);
    [[maybe_unused]] const fixture::early_exit::Guarded site{guard, "the guard restores the depth"};
    host_poll_events();
}

void guard_without_mark() {
    const PlainScope scope{};
    [[maybe_unused]] const fixture::early_exit::Guarded site{scope, "the scope restores nothing"};  // expect: R3
    host_poll_events();
}

int global_depth = 0;
DepthGuard global_guard(global_depth);

void guard_not_live() {
    [[maybe_unused]] const fixture::early_exit::Guarded site{global_guard, "not a local of this function"};  // expect: R3
    host_poll_events();
}
