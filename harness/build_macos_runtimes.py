#!/usr/bin/env python3
"""Build the pinned LLVM runtimes with a real macOS 14 arm64 minimum."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import urllib.request

from macos_runtime_manifest import VERSION, SOURCE_SHA256, description


def run(arguments: list[str]) -> None:
    subprocess.run(arguments, check=True)


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=4)
    args: argparse.Namespace = parser.parse_args()
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
    build: Path = work / 'build'
    run(['cmake', '-S', str(source / 'runtimes'), '-B', str(build), '-G', 'Ninja',
         '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_COMPILER=clang', '-DCMAKE_CXX_COMPILER=clang++',
         '-DCMAKE_ASM_COMPILER=clang', '-DCMAKE_OSX_ARCHITECTURES=arm64',
         '-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0',
         '-DCMAKE_OSX_SYSROOT=' + subprocess.check_output(['xcrun', '--show-sdk-path'], text=True).strip(), '-DCMAKE_INSTALL_PREFIX=' + str(prefix),
         '-DCMAKE_INSTALL_NAME_DIR=@rpath', '-DCMAKE_BUILD_WITH_INSTALL_NAME_DIR=ON',
         '-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind',
         '-DLLVM_INCLUDE_TESTS=OFF', '-DLIBCXX_INCLUDE_TESTS=OFF', '-DLIBCXX_INCLUDE_BENCHMARKS=OFF',
         '-DLIBCXXABI_INCLUDE_TESTS=OFF', '-DLIBUNWIND_INCLUDE_TESTS=OFF',
         '-DLIBCXX_ENABLE_STATIC=OFF', '-DLIBCXXABI_ENABLE_STATIC=OFF',
         '-DLIBUNWIND_ENABLE_STATIC=OFF', '-DLIBCXXABI_USE_LLVM_UNWINDER=ON',
         '-DLIBCXX_ENABLE_VENDOR_AVAILABILITY_ANNOTATIONS=OFF'])
    run(['cmake', '--build', str(build), '--parallel', str(args.jobs)])
    run(['cmake', '--install', str(build)])
    licenses: Path = prefix / 'share/licenses/llvm-runtimes'
    licenses.mkdir(parents=True, exist_ok=True)
    for component in ('libcxx', 'libcxxabi', 'libunwind', 'libc'):
        shutil.copyfile(source / component / 'LICENSE.TXT', licenses / (component + '-LICENSE.TXT'))
    receipt: dict[str, object] = description(prefix)
    (prefix / 'runtime-manifest.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
