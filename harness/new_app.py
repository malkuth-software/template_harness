#!/usr/bin/env python3
"""One command to a harnessed GUI.Forms app: the repositories, the workspace and a first verify.

new_app.sh (and new_app.ps1, which finds Git Bash) runs this with a Python of
3.11 or later:

    sh new_app.sh --name APP --workspace DIR [--title TITLE] [--dry-run]
                  [--create-repo] [--remote-base URL] [--harness DIR]
                  [--no-restore] [--no-smoke] [--build-wrapper NAME] ...

APP is the app's name in snake case (paint_box). In order, each step
idempotent (a re-run finds the work done and changes nothing):

1. validate the name and derive the names (docs/adopting.md §2): the
   Pascal-case type name, the kebab-case build name and CI prefixes, the
   environment prefix; refuse a name that is reserved, starts with gui, or
   collides with another project file's names;
2. template_harness: add projects/APP.toml and
   harness/test_project_names_APP.py (from templates/harness/) in a
   temporary worktree of --harness on the remote's main, commit, push. The
   commit that added the project file is the harness pin;
3. the app repository: create <remote-base>/APP (private, with
   --create-repo, through gh), instantiate templates/app/ with the names
   and the pin (harness.lock; the plugins load from the sibling ../template_harness/claude, which lock.py checkout keeps at the pin; the
   CI workflow's uses: lines), first commit, push main;
4. notes: an orphan branch APP with the brief, decisions and interviews
   skeleton (templates/notes/), pushed;
5. projects: tracks/APP/, desks/APP/ and the seats APP-orchestrator,
   APP-w1 and APP-integrator in desks/seats/ (templates/projects/),
   committed and pushed to main;
6. the workspace: harness/bootstrap.py --in-house clones everything into
   the sibling layout (DIR/APP, DIR/template_harness at the pin, DIR/notes
   with DIR/notes-APP, DIR/projects) and, unless --no-restore, restores
   the toolchain and caches; the smoke build runs only with --smoke (it
   compiles: run it on the build host, never on a laptop that must not);
7. first verify: `claude plugin validate` on the pinned harness's claude/
   marketplace (when the CLI is found), the house-style and lifetime
   checkers over the app's sources, and the build-host bootstrap command
   (run over ssh with --build-host HOST --build-host-workspace DIR,
   printed otherwise);
8. print "open Claude in DIR/APP" with a starter prompt.

Every remote operation goes through one small layer (Remote): repository
URLs are <remote-base>/<name>.git, --remote-base defaults to
https://github.com/malkuth-software (its last component is the owner), and
a file:// base stands local bare repositories in for GitHub (tests).
--create-repo creates a missing app repository: with gh for GitHub, with
git init --bare for a file:// base; never in a dry run.

--dry-run prints every step, every repository and every path it would
touch, and changes nothing (it reads remotes with git ls-remote).
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import project

HARNESS: Path = Path(__file__).resolve().parent
REPOSITORY: Path = HARNESS.parent
TEMPLATES: Path = REPOSITORY / 'templates'
REMOTE_BASE: str = 'https://github.com/malkuth-software'
HARNESS_NAME: str = 'template_harness'
NOTES_NAME: str = 'notes'
PROJECTS_NAME: str = 'projects'
MAIN: str = 'main'
# The ref new_app fetches the harness's main into, in --harness.
FETCHED_MAIN: str = 'refs/new_app/harness-main'
NAME_PATTERN: re.Pattern[str] = re.compile(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*')
NAME_MAX: int = 40
# Repositories and directories of the workspace, and the harness's own example.
RESERVED_NAMES: frozenset[str] = frozenset({
    HARNESS_NAME, NOTES_NAME, PROJECTS_NAME, 'harness', 'fransushi', 'claude', 'example_app', 'templates',
    'checkpoint', 'workspace', 'seats', 'desks', 'tracks'})
PLACEHOLDER: re.Pattern[str] = re.compile(r'\{\{([A-Za-z_]+)\}\}')
# A {{ that is not GitHub's ${{ expression: an unrendered placeholder.
LEFTOVER: re.Pattern[str] = re.compile(r'(?<!\$)\{\{')
SKIPPED_NAMES: frozenset[str] = frozenset({'__pycache__', '.DS_Store'})
SEAT_ROLES: tuple[tuple[str, str], ...] = (('orchestrator', 'orchestrator'), ('w1', 'coder'),
                                           ('integrator', 'integrator'))
STEPS: int = 8


class NewAppError(Exception):
    """An expected refusal or failure; the command prints it and exits 1."""


def say(text: str) -> None:
    print(text, flush=True)


# ---------------------------------------------------------------- names

@dataclass(frozen=True)
class Names:
    """Every name the app's repositories, project file and sources use (docs/adopting.md §2)."""
    app: str
    pascal: str
    kebab: str
    env_prefix: str
    namespace: str
    title: str

    @property
    def build_name(self) -> str:
        return self.kebab

    @property
    def artifact_prefix(self) -> str:
        result: str = self.kebab + '-'
        return result

    @property
    def cache_asset_prefix(self) -> str:
        result: str = self.kebab + '-cache-'
        return result


def derive_names(app: str, title: str = '', namespace: str = '') -> Names:
    """Validates app (snake case) and derives the other names; raises NewAppError on a bad name."""
    if NAME_PATTERN.fullmatch(app) is None:
        raise NewAppError('--name must be snake case: lowercase letters and digits in words joined by _, '
                          'starting with a letter (paint_box); got ' + repr(app))
    if len(app) > NAME_MAX:
        raise NewAppError('--name is longer than ' + str(NAME_MAX) + ' characters')
    if app in RESERVED_NAMES:
        raise NewAppError('--name ' + app + ' is reserved (a workspace directory or a harness name)')
    if app.startswith('gui'):
        raise NewAppError('--name may not start with gui: gui- and gui-forms- are GUI.Forms\' names '
                          '(docs/adopting.md §2)')
    words: list[str] = app.split('_')
    capitalized: list[str] = []
    word: str
    for word in words:
        capitalized.append(word[:1].upper() + word[1:])
    pascal: str = ''.join(capitalized)
    chosen_title: str = title or ' '.join(capitalized)
    chosen_namespace: str = namespace or app
    if NAME_PATTERN.fullmatch(chosen_namespace) is None:
        raise NewAppError('--namespace must be a snake-case C++ identifier; got ' + repr(chosen_namespace))
    if '"' in chosen_title or '\\' in chosen_title or '\n' in chosen_title:
        raise NewAppError('--title may not contain quotes, backslashes or newlines')
    names: Names = Names(app=app, pascal=pascal, kebab='-'.join(words), env_prefix=app.upper(),
                         namespace=chosen_namespace, title=chosen_title)
    return names


def overlapping(first: str, second: str) -> bool:
    result: bool = first.startswith(second) or second.startswith(first)
    return result


def name_collisions(names: Names, others: list[project.Project]) -> list[str]:
    """The app's names that would collide with another project file's (test_project_prefixes.py's rules)."""
    mine: list[tuple[str, str]] = [('project.name', names.app), ('project.env_prefix', names.env_prefix + '_'),
                                   ('project.build_name', names.build_name + '-'),
                                   ('ci.artifact_prefix', names.artifact_prefix),
                                   ('ci.cache_asset_prefix', names.cache_asset_prefix)]
    findings: list[str] = []
    other: project.Project
    for other in others:
        if other.name == names.app:
            continue
        theirs: list[str | None] = [other.name, other.env_prefix + '_', other.build_name + '-', other.artifact_prefix,
                                    other.cache_asset_prefix]
        index: int
        for index in range(len(mine)):
            value: str | None = theirs[index]
            if value is not None and overlapping(mine[index][1], value):
                findings.append(mine[index][0] + ' ' + repr(mine[index][1]) + ' overlaps ' + other.name + "'s "
                                + repr(value))
    return findings


def project_files(directory: Path) -> list[project.Project]:
    products: list[project.Project] = []
    path: Path
    for path in sorted((directory / 'projects').glob('*.toml')):
        products.append(project.load(path))
    return products


# ---------------------------------------------------------------- templates

def render_text(text: str, values: dict[str, str], where: str) -> str:
    """Substitutes every {{name}}; an unknown name is an error, so a template cannot leave one behind."""
    pieces: list[str] = []
    unknown: list[str] = []
    position: int = 0
    match: re.Match[str]
    for match in PLACEHOLDER.finditer(text):
        name: str = match.group(1)
        pieces.append(text[position:match.start()])
        if name in values:
            pieces.append(values[name])
        elif name not in unknown:
            unknown.append(name)
        position = match.end()
    if unknown:
        raise NewAppError(where + ' uses unknown placeholders: ' + ', '.join(sorted(unknown)))
    pieces.append(text[position:])
    result: str = ''.join(pieces)
    return result


def leftover_placeholders(text: str) -> bool:
    found: bool = LEFTOVER.search(text) is not None
    return found


def skipped(relative: Path) -> bool:
    """Whether a path under a template directory is a cache, not a template."""
    part: str
    for part in relative.parts:
        if part in SKIPPED_NAMES or part.endswith('.pyc'):
            return True
    return False


def template_files(source: Path) -> list[Path]:
    """The files under a template directory, relative to it, without caches."""
    files: list[Path] = []
    path: Path
    for path in sorted(source.rglob('*')):
        relative: Path = path.relative_to(source)
        if skipped(relative):
            continue
        if path.is_file():
            files.append(relative)
    return files


def render_tree(source: Path, destination: Path, values: dict[str, str]) -> list[Path]:
    """Renders every template file into destination (names and contents); returns the written paths, relative."""
    written: list[Path] = []
    relative: Path
    for relative in template_files(source):
        target: Path = Path(render_text(relative.as_posix(), values, str(source / relative)))
        text: str = (source / relative).read_text(encoding='utf-8')
        rendered: str = render_text(text, values, str(source / relative))
        if leftover_placeholders(rendered) or leftover_placeholders(target.as_posix()):
            raise NewAppError(str(source / relative) + ' leaves a {{ after rendering')
        path: Path = destination / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding='utf-8', newline='\n')
        mode: int = (source / relative).stat().st_mode & 0o777
        path.chmod(mode)
        written.append(target)
    return written


LICENSES: tuple[str, ...] = ('proprietary', 'mit', 'polyform-noncommercial', 'none')


def render_app(destination: Path, values: dict[str, str], license_choice: str) -> list[Path]:
    """Renders templates/app/ and the chosen LICENSE (none writes no LICENSE file)."""
    written: list[Path] = render_tree(TEMPLATES / 'app', destination, values)
    if license_choice not in LICENSES:
        raise NewAppError('--license must be one of ' + ', '.join(LICENSES))
    if license_choice != 'none':
        source: Path = TEMPLATES / 'licenses' / (license_choice + '.txt')
        text: str = render_text(source.read_text(encoding='utf-8'), values, str(source))
        (destination / 'LICENSE').write_text(text, encoding='utf-8', newline='\n')
        written.append(Path('LICENSE'))
    return written


def default_licensor() -> str:
    """The person's git name: the generator never presumes whose program this is."""
    try:
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            ['git', 'config', 'user.name'], capture_output=True, text=True, check=False)
    except OSError:
        return ''
    return completed.stdout.strip()


def seat_text(names: Names, seat: str, role: str, values: dict[str, str]) -> str:
    """A SushiWrap seat file (its render_seat format) for one seeded seat."""
    track: str = 'tracks/' + names.app + '/w1.md' if seat == 'w1' else 'tracks/' + names.app + '/README.md'
    header: dict[str, object] = {
        'seat': names.app + '-' + seat, 'role': role, 'track_file': track,
        'branch': None if seat != 'w1' else 'claude/w1', 'worktree': None, 'owned_paths': [], 'open_tickets': [],
        'host_manifest': None, 'holder': None, 'updated_at': values['timestamp'], 'pointer_updated_at': None,
        'app': names.app}
    lines: list[str] = ['# Seat ' + names.app + '-' + seat, '',
                        'Managed by SushiWrap. Edit the header with `sushiwrap seat set`, the resume note with '
                        '`sushiwrap seat pointer`.', '',
                        '```json', json.dumps(header, indent=1, ensure_ascii=False), '```', '',
                        '## Resume note (never)', '',
                        'Seeded by template_harness new_app on ' + values['date'] + '; not taken yet.']
    text: str = '\n'.join(lines) + '\n'
    return text


# ---------------------------------------------------------------- remotes

def git(*arguments: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', *arguments], cwd=None if cwd is None else str(cwd), capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise NewAppError(shlex.join(['git', *arguments]) + ' failed: ' + completed.stderr.strip())
    return completed


@dataclass(frozen=True)
class Remote:
    """Every remote operation: URLs under one base (GitHub, or file:// bare repositories in tests)."""
    base: str
    dry_run: bool
    create_missing: bool

    @property
    def owner(self) -> str:
        result: str = self.base.rstrip('/').rsplit('/', 1)[1]
        return result

    @property
    def github_base(self) -> str:
        """The base bootstrap.py and lock.py take (--github-base): this base without the owner."""
        result: str = self.base.rstrip('/').rsplit('/', 1)[0]
        return result

    def local(self) -> bool:
        result: bool = self.base.startswith('file://')
        return result

    def repository(self, name: str) -> str:
        result: str = self.owner + '/' + name
        return result

    def url(self, name: str) -> str:
        result: str = self.base.rstrip('/') + '/' + name + '.git'
        return result

    def heads(self, name: str) -> dict[str, str] | None:
        """The repository's branches and their commits, or None when it does not exist (read-only)."""
        completed: subprocess.CompletedProcess[str] = git('ls-remote', '--heads', self.url(name), check=False)
        if completed.returncode != 0:
            return None
        heads: dict[str, str] = {}
        line: str
        for line in completed.stdout.splitlines():
            sha: str
            reference: str
            sha, _, reference = line.partition('\t')
            heads[reference.removeprefix('refs/heads/')] = sha
        return heads

    def create(self, name: str) -> None:
        """Creates a missing repository (private on GitHub); refuses without --create-repo."""
        if not self.create_missing:
            raise NewAppError(self.url(name) + ' does not exist; create it (private) or pass --create-repo')
        if self.local():
            path: Path = Path(self.url(name).removeprefix('file://'))
            say('+ git init --bare -b main ' + str(path))
            path.parent.mkdir(parents=True, exist_ok=True)
            git('init', '--quiet', '--bare', '-b', MAIN, str(path))
            return
        command: list[str] = ['gh', 'repo', 'create', self.repository(name), '--private',
                              '--description', 'A GUI.Forms application (template_harness new_app)']
        say('+ ' + shlex.join(command))
        completed: subprocess.CompletedProcess[str] = subprocess.run(command, capture_output=True, text=True,
                                                                     check=False)
        if completed.returncode != 0:
            raise NewAppError('creating ' + self.repository(name) + ' failed: ' + completed.stderr.strip())

    def push(self, checkout: Path, name: str, branch: str) -> None:
        say('+ git push ' + self.url(name) + ' HEAD:refs/heads/' + branch)
        git('-C', str(checkout), 'push', '--quiet', self.url(name), 'HEAD:refs/heads/' + branch)


# ---------------------------------------------------------------- the run

@dataclass(frozen=True)
class Plan:
    names: Names
    workspace: Path
    harness: Path
    remote: Remote
    dry_run: bool
    restore: bool
    smoke: bool
    skip_workspace: bool
    build_host: str
    build_host_workspace: str
    claude: str
    options: dict[str, str]


def values_for(plan: Plan, harness_commit: str) -> dict[str, str]:
    """The template values; the harness commit is the pin from step 2."""
    names: Names = plan.names
    now: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    workspace_word: str = '<workspace>'
    values: dict[str, str] = {
        'app': names.app, 'App': names.pascal, 'namespace': names.namespace, 'title': names.title,
        'build_name': names.build_name, 'env_prefix': names.env_prefix, 'artifact_prefix': names.artifact_prefix,
        'cache_asset_prefix': names.cache_asset_prefix, 'owner': plan.remote.owner,
        'repository': plan.remote.repository(names.app), 'harness_repository': plan.remote.repository(HARNESS_NAME),
        'harness_commit': harness_commit, 'harness_ref': harness_commit,
        'notes_repository': plan.remote.repository(NOTES_NAME),
        'projects_repository': plan.remote.repository(PROJECTS_NAME),
        'year': str(now.year), 'date': now.strftime('%Y-%m-%d'), 'timestamp': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'licensor': plan.options['licensor'],
        'build_wrapper': plan.options['build_wrapper'], 'build_wrapper_regex': re.escape(plan.options['build_wrapper']),
        'workers_dir': plan.options['workers_dir'] or workspace_word + '/' + names.app + '_workers',
        'projects_dir': plan.options['projects_dir'] or workspace_word + '/projects',
        'notes_dir': plan.options['notes_dir'] or workspace_word + '/notes',
    }
    return values


def heading(number: int, text: str) -> None:
    say('')
    say('[' + str(number) + '/' + str(STEPS) + '] ' + text)


def touches(text: str) -> None:
    say('  touches: ' + text)


def posix_paths(paths: list[Path]) -> list[str]:
    result: list[str] = []
    path: Path
    for path in paths:
        result.append(path.as_posix())
    return result


def commit_all(checkout: Path, message: str) -> str:
    git('-C', str(checkout), 'add', '--all')
    git('-C', str(checkout), 'commit', '--quiet', '-m', message)
    head: str = git('-C', str(checkout), 'rev-parse', 'HEAD').stdout.strip()
    return head


def step_validate(plan: Plan) -> None:
    heading(1, 'Validate the name ' + plan.names.app + ' and derive the names')
    names: Names = plan.names
    say('  type ' + names.pascal + ', namespace ' + names.namespace + ', title "' + names.title + '"')
    say('  build tree .build/native-<platform>/' + names.build_name + ', environment ' + names.env_prefix + '_*')
    say('  artifacts ' + names.artifact_prefix + '*, caches ' + names.cache_asset_prefix + '<platform>.tar.gz')
    findings: list[str] = name_collisions(names, project_files(plan.harness))
    if findings:
        raise NewAppError('the names collide with existing project files:\n  ' + '\n  '.join(findings))
    say('  no collision with the ' + str(len(project_files(plan.harness))) + ' project files in '
        + str(plan.harness / 'projects'))


def step_harness(plan: Plan) -> str:
    """Adds projects/<app>.toml to the harness's main; returns the commit that added it (the pin)."""
    names: Names = plan.names
    heading(2, 'template_harness: add projects/' + names.app + '.toml')
    url: str = plan.remote.url(HARNESS_NAME)
    relative: str = 'projects/' + names.app + '.toml'
    touches(url + ' (main): ' + relative + ', harness/test_project_names_' + names.app + '.py; '
            + 'a temporary worktree of ' + str(plan.harness))
    if plan.dry_run:
        heads: dict[str, str] | None = plan.remote.heads(HARNESS_NAME)
        if heads is None or MAIN not in heads:
            raise NewAppError(url + ' has no main branch')
        say('  would fetch main into ' + str(plan.harness) + ', add the files if main lacks them, commit and push')
        return '0' * 40
    git('-C', str(plan.harness), 'fetch', '--quiet', url, '+refs/heads/' + MAIN + ':' + FETCHED_MAIN)
    present: bool = git('-C', str(plan.harness), 'cat-file', '-e', FETCHED_MAIN + ':' + relative,
                        check=False).returncode == 0
    if present:
        commit: str = git('-C', str(plan.harness), 'log', '-1', '--diff-filter=A', '--format=%H', FETCHED_MAIN, '--',
                          relative).stdout.strip()
        say('  already on main, added in ' + commit + '; nothing to do')
        return commit
    worktree: Path = Path(tempfile.mkdtemp(prefix='new_app-harness-'))
    shutil.rmtree(worktree)
    git('-C', str(plan.harness), 'worktree', 'add', '--quiet', '--detach', str(worktree), FETCHED_MAIN)
    try:
        written: list[Path] = render_tree(TEMPLATES / 'harness', worktree, values_for(plan, '0' * 40))
        product: project.Project = project.load(worktree / relative)
        others: list[project.Project] = project_files(worktree)
        findings: list[str] = name_collisions(names, others)
        if findings or product.name != names.app:
            raise NewAppError('the rendered project file collides:\n  ' + '\n  '.join(findings))
        say('  wrote ' + ', '.join(posix_paths(written)))
        commit = commit_all(worktree, 'projects: add ' + names.app + ' (new_app)\n\n'
                            + names.title + "'s project file and names test, from templates/harness/.")
        plan.remote.push(worktree, HARNESS_NAME, MAIN)
        git('-C', str(plan.harness), 'fetch', '--quiet', url, '+refs/heads/' + MAIN + ':' + FETCHED_MAIN)
    finally:
        git('-C', str(plan.harness), 'worktree', 'remove', '--force', str(worktree), check=False)
    say('  pushed ' + commit + ': the harness pin')
    return commit


def step_app(plan: Plan, harness_commit: str) -> None:
    names: Names = plan.names
    heading(3, 'App repository ' + plan.remote.repository(names.app) + ': instantiate templates/app/')
    url: str = plan.remote.url(names.app)
    touches(url + ' (main, created private when missing): the whole tree from templates/app/')
    heads: dict[str, str] | None = plan.remote.heads(names.app)
    if heads is None:
        if plan.dry_run:
            say('  would create ' + plan.remote.repository(names.app) + ' (private)'
                + ('' if plan.remote.create_missing else '; needs --create-repo'))
        else:
            plan.remote.create(names.app)
            heads = {}
    elif MAIN in heads:
        say('  main exists at ' + heads[MAIN] + '; already instantiated, nothing to do')
        return
    if plan.dry_run:
        files: list[Path] = template_files(TEMPLATES / 'app')
        say('  would render ' + str(len(files)) + ' files with LICENSE ' + plan.options['license']
            + ' (holder: ' + plan.options['licensor'] + '), pin harness.lock to the step 2 commit, commit and push main')
        return
    checkout: Path = Path(tempfile.mkdtemp(prefix='new_app-app-'))
    try:
        git('init', '--quiet', '-b', MAIN, str(checkout))
        written: list[Path] = render_app(checkout, values_for(plan, harness_commit), plan.options['license'])
        say('  rendered ' + str(len(written)) + ' files')
        commit: str = commit_all(checkout, names.title + ': instantiate from template_harness ' + harness_commit[:12]
                                 + '\n\nGenerated by template_harness harness/new_app.py from templates/app/.')
        plan.remote.push(checkout, names.app, MAIN)
        say('  pushed ' + commit)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def step_notes(plan: Plan) -> None:
    names: Names = plan.names
    heading(4, 'notes: branch ' + names.app + ' with the brief and decisions skeleton')
    url: str = plan.remote.url(NOTES_NAME)
    touches(url + ' (new orphan branch ' + names.app + '): README.md, planning/, decisions/, interviews/')
    heads: dict[str, str] | None = plan.remote.heads(NOTES_NAME)
    if heads is None:
        raise NewAppError(url + ' is not readable')
    if names.app in heads:
        say('  branch ' + names.app + ' exists at ' + heads[names.app] + '; nothing to do')
        return
    if plan.dry_run:
        say('  would commit the skeleton on an orphan branch and push it')
        return
    checkout: Path = Path(tempfile.mkdtemp(prefix='new_app-notes-'))
    try:
        git('init', '--quiet', '-b', names.app, str(checkout))
        render_tree(TEMPLATES / 'notes', checkout, values_for(plan, '0' * 40))
        commit_all(checkout, names.title + ': notes branch skeleton (new_app)')
        plan.remote.push(checkout, NOTES_NAME, names.app)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def step_projects(plan: Plan) -> None:
    names: Names = plan.names
    heading(5, 'projects: tracks/' + names.app + '/, desks/' + names.app + '/ and the seats')
    url: str = plan.remote.url(PROJECTS_NAME)
    seats: list[str] = []
    seat: str
    role: str
    for seat, role in SEAT_ROLES:
        seats.append('desks/seats/' + names.app + '-' + seat + '.md')
    touches(url + ' (main): tracks/' + names.app + '/, desks/' + names.app + '/, ' + ', '.join(seats))
    heads: dict[str, str] | None = plan.remote.heads(PROJECTS_NAME)
    if heads is None or MAIN not in heads:
        raise NewAppError(url + ' has no main branch (or is not readable)')
    if plan.dry_run:
        say('  would clone main, add the files it lacks, commit and push')
        return
    checkout: Path = Path(tempfile.mkdtemp(prefix='new_app-projects-'))
    shutil.rmtree(checkout)
    try:
        git('clone', '--quiet', '--depth', '1', '--branch', MAIN, url, str(checkout))
        if (checkout / 'tracks' / names.app).exists():
            say('  tracks/' + names.app + '/ exists; nothing to do')
            return
        values: dict[str, str] = values_for(plan, '0' * 40)
        render_tree(TEMPLATES / 'projects', checkout, values)
        for seat, role in SEAT_ROLES:
            path: Path = checkout / 'desks' / 'seats' / (names.app + '-' + seat + '.md')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(seat_text(names, seat, role, values), encoding='utf-8', newline='\n')
        commit_all(checkout, names.title + ': tracks, desk and seats (new_app)')
        plan.remote.push(checkout, PROJECTS_NAME, MAIN)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def project_file_at(plan: Plan, harness_commit: str, scratch: Path) -> Path:
    """The app's project file as committed in step 2 (rendered from the template in a dry run)."""
    path: Path = scratch / (plan.names.app + '.toml')
    if plan.dry_run:
        rendered: Path = scratch / 'rendered'
        render_tree(TEMPLATES / 'harness', rendered, values_for(plan, harness_commit))
        shutil.copyfile(rendered / 'projects' / (plan.names.app + '.toml'), path)
        return path
    text: str = git('-C', str(plan.harness), 'show', harness_commit + ':projects/' + plan.names.app + '.toml').stdout
    path.write_text(text, encoding='utf-8')
    return path


def step_workspace(plan: Plan, harness_commit: str) -> None:
    names: Names = plan.names
    heading(6, 'Workspace ' + str(plan.workspace) + ': bootstrap --in-house')
    touches(str(plan.workspace / names.app) + ', ' + str(plan.workspace / HARNESS_NAME) + ', '
            + str(plan.workspace / 'notes') + ', ' + str(plan.workspace / ('notes-' + names.app)) + ', '
            + str(plan.workspace / 'projects'))
    if plan.skip_workspace:
        say('  skipped (--skip-workspace)')
        return
    scratch: Path = Path(tempfile.mkdtemp(prefix='new_app-project-'))
    try:
        command: list[str] = [sys.executable, '-B', str(HARNESS / 'bootstrap.py'), '--workspace', str(plan.workspace),
                              '--project-file', str(project_file_at(plan, harness_commit, scratch)), '--in-house',
                              '--github-base', plan.remote.github_base]
        if not plan.restore:
            command.append('--no-restore')
        if not plan.smoke:
            command.append('--no-smoke')
        if plan.dry_run:
            command.append('--dry-run')
        say(('  running: ' if not plan.dry_run else '  running (dry run): ') + shlex.join(command))
        completed: subprocess.CompletedProcess[str] = subprocess.run(command, capture_output=True, text=True,
                                                                     check=False)
        output: str
        for output in completed.stdout.splitlines():
            say('    ' + output)
        if completed.returncode != 0:
            raise NewAppError('the bootstrap failed: ' + completed.stderr.strip())
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def claude_cli(plan: Plan) -> str:
    """The Claude Code CLI for plugin validate: --claude, else CLAUDE_CODE_EXECPATH, else claude on PATH."""
    if plan.claude:
        return plan.claude
    named: str = os.environ.get('CLAUDE_CODE_EXECPATH', '')
    if named and Path(named).is_file():
        return named
    found: str | None = shutil.which('claude')
    return found or ''


def step_verify(plan: Plan) -> list[str]:
    """Runs the checks; returns the failures (empty when all passed or were skipped)."""
    names: Names = plan.names
    heading(7, 'First verify')
    failures: list[str] = []
    harness: Path = plan.workspace / HARNESS_NAME
    app: Path = plan.workspace / names.app
    marketplace: Path = harness / 'claude'
    cli: str = claude_cli(plan)
    checks: list[tuple[str, list[str], bool]] = [
        ('plugin validate', [cli, 'plugin', 'validate', str(marketplace)],
         bool(cli) and (marketplace / '.claude-plugin' / 'marketplace.json').is_file()),
        ('house style', [sys.executable, '-B', str(harness / 'harness' / 'check_house_style.py'), str(app / 'src'),
                         str(app / 'tests')],
         (harness / 'harness' / 'check_house_style.py').is_file() and (app / 'src').is_dir()),
        ('lifetime rules', [sys.executable, '-B', str(harness / 'harness' / 'check_lifetime_rules.py'),
                            str(app / 'src'), str(app / 'tests')],
         (harness / 'harness' / 'check_lifetime_rules.py').is_file() and (app / 'src').is_dir()),
    ]
    label: str
    command: list[str]
    available: bool
    for label, command, available in checks:
        if plan.dry_run:
            say('  would run: ' + shlex.join(command))
            continue
        if not available:
            say('  ' + label + ': skipped (' + ('no Claude Code CLI found; pass --claude' if label == 'plugin validate'
                                                and not cli else 'not in the workspace') + ')')
            continue
        completed: subprocess.CompletedProcess[str] = subprocess.run(command, capture_output=True, text=True,
                                                                     check=False)
        last: str = (completed.stdout.strip().splitlines() or [''])[-1]
        say('  ' + label + ': ' + ('passed' if completed.returncode == 0 else 'FAILED') + (' (' + last + ')' if last
                                                                                          else ''))
        if completed.returncode != 0:
            failures.append(label + ': ' + (completed.stdout + completed.stderr).strip()[-2000:])
    remote_workspace: str = plan.build_host_workspace or '<build-host workspace>'
    smoke: str = ('sh ' + remote_workspace + '/template_harness/bootstrap.sh --workspace ' + remote_workspace
                  + ' --project ' + names.app + ' --in-house')
    if plan.build_host and plan.build_host_workspace and not plan.dry_run:
        say('  build-host smoke: + ssh ' + plan.build_host + ' ' + shlex.quote(smoke))
        completed = subprocess.run(['ssh', plan.build_host, smoke], capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            failures.append('build-host smoke: ' + (completed.stdout + completed.stderr).strip()[-2000:])
        else:
            say('  build-host smoke: passed')
    else:
        say('  build-host smoke (on the build host, never this laptop; the harness must be cloned there first):')
        say('    ' + smoke)
    return failures


def step_finish(plan: Plan) -> None:
    names: Names = plan.names
    heading(8, 'Ready')
    say('  Open Claude in ' + str(plan.workspace / names.app) + ' and trust the folder (the FranSushi plugins load '
        'from .claude/settings.json), then start with:')
    say('')
    say('    Read AGENTS.md, ../notes/HOUSE_STYLE.md and ../notes-' + names.app + '/planning/BRIEF.md. You are the '
        'orchestrator seat ' + names.app + '-orchestrator. Interview me to fill in the brief, then plan the first '
        'track for w1 and record it in ../projects/tracks/' + names.app + '/w1.md.')


def run(plan: Plan) -> int:
    say('new_app ' + plan.names.app + ' into ' + str(plan.workspace) + ' (remotes under ' + plan.remote.base + ')'
        + (' -- dry run: nothing will be changed' if plan.dry_run else ''))
    step_validate(plan)
    harness_commit: str = step_harness(plan)
    step_app(plan, harness_commit)
    step_notes(plan)
    step_projects(plan)
    step_workspace(plan, harness_commit)
    failures: list[str] = step_verify(plan)
    step_finish(plan)
    if failures:
        say('')
        say('new_app: the first verify failed:\n' + '\n'.join(failures))
        return 1
    say('')
    say('new_app finished' + (' (dry run: nothing was changed)' if plan.dry_run else ''))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--name', required=True, help='the app name, snake case (paint_box)')
    parser.add_argument('--workspace', required=True, type=Path, help='the sibling-layout workspace')
    parser.add_argument('--title', default='', help='the window title and display name (default: from --name)')
    parser.add_argument('--namespace', default='', help='the C++ namespace (default: --name)')
    parser.add_argument('--dry-run', action='store_true', help='print every step; change nothing')
    parser.add_argument('--remote-base', default=REMOTE_BASE, help='repositories are <base>/<name>.git')
    parser.add_argument('--create-repo', action='store_true', help='create the app repository when missing')
    parser.add_argument('--harness', type=Path, default=REPOSITORY,
                        help='the template_harness clone to commit the project file from (default: this one)')
    parser.add_argument('--no-restore', action='store_true', help='skip the toolchain and cache restore')
    parser.add_argument('--smoke', action='store_true', help='run the smoke build here (it compiles)')
    parser.add_argument('--skip-workspace', action='store_true', help='skip step 6')
    parser.add_argument('--build-host', default='', help='run the bootstrap smoke there over ssh (step 7)')
    parser.add_argument('--build-host-workspace', default='', help="the workspace on the build host")
    parser.add_argument('--claude', default='', help='the Claude Code CLI for plugin validate')
    parser.add_argument('--license', choices=LICENSES, default='proprietary',
                        help="the app's licence: proprietary (default, all rights reserved), mit, "
                             'polyform-noncommercial, or none (no LICENSE file)')
    parser.add_argument('--licensor', default='', help='the copyright holder (default: your git user.name)')
    parser.add_argument('--build-wrapper', default='m4ws', help="the build host wrapper agents build through")
    parser.add_argument('--workers-dir', default='', help='where worker clones live (default <workspace>/APP_workers)')
    parser.add_argument('--projects-dir', default='', help='the projects clone as agents name it')
    parser.add_argument('--notes-dir', default='', help='the notes clone as agents name it')
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        if args.license != 'none' and not (args.licensor or default_licensor()):
            raise NewAppError('no copyright holder: pass --licensor NAME (or set git user.name)')
        names: Names = derive_names(args.name, args.title, args.namespace)
        harness: Path = args.harness.resolve()
        if not (harness / '.git').exists():
            raise NewAppError('--harness ' + str(harness) + ' is not a git clone')
        plan: Plan = Plan(
            names=names, workspace=args.workspace.resolve(), harness=harness,
            remote=Remote(args.remote_base, args.dry_run, args.create_repo), dry_run=args.dry_run,
            restore=not args.no_restore, smoke=args.smoke, skip_workspace=args.skip_workspace,
            build_host=args.build_host, build_host_workspace=args.build_host_workspace, claude=args.claude,
            options={'license': args.license, 'licensor': args.licensor or default_licensor(), 'build_wrapper': args.build_wrapper, 'workers_dir': args.workers_dir,
                     'projects_dir': args.projects_dir, 'notes_dir': args.notes_dir})
        status: int = run(plan)
    except (NewAppError, ValueError) as error:
        print('new_app: ' + str(error), file=sys.stderr)
        return 1
    return status


if __name__ == '__main__':
    sys.exit(main())
