#include <coroutine>
struct Task {
    struct promise_type {
        Task get_return_object() { return Task{}; }
        std::suspend_never initial_suspend() { return {}; }
        std::suspend_never final_suspend() noexcept { return {}; }
        void return_void() {}
        void unhandled_exception() {}
    };
};
Task run() { // expect: HS-coroutine
    co_return;
}
