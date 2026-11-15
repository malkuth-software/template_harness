// flags: -fexceptions
// D1 through a refusal that is an exception. A-gf-controls-11-F8: a bound
// value's validating subscriber (another range control) refused a candidate
// by throwing, and set_value_from_input let the throw out of pointer, key
// and timer handling. The fix makes the refusal a result.
#include "vocabulary.hpp"

class BoundValue final {
public:
    // Throws when a bound control refuses the candidate.
    FIXTURE_REFUSAL_THROW void set(double candidate) {
        if (candidate < minimum_ || candidate > maximum_) {
            throw std::out_of_range("refused by a bound control");  // expect: D1
        }
        value_ = candidate;
    }

    // The same refusal as a result: the value is unchanged when it returns false.
    bool try_set(double candidate) noexcept {
        if (candidate < minimum_ || candidate > maximum_) return false;
        value_ = candidate;
        return true;
    }

private:
    double minimum_{0.0};
    double maximum_{10.0};
    double value_{0.0};
};

class TrackBar final : public fixture::Control {
public:
    void set_value_from_input_bad(double candidate) {
        value_.set(candidate);  // expect: D2
    }

    bool set_value_from_input_good(double candidate) noexcept {
        const bool changed = value_.try_set(candidate);
        return changed;
    }

    void on_wheel_bad(double delta) { set_value_from_input_bad(delta * 100.0); }

    void on_wheel_good(double delta) {
        const bool changed = set_value_from_input_good(delta * 100.0);
        static_cast<void>(changed);
    }

private:
    BoundValue value_;
};

class WheelDelivery final {
public:
    FIXTURE_DISPATCH_ENTRY void deliver_bad(double delta) { bar_.on_wheel_bad(delta); }
    FIXTURE_DISPATCH_ENTRY void deliver_good(double delta) { bar_.on_wheel_good(delta); }

private:
    TrackBar bar_;
};
