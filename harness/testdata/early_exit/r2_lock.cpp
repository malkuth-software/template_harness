// flags: -fexceptions
// R2: no foreign call of class cancel, terminate or any while a lock is held.
#include "host.hpp"
#include "std.hpp"
#include "vocabulary.hpp"

std::mutex table_mutex;

void read_under_lock(int descriptor, char* buffer) {
    const std::lock_guard<std::mutex> held(table_mutex);
    read(descriptor, buffer, 1U);  // expect: R1, R2
}

void call_back_under_spin_lock(void (*callback)()) {
    const SpinLock held{};
    callback();  // expect: R1, R2
}

void call_back_between_lock_and_unlock(void (*callback)()) {
    table_mutex.lock();
    callback();  // expect: R1, R2
    table_mutex.unlock();
}

void call_back_after_the_lock(void (*callback)()) {
    {
        const std::lock_guard<std::mutex> held(table_mutex);
    }
    [[maybe_unused]] const fixture::early_exit::Whole site{"the lock is released before the call"};
    callback();
}

// Inside a no-cancel scope, class cancel counts as none.
void read_without_cancellation(int descriptor, char* buffer) {
    const CancelDisabled no_cancel{};
    const std::lock_guard<std::mutex> held(table_mutex);
    read(descriptor, buffer, 1U);
}
