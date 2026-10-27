#!/usr/bin/env python3
"""Describe a successful cache producer; this is not a consumer test receipt."""
import json
import hashlib
import platform
import re
import shutil
import os
from pathlib import Path
import subprocess
import sys

import native_tests
import project
import windows_toolchain


def main() -> None:
    product: project.Project = project.current()
    source: Path = product.source_root()
    workspace: Path = product.workspace_root()
    build: str = product.build_tree(sys.argv[1])
    # A product at the workspace root (source_dir ".") is its own base directory.
    basedir: str = ('workspace root, the product checkout' if product.at_workspace_root()
                    else 'workspace root containing ' + product.source_dir + '/')
    revision_text: str = subprocess.check_output(
        ['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True)
    revision: str = revision_text.strip()
    compiler_path: str | None = shutil.which('clang')
    if compiler_path is None:
        raise RuntimeError('clang is missing')
    compiler_text: str = subprocess.check_output(['clang', '--version'], text=True)
    match: re.Match[str] | None = re.search(r'clang version (22\.1\.[0-9]+)', compiler_text)
    if match is None:
        raise RuntimeError('The cache requires LLVM 22.1.x')
    manifest: dict[str, object] = {
        'schema': 2, 'repository': product.repository,
        'revision': revision, 'platform': sys.argv[1], 'compiler': sys.argv[2],
        'compiler_version': match.group(1), 'compiler_version_text': compiler_text,
        'compiler_sha256': hashlib.sha256(Path(compiler_path).read_bytes()).hexdigest(),
        'compiler_launchers': {'CC': 'clang', 'CXX': 'clang++', 'OBJCXX': 'clang++', 'launcher': 'ccache'},
        'runner': os.environ.get(product.variable('RUNNER'), ''),
        'runner_image_version': os.environ.get('ImageVersion', ''),
        'architecture': platform.machine(),
        'layout': {'source': product.source_dir + '/', 'build': build + '/'},
        'ccache': {'basedir': basedir, 'compiler_check': 'content',
                   'version': subprocess.check_output(['ccache', '--version'], text=True)},
        'minimum_platform': '14.0' if sys.argv[1] == 'macos-arm64' else
                            ('Windows 10 (0x0A00)' if sys.argv[1] == 'windows-x64' else 'Ubuntu 24.04'),
        'run_id': os.environ.get('GITHUB_RUN_ID', ''),
        'profile': product.cache_profile,
        'native_options': product.native_options_for(sys.argv[1]),
        'validation': 'provider CTest passed; consumer compilation and tests remain required'}
    if sys.argv[1] == 'macos-arm64':
        manifest['sdk_version'] = subprocess.check_output(['xcrun', '--show-sdk-version'], text=True).strip()
        runtime: Path = Path(os.environ[product.variable('LLVM_RUNTIME')])
        manifest['runtime'] = json.loads((runtime / 'runtime-manifest.json').read_text(encoding='utf-8'))
        manifest['runtime_layout'] = '.build/toolchain/llvm-22.1.8-macos14'
    if sys.argv[1] == 'windows-x64':
        # The packaged MSYS2 tree must contain the compiler that filled this cache.
        toolchain: dict[str, object] = json.loads(
            (workspace / '.ci/windows-toolchain.json').read_text(encoding='utf-8'))
        if toolchain['compiler_sha256'] != manifest['compiler_sha256']:
            raise RuntimeError('Cache compiler differs from the packaged Windows toolchain')
        manifest['toolchain'] = windows_toolchain.cache_record(toolchain)
    destination: Path = workspace / 'cache-manifest.json'
    reused: Path = workspace / '.ci/reused.json'
    inputs: Path = workspace / '.ci/inputs.json'
    if inputs.exists():
        manifest['validation_environment'] = json.loads(inputs.read_text(encoding='utf-8'))['environment']
    # The tests this job's ci_build.py run passed; a reused validation keeps
    # the producer's list from the manifest its archive restored here.
    junit: Path = workspace / build / native_tests.JUNIT_NAME
    if junit.is_file():
        manifest['validated_tests'] = native_tests.passed_tests(junit)
    elif reused.exists() and destination.is_file():
        producer: dict[str, object] = json.loads(destination.read_text(encoding='utf-8'))
        carried: list[str] | None = native_tests.validated_tests(producer)
        if carried is not None:
            manifest['validated_tests'] = carried
    if reused.exists():
        receipt: dict[str, object] = json.loads(reused.read_text(encoding='utf-8'))
        manifest['validation'] = 'reused successful provider validation for identical tracked tree and build environment'
        manifest['validation_provenance'] = {
            'tree': receipt['tree'], 'run_id': receipt['validation_run'],
            'revision': receipt['validation_revision'], 'receipt_run_id': receipt['run_id']}
    destination.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
