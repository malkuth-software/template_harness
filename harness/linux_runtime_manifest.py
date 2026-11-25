"""Integrity of the built Linux runtime payload: the static archives and the configured headers.

The payload (build_linux_runtimes.py) holds position-independent, hermetic
static archives of libc++, libc++abi and libunwind from the pinned LLVM, for
one C library (glibc or musl) and one architecture. A consumer links these
instead of a distribution's archives, so its C++ runtime is the same on every
Linux, and a shared library of its own (a C ABI library) can carry it: a
distribution's archives need not be position independent (Alpine's libc++abi.a
is not). The distribution's packages stay only as the bootstrap compiler.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any

from macos_runtime_manifest import VERSION, SOURCE_SHA256

# The archives the payload carries, and nothing else under lib/.
STATIC_ARCHIVES: tuple[str, ...] = ('libc++.a', 'libc++abi.a', 'libunwind.a')
# The C libraries a payload may be built for.
C_LIBRARIES: tuple[str, ...] = ('glibc', 'musl')


def c_library() -> str:
    """The host's C library: musl when its dynamic loader is present, else glibc."""
    loaders: list[Path] = sorted(Path('/lib').glob('ld-musl-*.so.1'))
    if loaders:
        return 'musl'
    return 'glibc'


def runtime_directory_name(library: str, machine: str) -> str:
    """The payload's directory under .build/toolchain/, one per C library and architecture."""
    if library not in C_LIBRARIES:
        raise RuntimeError('Unknown C library: ' + library)
    return 'llvm-' + VERSION + '-linux-' + machine + '-' + library


def header_digest(headers: Path) -> str:
    if not (headers / '__config_site').is_file():
        raise RuntimeError('Configured libc++ headers are missing')
    digest: Any = hashlib.sha256()
    header: Path
    for header in sorted(headers.rglob('*')):
        if header.is_file():
            digest.update(header.relative_to(headers).as_posix().encode('utf-8'))
            digest.update(b'\0')
            digest.update(header.read_bytes())
            digest.update(b'\0')
    return digest.hexdigest()


def payload_hashes(prefix: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    name: str
    for name in STATIC_ARCHIVES:
        archive: Path = prefix / 'lib' / name
        if not archive.is_file() or archive.is_symlink():
            raise RuntimeError('Static LLVM runtime archive is missing: lib/' + name)
        result['lib/' + name] = hashlib.sha256(archive.read_bytes()).hexdigest()
    # A shared runtime library in the payload would let a consumer link it by
    # accident: the payload is static only.
    shared: list[Path] = sorted((prefix / 'lib').glob('*.so*'))
    if shared:
        raise RuntimeError('The Linux runtime payload holds a shared library: ' + shared[0].name)
    result['include/c++/v1'] = header_digest(prefix / 'include/c++/v1')
    return result


def description(prefix: Path, library: str | None = None) -> dict[str, object]:
    compiler: str | None = shutil.which('clang++')
    if compiler is None:
        raise RuntimeError('clang++ is missing')
    chosen: str = library if library is not None else c_library()
    if chosen not in C_LIBRARIES:
        raise RuntimeError('Unknown C library: ' + chosen)
    result: dict[str, object] = {
        'llvm_version': VERSION, 'source_sha256': SOURCE_SHA256,
        'architecture': platform.machine(), 'c_library': chosen,
        'position_independent': True, 'hermetic': True,
        'payload_sha256': payload_hashes(prefix),
        'compiler_sha256': hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
        'compiler': subprocess.check_output(['clang++', '--version'], text=True)}
    return result


def verify(prefix: Path) -> None:
    recorded: object = json.loads((prefix / 'runtime-manifest.json').read_text(encoding='utf-8'))
    library: str | None = None
    if isinstance(recorded, dict) and isinstance(recorded.get('c_library'), str):
        library = str(recorded['c_library'])
    if library is not None and library != c_library():
        raise RuntimeError('Runtime built for ' + library + ', host C library is ' + c_library())
    current: dict[str, object] = description(prefix, library)
    if recorded != current:
        raise RuntimeError('Runtime integrity/compiler mismatch; restore the matching cache or rebuild runtimes')
    print('LLVM Linux runtime payload and compiler verified')


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == '--directory-name':
        print(runtime_directory_name(c_library(), platform.machine()))
    elif len(sys.argv) == 2:
        verify(Path(sys.argv[1]))
    else:
        print('usage: linux_runtime_manifest.py PREFIX | --directory-name', file=sys.stderr)
        raise SystemExit(2)
