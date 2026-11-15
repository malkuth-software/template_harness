// flags: -fno-exceptions
// R3: without exceptions no guard runs on an early exit, so Guarded is wrong.
#include "host.hpp"
#include "vocabulary.hpp"

void guarded_without_exceptions(int& depth) {
    const DepthGuard guard(depth);
    [[maybe_unused]] const fixture::early_exit::Guarded site{guard, "no landing pad runs this guard"};  // expect: R3
    host_poll_events();
}

void whole_without_exceptions() {
    [[maybe_unused]] const fixture::early_exit::Whole site{"form 1 is available everywhere"};
    host_poll_events();
}
