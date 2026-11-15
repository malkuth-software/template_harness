// flags: -fexceptions
// R10: a catch (...) rethrows, or first calls an early_exit.forced_unwind
// function, so a forced unwind is never swallowed.
#include "vocabulary.hpp"

void work();
void report_failure() noexcept;
FIXTURE_FORCED_UNWIND void rethrow_forced_unwind() noexcept;

void swallow() {
    try {
        work();
    } catch (...) {  // expect: R10
        report_failure();
    }
}

void rethrow() {
    try {
        work();
    } catch (...) {
        report_failure();
        throw;
    }
}

void forced_unwind_first() {
    try {
        work();
    } catch (...) {
        rethrow_forced_unwind();
        report_failure();
    }
}

void typed_handler() {
    try {
        work();
    } catch (int) {
        report_failure();
    }
}
