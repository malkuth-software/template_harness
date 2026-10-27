#!/usr/bin/env python3
"""Reject shipped Mach-O images outside the arm64/macOS 14 runtime contract."""
from __future__ import annotations
from pathlib import Path
import re
import subprocess
import sys


def audit(path: Path) -> bool:
    with path.open('rb') as stream:
        magic: bytes = stream.read(4)
    if magic not in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                      b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
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
    dependencies: str = subprocess.check_output(['otool', '-L', str(path)], text=True)
    line: str
    for line in dependencies.splitlines()[1:]:
        library: str = line.strip().split(' (')[0]
        if ('libc++' in library or 'libunwind' in library) and not library.startswith('@rpath/'):
            raise RuntimeError(str(path) + ': unbundled C++ runtime dependency: ' + library)
    print(str(path) + ': arm64, minimum ' + ','.join(matches))
    return True


def main() -> None:
    count: int = 0
    argument: str
    for argument in sys.argv[1:]:
        root: Path = Path(argument)
        if not root.exists():
            raise RuntimeError('Audit root missing: ' + str(root))
        path: Path
        for path in root.rglob('*'):
            if path.is_symlink() or not path.is_file() or 'CMakeFiles' in path.parts:
                continue
            if audit(path):
                count += 1
    if count == 0:
        raise RuntimeError('No Mach-O images audited')
    print('Audited ' + str(count) + ' Mach-O images')


if __name__ == '__main__':
    main()
