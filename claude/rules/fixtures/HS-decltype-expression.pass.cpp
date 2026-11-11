#include <string>
int foreign_open(const char* path, int flags);
struct Cell { std::string value; };
struct TextBlock {
    Cell text;
    void prepare_write(const decltype(text)& cell, std::string& incoming);
};
using OpenFunction = decltype(&foreign_open);
using TextMember = decltype(&TextBlock::text);
struct scale_traits { using Result = decltype(foreign_open("", 0) + 1L); };
struct NameCapability { Cell name; };
struct Control : NameCapability { void prepare_write(const decltype(name)& cell); };
void Control::prepare_write(const decltype(name)& cell) {}
