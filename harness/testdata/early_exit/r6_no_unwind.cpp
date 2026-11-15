// flags: -fexceptions
// R6: an early_exit.no_unwind function is noexcept and reaches nothing that
// can unwind.
#include "host.hpp"
#include "vocabulary.hpp"

FIXTURE_NO_UNWIND void* allocate(size_type size) noexcept {
    void* block = malloc(size);
    return block;
}

FIXTURE_NO_UNWIND void* allocate_not_noexcept(size_type size) {  // expect: R6
    void* block = malloc(size);
    return block;
}

size_type grown_size(size_type size);

FIXTURE_NO_UNWIND void* allocate_through_a_throwing_helper(size_type size) noexcept {
    const size_type grown = grown_size(size);  // expect: R6
    void* block = malloc(grown);
    return block;
}

FIXTURE_NO_UNWIND void* allocate_after_a_host_call(size_type size) noexcept {
    host_poll_events();  // expect: R1, R6
    void* block = malloc(size);
    return block;
}

FIXTURE_NO_UNWIND void* allocate_or_throw(size_type size) noexcept {
    if (size == 0U) throw 1;  // expect: R6
    void* block = malloc(size);
    return block;
}
