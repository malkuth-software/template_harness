#!/usr/bin/env python3
"""Build and test the example app against the pinned GUI.Forms SDK.

    python3 template_harness/example_app/ci_build.py [--configure-only] [--jobs N]

Run from the workspace root (the directory that holds template_harness/),
with LLVM 22.1.8, CMake and Ninja on PATH: the harness's native workflow
provides them (the Windows CLANG64 toolchain GUI.Forms publishes, Homebrew's
llvm@22 with the harness-built macOS runtimes, apt.llvm.org's clang-22).

1. fetch_sdk.py fetches the SDK pinned in gui_forms.lock into the cache
   (--cache, else RUNNER_TEMP/harness-cache in CI, else the user cache).
2. CMake configures example_app/ with the SDK's prefix and the harness's
   harness/cmake/llvm22.cmake into .build/native-<platform>/example-app/.
   On macOS the toolchain file needs the LLVM runtime prefix (in
   <HARNESS_ENV_PREFIX>_LLVM_RUNTIME, else HARNESS_LLVM_RUNTIME): the
   environment's, else the one the SDK carries (llvm-runtime/).
3. The tree is built, and ctest runs its tests (the form's test, and the
   start test when the SDK has a native host; on Linux it needs DISPLAY).

--configure-only stops after step 2, for the state-machine checker, which
reads the tree's compile commands.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys

HERE: Path = Path(__file__).resolve().parent
HARNESS_ROOT: Path = HERE.parent
sys.path.insert(0, str(HERE))
import fetch_sdk  # noqa: E402


def runtime_variable() -> str:
    """The variable harness/cmake/llvm22.cmake reads the macOS runtime prefix from."""
    prefix: str = os.environ.get('HARNESS_ENV_PREFIX', '')
    result: str = prefix + '_LLVM_RUNTIME' if prefix else 'HARNESS_LLVM_RUNTIME'
    return result


def run(command: list[str]) -> None:
    print('+ ' + ' '.join(command), flush=True)
    subprocess.run(command, check=True)


def value_of(lines: list[str], key: str) -> str:
    """The value of a `key=value` line fetch_sdk printed, or '' when it printed none."""
    line: str
    for line in lines:
        if line.startswith(key + '='):
            result: str = line[len(key) + 1:]
            return result
    return ''


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--configure-only', action='store_true')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--cache', type=Path)
    args: argparse.Namespace = parser.parse_args()
    platform: str = fetch_sdk.running_platform()
    cache: Path
    if args.cache is not None:
        cache = args.cache
    elif os.environ.get('RUNNER_TEMP', ''):
        cache = Path(os.environ['RUNNER_TEMP']) / 'harness-cache'
    else:
        cache = fetch_sdk.default_cache()
    try:
        lock: fetch_sdk.Lock = fetch_sdk.read_lock(fetch_sdk.LOCK_PATH)
        lines: list[str] = fetch_sdk.fetch_command(lock, platform, fetch_sdk.checked_cache(cache))
    except fetch_sdk.FetchError as error:
        print('ci_build: ' + str(error), file=sys.stderr)
        return 1
    prefix: str = value_of(lines, 'prefix')
    bundled_runtime: str = value_of(lines, 'llvm_runtime')
    variable: str = runtime_variable()
    if platform.startswith('macos') and bundled_runtime and not os.environ.get(variable, ''):
        os.environ[variable] = bundled_runtime
    tree: Path = Path.cwd() / '.build' / ('native-' + platform) / 'example-app'
    toolchain: Path = HARNESS_ROOT / 'harness' / 'cmake' / 'llvm22.cmake'
    run(['cmake', '-S', HERE.as_posix(), '-B', tree.as_posix(), '-G', 'Ninja',
         '-DCMAKE_BUILD_TYPE=Release',
         '-DCMAKE_PREFIX_PATH=' + prefix,
         '-DCMAKE_TOOLCHAIN_FILE=' + toolchain.as_posix(),
         '-DCMAKE_NO_SYSTEM_FROM_IMPORTED=ON',
         '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'])
    if args.configure_only:
        return 0
    run(['cmake', '--build', tree.as_posix(), '--parallel', str(args.jobs)])
    run(['ctest', '--test-dir', tree.as_posix(), '--output-on-failure'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
