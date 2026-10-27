#!/usr/bin/env python3
"""Emit a compiler/SDK identity; source identity is a separate cache dimension."""
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import TextIO


def main() -> None:
    version: str = subprocess.check_output(['clang', '--version'], text=True)
    if re.search(r'clang version 22\.1\.8(?:\s|$)', version) is None:
        raise RuntimeError('Expected LLVM 22.1.8; got ' + version)
    compiler_path: str | None = shutil.which('clang')
    if compiler_path is None:
        raise RuntimeError('clang is missing')
    compiler_hash: str = hashlib.sha256(Path(compiler_path).read_bytes()).hexdigest()
    commands: list[list[str]] = [['clang', '--version'], ['ccache', '--version']]
    if platform.system() == 'Darwin':
        commands.append(['xcrun', '--show-sdk-version'])
    values: list[str] = [compiler_hash, os.environ.get('ImageVersion', ''), platform.machine()]
    command: list[str]
    for command in commands:
        values.append(subprocess.check_output(command, text=True))
    text: str = '\n'.join(values)
    digest: str = hashlib.sha256(text.encode('utf-8')).hexdigest()
    environment_commands: list[list[str]] = [['cmake', '--version'], ['ninja', '--version']]
    if platform.system() == 'Darwin':
        environment_commands.append(['brew', 'list', '--versions'])
    elif platform.system() == 'Windows':
        environment_commands.append(['pacman', '-Q'])
    else:
        environment_commands.append(['dpkg-query', '-W', '-f=${binary:Package}=${Version}\n'])
    for command in environment_commands:
        values.append(subprocess.check_output(command, text=True))
    environment: str = hashlib.sha256('\n'.join(values).encode('utf-8')).hexdigest()
    destination: Path = Path(os.environ['GITHUB_OUTPUT'])
    stream: TextIO
    with destination.open('a', encoding='utf-8') as stream:
        stream.write('identity=' + digest + '\n')
        stream.write('version=22.1.8\n')
        stream.write('environment=' + environment + '\n')


if __name__ == '__main__':
    main()
