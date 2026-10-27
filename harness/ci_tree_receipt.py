#!/usr/bin/env python3
"""Skip a CI job whose work an earlier run already passed on the same tree.

native.yml's docs and Sanitizers jobs build and check a checkout's tree
and nothing else: the same tree gives the same result (the documentation
requirements are pinned in docs/requirements.txt; the sanitizer toolchain
is the runner image's, as on the run that passed). When such a job passes,
`write` records a receipt naming the job's kind, the tree, the revision it
checked out, the run, and what the run covered. The job uploads it as the
artifact <artifact prefix>tree-<kind>-<tree> (gui-tree-... for GUI.Forms).

`lookup`, at the start of the same job in a later run, finds that artifact
for its own tree and accepts it only when:
- the producer run is an older native.yml run of this repository (a push,
  a merge group, a pull request from a branch of it, or a direct run),
  never a fork's;
- the producer's job named --job succeeded;
- the artifact's digest and the receipt inside it match, and the receipt
  names this tree and the producer run;
- the producer checked out this tree: its head has it, or the receipt's
  revision is a merge of its head with it (a pull request's test merge
  commit, as tools/ci_reuse.py checks).
It then writes the step outputs reused=true, producer-run and coverage,
and the job skips its build and checks and passes under its own name.
Otherwise reused=false and the job runs as before. Any error looking up a
receipt also leaves reused=false: a lookup never fails a job.

A job that reused one writes its own receipt with `write --carried-from`,
naming the run it carried and that run's coverage, so every run of the
tree leaves a record of what passed it.

A pull request's Sanitizers run builds and tests only what its changed
paths can affect (tools/ci_build.py --changed-from); its coverage is
`selected`, or `nothing` when no test reads them. A merge group, and the
push to main that makes its commit main's, take that receipt all the
same (the owner's choice of 2026-10-23: the sanitizers need to run only
on new code). A merge group of the pull request's tree carries its run;
one of another tree (main moved on) selects afresh from the main commit
it extends (merge_group.base_sha), and main's push then carries the merge
group's run. native.yml's daily scheduled run looks up nothing and runs
the whole suite, as the backstop for tools/change_selection.py.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import zipfile

import project

SOURCE: Path = project.current().source_root()
SCHEMA: int = 1
RECEIPT_NAME: str = 'receipt.json'
RECEIPT_BOUND: int = 64 * 1024
ARTIFACTS_READ: int = 30
WORKFLOW: str = project.current().native_workflow
# A merge group runs on a temporary branch of this repository, the commit
# main then becomes.
TRUSTED_EVENTS: tuple[str, ...] = ('push', 'merge_group', 'pull_request', 'workflow_dispatch')
# What a run covered, from least to most. ci_build.py's build record names
# the first three for a sanitized build (BUILT_NOTHING, BUILT_SELECTED,
# BUILT_EVERYTHING); a job that has no narrowing records everything.
COVERAGES: tuple[str, ...] = ('nothing', 'selected', 'everything')


def git_field(format_code: str) -> str:
    # git show --format, not rev-parse HEAD^{tree}: MSYS2's argument
    # conversion can turn the braces into HEAD^tree (tools/ci_reuse.py).
    field: str = subprocess.check_output(
        ['git', '-C', str(SOURCE), 'show', '-s', '--format=' + format_code, 'HEAD'], text=True).strip()
    return field


def artifact_name(kind: str, tree: str) -> str:
    return project.current().artifact('tree-' + kind + '-' + tree)


def receipt_for(kind: str, tree: str, revision: str, run_id: str, coverage: str,
                carried_from: str = '') -> dict[str, Any]:
    """A receipt; carried_from names the run whose receipt this run reused, or is empty."""
    receipt: dict[str, Any] = {'schema': SCHEMA, 'kind': kind, 'tree': tree, 'revision': revision,
                               'run_id': run_id, 'coverage': coverage, 'carried_from': carried_from}
    return receipt


def coverage_of_record(record: Path) -> str:
    """The coverage a ci_build.py build record names, or 'nothing' when it has none.

    A run that failed leaves no record, and nothing is a receipt no lookup
    that requires coverage accepts.
    """
    if not record.is_file():
        return 'nothing'
    built: Any = json.loads(record.read_text(encoding='utf-8')).get('built')
    if built in COVERAGES:
        return str(built)
    return 'nothing'


class GitHub:
    """The repository's artifacts, runs, jobs and commits, through `gh api`."""

    def __init__(self, repository: str) -> None:
        self.repository: str = repository

    def api(self, endpoint: str) -> Any:
        return json.loads(subprocess.check_output(['gh', 'api', 'repos/' + self.repository + '/' + endpoint],
                                                  text=True))

    def artifacts(self, name: str) -> list[dict[str, Any]]:
        listing: dict[str, Any] = self.api('actions/artifacts?per_page=' + str(ARTIFACTS_READ) + '&name=' + name)
        artifacts: list[dict[str, Any]] = listing['artifacts']
        return artifacts

    def run(self, run_id: int) -> dict[str, Any]:
        run: dict[str, Any] = self.api('actions/runs/' + str(run_id))
        return run

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        listing: dict[str, Any] = self.api('actions/runs/' + str(run_id) + '/jobs?per_page=100')
        jobs: list[dict[str, Any]] = listing['jobs']
        return jobs

    def commit(self, revision: str) -> dict[str, Any]:
        commit: dict[str, Any] = self.api('git/commits/' + revision)
        return commit

    def download(self, artifact_id: int) -> bytes:
        data: bytes = subprocess.check_output(['gh', 'api', 'repos/' + self.repository + '/actions/artifacts/'
                                               + str(artifact_id) + '/zip'])
        return data


def trusted_run(run: dict[str, Any], repository: str) -> bool:
    return (run.get('path') == WORKFLOW
            and run.get('repository', {}).get('full_name') == repository
            and run.get('head_repository', {}).get('full_name') == repository
            and run.get('event') in TRUSTED_EVENTS)


def job_succeeded(jobs: list[dict[str, Any]], name: str) -> bool:
    """Whether exactly one job is named name and it succeeded."""
    named: list[dict[str, Any]] = []
    job: dict[str, Any]
    for job in jobs:
        if job.get('name') == name:
            named.append(job)
    return len(named) == 1 and named[0].get('conclusion') == 'success'


def receipt_from_zip(data: bytes, digest: str) -> dict[str, Any] | None:
    """The receipt in an artifact's zip, or None when the digest, member or contents are wrong."""
    if 'sha256:' + hashlib.sha256(data).hexdigest() != digest:
        return None
    try:
        archive: zipfile.ZipFile
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.namelist() != [RECEIPT_NAME]:
                return None
            entry: zipfile.ZipInfo = archive.getinfo(RECEIPT_NAME)
            if entry.file_size > RECEIPT_BOUND:
                return None
            receipt: Any = json.loads(archive.read(entry))
    except (zipfile.BadZipFile, ValueError):
        return None
    if not isinstance(receipt, dict):
        return None
    return receipt


def matching_receipt(receipt: dict[str, Any], kind: str, tree: str, producer: int) -> bool:
    return (receipt.get('schema') == SCHEMA and receipt.get('kind') == kind and receipt.get('tree') == tree
            and receipt.get('run_id') == str(producer) and receipt.get('coverage') in COVERAGES)


def checked_out_tree(github: GitHub, run: dict[str, Any], receipt: dict[str, Any], tree: str) -> bool:
    """Whether the producer checked out tree: its head has it, or the receipt's revision merges its head."""
    if github.commit(str(run['head_sha']))['tree']['sha'] == tree:
        return True
    revision: Any = receipt.get('revision')
    if not isinstance(revision, str) or not revision:
        return False
    commit: dict[str, Any] = github.commit(revision)
    if commit['tree']['sha'] != tree:
        return False
    parent: dict[str, Any]
    for parent in commit.get('parents', []):
        if parent.get('sha') == run['head_sha']:
            return True
    return False


def lookup(github: GitHub, kind: str, job: str, tree: str, current_run: int) -> dict[str, Any] | None:
    """The receipt that lets this job skip its work, or None."""
    artifact: dict[str, Any]
    for artifact in github.artifacts(artifact_name(kind, tree)):
        producer: int = int(artifact['workflow_run']['id'])
        # Older runs only: a rerun of this run, or a newer one, checks afresh.
        if producer >= current_run or artifact.get('expired') or artifact.get('name') != artifact_name(kind, tree):
            continue
        if int(artifact.get('size_in_bytes', RECEIPT_BOUND + 1)) > RECEIPT_BOUND:
            continue
        run: dict[str, Any] = github.run(producer)
        if not trusted_run(run, github.repository):
            print('Run ' + str(producer) + ' is not a trusted native.yml run', flush=True)
            continue
        if not job_succeeded(github.jobs(producer), job):
            print('Run ' + str(producer) + "'s job " + job + ' did not succeed', flush=True)
            continue
        receipt: dict[str, Any] | None = receipt_from_zip(github.download(int(artifact['id'])),
                                                          str(artifact.get('digest', '')))
        if receipt is None or not matching_receipt(receipt, kind, tree, producer):
            print('Run ' + str(producer) + "'s receipt does not match this tree and run", flush=True)
            continue
        if not checked_out_tree(github, run, receipt, tree):
            print('Run ' + str(producer) + ' did not check out this tree', flush=True)
            continue
        print('Run ' + str(producer) + ' passed ' + job + ' on tree ' + tree + ' (covered '
              + str(receipt['coverage']) + '); reusing it', flush=True)
        return receipt
    print('No earlier run passed ' + job + ' on tree ' + tree + '; running it', flush=True)
    return None


def write_output(name: str, value: str) -> None:
    path: str = os.environ.get('GITHUB_OUTPUT', '')
    if path:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(name + '=' + value + '\n')


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands: Any = parser.add_subparsers(dest='command', required=True)
    write_parser: argparse.ArgumentParser = commands.add_parser('write', help="record this job's receipt")
    write_parser.add_argument('--kind', required=True)
    write_parser.add_argument('--out', type=Path, required=True, help='directory to write receipt.json into')
    write_parser.add_argument('--build-record', type=Path, default=None,
                              help="ci_build.py's build record; without it the job covered everything")
    write_parser.add_argument('--carried-from', default='', help='the run whose receipt this job reused')
    write_parser.add_argument('--coverage', choices=COVERAGES, default=None,
                              help="with --carried-from, that run's coverage")
    lookup_parser: argparse.ArgumentParser = commands.add_parser('lookup', help="find an earlier run's receipt")
    lookup_parser.add_argument('--kind', required=True)
    lookup_parser.add_argument('--job', required=True, help="the job's name, as the producer run reports it")
    lookup_parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY'))
    args: argparse.Namespace = parser.parse_args(argv)
    tree: str = git_field('%T')
    if args.command == 'write':
        coverage: str = 'everything'
        if args.carried_from:
            if args.coverage is None:
                parser.error('--carried-from needs --coverage')
            coverage = args.coverage
        elif args.build_record is not None:
            coverage = coverage_of_record(args.build_record)
        receipt: dict[str, Any] = receipt_for(args.kind, tree, git_field('%H'), os.environ['GITHUB_RUN_ID'],
                                              coverage, args.carried_from)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / RECEIPT_NAME).write_text(json.dumps(receipt, indent=1) + '\n', encoding='utf-8')
        write_output('artifact', artifact_name(args.kind, tree))
        carried: str = ''
        if args.carried_from:
            carried = ', carried from run ' + args.carried_from
        print('Receipt for ' + args.kind + ' on tree ' + tree + ': covered ' + coverage + carried, flush=True)
        return 0
    if not args.repo:
        parser.error('lookup needs --repo or GITHUB_REPOSITORY')
    found: dict[str, Any] | None = None
    try:
        found = lookup(GitHub(args.repo), args.kind, args.job, tree, int(os.environ['GITHUB_RUN_ID']))
    except (subprocess.CalledProcessError, KeyError, ValueError, TypeError) as error:
        print('Receipt lookup failed (' + str(error) + '); running the job', flush=True)
        found = None
    if found is None:
        write_output('reused', 'false')
        write_output('producer-run', '')
        write_output('coverage', '')
        return 0
    write_output('reused', 'true')
    write_output('producer-run', str(found['run_id']))
    write_output('coverage', str(found['coverage']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
