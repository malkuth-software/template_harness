// flags: -fexceptions --target=x86_64-pc-windows-gnu
// Windows has no pthread_cancel: in a unit built for Windows, the exit
// class cancel counts as none. Class any is unchanged.
#include "host.hpp"
#include "std.hpp"
#include "vocabulary.hpp"

std::mutex table_mutex;

// A cancellation point under a lock: no finding on Windows.
void read_under_lock(int descriptor, char* buffer) {
    const std::lock_guard<std::mutex> held(table_mutex);
    read(descriptor, buffer, 1U);
}

void poll_under_lock() {
    const std::lock_guard<std::mutex> held(table_mutex);
    host_poll_events();  // expect: R1, R2
}
