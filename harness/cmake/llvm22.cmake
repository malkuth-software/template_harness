# Pass to the provider AND consumer: -DCMAKE_TOOLCHAIN_FILE=.../llvm22.cmake.
# CC/CXX names intentionally match ccache's content-checked compiler identity.
# On macOS the libc++ runtime prefix comes from <prefix>_LLVM_RUNTIME, where
# <prefix> is the environment's HARNESS_ENV_PREFIX (the project file's
# env_prefix: GUI_FORMS_LLVM_RUNTIME for GUI.Forms), else HARNESS_LLVM_RUNTIME.
# An environment variable, not a cache entry, so try_compile sees it too.
find_program(_harness_clang NAMES clang REQUIRED)
find_program(_harness_clangxx NAMES clang++ REQUIRED)
set(CMAKE_C_COMPILER "${_harness_clang}" CACHE FILEPATH "LLVM C driver")
set(CMAKE_CXX_COMPILER "${_harness_clangxx}" CACHE FILEPATH "LLVM C++ driver")
set(CMAKE_OBJCXX_COMPILER "${_harness_clangxx}" CACHE FILEPATH "LLVM Objective-C++ driver")
if(CMAKE_HOST_SYSTEM_NAME STREQUAL "Darwin")
    execute_process(COMMAND xcrun --show-sdk-path OUTPUT_VARIABLE _harness_sdk
                    OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
    set(CMAKE_OSX_SYSROOT "${_harness_sdk}" CACHE PATH "Selected Xcode SDK")
    set(CMAKE_OSX_ARCHITECTURES arm64 CACHE STRING "Supported Apple architecture")
    set(CMAKE_OSX_DEPLOYMENT_TARGET 14.0 CACHE STRING "Minimum supported macOS")
    if(DEFINED ENV{HARNESS_ENV_PREFIX})
        set(_harness_runtime_variable "$ENV{HARNESS_ENV_PREFIX}_LLVM_RUNTIME")
    else()
        set(_harness_runtime_variable HARNESS_LLVM_RUNTIME)
    endif()
    if(NOT DEFINED ENV{${_harness_runtime_variable}})
        message(FATAL_ERROR "Set ${_harness_runtime_variable} to the LLVM 22.1.8 macOS 14 runtime prefix")
    endif()
    file(TO_CMAKE_PATH "$ENV{${_harness_runtime_variable}}" _harness_runtime)
    set(_harness_cxx "-stdlib=libc++ -nostdinc++ -isystem \"${_harness_runtime}/include/c++/v1\"")
    set(CMAKE_CXX_FLAGS_INIT "${_harness_cxx}")
    set(CMAKE_OBJCXX_FLAGS_INIT "${_harness_cxx}")
    set(_harness_link "-L\"${_harness_runtime}/lib\" -Wl,-rpath,\"${_harness_runtime}/lib\" -lunwind")
    set(CMAKE_EXE_LINKER_FLAGS_INIT "${_harness_link}")
    set(CMAKE_SHARED_LINKER_FLAGS_INIT "${_harness_link}")
    set(CMAKE_MODULE_LINKER_FLAGS_INIT "${_harness_link}")
endif()
