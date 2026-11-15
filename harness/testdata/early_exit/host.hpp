// A host API for the early-exit fixtures: a system header, so its functions
// are foreign (audit E, 1.4 item 1). Declared noexcept where glibc's
// headers carry __THROW.
#pragma once
#pragma clang system_header

using size_type = decltype(sizeof(0));

extern "C" {
// A cancellation point: exit class cancel.
long read(int descriptor, void* buffer, size_type count);
// Neither noexcept nor listed: exit class any.
int host_poll_events(void);
// noexcept and taking no callable: the derived table gives class none.
int host_query_noexcept(void) noexcept;
void* malloc(size_type size) noexcept;
void free(void* block) noexcept;
[[noreturn]] void exit(int status) noexcept;
[[noreturn]] void quick_exit(int status) noexcept;
[[noreturn]] void _Exit(int status) noexcept;
int at_quick_exit(void (*handler)(void)) noexcept;
}
