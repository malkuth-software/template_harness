// flags: -fexceptions
// R5: no foreign call between the halves of a pair without a live guard.
// The first case is GUI.Forms' abi_class_bridge.cpp top_frame bug, the
// second its fix (a guard, cp32).
#include "vocabulary.hpp"

struct HookFrame final {
    HookFrame* previous;
    int hook;
};

thread_local HookFrame* top_frame = nullptr;

using HookEntry = void (*)(int);

void enter_frame_restored_by_hand(HookEntry entry, int hook) {
    HookFrame frame{};
    frame.previous = top_frame;
    frame.hook = hook;
    top_frame = &frame;
    entry(hook);  // expect: R1, R5
    top_frame = frame.previous;
}

// Makes `frame` the innermost frame for the scope and restores the previous
// one on every exit.
class FIXTURE_GUARD TopFrameScope final {
public:
    explicit TopFrameScope(HookFrame& frame) noexcept : frame_(frame) {
        frame_.previous = top_frame;
        top_frame = &frame_;
    }
    ~TopFrameScope() { top_frame = frame_.previous; }

private:
    HookFrame& frame_;
};

void enter_frame_guarded(HookEntry entry, int hook) {
    HookFrame frame{};
    frame.hook = hook;
    const TopFrameScope scope(frame);
    [[maybe_unused]] const fixture::early_exit::Guarded site{scope, "the scope restores top_frame"};
    entry(hook);
}

int depth = 0;

void counted(void (*callback)()) {
    ++depth;
    callback();  // expect: R1, R5
    --depth;
}

void counted_in_house_style(void (*callback)()) {
    depth = depth + 1;
    callback();  // expect: R1, R5
    depth = depth - 1;
}

bool busy = false;

void flagged(void (*callback)()) {
    busy = true;
    callback();  // expect: R1, R5
    busy = false;
}

struct Recorder final {
    void begin_batch() noexcept;
    void end_batch() noexcept;
};

Recorder recorder{};

void batched(void (*callback)()) {
    recorder.begin_batch();
    callback();  // expect: R1, R5
    recorder.end_batch();
}

void counted_with_guard(void (*callback)(), int& level) {
    const DepthGuard guard(level);
    ++depth;
    [[maybe_unused]] const fixture::early_exit::Guarded site{guard, "the guard restores the level"};
    callback();
    --depth;
}
