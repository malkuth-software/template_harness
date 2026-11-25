#!/usr/bin/env python3
"""Build the pinned LLVM runtimes for Linux as position-independent static archives.

The prefix holds libc++, libc++abi and libunwind as static archives only, for
the host's C library (glibc or musl, detected; --libc overrides) and
architecture. They are position independent, so a shared library of the
consumer's own (a C ABI library) can link them, and hermetic (hidden
visibility), so it exports none of their symbols. A consumer links them
instead of its distribution's archives: the runtime is then the same on every
Linux. The distribution's clang only compiles them (the bootstrap compiler).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import urllib.request

from linux_runtime_manifest import C_LIBRARIES, VERSION, SOURCE_SHA256, c_library, description


def run(arguments: list[str]) -> None:
    subprocess.run(arguments, check=True)


def configure_arguments(source: Path, build: Path, prefix: Path, library: str) -> list[str]:
    """The runtimes build's configuration: static, position independent, hermetic."""
    arguments: list[str] = [
        'cmake', '-S', str(source / 'runtimes'), '-B', str(build), '-G', 'Ninja',
        '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_COMPILER=clang', '-DCMAKE_CXX_COMPILER=clang++',
        '-DCMAKE_ASM_COMPILER=clang', '-DCMAKE_INSTALL_PREFIX=' + str(prefix),
        '-DCMAKE_POSITION_INDEPENDENT_CODE=ON',
        '-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind',
        '-DLLVM_INCLUDE_TESTS=OFF', '-DLIBCXX_INCLUDE_TESTS=OFF', '-DLIBCXX_INCLUDE_BENCHMARKS=OFF',
        '-DLIBCXXABI_INCLUDE_TESTS=OFF', '-DLIBUNWIND_INCLUDE_TESTS=OFF',
        '-DLIBCXX_ENABLE_SHARED=OFF', '-DLIBCXXABI_ENABLE_SHARED=OFF', '-DLIBUNWIND_ENABLE_SHARED=OFF',
        '-DLIBCXX_ENABLE_STATIC=ON', '-DLIBCXXABI_ENABLE_STATIC=ON', '-DLIBUNWIND_ENABLE_STATIC=ON',
        '-DLIBCXX_HERMETIC_STATIC_LIBRARY=ON', '-DLIBCXXABI_HERMETIC_STATIC_LIBRARY=ON',
        '-DLIBUNWIND_HIDE_SYMBOLS=ON',
        # Three separate archives, as on macOS: a program links all three.
        '-DLIBCXXABI_STATICALLY_LINK_UNWINDER_IN_STATIC_LIBRARY=OFF',
        '-DLIBCXX_STATICALLY_LINK_ABI_IN_STATIC_LIBRARY=OFF',
        '-DLIBCXXABI_USE_LLVM_UNWINDER=ON',
        '-DLIBCXX_CXX_ABI=libcxxabi']
    if library == 'musl':
        arguments.append('-DLIBCXX_HAS_MUSL_LIBC=ON')
    return arguments


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--libc', choices=C_LIBRARIES, default=None)
    parser.add_argument('--jobs', type=int, default=4)
    args: argparse.Namespace = parser.parse_args()
    library: str = args.libc if args.libc is not None else c_library()
    if library != c_library():
        raise RuntimeError('Build the ' + library + ' runtimes on a ' + library + ' host')
    work: Path = args.work.resolve()
    prefix: Path = args.prefix.resolve()
    work.mkdir(parents=True, exist_ok=True)
    filename: str = 'llvm-project-' + VERSION + '.src.tar.xz'
    archive: Path = work / filename
    if not archive.exists():
        urllib.request.urlretrieve('https://github.com/llvm/llvm-project/releases/download/llvmorg-' +
                                   VERSION + '/' + filename, archive)
    with archive.open('rb') as stream:
        digest: str = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != SOURCE_SHA256:
        raise RuntimeError('LLVM source SHA-256 mismatch')
    source: Path = work / ('llvm-project-' + VERSION + '.src')
    entries: list[str] = ['tar', '-xf', str(archive), '-C', str(work)]
    component: str
    for component in ('runtimes', 'cmake', 'libcxx', 'libcxxabi', 'libunwind', 'llvm/cmake', 'libc'):
        if not (source / component).exists() or (component == 'libc' and not (source / 'libc/CMakeLists.txt').exists()):
            entries.append(source.name + '/' + component)
    if len(entries) > 5:
        run(entries)
    build: Path = work / ('build-linux-' + library)
    run(configure_arguments(source, build, prefix, library))
    run(['cmake', '--build', str(build), '--parallel', str(args.jobs)])
    run(['cmake', '--install', str(build)])
    licenses: Path = prefix / 'share/licenses/llvm-runtimes'
    licenses.mkdir(parents=True, exist_ok=True)
    for component in ('libcxx', 'libcxxabi', 'libunwind', 'libc'):
        shutil.copyfile(source / component / 'LICENSE.TXT', licenses / (component + '-LICENSE.TXT'))
    receipt: dict[str, object] = description(prefix, library)
    (prefix / 'runtime-manifest.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
