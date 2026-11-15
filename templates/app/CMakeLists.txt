cmake_minimum_required(VERSION 3.25)
project({{App}} LANGUAGES CXX)

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

# A composite control and a form that places it. controls/greeting-card.control
# is a control written as a document; gui_forms_add_control generates its
# class, GreetingCard. forms/welcome.form names it like any control and fills
# its slot, and gui_forms_add_form compiles it with the card listed in
# COMPOSITES. gui_forms_add_form_schema writes the form schema a designer reads
# (every type the form may name, the card's slot and properties included).
# Composite controls arrived with GUI.Forms cp34: an SDK without them skips
# this block.
if(COMMAND gui_forms_add_form_schema AND TARGET GUIForms::form_compiler)
    set({{app}}_composites
        "controls/greeting-card.control,{{namespace}}::GreetingCard,greeting_card_control.hpp")
    add_executable(welcome_tests tests/welcome_tests.cpp)
    target_compile_features(welcome_tests PRIVATE cxx_std_20)
    target_link_libraries(welcome_tests PRIVATE GUIForms::Controls)
    set_target_properties(welcome_tests PROPERTIES NO_SYSTEM_FROM_IMPORTED TRUE)
    gui_forms_add_control(welcome_tests controls/greeting-card.control CLASS GreetingCard NAMESPACE {{namespace}})
    gui_forms_add_form(welcome_tests forms/welcome.form CLASS WelcomePanel NAMESPACE {{namespace}} DERIVED
        COMPOSITES ${{{app}}_composites})
    gui_forms_add_form_schema(welcome_tests OUTPUT {{app}}_form_schema.json COMPOSITES ${{{app}}_composites})
    add_test(NAME welcome_tests COMMAND welcome_tests)
    set_tests_properties(welcome_tests PROPERTIES TIMEOUT 30)
endif()

if(TARGET GUIForms::Application)
    add_executable({{app}} src/greeter.cpp src/main.cpp)
    target_compile_features({{app}} PRIVATE cxx_std_20)
    target_include_directories({{app}} PRIVATE src)
    target_link_libraries({{app}} PRIVATE GUIForms::Application)
    set_target_properties({{app}} PROPERTIES NO_SYSTEM_FROM_IMPORTED TRUE)

    # Controls draw only the bundled faces, so the SDK's font directory goes
    # where the host looks: Contents/Resources/fonts in a macOS bundle, and
    # fonts/ beside the executable on Windows and Linux.
    if(APPLE)
        set_target_properties({{app}} PROPERTIES MACOSX_BUNDLE TRUE)
        set({{app}}_fonts "$<TARGET_BUNDLE_CONTENT_DIR:{{app}}>/Resources/fonts")
    else()
        set({{app}}_fonts "$<TARGET_FILE_DIR:{{app}}>/fonts")
    endif()
    add_custom_command(TARGET {{app}} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_directory "${GUIForms_FONT_DIR}" "${{{app}}_fonts}")

    # An installed Windows SDK keeps its DLLs in bin/; stage the ones the
    # program actually uses beside it.
    if(WIN32)
        add_custom_command(TARGET {{app}} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                $<TARGET_RUNTIME_DLLS:{{app}}> $<TARGET_FILE_DIR:{{app}}>
            COMMAND_EXPAND_LISTS)
    endif()

    # Opens the native window and closes it once ready. It needs a display:
    # CI runs it under xvfb-run on Linux.
    add_test(NAME {{app}}_starts COMMAND {{app}} --close-when-ready)
    set_tests_properties({{app}}_starts PROPERTIES TIMEOUT 60)
endif()
