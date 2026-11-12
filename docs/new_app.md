# A new harnessed GUI.Forms app in one command

`new_app` creates a GUI.Forms application that is already a harness instance
(`docs/adopting.md`'s whole checklist, done), with its notes branch, its
coordination files, a workspace in the sibling layout, and the FranSushi
plugins enabled, so a Claude session opened in it can start implementing.

```sh
gh repo clone malkuth-software/template_harness
sh template_harness/new_app.sh --name paint_box --workspace ~/Developer/paint_box_ws --create-repo --dry-run
sh template_harness/new_app.sh --name paint_box --workspace ~/Developer/paint_box_ws --create-repo
```

From PowerShell, `new_app.ps1` runs `new_app.sh` with Git Bash. Python 3.11
or later, `git`, and for `--create-repo` an authenticated `gh` are needed.
Run `--dry-run` first: it prints every step, every repository and every path
it would touch, and changes nothing.

## Repositories

Each concern lives in its own repository (owner, 2026-11-11):

| Repository | What new_app writes there |
|---|---|
| `malkuth-software/template_harness` | `projects/<app>.toml` and `harness/test_project_names_<app>.py`, one commit on `main`, pushed. That commit is the app's harness pin. |
| `malkuth-software/<app>` (created private) | The app, from `templates/app/`: one commit on `main`. |
| `malkuth-software/notes` | An orphan branch `<app>`: `README.md`, `planning/BRIEF.md`, `decisions/`, `interviews/`. |
| `malkuth-software/projects` | `tracks/<app>/` (with `w1.md`), `desks/<app>/` (README, integration queue) and the seats `desks/seats/<app>-orchestrator.md`, `<app>-w1.md`, `<app>-integrator.md`, one commit on `main`. |

The owner runs the command, so the harness commit is pushed to `main`
directly; there is no PR to wait for.

## The steps

Each step is idempotent: a re-run finds its work done, says so, and pushes
nothing.

1. **The name.** `--name` is snake case (`paint_box`); it may not start with
   `gui` or name a workspace directory. The other names follow
   `docs/adopting.md` §2: type `PaintBox`, namespace `paint_box`, build tree
   `.build/native-<platform>/paint-box`, environment `PAINT_BOX_*`,
   artifacts `paint-box-*`, caches `paint-box-cache-<platform>.tar.gz`, and
   GUI.Forms' published Windows toolchain. A name that overlaps another
   project file's (the rules of `harness/test_project_prefixes.py`) stops
   the run before anything is pushed. `--title` and `--namespace` override
   the derived title and namespace.
2. **template_harness.** In a temporary worktree of `--harness` (default:
   this clone) on the remote's `main`, write the project file and its names
   test from `templates/harness/`, commit, push. Your own checkout is not
   touched; pull it afterwards if you want the file locally.
3. **The app repository.** Created with `gh repo create --private` when
   missing and `--create-repo` is given. `templates/app/` is rendered with
   the names and the pin, committed once and pushed.
4. **notes.** The branch `<app>` with the brief and decisions skeleton.
5. **projects.** Tracks, desk and seats.
6. **The workspace.** `harness/bootstrap.py --in-house` clones the app,
   checks the harness out at the pin, clones the notes with the
   `notes-<app>` worktree and the projects repository, and restores the
   toolchain and compiler cache (`--no-restore` skips it). The smoke build
   compiles, so it runs only with `--smoke`; never on a laptop that must not
   compile.
7. **First verify.** `claude plugin validate` on the pinned harness's
   `claude/` (the CLI is `--claude`, else `CLAUDE_CODE_EXECPATH`, else
   `claude` on `PATH`; skipped when none is found), then the house-style and
   lifetime checkers over the app's sources. The bootstrap smoke on the
   build host runs over ssh with `--build-host HOST --build-host-workspace
   DIR` (the harness must already be cloned there); otherwise its command is
   printed.
8. **Ready.** It prints where to open Claude and a starter prompt.

## The generated app

| Path | What |
|---|---|
| `src/`, `tests/`, `CMakeLists.txt` | the example app's greeter form, renamed: namespace, stable ids, executable and window title |
| `ci_build.py`, `fetch_sdk.py`, `gui_forms.lock`, `test_fetch_sdk.py` | the build against the pinned GUI.Forms source SDK; the harness is `../template_harness` (or `HARNESS_ROOT`) |
| `harness.lock` | the harness pin: the step 2 commit |
| `.github/workflows/ci.yml` | house style, the state-machine gate and the four native builds through the harness's reusable workflows, `@<pin>` |
| `.claude/settings.json` | the `fransushi` marketplace (github source: this repository, `path` `claude/.claude-plugin/marketplace.json`, `ref` the pin) and every plugin enabled except sushidelivery |
| `.claude/agents/<app>-{coder,integrator,tedium}.md` | project agents, from GUI.Forms' worker definitions |
| `CLAUDE.md`, `AGENTS.md` | imports AGENTS.md and points at `../notes/HOUSE_STYLE.md`; guardrails, the checkpoint workflow, the commands and the roles |
| `.sushi/*.json`, `.sushi/sushicomms/roles.json` | the plugins' project config: SushiStyle reads `../template_harness/claude/rules/house.json`; seats in `../projects/desks/seats/`; the SushiComms ticket store in `../projects/desks/<app>/sushicomms.json`; the role registry |
| `LICENSE` | `--license proprietary` (default: all rights reserved), `mit`, `polyform-noncommercial` (Malkuth in-house apps), or `none`; holder `--licensor`, default your git `user.name` |
| (no harness notice) | `templates/` is MIT-0 (see `templates/LICENSE`): generated code owes template_harness nothing |
| `THIRD_PARTY_NOTICES.md` | GUI.Forms (MIT) and its components: what any distribution, open or closed, must carry |
| `.gitignore`, `.gitattributes` | the workspace entries and `.sushi/state`, `.sushi/logs`; LF everywhere but `.ps1`/`.cmd` |

Agent files name paths as `<workspace>/...` (the directory holding the app,
`template_harness/`, `notes/` and `projects/`) so they hold on every machine;
`--workers-dir`, `--projects-dir`, `--notes-dir` and `--build-wrapper`
(default `m4ws`) change what they say. Per-machine plugin settings (the build
host, `clang-query`, `path_map`, a build wrapper's `build_command`) go in
`~/.sushi/<plugin>.json`, which every plugin reads under the project file.

## Keeping the pins together

`harness.lock` and the `@<commit>` of every `uses:` line in `ci.yml` name one
commit. The plugins load from the sibling checkout `../template_harness/claude`
(a `directory` marketplace in `.claude/settings.json`), which `lock.py checkout`
keeps at that commit, so the tools and the plugins move together. `lock.py bump`
rewrites the lock (and the ref of any github-sourced harness marketplace, for
products that use one); change the `uses:` lines in the same commit.

Claude Code reads a project's marketplaces after you trust the folder: open
Claude in the app once and accept the trust prompt.

## Testing

`harness/test_new_app.py` renders every template into temporary
directories, checks the tree, that no `{{` placeholder survives, that the
names pass the prefix rules and the rendered sources pass the house
checkers, that `--dry-run` changes nothing, and runs the whole command
against local bare repositories (`--remote-base file://...`): the pin, the
notes branch, the projects commit, the workspace, and that a re-run pushes
nothing. Real GitHub creation (`gh`) and the build-host smoke are not
exercised by the tests.
