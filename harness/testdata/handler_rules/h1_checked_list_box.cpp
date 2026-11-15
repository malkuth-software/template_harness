// flags: -fexceptions
// H1 on a parameter validated against member state. A-gf-controls-08-F2:
// set_item_check_state checked index against check_states_, raised
// item_checking (whose handler may remove the item), then wrote
// check_states_[index]. The fix checks the index again after the emit.
#include "vocabulary.hpp"

class CheckedListBox final : public fixture::Control {
public:
    void set_item_check_state_bad(std::size_t index, unsigned char state) {
        if (index >= check_states_.size()) return;
        item_checking_.emit(index);
        check_states_[index] = state;  // expect: H1
    }

    void set_item_check_state_good(std::size_t index, unsigned char state) {
        if (index >= check_states_.size()) return;
        item_checking_.emit(index);
        if (index >= check_states_.size()) return;
        check_states_[index] = state;
    }

private:
    std::vector<unsigned char> check_states_;
    fixture::Event<std::size_t> item_checking_;
};
