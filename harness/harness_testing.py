"""Imported first by every test: the ported tests assert GUI.Forms' names.

The tools resolve the project when they are imported, so this sets
HARNESS_PROJECT to projects/gui_forms.toml, the reference project, unless
the caller named one. Tests that need another project load it explicitly
with project.load().
"""
import os
from pathlib import Path

HARNESS: Path = Path(__file__).resolve().parent
REPOSITORY: Path = HARNESS.parent
REFERENCE_PROJECT: Path = REPOSITORY / 'projects' / 'gui_forms.toml'

os.environ.setdefault('HARNESS_PROJECT', str(REFERENCE_PROJECT))
