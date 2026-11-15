# Pass to the provider AND consumer: -DCMAKE_TOOLCHAIN_FILE=.../llvm22.cmake.
# CC/CXX names intentionally match ccache's content-checked compiler identity.
# On macOS and Linux the libc++ runtime prefix comes from <prefix>_LLVM_RUNTIME,
# where <prefix> is the environment's HARNESS_ENV_PREFIX (the project file's
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
    # One module per program: when the runtime tree carries the static
    # archives (build_macos_runtimes.py builds them, hermetic), libc++,
    # libc++abi and libunwind are linked statically, as on Linux and Windows,
    # and a program depends only on system libraries. A tree with dylibs only
    # is linked through them and the rpath. HARNESS_STATIC_CXX_RUNTIME says
    # which, for checks that refuse a shared C++ runtime.
    if(EXISTS "${_harness_runtime}/lib/libc++.a" AND EXISTS "${_harness_runtime}/lib/libc++abi.a"
       AND EXISTS "${_harness_runtime}/lib/libunwind.a")
        set(_harness_link "-nostdlib++")
        set(CMAKE_CXX_STANDARD_LIBRARIES_INIT
            "\"${_harness_runtime}/lib/libc++.a\" \"${_harness_runtime}/lib/libc++abi.a\" \"${_harness_runtime}/lib/libunwind.a\"")
        set(CMAKE_OBJCXX_STANDARD_LIBRARIES_INIT "${CMAKE_CXX_STANDARD_LIBRARIES_INIT}")
        set(HARNESS_STATIC_CXX_RUNTIME ON CACHE BOOL "The C++ runtime is linked statically (harness/cmake/llvm22.cmake)" FORCE)
    else()
        set(_harness_link "-L\"${_harness_runtime}/lib\" -Wl,-rpath,\"${_harness_runtime}/lib\" -lunwind")
        set(HARNESS_STATIC_CXX_RUNTIME OFF CACHE BOOL "The C++ runtime is linked statically (harness/cmake/llvm22.cmake)" FORCE)
    endif()
    set(CMAKE_EXE_LINKER_FLAGS_INIT "${_harness_link}")
    set(CMAKE_SHARED_LINKER_FLAGS_INIT "${_harness_link}")
    set(CMAKE_MODULE_LINKER_FLAGS_INIT "${_harness_link}")
elseif(CMAKE_HOST_SYSTEM_NAME STREQUAL "Linux")
    # The C++ runtime is the harness's own on every Linux, glibc or musl:
    # build_linux_runtimes.py builds libc++, libc++abi and libunwind from the
    # pinned LLVM as position-independent, hermetic static archives, so a
    # shared library (a C ABI library) can carry them and exports none of
    # their symbols. A distribution's archives are refused: they need not be
    # position independent (Alpine's libc++abi.a is not) and differ between
    # distributions. The distribution's clang is only the bootstrap compiler.
    if(DEFINED ENV{HARNESS_ENV_PREFIX})
        set(_harness_runtime_variable "$ENV{HARNESS_ENV_PREFIX}_LLVM_RUNTIME")
    else()
        set(_harness_runtime_variable HARNESS_LLVM_RUNTIME)
    endif()
    if(NOT DEFINED ENV{${_harness_runtime_variable}})
        message(FATAL_ERROR "Set ${_harness_runtime_variable} to the LLVM 22.1.8 Linux runtime prefix "
                            "(harness/build_linux_runtimes.py)")
    endif()
    file(TO_CMAKE_PATH "$ENV{${_harness_runtime_variable}}" _harness_runtime)
    foreach(_harness_required IN ITEMS lib/libc++.a lib/libc++abi.a lib/libunwind.a include/c++/v1/__config_site
                                       runtime-manifest.json)
        if(NOT EXISTS "${_harness_runtime}/${_harness_required}")
            message(FATAL_ERROR "${_harness_runtime_variable} (${_harness_runtime}) lacks ${_harness_required}; "
                                "build it with harness/build_linux_runtimes.py")
        endif()
    endforeach()
    set(_harness_cxx "-stdlib=libc++ -nostdinc++ -isystem \"${_harness_runtime}/include/c++/v1\"")
    set(CMAKE_CXX_FLAGS_INIT "${_harness_cxx}")
    # -nostdlib++ also drops the -lm the driver adds for C++; libc++ needs it.
    set(_harness_link "-nostdlib++")
    set(CMAKE_CXX_STANDARD_LIBRARIES_INIT
        "\"${_harness_runtime}/lib/libc++.a\" \"${_harness_runtime}/lib/libc++abi.a\" \"${_harness_runtime}/lib/libunwind.a\" -lm")
    set(HARNESS_STATIC_CXX_RUNTIME ON CACHE BOOL "The C++ runtime is linked statically (harness/cmake/llvm22.cmake)" FORCE)
    set(CMAKE_EXE_LINKER_FLAGS_INIT "${_harness_link}")
    set(CMAKE_SHARED_LINKER_FLAGS_INIT "${_harness_link}")
    set(CMAKE_MODULE_LINKER_FLAGS_INIT "${_harness_link}")
endif()
