"""Integrity of the prebuilt macOS runtime payload, including configured headers."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import shutil
from typing import Any

VERSION: str = '22.1.8'
SOURCE_SHA256: str = '922f1817a0df7b1489272d18134ee0087a8b068828f87ac63b9861b1a9965888'


def payload_hashes(prefix: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    library: Path
    for library in sorted((prefix / 'lib').glob('*.dylib')):
        if not library.is_symlink():
            result['lib/' + library.name] = hashlib.sha256(library.read_bytes()).hexdigest()
    if len(result) != 3:
        raise RuntimeError('Expected exactly three LLVM runtime libraries')
    link: Path
    for link in sorted((prefix / 'lib').glob('*.dylib')):
        if link.is_symlink():
            result['lib/' + link.name] = hashlib.sha256(str(link.readlink()).encode('utf-8')).hexdigest()
    headers: Path = prefix / 'include/c++/v1'
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
    result['include/c++/v1'] = digest.hexdigest()
    return result


def description(prefix: Path) -> dict[str, object]:
    compiler: str | None = shutil.which('clang++')
    if compiler is None:
        raise RuntimeError('clang++ is missing')
    result: dict[str, object] = {
        'llvm_version': VERSION, 'source_sha256': SOURCE_SHA256,
        'architecture': 'arm64', 'deployment_target': '14.0',
        'payload_sha256': payload_hashes(prefix),
        'sdk': subprocess.check_output(['xcrun', '--show-sdk-version'], text=True).strip(),
        'compiler_sha256': hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
        'compiler': subprocess.check_output(['clang++', '--version'], text=True)}
    return result


def verify(prefix: Path) -> None:
    recorded: object = json.loads((prefix / 'runtime-manifest.json').read_text(encoding='utf-8'))
    current: dict[str, object] = description(prefix)
    if recorded != current:
        raise RuntimeError('Runtime integrity/compiler/SDK mismatch; restore the matching cache or rebuild runtimes')
    print('LLVM runtime payload, compiler and SDK verified')


if __name__ == '__main__':
    verify(Path(sys.argv[1]))
