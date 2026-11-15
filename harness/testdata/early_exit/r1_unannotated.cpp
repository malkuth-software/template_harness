// flags: -fexceptions
// R1: a foreign call of class cancel, terminate or any states its form.
#include "host.hpp"
#include "vocabulary.hpp"

void poll_unmarked() {
    host_poll_events();  // expect: R1
}

void poll_whole() {
    [[maybe_unused]] const fixture::early_exit::Whole site{"nothing is mutated around the poll"};
    host_poll_events();
}

void poll_no_exit() {
    [[maybe_unused]] const fixture::early_exit::NoExit site{"the host documents no early exit"};  // expect-report: R1
    host_poll_events();
}

void poll_empty_reason() {
    [[maybe_unused]] const fixture::early_exit::Whole site{""};  // expect: R1
    host_poll_events();
}

// Function-level form 1 covers every call in the body.
FIXTURE_WHOLE void poll_whole_body() {
    host_poll_events();
    host_poll_events();
}

// Class none (noexcept, no callable parameter): nothing to state.
void query_quietly() {
    host_query_noexcept();
}

// An indirect call: class any through a type that is not noexcept (P7).
void call_back(void (*callback)()) {
    callback();  // expect: R1
}

// Indirect calls through an own alias reach only our code.
using Kernel FIXTURE_OWN = void (*)(int) noexcept;

void run_kernel(Kernel kernel) {
    kernel(1);
}

// A first-party declaration marked foreign: class any, the derived table
// not applying.
FIXTURE_FOREIGN int host_wrapper() noexcept;

void call_wrapper() {
    host_wrapper();  // expect: R1
}

// A dependent callable: the callee's type was a template parameter.
template <typename Callable>
void invoke_callable(Callable& callable) {
    callable();  // expect: R1
}

struct Task final {
    void operator()() const;
};

void run_task() {
    Task task{};
    invoke_callable(task);
}
