#include "greeter.hpp"

#include <gui_forms/application.hpp>

#include <exception>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string_view>
#include <utility>

namespace example_app {
namespace {

// The smoke run's ready handler: it asks the native window to close as soon
// as the host has attached it, so the run proves that the app starts, opens
// its window and shuts down. A refused request throws, which the run reports
// as a callback failure instead of leaving the window open.
struct CloseWhenReady final {
    void operator()(gui_forms::Window&, gui_forms::ApplicationWindowHandle handle) const {
        const gui_forms::HostServiceStatus status = handle.request_close();
        if (!status.accepted()) throw std::runtime_error("the host refused to close the window");
    }
};

int run(const bool close_when_ready) {
    Greeter greeter{};
    std::unique_ptr<gui_forms::Window> window =
        std::make_unique<gui_forms::Window>(greeter.root, gui_forms::Size{420.0, 360.0});
    gui_forms::ApplicationWindowOptions options{};
    options.title = "GUI.Forms example";
    options.initial_size = {420.0, 360.0};
    options.minimum_size = {420.0, 360.0};
    if (close_when_ready) options.ready = CloseWhenReady{};
    const gui_forms::ApplicationResult result = gui_forms::Application::run(std::move(window), std::move(options));
    if (!result.accepted()) {
        std::cerr << "example_app: the application run failed (error "
                  << static_cast<int>(result.error) << ")\n";
        return 1;
    }
    return 0;
}

} // namespace
} // namespace example_app

// `example_app` opens the form; `example_app --close-when-ready` opens it and
// closes it again at once (the smoke test).
int main(int argc, char** argv) {
    bool close_when_ready = false;
    if (argc == 2 && std::string_view(argv[1]) == "--close-when-ready") {
        close_when_ready = true;
    } else if (argc != 1) {
        std::cerr << "usage: example_app [--close-when-ready]\n";
        return 2;
    }
    try {
        const int status = example_app::run(close_when_ready);
        return status;
    } catch (const std::exception& error) {
        std::cerr << "example_app: " << error.what() << '\n';
        return 1;
    }
}
