#!/usr/bin/env python3
"""Reject shipped Mach-O images outside the arm64/macOS 14 runtime contract.

Every image is arm64 with a minimum of macOS 14.0 at most. A program or
library image may depend on the C++ runtime (libc++, libunwind) only through
@rpath, so that the runtime it ships with is the one it loads.

A compiler plugin is the exception, and is named with --compiler-plugin. The
compiler loads it into its own process, where the compiler's C++ runtime is
already loaded: the plugin exchanges C++ objects with the compiler and must
use that runtime, never a bundled copy of its own (two copies of libc++ in one
process do not share their allocations, exceptions or type information). Its
C++ runtime dependencies must therefore be exactly those of the loading
compiler (--compiler), read from the compiler image's own load commands. Its
architecture and minimum are checked like any other image's.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import subprocess
import sys


MACH_O_MAGIC: tuple[bytes, ...] = (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                                   b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca')


def is_mach_o(path: Path) -> bool:
    with path.open('rb') as stream:
        magic: bytes = stream.read(4)
    result: bool = magic in MACH_O_MAGIC
    return result


def is_cxx_runtime(library: str) -> bool:
    result: bool = 'libc++' in library or 'libunwind' in library
    return result


def cxx_runtime_dependencies(path: Path) -> list[str]:
    """The libc++ and libunwind install names `path` links, in load order."""
    listing: str = subprocess.check_output(['otool', '-L', str(path)], text=True)
    libraries: list[str] = []
    line: str
    # The first line names the image itself.
    for line in listing.splitlines()[1:]:
        library: str = line.strip().split(' (')[0]
        if is_cxx_runtime(library):
            libraries.append(library)
    return libraries


def check_cxx_runtime(path: Path, dependencies: list[str], compiler_runtime: list[str] | None) -> None:
    """Raise unless `path`'s C++ runtime `dependencies` follow its class's rule.

    `compiler_runtime` is None for a program or library image, whose runtime
    must be bundled (@rpath), and the loading compiler's runtime for a
    compiler plugin, which must equal it.
    """
    library: str
    if compiler_runtime is None:
        for library in dependencies:
            if not library.startswith('@rpath/'):
                raise RuntimeError(str(path) + ': unbundled C++ runtime dependency: ' + library)
        return
    if sorted(dependencies) != sorted(compiler_runtime):
        raise RuntimeError(str(path) + ': compiler plugin C++ runtime ' + repr(dependencies)
                           + " differs from the loading compiler's " + repr(compiler_runtime))


def audit(path: Path, compiler_runtime: list[str] | None = None) -> bool:
    """Audit one file; False when it is not a Mach-O image.

    `compiler_runtime` as for check_cxx_runtime.
    """
    if not is_mach_o(path):
        return False
    architectures: str = subprocess.check_output(['lipo', '-archs', str(path)], text=True).strip()
    if architectures != 'arm64':
        raise RuntimeError(str(path) + ': expected arm64, got ' + architectures)
    commands: str = subprocess.check_output(['otool', '-l', str(path)], text=True)
    matches: list[str] = re.findall(r'\bminos\s+([0-9.]+)', commands)
    if not matches:
        raise RuntimeError(str(path) + ': missing LC_BUILD_VERSION minimum')
    version: str
    for version in matches:
        parts: list[int] = []
        part: str
        for part in version.split('.'):
            parts.append(int(part))
        if parts[0] > 14 or (parts[0] == 14 and any(parts[1:])):
            raise RuntimeError(str(path) + ': minimum exceeds macOS 14.0: ' + version)
    dependencies: list[str] = cxx_runtime_dependencies(path)
    check_cxx_runtime(path, dependencies, compiler_runtime)
    kind: str = '' if compiler_runtime is None else ' (compiler plugin)'
    print(str(path) + ': arm64, minimum ' + ','.join(matches) + kind)
    return True


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('roots', nargs='+', type=Path, help='directories whose Mach-O images are audited')
    parser.add_argument('--compiler-plugin', action='append', default=[], metavar='NAME',
                        help='file name of a compiler plugin (repeatable); no root need hold it')
    parser.add_argument('--compiler', type=Path,
                        help='the compiler that loads the --compiler-plugin images (required with them)')
    options: argparse.Namespace = parser.parse_args(arguments)
    if options.compiler_plugin and options.compiler is None:
        parser.error('--compiler-plugin needs --compiler, the compiler that loads the plugin')
    return options


def main(arguments: list[str]) -> None:
    options: argparse.Namespace = parse_arguments(arguments)
    plugin_names: set[str] = set(options.compiler_plugin)
    # The loading compiler's runtime, read once; unused without plugins.
    compiler_runtime: list[str] = []
    if plugin_names:
        # Through its links (clang++ -> clang -> clang-22) to the image.
        compiler: Path = Path(options.compiler).resolve()
        if not compiler.is_file() or not is_mach_o(compiler):
            raise RuntimeError('--compiler is not a Mach-O image: ' + str(options.compiler))
        compiler_runtime = cxx_runtime_dependencies(compiler)
    count: int = 0
    root: Path
    for root in options.roots:
        if not root.exists():
            raise RuntimeError('Audit root missing: ' + str(root))
        path: Path
        for path in root.rglob('*'):
            if path.is_symlink() or not path.is_file() or 'CMakeFiles' in path.parts:
                continue
            is_plugin: bool = path.name in plugin_names
            audited: bool = audit(path, compiler_runtime) if is_plugin else audit(path)
            if audited:
                count += 1
    if count == 0:
        raise RuntimeError('No Mach-O images audited')
    print('Audited ' + str(count) + ' Mach-O images')


if __name__ == '__main__':
    main(sys.argv[1:])
