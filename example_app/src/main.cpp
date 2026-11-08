#include "example_theme.hpp"
#include "greeter.hpp"
#include "greeter_form.hpp"
#include "greeter_model.hpp"

#include <gui_forms/application.hpp>

#include <exception>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
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

// Prints each theme finding (a warning, or the errors of a refusal).
void report_findings(const std::vector<gui_forms::ThemeCompileDiagnostic>& findings) {
    for (const gui_forms::ThemeCompileDiagnostic& finding : findings) {
        std::cerr << "example_app: " << finding.file << ':' << finding.line << ':' << finding.column << ": "
                  << finding.code << ' ' << finding.name << '\n';
    }
}

int run(const bool close_when_ready) {
    // The application first: it compiles the theme once (GUI.Forms' own
    // theme, then the example's package) and every window made after it
    // joins it.
    gui_forms::ApplicationConfig config{};
    config.theme.base = gui_forms::ThemeBase::gui_forms;
    config.theme.packages.push_back(example_theme());
    gui_forms::ApplicationCreateResult created = gui_forms::Application::create(std::move(config));
    report_findings(created.findings);
    if (!created) {
        std::cerr << "example_app: the application was refused (error " << static_cast<int>(created.error) << ")\n";
        return 1;
    }
    gui_forms::Application& application = *created.application;

    // The model, then the window bound to it, then the behaviour over both:
    // each ends before what it borrows.
    GreeterModel model{};
    std::unique_ptr<GreeterWindow> window = std::make_unique<GreeterWindow>(application, model);
    Greeter greeter(*window, model);

    gui_forms::ApplicationWindowOptions options{};
    options.title = std::string(GreeterWindow::window_title);
    options.initial_size = {420.0, 360.0};
    options.minimum_size = {420.0, 360.0};
    if (close_when_ready) options.ready = CloseWhenReady{};
    const gui_forms::ApplicationResult result = application.run(std::move(window), std::move(options));
    // Once, after the last run: ends the rendering library for the process.
    const gui_forms::ApplicationResult ended = gui_forms::Application::shutdown();
    if (!result.accepted() || !ended.accepted()) {
        std::cerr << "example_app: the application run failed (error " << static_cast<int>(result.error) << ")\n";
        return 1;
    }
    return 0;
}

} // namespace
} // namespace example_app

// `example_app` opens the window; `example_app --close-when-ready` opens it
// and closes it again at once (the smoke test).
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
