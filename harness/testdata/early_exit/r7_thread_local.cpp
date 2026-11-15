// flags: -fexceptions
// R7: every thread_local is trivially destructible; dynamic initialization
// is listed for review.
struct Records final {
    ~Records();
    int count;
};

thread_local Records records;  // expect: R7
thread_local Records* records_pointer = nullptr;
thread_local int record_index = 0;

int first_index();

thread_local int dynamic_index = first_index();  // expect-report: R7
constinit thread_local int constant_index = 0;
