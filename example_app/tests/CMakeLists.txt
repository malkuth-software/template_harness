# The form's behaviour without a native window: GUIForms::Controls only, the
# form and the theme generated into this directory's build tree.
add_executable(greeter_tests ../src/greeter.cpp greeter_tests.cpp)
target_compile_features(greeter_tests PRIVATE cxx_std_20)
target_include_directories(greeter_tests PRIVATE ../src)
target_link_libraries(greeter_tests PRIVATE GUIForms::Controls)
# Keep the selected SDK ahead of any other GUI.Forms installation in the
# compiler's normal include search path.
set_target_properties(greeter_tests PROPERTIES NO_SYSTEM_FROM_IMPORTED TRUE)
gui_forms_add_form(greeter_tests ../forms/greeter.form CLASS GreeterWindow NAMESPACE example_app
                   MODEL example_app::GreeterModel MODEL_HEADER greeter_model.hpp)
gui_forms_add_theme(greeter_tests ../themes/example FUNCTION example_theme NAMESPACE example_app
                    FORMS ../forms/greeter.form)
add_test(NAME greeter_tests COMMAND greeter_tests)
set_tests_properties(greeter_tests PROPERTIES TIMEOUT 30)
