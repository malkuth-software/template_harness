cmake_minimum_required(VERSION 3.25)
project(GUIFormsExampleApp LANGUAGES CXX)

# GUI.Forms comes from an installed SDK: fetch_sdk.py unpacks the release
# pinned in gui_forms.lock and prints its prefix, which goes in
# CMAKE_PREFIX_PATH. Configure with the toolchain file the SDK was built with
# (the harness's harness/cmake/llvm22.cmake).
#
# Controls is the renderer-free toolkit the form's test links. Application is
# the native host, present when the SDK was built with one; it already holds
# the retained engine, so no program here links both (the SDK's one linking
# rule).
find_package(GUIForms 0.2 CONFIG REQUIRED COMPONENTS Controls OPTIONAL_COMPONENTS Application)

enable_testing()

# The form's behaviour, without a native window.
add_executable(greeter_tests src/greeter.cpp tests/greeter_tests.cpp)
target_compile_features(greeter_tests PRIVATE cxx_std_20)
target_include_directories(greeter_tests PRIVATE src)
target_link_libraries(greeter_tests PRIVATE GUIForms::Controls)
# Keep the selected SDK ahead of any other GUI.Forms installation in the
# compiler's normal include search path.
set_target_properties(greeter_tests PROPERTIES NO_SYSTEM_FROM_IMPORTED TRUE)
add_test(NAME greeter_tests COMMAND greeter_tests)
set_tests_properties(greeter_tests PROPERTIES TIMEOUT 30)

if(TARGET GUIForms::Application)
    add_executable(example_app src/greeter.cpp src/main.cpp)
    target_compile_features(example_app PRIVATE cxx_std_20)
    target_include_directories(example_app PRIVATE src)
    target_link_libraries(example_app PRIVATE GUIForms::Application)
    set_target_properties(example_app PROPERTIES NO_SYSTEM_FROM_IMPORTED TRUE)

    # Controls draw only the bundled faces, so the SDK's font directory goes
    # where the host looks: Contents/Resources/fonts in a macOS bundle, and
    # fonts/ beside the executable on Windows and Linux.
    if(APPLE)
        set_target_properties(example_app PROPERTIES MACOSX_BUNDLE TRUE)
        set(example_app_fonts "$<TARGET_BUNDLE_CONTENT_DIR:example_app>/Resources/fonts")
    else()
        set(example_app_fonts "$<TARGET_FILE_DIR:example_app>/fonts")
    endif()
    add_custom_command(TARGET example_app POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_directory "${GUIForms_FONT_DIR}" "${example_app_fonts}")

    # An installed Windows SDK keeps its DLLs in bin/; stage the ones the
    # program actually uses beside it.
    if(WIN32)
        add_custom_command(TARGET example_app POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                $<TARGET_RUNTIME_DLLS:example_app> $<TARGET_FILE_DIR:example_app>
            COMMAND_EXPAND_LISTS)
    endif()

    # Opens the native window and closes it once ready. It needs a display:
    # CI runs it under xvfb-run on Linux.
    add_test(NAME example_app_starts COMMAND example_app --close-when-ready)
    set_tests_properties(example_app_starts PROPERTIES TIMEOUT 60)
endif()
