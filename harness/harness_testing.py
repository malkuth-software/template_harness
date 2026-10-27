"""Imported first by every test: the reference project and a fixture workspace.

The tools resolve the project and its paths when they are imported, so this
module runs before any of them:

- HARNESS_PROJECT becomes projects/gui_forms.toml, the reference project
  whose names the ported tests assert, unless the caller named one;
- HARNESS_WORKSPACE becomes a fixture workspace, made once per process and
  removed at exit, unless the caller named one. Its product checkout
  (gui_forms/) is a git repository with one commit, because the tools read
  the checkout's HEAD (ci_reuse.git_field, ci_tree_receipt.git_field).

Tests that need another project load it explicitly with project.load().
"""
import atexit
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HARNESS: Path = Path(__file__).resolve().parent
REPOSITORY: Path = HARNESS.parent
REFERENCE_PROJECT: Path = REPOSITORY / 'projects' / 'gui_forms.toml'
# The reference project's source_dir; the fixture checkout lives there.
REFERENCE_SOURCE_DIR: str = 'gui_forms'


def git(checkout: Path, *arguments: str) -> None:
    """Runs git in the checkout with a fixed identity, raising on any error."""
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'commit.gpgsign=false', *arguments], check=True, capture_output=True)


def make_fixture_workspace() -> Path:
    """A workspace whose product checkout holds one committed file; removed when the process exits."""
    workspace: Path = Path(tempfile.mkdtemp(prefix='harness-workspace-'))
    atexit.register(shutil.rmtree, workspace, True)
    checkout: Path = workspace / REFERENCE_SOURCE_DIR
    checkout.mkdir()
    git(checkout, 'init', '-q')
    (checkout / 'README.md').write_text('fixture checkout\n', encoding='utf-8')
    git(checkout, 'add', 'README.md')
    git(checkout, 'commit', '-q', '-m', 'fixture')
    return workspace


os.environ.setdefault('HARNESS_PROJECT', str(REFERENCE_PROJECT))
if 'HARNESS_WORKSPACE' not in os.environ:
    os.environ['HARNESS_WORKSPACE'] = str(make_fixture_workspace())
