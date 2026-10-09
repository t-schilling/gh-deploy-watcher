# gh-deploy-watcher — Design

Date: 2026-10-09
Status: draft, pending review

## Purpose

Deploy workflows on GitHub Actions fail often, and checking them by hand
is a chore. gh-deploy-watcher is a macOS menu bar tool that shows the
state of the latest run of each deploy workflow the user cares about,
notifies on failure, and lets the user re-run only the failed jobs from
the menu.

It is distributed as a public repo. Each teammate clones it, runs an
installer, and registers their own repos and workflows. Nothing about any
specific organization lives in the repo.

## Success criteria

- At a glance, the menu bar icon tells the user the worst state among the
  workflows they follow.
- Opening the menu shows, per repo, each tracked workflow with its latest
  result, how long ago it ran, and which commit/PR it deployed.
- A failed workflow offers "re-run failed jobs" directly from its menu
  entry; it never re-runs jobs that already succeeded.
- Polling runs every 60 seconds, but only after the user starts it, and
  stops when the user stops it.
- A new teammate can go from `git clone` to a working menu by following
  the README, with no manual config editing.

## Non-goals

- Linux/Windows support. macOS only.
- Storing or managing GitHub tokens. Authentication is whatever `gh` has.
- Support for CI systems other than GitHub Actions.
- A GUI for editing config (a terminal wizard is enough).
- Per-user filtering of runs by actor. Everyone sees the latest run of
  each tracked workflow regardless of who triggered it.
- Notifications for successes or recoveries. Failures only.

## Approach

A SwiftBar plugin backed by a state file. No background process.

SwiftBar runs `gh-deploy-watcher.1m.py` every 60 seconds (the filename
encodes the interval). Each invocation either polls GitHub or just
redraws from cached state, depending on whether polling is active. Menu
items invoke the same script with a subcommand (`start`, `stop`,
`filter`, `refresh`, `rerun`, `setup`), so the plugin and its actions
share code and state.

Alternatives considered and rejected:
- A launchd daemon writing JSON for a read-only plugin: more moving parts,
  and polling while SwiftBar is closed has no value here.
- A standalone Python menu bar app (`rumps`): needs a long-running
  process and login-item setup; adds nothing over SwiftBar.

## Configuration

Per-user, outside the repo: `~/.config/gh-deploy-watcher/config.json`.
The repo ships only a generic `config.example.json`.

```json
{
  "repos": [
    {
      "repo": "acme/api",
      "workflows": [
        { "file": "deploy-prd-eu.yaml", "env": "prd", "label": "PRD · EU" },
        { "file": "deploy-dev-eu.yaml", "env": "dev", "label": "DEV · EU" }
      ]
    }
  ]
}
```

- Workflows are identified by **file name**, not display name. Repos can
  contain stale workflows with near-identical display names (for example
  `*-old.yaml`); the file name is unambiguous.
- `env` is `prd` or `dev` and drives the filter, the re-run confirmation,
  and icon severity. It is chosen explicitly at setup so it does not
  depend on how a repo names its workflows.
- `label` is free text shown in the menu.
- The tracked run is the **latest run of that workflow on any branch**.
  Deploy workflows in practice trigger from different branches (and a
  DEV deploy may also run on a push to the production branch), so a
  branch filter would hide real deploys.

## State

`~/.config/gh-deploy-watcher/state.json`:

- `polling`: `true` or `false`.
- `filter`: `both`, `prd`, or `dev`.
- `last`: per `repo/file`, the last known run (id, status, conclusion,
  timestamps, display title, URL) and the time of the last poll.
- `notified`: ids of failed runs already notified.

State writes are atomic (write to a temp file, then rename) because the
plugin and menu actions can run concurrently.

## Menu

Icon (worst visible state wins, evaluated only over workflows allowed by
the filter):

| Icon | Meaning |
|------|---------|
| 🔴 | a `prd` workflow's latest run failed |
| 🟠 | only `dev` workflows failed |
| 🟡 | something is running/queued, nothing failed |
| 🟢 | everything succeeded |
| ⏸ | polling stopped |
| ⚠ | cannot poll (see Errors) |

Dropdown:

```
▶ Start polling / ⏸ Stop polling
View: ● Both · ○ PRD only · ○ DEV only
Last poll: 40s ago
---
acme/api
  🔴 PRD · EU   failed · 12m ago   #2183   ▸ ↻ Re-run failed jobs
                                            ↗ Open run
  🟢 DEV · EU   ok · 1h ago        #2182   ▸ ↗ Open run
---
Poll now
Add / remove repos…
Open config
```

- Each workflow is a submenu. "Re-run failed jobs" appears only when the
  latest run is completed and failed. GitHub does not allow re-running a
  run that is still in progress, so the entry is absent in that case.
- A run that finished with `cancelled` or `skipped` is shown as such and
  does not count as a failure for the icon or notifications.
- The commit/PR reference comes from the run's title.

## Polling

- While `polling` is `false`: redraw from `last`, mark ⏸, make no API
  calls.
- While `true`: for each visible workflow (per filter), fetch its latest
  run via `gh`, update `last`, then redraw. A filter of "PRD only" makes
  half the calls.
- "Start polling" sets `polling` to true and polls immediately. It also
  records the current state as a baseline (see Notifications).
- Changing the filter triggers an immediate poll so newly visible
  workflows are not empty.
- "Poll now" performs a single poll without changing `polling`.
- Volume is small: N tracked workflows is N calls per minute, well under
  the 5,000 requests/hour limit for typical use.

## Notifications

- A native macOS notification fires when a visible workflow's latest run
  becomes a failure, once per run id (tracked in `notified`).
- When polling starts, existing results form a baseline: failures that
  already exist are shown in the menu but do not notify.
- Hidden environments (per filter) do not notify.
- Only failures notify.

## Re-run

- Command: `gh run rerun <run-id> --failed --repo <repo>`. This re-runs
  failed jobs, plus jobs that depend on them and were skipped; jobs that
  already succeeded are not re-run.
- For `env: prd`, a confirmation dialog is shown first, since it
  redeploys to production. `dev` re-runs without confirmation.
- After the call, an immediate poll shows the workflow as in progress.
- On failure (no permission, run not re-runnable), the error from `gh`
  is shown in a notification.
- `--dry-run` builds and prints the command without executing it.

## Setup wizard (`setup`)

Interactive, run by the installer and from the menu ("Add / remove
repos…", which opens it in a terminal). Re-runnable to edit config.

1. **Repos**: list repositories the user's `gh` account can access
   (paginated; `gh api user/repos`, plus `gh repo list <org>` for chosen
   organizations). The user selects one or more. Multi-select with
   type-to-filter uses `fzf`; if `fzf` is missing, fall back to a
   numbered list with text search.
2. **Workflows**: for each selected repo, list its workflows (fetched
   only for selected repos to limit API calls). Those whose name or path
   contains "deploy" are preselected; `*-old` and common non-deploy
   workflows (CodeQL, Dependabot) are not.
3. **Environment and label**: for each chosen workflow, propose `env` and
   `label` from its name (for example "PRD - Deploy to EU" →
   `prd` / `PRD · EU`); the user confirms or edits.

It also supports removing repos and workflows and listing the current
config, without starting over.

## Installer (`install.sh`)

Idempotent; safe to re-run. Steps:

1. Verify macOS.
2. Ensure Homebrew; install `gh`, SwiftBar, and `fzf` via brew if missing.
3. Ensure a usable Python 3. The system `python3` may be 3.9 or absent;
   the code targets 3.9+ and uses the standard library only. If missing,
   install via brew.
4. If `gh` is not authenticated, run `gh auth login`. Authentication is
   per user; the tool never handles tokens.
5. Link the plugin into the SwiftBar plugin directory, guiding the user to
   choose that directory on first launch of SwiftBar.
6. Launch the setup wizard.

README documents: clone, run the installer, first-run steps, and how to
update (`git pull`). It states plainly that this is **not** a `gh`
extension, despite the `gh-` prefix, and is not installed with
`gh extension install`.

## Project layout

```
gh-deploy-watcher/
  gh-deploy-watcher.1m.py      # SwiftBar entrypoint; dispatches subcommands
  gh_deploy_watcher/
    config.py                  # read/validate config.json
    state.py                   # read/write state.json (atomic)
    github.py                  # only module that calls `gh`
    model.py                   # per-workflow status, filter, icon (pure)
    render.py                  # model -> SwiftBar menu text
    notify.py                  # notifications and confirmation dialog
    actions.py                 # start, stop, filter, refresh, rerun
    setup_wizard.py            # repo/workflow picker
  tests/
  config.example.json
  install.sh
  README.md
  LICENSE
```

Boundaries: only `github.py` knows about `gh`; only `render.py` knows the
SwiftBar format; `model.py` is pure logic, testable without network or UI.

## Errors

If `gh` is missing or unauthenticated, there is no network, or the rate
limit is hit, the icon becomes ⚠ and the menu shows the reason. The last
good results and the state file are kept, never wiped. A repo the user
cannot access is reported per repo and does not block the others.

## Testing

- Unit tests (`unittest`, no extra dependencies) for `model.py`: worst
  state, PRD/DEV/both filtering, once-per-run notification dedup, the
  baseline on start, cancelled/skipped not counted as failures.
- `github.py` tested against **synthetic fixtures** shaped like real
  `gh` JSON output (success, failure, in progress, a stale `*-old`
  workflow). Fixtures must not be copied from real private repos: this
  repo is public.
- `rerun --dry-run` tests assert the exact command and that `prd` asks
  for confirmation. Automated tests never re-run real runs.
- Final manual check with SwiftBar against real repos: menu, filter,
  start/stop, notifications. A real re-run needs an actual failed run and
  the user's go-ahead.

## Public repo hygiene

No organization, repo, workflow, or PR names from any private project in
code, docs, fixtures, or examples; use placeholders such as `acme/api`.
Per-user config and state stay in `~/.config/gh-deploy-watcher/`.

## Open decisions

- `fzf` as a dependency, installed by the installer, with a numbered-list
  fallback. Assumed yes; revisit if the extra dependency is unwanted.
- Re-run confirmation applies to `prd` only. Easy to extend to all
  environments.
