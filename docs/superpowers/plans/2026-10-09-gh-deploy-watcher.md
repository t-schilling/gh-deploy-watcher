# gh-deploy-watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A macOS menu bar tool (SwiftBar plugin) that shows the latest result of tracked GitHub Actions deploy workflows, notifies on failure, and re-runs only failed jobs.

**Architecture:** A SwiftBar plugin run every 60s that polls via `gh` only while the user has started polling, otherwise redraws cached state. Menu items call the same script with subcommands. Pure logic (`model.py`) is separated from I/O (`github.py`, `state.py`, `notify.py`, `render.py`).

**Tech Stack:** Python 3.9+ standard library only, `gh` CLI, SwiftBar, `fzf` (optional, with fallback), `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-09-gh-deploy-watcher-design.md`

## Global Constraints

- Python 3.9 compatible, standard library only (no `X | Y` unions at runtime; use `Optional`, and `from __future__ import annotations`).
- macOS only. No tokens stored; authentication is whatever `gh` has.
- Package `gh_deploy_watcher/`; plugin `gh-deploy-watcher.1m.py`; user data in `~/.config/gh-deploy-watcher/` (`config.json`, `state.json`), overridable with env var `GH_DEPLOY_WATCHER_HOME` (used by tests).
- Workflows are identified by file name. `env` is `prd` or `dev`. Tracked run = latest run of that workflow on any branch.
- Only `github.py` calls `gh`; only `render.py` knows the SwiftBar format; `model.py` is pure.
- Re-run is `gh run rerun <id> --failed --repo <repo>`; `prd` asks for confirmation; `dev` does not.
- The repo is public: no real org, repo, workflow, or PR names anywhere (code, tests, docs, issues). Use placeholders like `acme/api`. Test fixtures are synthetic.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A tracked workflow that has never run (`gh run list` returns `[]`): shown as "no runs yet", never a failure, never a crash.
2. Corrupt or empty `config.json` / `state.json`: state falls back to defaults; an invalid config shows a ⚠ with the reason in the menu instead of a traceback.
3. SwiftBar runs plugins with a minimal `PATH` (no `/opt/homebrew/bin`): `gh` must still be found.
4. One repo is inaccessible (404/403/renamed): that repo shows an error line, the others still render and notify.
5. Run titles containing `|`, newlines, or quotes would break SwiftBar's `title | key=value` syntax: they must be sanitized.

---

### Task 1: Project scaffolding and config

**Files:**
- Create: `gh_deploy_watcher/__init__.py`, `gh_deploy_watcher/config.py`, `tests/__init__.py`, `tests/test_config.py`, `config.example.json`
- Modify: `.gitignore` (confirm `__pycache__/` is ignored).

**Interfaces:**
- Produces:
  - `config_dir() -> Path` — `$GH_DEPLOY_WATCHER_HOME` or `~/.config/gh-deploy-watcher`.
  - `@dataclass Workflow(file: str, env: str, label: str)`
  - `@dataclass RepoConfig(repo: str, workflows: List[Workflow])`
  - `@dataclass Config(repos: List[RepoConfig])`
  - `class ConfigError(Exception)`
  - `load_config(path: Optional[Path] = None) -> Config` — missing file returns `Config([])`; raises `ConfigError` with a readable message on bad JSON or invalid content.
  - `save_config(config: Config, path: Optional[Path] = None) -> None`
- Consumes: nothing.

- [ ] **Step 1: Write failing tests in `tests/test_config.py`**: `test_missing_file_gives_empty_config`, `test_roundtrip_save_load`, `test_invalid_env_raises` (`env: "stg"` → `ConfigError`), `test_bad_repo_name_raises` (`"acme"` without `/`), `test_duplicate_workflow_raises`, `test_corrupt_json_raises_config_error`, `test_config_dir_respects_env_var`.
- [ ] **Step 2: Run** `python3 -m unittest discover -s tests -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement `config.py`** per the interfaces; validation: `repo` matches `owner/name`, `env in {"prd","dev"}`, non-empty `file`, no duplicate `(repo, file)`.
- [ ] **Step 4: Add `config.example.json`** with the spec's generic `acme/api` example (two workflows).
- [ ] **Step 5: Run tests.** Expected: PASS.
- [ ] **Step 6: Commit** `feat: scaffold package and config loading`.

### Task 2: Pure model (`model.py`)

**Depends on:** Task 1

**Files:**
- Create: `gh_deploy_watcher/model.py`, `tests/test_model.py`

**Interfaces:**
- Produces:
  - `@dataclass Run(id: int, status: str, conclusion: Optional[str], created_at: str, title: str, url: str, branch: str)` with `to_dict() -> dict` and `@staticmethod from_dict(d: dict) -> Run`.
  - `classify(run: Optional[Run]) -> str` — one of `"none"` (no run), `"running"` (status not `completed`), `"success"`, `"failed"` (`failure` or `timed_out` or `startup_failure`), `"cancelled"`, `"skipped"`, `"unknown"`.
  - `env_visible(env: str, flt: str) -> bool` — `flt` in `both|prd|dev`.
  - `overall_state(items: List[Tuple[str, str]], polling: bool, error: Optional[str]) -> str` — `items` are `(env, classification)` of visible workflows; returns one of `"paused"`, `"error"`, `"prd_failed"`, `"dev_failed"`, `"running"`, `"ok"`. Precedence: paused, error, prd_failed, dev_failed, running, ok. Empty items with polling on → `"ok"`.
  - `new_failures(current: Dict[str, Run], notified: Set[int], baseline: bool) -> List[str]` — keys whose run classifies as `failed` and whose id is not in `notified`; with `baseline=True` returns `[]`.
  - `age_text(created_at: str, now: datetime) -> str` — `"45s"`, `"12m"`, `"3h"`, `"2d"` (ISO-8601 `Z` input).
  - `run_ref(title: str) -> str` — `"#2183"` for titles like `Merge pull request #2183 from …` or `PR #2183`; otherwise the title truncated to 30 chars.
  - `workflow_key(repo: str, file: str) -> str` — `"repo/file"`.
- Consumes: nothing.

- [ ] **Step 1: Write failing tests** covering: `classify(None) == "none"` (Review Focus 1), `completed/failure → failed`, `in_progress → running`, `completed/cancelled → cancelled` and that cancelled/skipped never make `overall_state` red; precedence cases of `overall_state` (paused beats error beats prd_failed beats dev_failed); `env_visible("dev","prd") is False`; `new_failures` dedups ids already in `notified` and returns `[]` with `baseline=True`; `age_text` for 45s/12m/3h/2d; `run_ref` for both title shapes and the fallback.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement `model.py`.** No I/O, no imports from other package modules.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add pure status model`.

### Task 3: State persistence (`state.py`)

**Depends on:** Task 1

**Files:**
- Create: `gh_deploy_watcher/state.py`, `tests/test_state.py`

**Interfaces:**
- Produces:
  - `@dataclass State(polling: bool = False, filter: str = "both", last: Dict[str, dict] = {}, last_poll: Optional[float] = None, notified: List[int] = [])` (use `field(default_factory=…)`); `last` values are `Run.to_dict()` or `{"error": str}` per workflow key.
  - `load_state(path: Optional[Path] = None) -> State` — missing, empty, or corrupt file returns defaults (Review Focus 2); unknown `filter` values reset to `"both"`.
  - `save_state(state: State, path: Optional[Path] = None) -> None` — atomic: write to a temp file in the same directory, then `os.replace`.
- Consumes: `config_dir()` from Task 1.

- [ ] **Step 1: Write failing tests:** `test_missing_file_gives_defaults`, `test_corrupt_file_gives_defaults`, `test_roundtrip`, `test_invalid_filter_resets_to_both`, `test_save_is_atomic` (no `.tmp` file left; original intact if the write raises midway, by patching `json.dump` to raise).
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement `state.py`.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add atomic state persistence`.

### Task 4: GitHub wrapper (`github.py`)

**Depends on:** Task 2

**Files:**
- Create: `gh_deploy_watcher/github.py`, `tests/test_github.py`, `tests/fixtures/` (synthetic JSON)

**Interfaces:**
- Produces:
  - `class GhError(Exception)` with attributes `kind: str` (`"missing"`, `"auth"`, `"network"`, `"rate_limit"`, `"not_found"`, `"other"`) and `message: str`.
  - `Runner = Callable[[List[str]], str]`
  - `run_gh(args: List[str]) -> str` — real runner. Locates `gh` by prepending `/opt/homebrew/bin` and `/usr/local/bin` to `PATH` (Review Focus 3); raises `GhError` mapped from stderr/exit code.
  - `latest_run(repo: str, workflow_file: str, runner: Runner = run_gh) -> Optional[Run]` — `gh run list --repo R --workflow F --limit 1 --json databaseId,status,conclusion,createdAt,displayTitle,url,headBranch`; `[]` → `None`.
  - `rerun_failed_args(repo: str, run_id: int) -> List[str]` — `["run","rerun",str(run_id),"--failed","--repo",repo]`.
  - `rerun_failed(repo: str, run_id: int, runner: Runner = run_gh) -> None`
  - `list_repos(runner: Runner = run_gh) -> List[str]` — `gh api --paginate "user/repos?per_page=100&affiliation=owner,collaborator,organization_member" --jq ".[].full_name"`.
  - `@dataclass WorkflowInfo(name: str, path: str, state: str)` and `list_workflows(repo: str, runner: Runner = run_gh) -> List[WorkflowInfo]` — `gh api --paginate repos/R/actions/workflows --jq '.workflows[]|[.name,.path,.state]|@tsv'`.
  - `auth_ok(runner: Runner = run_gh) -> bool` — `gh auth status` succeeds.
- Consumes: `Run` from Task 2.

- [ ] **Step 1: Write failing tests with a fake runner** returning synthetic fixtures: success run, failure run, in-progress run (`conclusion: ""`), empty list (Review Focus 1). Assert the exact args passed for `latest_run`, `rerun_failed_args`, `list_repos`, `list_workflows`; assert stderr `"HTTP 404"` maps to `GhError(kind="not_found")`, `"gh auth login"` to `"auth"`, `"rate limit"` to `"rate_limit"`, a missing `gh` binary to `"missing"`; assert a `*-old` workflow is returned by `list_workflows` unfiltered (filtering is the wizard's job).
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement `github.py`.** `conclusion` of `""` becomes `None`. Error mapping is a small pure function `classify_error(returncode, stderr) -> str`, tested directly.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add gh wrapper with error mapping`.

### Task 5: Notifications and confirmation (`notify.py`)

**Files:**
- Create: `gh_deploy_watcher/notify.py`, `tests/test_notify.py`

**Interfaces:**
- Produces:
  - `notify(title: str, message: str, runner: Callable[[List[str]], int] = <subprocess-based>) -> None` — macOS notification via `osascript -e 'display notification …'`; escapes `"` and `\` in inputs.
  - `confirm(message: str, runner: Callable[[List[str]], int] = <subprocess-based>) -> bool` — `osascript` dialog with Cancel/Re-run buttons; `True` only on the confirm button.
- Consumes: nothing.

- [ ] **Step 1: Write failing tests** with a fake runner: `test_notify_builds_osascript_command`, `test_notify_escapes_quotes`, `test_confirm_true_on_ok`, `test_confirm_false_on_cancel` (non-zero exit).
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement `notify.py`.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add notifications and confirm dialog`.

### Task 6: Menu rendering and plugin entrypoint

**Depends on:** Tasks 1, 2, 3

**Files:**
- Create: `gh_deploy_watcher/render.py`, `gh-deploy-watcher.1m.py` (executable), `tests/test_render.py`

**Interfaces:**
- Produces:
  - `render_menu(config: Config, state: State, error: Optional[str], now: datetime, script_path: str) -> str` — SwiftBar output: first line is the icon (🔴 prd failed, 🟠 dev failed, 🟡 running, 🟢 ok, ⏸ paused, ⚠ error); then a `---` and the dropdown from the spec (start/stop, view filter with the current choice marked, "Last poll", per-repo sections, per-workflow submenu with "Re-run failed jobs" only when `classify == "failed"`, "Open run", "Poll now", "Add / remove repos…", "Open config").
  - `sanitize(text: str) -> str` — replaces `|` and newlines with spaces and strips quotes (Review Focus 5).
  - Menu actions use SwiftBar params: `bash='<script_path>' param1=rerun param2=<repo> param3=<run_id> param4=<env> terminal=false refresh=true`.
  - Entrypoint: `main()` inserts the real path of the script's directory into `sys.path` (it is symlinked into SwiftBar's folder), then calls `actions.main(sys.argv[1:])` (Task 7). Add a thin fallback so that, until Task 7 lands, no-arg execution renders from state.
- Consumes: `Config`, `State`, `classify`, `overall_state`, `env_visible`, `age_text`, `run_ref`, `workflow_key`.

- [ ] **Step 1: Write failing tests** on `render_menu` output as text: first line per scenario (paused, error, `prd_failed`, running, ok); a failed `prd` workflow has a "Re-run failed jobs" line with `param1=rerun`; an in-progress workflow has none; "PRD only" filter hides `dev` workflows; a workflow with no runs shows "no runs yet" (Review Focus 1); a title with `|` and a newline produces no extra `|` in the line (Review Focus 5); a per-workflow error entry in `state.last` renders an error line under that repo only (Review Focus 4).
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement `render.py`** and the entrypoint (`chmod +x`).
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: render SwiftBar menu and plugin entrypoint`.

### Task 7: Actions and polling (`actions.py`)

**Depends on:** Tasks 1, 2, 3, 4, 5, 6

**Files:**
- Create: `gh_deploy_watcher/actions.py`, `tests/test_actions.py`
- Modify: `gh-deploy-watcher.1m.py` (call `actions.main`)

**Interfaces:**
- Produces:
  - `poll(config: Config, state: State, runner: Runner, now: float) -> Tuple[State, List[str], Optional[str]]` — fetches the latest run for each *visible* workflow, updates `state.last` (per-workflow `{"error": …}` on `GhError`, continuing with the others), returns the new state, the keys that should notify, and a global error (only when every call failed for `auth`/`missing`/`network`/`rate_limit`).
  - `start(…)`, `stop(…)`, `set_filter(flt: str, …)` — mutate and save state; `start` and `set_filter` poll immediately; `start` records a baseline so existing failures do not notify.
  - `rerun(repo: str, run_id: int, env: str, dry_run: bool = False, confirm_fn=confirm, runner=run_gh) -> int` — `prd` calls `confirm_fn` first and aborts with exit code 1 if declined; `dry_run` prints the command and executes nothing; after a real run, polls immediately.
  - `main(argv: List[str]) -> int` — no args: if `state.polling` poll, then print `render_menu`; subcommands `start`, `stop`, `filter <both|prd|dev>`, `refresh`, `rerun <repo> <run_id> <env> [--dry-run]`, `setup` (stub until Task 8).
- Consumes: Tasks 1–6 interfaces.

- [ ] **Step 1: Write failing tests** with a fake runner and fake `notify`/`confirm`: polling off makes zero runner calls; "PRD only" polls half the workflows; one repo raising `GhError(kind="not_found")` does not stop the others (Review Focus 4); a failed run notifies once and not again on the next poll; `start` with an existing failure does not notify (baseline); a new failure after `start` notifies; `rerun` for `prd` with `confirm_fn → False` never calls the runner; `rerun --dry-run` prints `gh run rerun 123 --failed --repo acme/api` and calls nothing; changing the filter triggers a poll.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement `actions.py`** and wire the entrypoint.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add polling, filter, and failed-jobs re-run`.

### Task 8: Setup wizard (`setup_wizard.py`)

**Depends on:** Tasks 1, 4

**Files:**
- Create: `gh_deploy_watcher/setup_wizard.py`, `tests/test_setup_wizard.py`
- Modify: `gh_deploy_watcher/actions.py` (`setup` subcommand calls the wizard)

**Interfaces:**
- Produces:
  - `suggest(workflow: WorkflowInfo) -> Tuple[str, str]` — `(env, label)`: `"PRD - Deploy to EU"` → `("prd", "PRD · EU")`; unknown names default to `("dev", name)`.
  - `preselect(workflows: List[WorkflowInfo]) -> List[WorkflowInfo]` — those whose name or path contains `deploy` (case-insensitive), excluding names/paths containing `-old`, and names `CodeQL` / `Dependabot Updates`.
  - `pick(options: List[str], multi: bool, preselected: List[str], runner=…) -> List[str]` — uses `fzf -m` when available, else a numbered list with text filter.
  - `run_wizard(config: Config, runner: Runner = run_gh, picker=pick, ask=input) -> Config` — steps from the spec (repos → workflows for the selected repos only → env/label confirmation); also supports removing repos/workflows and listing current config. Saves with `save_config`.
- Consumes: `list_repos`, `list_workflows`, `Config`, `save_config`.

- [ ] **Step 1: Write failing tests** using synthetic `WorkflowInfo` lists and a scripted picker/`ask`: `suggest` mappings; `preselect` keeps `deploy-prd-eu.yaml`, drops `deploy-prd-eu-old.yaml`, `CodeQL`, `Dependabot Updates`; the wizard fetches workflows only for selected repos (assert runner calls); a removal run deletes the chosen workflow and drops a repo left empty; re-running with an existing config preserves untouched repos.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement `setup_wizard.py`**; the `setup` subcommand opens it in the current terminal; the menu entry "Add / remove repos…" uses SwiftBar `terminal=true`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add repo and workflow setup wizard`.

### Task 9: Installer and README

**Depends on:** Tasks 6, 7, 8

**Files:**
- Create: `install.sh` (executable), `tests/test_install.sh` (bash, uses a stubbed `PATH`)
- Modify: `README.md`

**Interfaces:**
- Produces: `install.sh` — idempotent; steps from the spec: verify macOS, ensure Homebrew, `brew install` `gh`/`fzf`/SwiftBar cask if missing, ensure Python ≥ 3.9 (brew `python` if absent), `gh auth login` if `gh auth status` fails, symlink `gh-deploy-watcher.1m.py` into the SwiftBar plugin directory (read from `defaults read com.ameba.SwiftBar PluginDirectory`, otherwise guide the user and ask for the path), then run `gh-deploy-watcher.1m.py setup`. Supports `--dry-run` that prints each action without executing.
- README covers: what it is, a screenshot-free description of the menu, install (clone, `./install.sh`), first run, filter/polling usage, update with `git pull`, uninstall, and an explicit note that this is **not** a `gh` extension.

- [ ] **Step 1: Write failing shell test** that runs `install.sh --dry-run` with a fake `PATH` lacking `gh`, `fzf`, and `brew` bins, and asserts the output lists the expected install steps and exits 0; a second case with everything present asserts nothing is installed.
- [ ] **Step 2: Run** `bash tests/test_install.sh`. Expected: FAIL.
- [ ] **Step 3: Implement `install.sh`** with `set -euo pipefail` and a `run` helper that honors `--dry-run`.
- [ ] **Step 4: Write the README.**
- [ ] **Step 5: Run the shell test.** Expected: PASS.
- [ ] **Step 6: Commit** `feat: add installer and README`.

### Task 10: End-to-end verification with SwiftBar

**Depends on:** Task 9

**Files:**
- Modify: `README.md` (fixes found here only)

- [ ] **Step 1: Run the full suite:** `python3 -m unittest discover -s tests -v` and `bash tests/test_install.sh`. Expected: all pass.
- [ ] **Step 2: Install on the author's machine** via `./install.sh` and register two real repos in the wizard (done by the user; real names stay out of the repo).
- [ ] **Step 3: Verify manually:** icon and menu match the spec; polling off makes no API calls (check with `gh api rate_limit` before/after); start/stop; each filter option; a failure produces exactly one notification; the baseline suppresses pre-existing failures.
- [ ] **Step 4: Verify re-run** only with the user's go-ahead on a real failed run: `prd` asks for confirmation, `dev` does not, and only failed jobs re-run.
- [ ] **Step 5: Confirm** SwiftBar's minimal `PATH` still finds `gh` (Review Focus 3) by launching from SwiftBar, not a shell.
- [ ] **Step 6: Commit any README fixes** `docs: fixes from end-to-end verification`.
