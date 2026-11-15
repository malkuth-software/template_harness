# early_exit_check: the early-exit integrity check's clang tool
# (docs/early_exit_check.md). Built by harness/check_early_exit.py against
# the pinned LLVM 22's Clang libraries:
#
#   cmake -S harness/early_exit -B <dir> -G Ninja -DCMAKE_PREFIX_PATH=<llvm prefix>
#         -DCMAKE_CXX_COMPILER=<llvm prefix>/bin/clang++
#
# The tool links the same LLVM it is compiled with; the product's toolchain
# file (cmake/llvm22.cmake) is not used, since the tool links that LLVM's own
# libraries and C++ runtime rather than a product runtime.
cmake_minimum_required(VERSION 3.24)
project(early_exit_check LANGUAGES CXX)

find_package(Clang 22 REQUIRED CONFIG)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(early_exit_check early_exit_check.cpp)
target_include_directories(early_exit_check SYSTEM PRIVATE ${LLVM_INCLUDE_DIRS} ${CLANG_INCLUDE_DIRS})
# LLVM_DEFINITIONS is one space-separated string of -D flags.
separate_arguments(early_exit_definitions NATIVE_COMMAND "${LLVM_DEFINITIONS}")
target_compile_definitions(early_exit_check PRIVATE ${early_exit_definitions})
if(NOT LLVM_ENABLE_RTTI)
    target_compile_options(early_exit_check PRIVATE -fno-rtti)
endif()
# clang-cpp is the single shared library distributions ship (Homebrew,
# apt.llvm.org, MSYS2 CLANG64); the component libraries are the fallback.
if(TARGET clang-cpp)
    set(early_exit_libraries clang-cpp)
    if(TARGET LLVM)
        list(APPEND early_exit_libraries LLVM)
    else()
        list(APPEND early_exit_libraries LLVMSupport)
    endif()
else()
    set(early_exit_libraries clangTooling clangFrontend clangAST clangBasic clangLex LLVMSupport)
endif()
target_link_libraries(early_exit_check PRIVATE ${early_exit_libraries})
