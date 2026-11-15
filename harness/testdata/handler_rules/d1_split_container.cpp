// flags: -fexceptions
// D1: a throw reachable from dispatch. A-gf-controls-12-F1: SplitContainer's
// key step below zero reached set_distance, which refuses a negative
// distance with std::invalid_argument, inside the fail-fast boundary.
#include "vocabulary.hpp"

enum class Key { left, right };

class SplitContainer final : public fixture::Control {
public:
    // The application's API: a negative distance is misuse and throws.
    void set_distance(double distance) {
        if (distance < 0.0) throw std::invalid_argument("negative distance");  // expect: D1
        distance_ = distance;
    }

    // The user-input form: clamps, never throws.
    void set_distance_from_input(double distance) noexcept {
        const double clamped = distance < 0.0 ? 0.0 : distance;
        distance_ = clamped;
    }

    void on_key_bad(Key key) {
        const double delta = key == Key::left ? -40.0 : 40.0;
        set_distance(distance_ + delta);
    }

    void on_key_good(Key key) {
        const double delta = key == Key::left ? -40.0 : 40.0;
        set_distance_from_input(distance_ + delta);
    }

private:
    double distance_{25.0};
};

class HostSession final {
public:
    void deliver_bad(Key key) { split_.on_key_bad(key); }
    void deliver_good(Key key) { split_.on_key_good(key); }

    void dispatch(Key key) {
        fixture::call_failing_fast("host_session.dispatch", &HostSession::deliver_bad, *this, key);
        fixture::call_failing_fast("host_session.dispatch", &HostSession::deliver_good, *this, key);
    }

private:
    SplitContainer split_;
};
