// The few standard library pieces the early-exit fixtures use, declared here
// so the fixtures need no host headers. A system header in namespace std.
#pragma once
#pragma clang system_header

#include "host.hpp"

namespace std {

using ::at_quick_exit;
using ::exit;
using ::quick_exit;

template <class T>
constexpr T&& move(T& value) noexcept {
    return static_cast<T&&>(value);
}

template <class T, class U>
constexpr T exchange(T& object, U&& value) {
    T old = static_cast<T&&>(object);
    object = static_cast<U&&>(value);
    return old;
}

class mutex {
public:
    void lock();
    void unlock();
};

template <class Mutex>
class lock_guard {
public:
    explicit lock_guard(Mutex& mutex) : mutex_(mutex) { mutex_.lock(); }
    ~lock_guard() { mutex_.unlock(); }
    lock_guard(const lock_guard&) = delete;

private:
    Mutex& mutex_;
};

template <class T>
class optional {
public:
    void reset() noexcept;

private:
    T value_{};
    bool engaged_{};
};

template <class T>
class unique_ptr {
public:
    void reset() noexcept;

private:
    T* pointer_{};
};

}  // namespace std
