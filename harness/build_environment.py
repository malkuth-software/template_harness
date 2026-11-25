#!/usr/bin/env python3
"""CI's build environment for a local workspace: what makes a restored compiler cache hit.

restore_local.py env writes it, bootstrap.py runs its later steps in it, and a
shell loads it before building:

    .ci/local-build-env.json   the variables, for bootstrap.py ("" = unset)
    .ci/local-build-env.sh     . .ci/local-build-env.sh in a POSIX shell
    .ci/local-build-env.ps1    . .ci/local-build-env.ps1 in PowerShell

The variables:

- the ccache settings CI's build has: CCACHE_DIR (<workspace>/.ccache),
  CCACHE_BASEDIR (the workspace), and CCACHE_COMPILERCHECK and
  CCACHE_SLOPPINESS as the restored cache-manifest.json records them under
  "environment" (cache_manifest.py), else as CI set them before it recorded
  them (RECORDED_BEFORE); the compiler launchers;
- the locale CI's shell had, as the manifest records it: ccache hashes LANG,
  LC_ALL, LC_CTYPE and LC_MESSAGES into every key unless the sloppiness
  holds "locale" (GUI.Forms' CI sets it since the cache bootstrap: clang
  translates no message, so the locale changes no output);
- the restored Windows toolchain first on PATH;
- <PREFIX>_LLVM_RUNTIME, the C++ runtime under .build/toolchain/ when
  exactly one holds a runtime-manifest.json (the macOS runtime in the cache
  archive, the Linux runtime in the runtime archive);
- the project's [bootstrap] environment, then its [bootstrap.<platform>]
  environment, {workspace} becoming the workspace root;
- each NAME=VALUE setting given (restore_local.py env --set; bootstrap.py
  passes the lines its prepare commands print).

compatibility() names what still makes the cache miss on this machine: a
compiler that differs from the producer's byte for byte (CCACHE_COMPILERCHECK
content), or another macOS SDK version.

This module imports nothing that needs a Windows toolchain specification, so
bootstrap.py can apply the record with any pinned harness.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any, BinaryIO

import project

BUILD_ENVIRONMENT_RECORD: str = '.ci/local-build-env.json'
BUILD_ENVIRONMENT_POWERSHELL: str = '.ci/local-build-env.ps1'
BUILD_ENVIRONMENT_SHELL: str = '.ci/local-build-env.sh'
# The restored Windows toolchain's PATH directories, one per line, first first
# (restore_local.py toolchain writes it).
TOOLCHAIN_PATH_RECORD: str = '.ci/local-toolchain-path.txt'
CACHE_MANIFEST: str = 'cache-manifest.json'
RUNTIME_DIRECTORY: str = '.build/toolchain'
RUNTIME_MANIFEST: str = 'runtime-manifest.json'
# A restored runtime that did not verify, set aside by restore_local.py runtime.
UNVERIFIED_SUFFIX: str = '.unverified'
# The locale variables ccache hashes into every key (ccache's hash_common_info).
LOCALE_VARIABLES: tuple[str, ...] = ('LANG', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES')
# The ccache settings cache_manifest.py records under "environment" (with the locale).
CCACHE_VARIABLES: tuple[str, ...] = ('CCACHE_COMPILERCHECK', 'CCACHE_SLOPPINESS')
RECORDED_VARIABLES: tuple[str, ...] = CCACHE_VARIABLES + LOCALE_VARIABLES
# What CI set before its manifests recorded an environment (GUI.Forms native-platform.yml
# until the cache bootstrap): these settings, and on Windows the msys2 shell's LANG, the
# only locale measured (Windows field test, 2026-11-24). A variable left out is unset.
RECORDED_BEFORE: dict[str, dict[str, str]] = {
    'windows-x64': {'CCACHE_COMPILERCHECK': 'content', 'CCACHE_SLOPPINESS': 'pch_defines,time_macros',
                    'LANG': 'en_US.UTF-8'},
    'linux-x64': {'CCACHE_COMPILERCHECK': 'content', 'CCACHE_SLOPPINESS': 'pch_defines,time_macros'},
    'linux-arm64': {'CCACHE_COMPILERCHECK': 'content', 'CCACHE_SLOPPINESS': 'pch_defines,time_macros'},
    'macos-arm64': {'CCACHE_COMPILERCHECK': 'content', 'CCACHE_SLOPPINESS': 'pch_defines,time_macros'}}
LAUNCHER_VARIABLES: tuple[str, ...] = ('CMAKE_C_COMPILER_LAUNCHER', 'CMAKE_CXX_COMPILER_LAUNCHER',
                                       'CMAKE_OBJCXX_COMPILER_LAUNCHER')
ENVIRONMENT_NAME: re.Pattern[str] = re.compile(r'[A-Z][A-Z0-9_]*')
WORKSPACE_PLACEHOLDER: str = '{workspace}'
# In the record's PATH: the PATH of the shell or step that applies it.
PATH_PLACEHOLDER: str = '${PATH}'


class BuildEnvironmentError(ValueError):
    """A setting that cannot enter the environment (restore_local prints it and exits 1)."""


def read_manifest(workspace: Path) -> dict[str, Any]:
    """The restored cache-manifest.json, or an empty record when there is none."""
    path: Path = workspace / CACHE_MANIFEST
    if not path.is_file():
        return {}
    value: Any = json.loads(path.read_text(encoding='utf-8'))
    manifest: dict[str, Any] = value if isinstance(value, dict) else {}
    return manifest


def recorded_environment(manifest: dict[str, Any], platform: str) -> dict[str, str]:
    """The ccache settings and locale the manifest records ("" for one CI left unset), else RECORDED_BEFORE's."""
    recorded: Any = manifest.get('environment')
    if not isinstance(recorded, dict):
        return dict(RECORDED_BEFORE.get(platform, {}))
    values: dict[str, str] = {}
    name: str
    for name in RECORDED_VARIABLES:
        value: Any = recorded.get(name)
        values[name] = '' if value is None else str(value)
    return values


def runtime_prefix(workspace: Path) -> Path | None:
    """The one directory under .build/toolchain/ holding a runtime-manifest.json, or None (none, or several);
    one set aside as unverified does not count."""
    directory: Path = workspace / RUNTIME_DIRECTORY
    if not directory.is_dir():
        return None
    found: list[Path] = sorted(manifest.parent for manifest in directory.glob('*/' + RUNTIME_MANIFEST)
                               if not manifest.parent.name.endswith(UNVERIFIED_SUFFIX))
    prefix: Path | None = found[0] if len(found) == 1 else None
    return prefix


def build_environment(product: project.Project, platform: str, settings: list[tuple[str, str]]) -> dict[str, str]:
    """The variables of CI's build environment for this workspace (the module docstring's list)."""
    workspace: Path = product.workspace_root()
    base: str = workspace.as_posix()
    values: dict[str, str] = {'CCACHE_DIR': (workspace / '.ccache').as_posix(), 'CCACHE_BASEDIR': base}
    recorded: dict[str, str] = recorded_environment(read_manifest(workspace), platform)
    name: str
    for name in RECORDED_VARIABLES:
        values[name] = recorded.get(name, '')
    for name in LAUNCHER_VARIABLES:
        values[name] = 'ccache'
    record: Path = workspace / TOOLCHAIN_PATH_RECORD
    if platform == 'windows-x64' and record.is_file():
        directories: list[str] = record.read_text(encoding='utf-8').splitlines()
        values['PATH'] = os.pathsep.join(directories) + os.pathsep + PATH_PLACEHOLDER
    runtime: Path | None = runtime_prefix(workspace)
    if runtime is not None:
        values[product.variable('LLVM_RUNTIME')] = runtime.as_posix()
    tables: list[tuple[tuple[str, str], ...]] = []
    if product.bootstrap is not None:
        tables.append(product.bootstrap.environment)
        table: project.BootstrapPlatform | None = product.bootstrap.platforms.get(platform)
        if table is not None:
            tables.append(table.environment)
    pairs: tuple[tuple[str, str], ...]
    value: str
    for pairs in tables:
        for name, value in pairs:
            values[name] = value.replace(WORKSPACE_PLACEHOLDER, base)
    for name, value in settings:
        if not ENVIRONMENT_NAME.fullmatch(name):
            raise BuildEnvironmentError('Not an environment variable name: ' + name)
        values[name] = value
    return values


def file_digest(path: Path) -> str:
    stream: BinaryIO
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def compatibility(manifest: dict[str, Any], platform: str, environment: dict[str, str]) -> list[str]:
    """Why the restored cache will still miss on this machine (empty: nothing found).

    The compiler is the clang on environment's PATH; ccache's content check
    compares it with the producer's byte for byte. On macOS the SDK's
    headers are read by absolute path and content, so another SDK version
    misses too.
    """
    problems: list[str] = []
    if not manifest:
        return problems
    compiler_text: str | None = shutil.which('clang', path=environment.get('PATH'))
    expected: str = str(manifest.get('compiler_sha256', ''))
    if compiler_text is not None and expected:
        found: str = file_digest(Path(compiler_text).resolve())
        if found != expected and environment.get('CCACHE_COMPILERCHECK', 'content') == 'content':
            problems.append('the cache was filled by another build of clang (' + str(manifest.get('compiler_version', ''))
                            + ', sha256 ' + expected[:12] + '; ' + compiler_text + ' is ' + found[:12]
                            + '): every object misses')
    sdk: str = str(manifest.get('sdk_version', ''))
    if platform == 'macos-arm64' and sdk and shutil.which('xcrun') is not None:
        local: str = subprocess.run(['xcrun', '--show-sdk-version'], capture_output=True, text=True,
                                    check=False).stdout.strip()
        if local and local != sdk:
            problems.append('the cache was filled with the macOS ' + sdk + ' SDK and this machine has ' + local
                            + ': every object that includes a system header misses')
    return problems


def powershell_line(name: str, value: str) -> str:
    if value == '':
        return 'Remove-Item -ErrorAction SilentlyContinue env:' + name
    quoted: str = "'" + value.replace("'", "''") + "'"
    if name == 'PATH':
        quoted = quoted.replace(PATH_PLACEHOLDER, "' + $env:PATH + '")
    return '$env:' + name + ' = ' + quoted


def shell_line(name: str, value: str) -> str:
    if value == '':
        return 'unset ' + name
    if name == 'PATH':
        # MSYS2 and Git Bash spell D:\x as /d/x; cygpath converts the list.
        listing: str = value.replace(PATH_PLACEHOLDER, '').rstrip(os.pathsep)
        return 'export PATH="$(cygpath -p -u ' + shlex.quote(listing) + '):$PATH"'
    return 'export ' + name + '=' + shlex.quote(value)


def write_build_environment(workspace: Path, values: dict[str, str]) -> Path:
    """Writes BUILD_ENVIRONMENT_RECORD and the two scripts; returns the record's path."""
    record: Path = workspace / BUILD_ENVIRONMENT_RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(values, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    names: list[str] = sorted(values)
    header: str = "CI's build environment for this workspace (harness/restore_local.py env); load it before building."
    powershell: list[str] = ['# ' + header] + [powershell_line(name, values[name]) for name in names]
    shell: list[str] = ['# ' + header, '# . ' + BUILD_ENVIRONMENT_SHELL] + [shell_line(name, values[name])
                                                                           for name in names]
    (workspace / BUILD_ENVIRONMENT_POWERSHELL).write_text('\n'.join(powershell) + '\n', encoding='utf-8')
    (workspace / BUILD_ENVIRONMENT_SHELL).write_text('\n'.join(shell) + '\n', encoding='utf-8')
    return record


def apply_build_environment(record: Path, environment: dict[str, str]) -> dict[str, str]:
    """environment with the record's variables applied: an empty value removes the variable, and
    ${PATH} in PATH stands for environment's PATH. No record: environment unchanged."""
    result: dict[str, str] = dict(environment)
    if not record.is_file():
        return result
    values: dict[str, str] = json.loads(record.read_text(encoding='utf-8'))
    name: str
    for name in sorted(values):
        value: str = values[name]
        if value == '':
            result.pop(name, None)
        elif name == 'PATH':
            result['PATH'] = value.replace(PATH_PLACEHOLDER, environment.get('PATH', ''))
        else:
            result[name] = value
    return result
