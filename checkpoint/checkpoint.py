#!/usr/bin/env python3
"""The local checkpoint tool: fold workers' local branches into one verified PR.

Workers commit on local branches in their own clones and never push. One
dedicated integration tree, named by the project file's [checkpoint] table,
folds their branches, verifies the result once, and pushes it as one PR:

    python3 checkpoint/checkpoint.py init [--from URL]
    python3 checkpoint/checkpoint.py fold <clone>:<branch> [<clone>:<branch> ...] [--title [N=]TEXT ...]
    python3 checkpoint/checkpoint.py verify
    python3 checkpoint/checkpoint.py push [--dry-run]
    python3 checkpoint/checkpoint.py after-merge

init      makes the integration tree: clones the project's repository there
          (or --from URL), or adopts an existing clean clone, and marks it
          (git config harness.checkpoint-tree true). Every other command
          refuses a tree without the mark, so a worker's clone is never
          folded into, reset or pushed from.
fold      fetches <remote>/<base> and resets the tree to it, then for each
          topic, in order, fetches the branch from the local clone (no
          network) and squashes it into one commit (owner decision 3: one
          commit per topic). The subject is the branch's first commit
          subject, or --title (N=TEXT for topic N; a bare TEXT when there
          is one topic); the body lists the folded commits. A topic that
          does not apply cleanly stops the fold: the files are named and
          the tree is reset to <remote>/<base>, never left half-folded. A
          topic with nothing left to add is skipped and logged. The fold
          log (a markdown list per topic) goes to <git dir>/checkpoint/.
verify    runs the project's [checkpoint] verify command from the workspace
          root, with HARNESS_CHECKPOINT_TREE naming the tree, and records
          pass or fail with the folded tree's hash.
push      refuses unless the last verify passed on exactly the current tree
          and head. Pushes one new branch <branch_prefix><date>-<n> (never
          with force), opens one PR with the fold log as its body, and
          turns on auto-merge with the project's merge method. --dry-run
          prints the commands instead.
after-merge
          reports, for each folded topic, whether its branch head is now
          contained in <remote>/<base> by tree content (merging it into
          <base> would change nothing), and prints the `git branch -D`
          commands for the contained ones. It never deletes anything.

The [checkpoint] table, read by harness/project.py (projects/gui_forms.toml has
GUI.Forms' values):

    tree          the integration tree, relative to the workspace root
    verify        the verification command, a list of arguments
    base          the branch folded onto and targeted by the PR (main)
    remote        the integration tree's remote (origin)
    branch_prefix the pushed branch's prefix (checkpoint/)
    merge_method  merge, squash or rebase, for gh pr merge --auto

State lives in the tree's git directory under checkpoint/: fold.json and
fold-log.md (written by a successful fold, removed when a fold starts),
verification.json (written by verify, removed by fold) and push.json.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

HARNESS: Path = Path(__file__).resolve().parents[1] / 'harness'
sys.path.insert(0, str(HARNESS))
import project  # noqa: E402

TREE_MARK: str = 'harness.checkpoint-tree'
TREE_VARIABLE: str = 'HARNESS_CHECKPOINT_TREE'
FOLD_BRANCH: str = 'checkpoint-fold'
TOPIC_REF_PREFIX: str = 'refs/checkpoint/topics/'
STATE_DIRECTORY: str = 'checkpoint'
FOLD_RECORD: str = 'fold.json'
FOLD_LOG: str = 'fold-log.md'
VERIFICATION_RECORD: str = 'verification.json'
PUSH_RECORD: str = 'push.json'
# Bounded search for a free branch name on one day.
MAXIMUM_BRANCHES_PER_DAY: int = 999
SHORT_SHA: int = 12


class CheckpointError(Exception):
    """An expected refusal or failure; the command prints it and exits 1."""


@dataclass(frozen=True)
class Settings:
    """The project file's [checkpoint] table, with the tree resolved against the workspace root."""
    tree: Path
    verify: tuple[str, ...]
    base: str
    remote: str
    branch_prefix: str
    merge_method: str
    # The product's repository (owner/name), for gh.
    repository: str


@dataclass(frozen=True)
class Topic:
    """One <clone>:<branch> argument."""
    clone: Path
    branch: str


def parse_settings(table: Any, workspace: Path, repository: str) -> Settings:
    """A [checkpoint] table (validated by project.checkpoint_table), with the tree resolved against workspace."""
    parsed: project.CheckpointTable | None = project.checkpoint_table(table)
    if parsed is None:
        raise ValueError('[checkpoint] is missing from the project file')
    settings: Settings = settings_from(parsed, workspace, repository)
    return settings


def settings_from(table: project.CheckpointTable, workspace: Path, repository: str) -> Settings:
    tree: Path = Path(table.tree)
    if not tree.is_absolute():
        tree = workspace / tree
    settings: Settings = Settings(
        tree=tree.resolve(), verify=table.verify, base=table.base, remote=table.remote,
        branch_prefix=table.branch_prefix, merge_method=table.merge_method, repository=repository)
    return settings


def load_settings() -> Settings:
    """The current project's [checkpoint] table (project.current())."""
    product: project.Project = project.current()
    if product.checkpoint is None:
        raise ValueError('[checkpoint] is missing from ' + str(product.path))
    settings: Settings = settings_from(product.checkpoint, product.workspace_root(), product.repository)
    return settings


def git(directory: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Runs git in directory, capturing text output; a failing command raises CheckpointError when check."""
    command: list[str] = ['git', '-C', str(directory), *arguments]
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        command, capture_output=True, text=True, encoding='utf-8', errors='replace', check=False)
    if check and completed.returncode != 0:
        raise CheckpointError(shlex.join(command) + ' failed: ' + completed.stderr.strip())
    return completed


def git_text(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = git(directory, *arguments)
    result: str = completed.stdout.strip()
    return result


def run_gh(arguments: list[str]) -> None:
    """The one place the tool calls gh (tests replace it)."""
    print(shlex.join(arguments), flush=True)
    subprocess.run(arguments, check=True)


def head_commit(tree: Path) -> str:
    return git_text(tree, 'rev-parse', 'HEAD')


def tree_hash(tree: Path) -> str:
    """HEAD's tree object. The %T format avoids HEAD^{tree}, which MSYS2 argument conversion can mangle."""
    return git_text(tree, 'show', '-s', '--format=%T', 'HEAD')


def tracked_changes(tree: Path) -> str:
    """Uncommitted changes to tracked files, including an unresolved merge; empty when clean."""
    return git_text(tree, 'status', '--porcelain', '--untracked-files=no')


def state_directory(tree: Path) -> Path:
    git_directory: Path = Path(git_text(tree, 'rev-parse', '--absolute-git-dir'))
    directory: Path = git_directory / STATE_DIRECTORY
    directory.mkdir(exist_ok=True)
    return directory


def read_record(tree: Path, name: str) -> dict[str, Any] | None:
    path: Path = state_directory(tree) / name
    if not path.is_file():
        return None
    record: dict[str, Any] = json.loads(path.read_text(encoding='utf-8'))
    return record


def write_record(tree: Path, name: str, record: dict[str, Any]) -> None:
    path: Path = state_directory(tree) / name
    path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')


def require_tree(settings: Settings) -> Path:
    """The integration tree, which must be a marked git checkout with no uncommitted tracked changes."""
    tree: Path = settings.tree
    if not tree.is_dir():
        raise CheckpointError('No integration tree at ' + str(tree) + '; run `checkpoint init`')
    mark: subprocess.CompletedProcess[str] = git(tree, 'config', '--local', '--get', TREE_MARK, check=False)
    if mark.returncode != 0 or mark.stdout.strip() != 'true':
        raise CheckpointError(str(tree) + ' is not marked as the integration tree (' + TREE_MARK
                              + '); run `checkpoint init` to adopt a dedicated clone, never a worker\'s')
    changes: str = tracked_changes(tree)
    if changes:
        raise CheckpointError(str(tree) + ' has uncommitted changes to tracked files:\n' + changes)
    return tree


def init_tree(settings: Settings, source: str) -> None:
    """Clones source into the tree, or adopts an existing clean clone; then marks it."""
    tree: Path = settings.tree
    if tree.exists():
        if not (tree / '.git').exists():
            raise CheckpointError(str(tree) + ' exists and is not a git checkout')
        changes: str = tracked_changes(tree)
        if changes:
            raise CheckpointError(str(tree) + ' has uncommitted changes to tracked files; not adopting it')
        print('Adopting the existing clone ' + str(tree), flush=True)
    else:
        tree.parent.mkdir(parents=True, exist_ok=True)
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            ['git', 'clone', '--quiet', '--origin', settings.remote, source, str(tree)],
            capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise CheckpointError('git clone ' + source + ' failed: ' + completed.stderr.strip())
        print('Cloned ' + source + ' into ' + str(tree), flush=True)
    git(tree, 'config', '--local', TREE_MARK, 'true')


def parse_topic(text: str) -> Topic:
    """<clone>:<branch>, split at the last colon (a branch name has none; a Windows path may)."""
    clone_text: str
    separator: str
    branch: str
    clone_text, separator, branch = text.rpartition(':')
    if not separator or not clone_text or not branch:
        raise CheckpointError('A topic is <clone>:<branch>, not ' + repr(text))
    clone: Path = Path(clone_text).resolve()
    if not (clone / '.git').exists():
        raise CheckpointError(clone_text + ' is not a git clone (topic ' + text + ')')
    format_check: subprocess.CompletedProcess[str] = git(clone, 'check-ref-format', '--branch', branch, check=False)
    if format_check.returncode != 0:
        raise CheckpointError(repr(branch) + ' is not a branch name (topic ' + text + ')')
    return Topic(clone, branch)


def parse_titles(titles: list[str], topic_count: int) -> dict[int, str]:
    """--title values by topic index (0-based): N=TEXT for topic N, or a bare TEXT for a single topic."""
    result: dict[int, str] = {}
    title: str
    for title in titles:
        number_text: str
        separator: str
        text: str
        number_text, separator, text = title.partition('=')
        if separator and number_text.isdigit():
            number: int = int(number_text)
            if number < 1 or number > topic_count:
                raise CheckpointError('--title ' + title + ' names topic ' + number_text + ' of ' + str(topic_count))
            result[number - 1] = text
        elif topic_count == 1:
            result[0] = title
        else:
            raise CheckpointError('With several topics, a title is N=TEXT: ' + repr(title))
    empty: int
    for empty in result:
        if not result[empty].strip():
            raise CheckpointError('Topic ' + str(empty + 1) + ' has an empty title')
    return result


def topic_commits(tree: Path, base: str, head: str) -> list[tuple[str, str]]:
    """The topic's own commits, oldest first, as (sha, subject); merges of main into the topic are left out."""
    log: str = git_text(tree, 'log', '--reverse', '--no-merges', '--format=%H%x1f%s', base + '..' + head)
    commits: list[tuple[str, str]] = []
    line: str
    for line in log.splitlines():
        sha: str
        subject: str
        sha, _, subject = line.partition('\x1f')
        commits.append((sha, subject))
    return commits


def commit_message(title: str, topic: Topic, head: str, commits: list[tuple[str, str]]) -> str:
    lines: list[str] = [title, '', 'Folded from ' + topic.branch + ' in ' + str(topic.clone) + ' at ' + head + ':']
    commit: tuple[str, str]
    for commit in commits:
        lines.append('- ' + commit[0][:SHORT_SHA] + ' ' + commit[1])
    message: str = '\n'.join(lines) + '\n'
    return message


def fold_log(record: dict[str, Any]) -> str:
    """The fold log: the base, then a markdown list per topic with its folded commits."""
    lines: list[str] = ['## Fold log', '',
                        'Base: `' + record['remote'] + '/' + record['base'] + '` at `' + record['base_commit'] + '`.',
                        '']
    entry: dict[str, Any]
    for entry in record['topics']:
        where: str = '`' + entry['branch'] + '` from `' + entry['clone'] + '`, head `' + entry['head'][:SHORT_SHA] + '`'
        if entry['folded'] is None:
            lines.append('- **' + entry['branch'] + '** (' + where + '): skipped, ' + entry['note'])
            continue
        lines.append('- **' + entry['title'] + '** (' + where + '), folded as `' + entry['folded'][:SHORT_SHA] + '`')
        commit: list[str]
        for commit in entry['commits']:
            lines.append('  - `' + commit[0][:SHORT_SHA] + '` ' + commit[1])
    text: str = '\n'.join(lines) + '\n'
    return text


def fold(settings: Settings, topic_texts: list[str], titles: list[str]) -> dict[str, Any]:
    """Folds the topics onto <remote>/<base>; returns the fold record. On any failure the tree is reset to the base."""
    tree: Path = require_tree(settings)
    topics: list[Topic] = []
    text: str
    for text in topic_texts:
        topic: Topic = parse_topic(text)
        if topic.clone == tree:
            raise CheckpointError('The integration tree cannot fold its own branch: ' + text)
        topics.append(topic)
    titled: dict[int, str] = parse_titles(titles, len(topics))
    state: Path = state_directory(tree)
    name: str
    for name in (FOLD_RECORD, FOLD_LOG, VERIFICATION_RECORD):
        (state / name).unlink(missing_ok=True)
    remote_ref: str = 'refs/remotes/' + settings.remote + '/' + settings.base
    git(tree, 'fetch', '--quiet', '--no-tags', settings.remote, '+refs/heads/' + settings.base + ':' + remote_ref)
    base_commit: str = git_text(tree, 'rev-parse', remote_ref)
    git(tree, 'switch', '--quiet', '--force-create', FOLD_BRANCH, base_commit)
    entries: list[dict[str, Any]] = []
    try:
        index: int
        for index in range(len(topics)):
            entries.append(fold_topic(tree, base_commit, index, topics[index], titled.get(index)))
    except BaseException:
        git(tree, 'reset', '--quiet', '--hard', base_commit)
        raise
    folded_count: int = 0
    entry: dict[str, Any]
    for entry in entries:
        if entry['folded'] is not None:
            folded_count += 1
    if folded_count == 0:
        raise CheckpointError('Nothing to fold: every topic is already on ' + settings.remote + '/' + settings.base)
    record: dict[str, Any] = {'remote': settings.remote, 'base': settings.base, 'base_commit': base_commit,
                              'head': head_commit(tree), 'tree': tree_hash(tree), 'topics': entries}
    write_record(tree, FOLD_RECORD, record)
    (state / FOLD_LOG).write_text(fold_log(record), encoding='utf-8')
    print('Folded ' + str(folded_count) + ' topic(s) onto ' + base_commit[:SHORT_SHA] + '; log: '
          + str(state / FOLD_LOG), flush=True)
    return record


def fold_topic(tree: Path, base_commit: str, index: int, topic: Topic, title: str | None) -> dict[str, Any]:
    """Squashes one topic onto HEAD as one commit, or raises naming the conflicting files."""
    topic_ref: str = TOPIC_REF_PREFIX + str(index + 1)
    git(tree, 'fetch', '--quiet', '--no-tags', str(topic.clone), '+refs/heads/' + topic.branch + ':' + topic_ref)
    head: str = git_text(tree, 'rev-parse', topic_ref)
    commits: list[tuple[str, str]] = topic_commits(tree, base_commit, head)
    entry: dict[str, Any] = {'clone': str(topic.clone), 'branch': topic.branch, 'head': head,
                             'commits': commits, 'title': None, 'folded': None, 'note': ''}
    if not commits:
        entry['note'] = 'no commits beyond the base'
        print('Skipped ' + topic.branch + ': ' + entry['note'], flush=True)
        return entry
    squash: subprocess.CompletedProcess[str] = git(tree, 'merge', '--squash', '--quiet', head, check=False)
    if squash.returncode != 0:
        conflicted: str = git_text(tree, 'diff', '--name-only', '--diff-filter=U')
        detail: str = conflicted if conflicted else squash.stdout.strip() + squash.stderr.strip()
        raise CheckpointError('Topic ' + topic.branch + ' from ' + str(topic.clone) + ' does not apply cleanly; '
                              'the tree is reset to the base. Conflicting files:\n' + detail)
    staged: subprocess.CompletedProcess[str] = git(tree, 'diff', '--cached', '--quiet', check=False)
    if staged.returncode == 0:
        entry['note'] = 'its changes are already in the fold'
        print('Skipped ' + topic.branch + ': ' + entry['note'], flush=True)
        return entry
    subject: str = title if title is not None else commits[0][1]
    message_path: Path = state_directory(tree) / 'message.txt'
    message_path.write_text(commit_message(subject, topic, head, commits), encoding='utf-8')
    git(tree, 'commit', '--quiet', '--file', str(message_path))
    entry['title'] = subject
    entry['folded'] = head_commit(tree)
    print('Folded ' + topic.branch + ' (' + str(len(commits)) + ' commit(s)) as ' + entry['folded'][:SHORT_SHA]
          + ': ' + subject, flush=True)
    return entry


def require_fold(tree: Path) -> dict[str, Any]:
    record: dict[str, Any] | None = read_record(tree, FOLD_RECORD)
    if record is None:
        raise CheckpointError('No fold recorded in ' + str(tree) + '; run `checkpoint fold` first')
    return record


def verify(settings: Settings, workspace: Path) -> bool:
    """Runs the verification command over the folded tree and records the result; True when it passed."""
    tree: Path = require_tree(settings)
    record: dict[str, Any] = require_fold(tree)
    head: str = head_commit(tree)
    if head != record['head']:
        raise CheckpointError('The tree moved since the fold (folded ' + record['head'][:SHORT_SHA] + ', now '
                              + head[:SHORT_SHA] + '); fold again')
    before: str = tree_hash(tree)
    environment: dict[str, str] = dict(os.environ)
    environment[TREE_VARIABLE] = str(tree)
    print('Verifying ' + before[:SHORT_SHA] + ': ' + shlex.join(settings.verify), flush=True)
    completed: subprocess.CompletedProcess[bytes] = subprocess.run(
        list(settings.verify), cwd=str(workspace), env=environment, check=False)
    passed: bool = completed.returncode == 0
    reason: str = '' if passed else 'the command exited with ' + str(completed.returncode)
    if tree_hash(tree) != before or head_commit(tree) != head or tracked_changes(tree):
        passed = False
        reason = 'the verification changed the tree'
    result: dict[str, Any] = {
        'result': 'pass' if passed else 'fail', 'reason': reason, 'tree': before, 'head': head,
        'command': list(settings.verify), 'returncode': completed.returncode,
        'finished': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
    write_record(tree, VERIFICATION_RECORD, result)
    print('Verification ' + result['result'] + (': ' + reason if reason else ''), flush=True)
    return passed


def require_verified(tree: Path) -> dict[str, Any]:
    """The fold record, after checking the last verification passed on exactly the current head and tree."""
    record: dict[str, Any] = require_fold(tree)
    verification: dict[str, Any] | None = read_record(tree, VERIFICATION_RECORD)
    if verification is None:
        raise CheckpointError('The fold is not verified; run `checkpoint verify`')
    if verification['result'] != 'pass':
        raise CheckpointError('The last verification failed (' + verification['reason'] + '); not pushing')
    if verification['tree'] != tree_hash(tree) or verification['head'] != head_commit(tree):
        raise CheckpointError('The tree changed since it was verified; run `checkpoint verify` again')
    return record


def free_branch(tree: Path, settings: Settings, today: str) -> str:
    """The first <branch_prefix><today>-<n> the remote does not have."""
    number: int
    for number in range(1, MAXIMUM_BRANCHES_PER_DAY + 1):
        name: str = settings.branch_prefix + today + '-' + str(number)
        listed: str = git_text(tree, 'ls-remote', '--heads', settings.remote, 'refs/heads/' + name)
        if not listed:
            return name
    raise CheckpointError('No free branch name for ' + today)


def pull_request_title(record: dict[str, Any], branch: str, settings: Settings) -> str:
    titles: list[str] = []
    entry: dict[str, Any]
    for entry in record['topics']:
        if entry['folded'] is not None:
            titles.append(entry['title'])
    if len(titles) == 1:
        return titles[0]
    label: str = branch[len(settings.branch_prefix):]
    title: str = 'Checkpoint ' + label + ': ' + str(len(titles)) + ' topics'
    return title


def push(settings: Settings, dry_run: bool, today: str) -> list[list[str]]:
    """Pushes the verified fold as a new branch, opens its PR and turns on auto-merge; returns the commands."""
    tree: Path = require_tree(settings)
    record: dict[str, Any] = require_verified(tree)
    branch: str = free_branch(tree, settings, today)
    log_path: Path = state_directory(tree) / FOLD_LOG
    push_command: list[str] = ['git', '-C', str(tree), 'push', settings.remote, 'HEAD:refs/heads/' + branch]
    create_command: list[str] = ['gh', 'pr', 'create', '--repo', settings.repository, '--base', settings.base,
                                 '--head', branch, '--title', pull_request_title(record, branch, settings),
                                 '--body-file', str(log_path)]
    merge_command: list[str] = ['gh', 'pr', 'merge', branch, '--repo', settings.repository, '--auto',
                                '--' + settings.merge_method]
    commands: list[list[str]] = [push_command, create_command, merge_command]
    if dry_run:
        command: list[str]
        for command in commands:
            print(shlex.join(command), flush=True)
        return commands
    print(shlex.join(push_command), flush=True)
    pushed: subprocess.CompletedProcess[str] = subprocess.run(push_command, capture_output=True, text=True,
                                                              check=False)
    if pushed.returncode != 0:
        raise CheckpointError('git push failed: ' + pushed.stderr.strip())
    write_record(tree, PUSH_RECORD, {'branch': branch, 'head': head_commit(tree), 'tree': tree_hash(tree)})
    run_gh(create_command)
    run_gh(merge_command)
    return commands


def contained(tree: Path, main: str, head: str) -> bool:
    """Whether merging head into main would change nothing: an ancestor, or a clean merge whose tree is main's."""
    ancestor: subprocess.CompletedProcess[str] = git(tree, 'merge-base', '--is-ancestor', head, main, check=False)
    if ancestor.returncode == 0:
        return True
    merged: subprocess.CompletedProcess[str] = git(tree, 'merge-tree', '--write-tree', main, head, check=False)
    if merged.returncode != 0:
        return False
    merged_tree: str = merged.stdout.splitlines()[0].strip()
    main_tree: str = git_text(tree, 'show', '-s', '--format=%T', main)
    return merged_tree == main_tree


def after_merge(settings: Settings) -> list[str]:
    """Reports each folded topic's state against <remote>/<base>; returns the suggested delete commands."""
    tree: Path = require_tree(settings)
    record: dict[str, Any] = require_fold(tree)
    remote_ref: str = 'refs/remotes/' + settings.remote + '/' + settings.base
    git(tree, 'fetch', '--quiet', '--no-tags', settings.remote, '+refs/heads/' + settings.base + ':' + remote_ref)
    main: str = git_text(tree, 'rev-parse', remote_ref)
    suggestions: list[str] = []
    index: int
    for index in range(len(record['topics'])):
        entry: dict[str, Any] = record['topics'][index]
        clone: Path = Path(entry['clone'])
        label: str = entry['branch'] + ' in ' + str(clone)
        listed: subprocess.CompletedProcess[str] = git(tree, 'ls-remote', '--heads', str(clone),
                                                       'refs/heads/' + entry['branch'], check=False)
        if listed.returncode != 0 or not listed.stdout.strip():
            print(label + ': the branch is gone', flush=True)
            continue
        current: str = listed.stdout.split()[0]
        moved: str = ''
        if current != entry['head']:
            moved = ' (moved since the fold: folded ' + entry['head'][:SHORT_SHA] + ', now ' + current[:SHORT_SHA] + ')'
        topic_ref: str = 'refs/checkpoint/after/' + str(index + 1)
        git(tree, 'fetch', '--quiet', '--no-tags', str(clone), '+refs/heads/' + entry['branch'] + ':' + topic_ref)
        if contained(tree, main, current):
            print(label + ': contained in ' + settings.remote + '/' + settings.base + moved, flush=True)
            suggestions.append(shlex.join(['git', '-C', str(clone), 'branch', '-D', entry['branch']]))
        else:
            print(label + ': not yet in ' + settings.remote + '/' + settings.base + moved, flush=True)
    if suggestions:
        print('To remove the contained branches (not run; a branch checked out in its clone must be left first):',
              flush=True)
        suggestion: str
        for suggestion in suggestions:
            print('  ' + suggestion, flush=True)
    return suggestions


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands: argparse._SubParsersAction = parser.add_subparsers(dest='command', required=True)
    init_parser: argparse.ArgumentParser = commands.add_parser('init', help='make or adopt the integration tree')
    init_parser.add_argument('--from', dest='source', default='', help='clone URL (default: the project repository)')
    fold_parser: argparse.ArgumentParser = commands.add_parser('fold', help='fold topics onto the base')
    fold_parser.add_argument('topics', nargs='+', metavar='<clone>:<branch>')
    fold_parser.add_argument('--title', action='append', default=[], metavar='[N=]TEXT',
                             help='the commit subject for topic N (a bare TEXT when there is one topic)')
    commands.add_parser('verify', help='run the verification command and record the result')
    push_parser: argparse.ArgumentParser = commands.add_parser('push', help='push the verified fold and open its PR')
    push_parser.add_argument('--dry-run', action='store_true', help='print the commands instead of running them')
    commands.add_parser('after-merge', help='report which folded branches main now contains')
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        settings: Settings = load_settings()
        if args.command == 'init':
            source: str = args.source or 'https://github.com/' + settings.repository + '.git'
            init_tree(settings, source)
        elif args.command == 'fold':
            fold(settings, args.topics, args.title)
        elif args.command == 'verify':
            if not verify(settings, project.current().workspace_root()):
                return 1
        elif args.command == 'push':
            today: str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
            push(settings, args.dry_run, today)
        else:
            after_merge(settings)
    except (CheckpointError, ValueError) as error:
        print('checkpoint: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
