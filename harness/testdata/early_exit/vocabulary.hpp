// The early-exit vocabulary as a project spells it (STXlib audit E, section
// 1): the declaration marks as macros, and the site markers in the
// project's own namespace, matched by their last two name components.
// Also the scope classes the fixtures share.
#pragma once

#define FIXTURE_FOREIGN [[clang::annotate("early_exit.foreign")]]
#define FIXTURE_OWN [[clang::annotate("early_exit.own")]]
#define FIXTURE_NO_UNWIND [[clang::annotate("early_exit.no_unwind")]]
#define FIXTURE_GUARD [[clang::annotate("early_exit.guard")]]
#define FIXTURE_LOCK [[clang::annotate("early_exit.lock")]]
#define FIXTURE_OPEN [[clang::annotate("early_exit.open")]]
#define FIXTURE_NO_CANCEL [[clang::annotate("early_exit.no_cancel")]]
#define FIXTURE_WHOLE [[clang::annotate("early_exit.whole")]]
#define FIXTURE_FORCED_UNWIND [[clang::annotate("early_exit.forced_unwind")]]

namespace fixture::early_exit {

// Form 1: the invariants are whole across the next statement's calls.
struct Whole final {
    constexpr explicit Whole(const char* reason) noexcept : reason_(reason) {}
    const char* reason_;
};

// Form 2: `guard` restores or completes the partial state.
template <typename Guard>
struct Guarded final {
    constexpr Guarded(const Guard& guard, const char* reason) noexcept : guard_(&guard), reason_(reason) {}
    const Guard* guard_;
    const char* reason_;
};

// The callee cannot exit early, for the stated reason (listed for review).
struct NoExit final {
    constexpr explicit NoExit(const char* reason) noexcept : reason_(reason) {}
    const char* reason_;
};

}  // namespace fixture::early_exit

// Restores a depth on every exit.
class FIXTURE_GUARD DepthGuard final {
public:
    explicit DepthGuard(int& depth) noexcept;
    ~DepthGuard();

private:
    int& depth_;
};

// A scope with a destructor but no guard mark.
class PlainScope final {
public:
    PlainScope() noexcept;
    ~PlainScope();
};

// Holds a lock while live.
class FIXTURE_LOCK SpinLock final {
public:
    SpinLock() noexcept;
    ~SpinLock();
};

// Holds a record half published while live.
class FIXTURE_OPEN Publishing final {
public:
    Publishing() noexcept;
    ~Publishing();
};

// Disables POSIX cancellation while live.
class FIXTURE_NO_CANCEL CancelDisabled final {
public:
    CancelDisabled() noexcept;
    ~CancelDisabled();
};
