# Selection UI — Design

Date: 2026-10-09
Status: draft, pending review
Builds on: `2026-10-09-gh-deploy-watcher-design.md` (the core tool, already shipped)

## Purpose

Choosing which repos and workflows to track currently happens in a terminal
wizard (`setup`). This adds a visual way to do the same job: a native macOS
window showing a page with a searchable repo list, a workflow list per repo,
and per-workflow environment and label controls, in a Liquid-Glass-inspired
style.

The terminal wizard stays as a fallback. Both interfaces share the same
selection logic and write the same `config.json`.

## Success criteria

- From the menu, "Add / remove repos…" opens a window (not a browser tab) with
  the selection page.
- A user can search their accessible repos, pick workflows, set `prd`/`dev`
  and a label, see a live summary of changes, and save. Nothing is written
  until Save.
- The result is exactly what the terminal wizard would have produced for the
  same choices (same file, same validation, same sanitization).
- If the native window is unavailable (not built, build failed), the same
  page opens in the default browser. The feature never depends on the window.
- Nothing about the page can be driven by another website or another local
  user process without the session token.

## Non-goals

- True Liquid Glass (real refraction via `glassEffect`). The page uses a CSS
  approximation. A fully native SwiftUI version is a possible later phase.
- Editing polling state, the PRD/DEV filter, or showing deploy status in the
  page. It only edits tracked repos and workflows.
- Windows/Linux. macOS only.
- Requiring Node, Xcode.app, or any package manager beyond what the core tool
  already needs. Building the window needs only the Command Line Tools.
- Persistent background servers. Everything is on demand.

## Approach

On-demand ephemeral local server plus a thin native window.

`gh-deploy-watcher.1m.py ui` starts an HTTP server on `127.0.0.1` (random
port) using the standard library, launches the native window pointed at it,
waits for the window to close, and shuts the server down. Python owns all
logic; Swift only hosts a web view.

Alternatives considered:
- Persistent `launchd` server: always-on process, port conflicts, more attack
  surface and installer complexity for an occasional task.
- Static HTML file: cannot call `gh` or save without a server.
- Full SwiftUI app: real Liquid Glass but duplicates (or has to bridge to) the
  Python selection logic, adds build and test surface in a second language.
  Deferred; the API below is the contract such a front end would use.

## Components

```
gh_deploy_watcher/
  selection.py        # clean, suggest, preselect (moved out of setup_wizard.py)
  ui_server.py        # HTTP server, routing, security checks, lifecycle
  ui_launcher.py      # find/spawn the native window, browser fallback
  ui/
    index.html  app.css  app.js    # the page, no inline code, no build step
  github.py           # + current_login()
  config.py           # + parse_config(dict) shared by load_config and the API
ui-shell/             # Swift package (no Xcode.app required)
  Package.swift
  Sources/GhDeployWatcherUI/ (main.swift, NavigationPolicy.swift)
  build.sh            # swift build + assemble and ad-hoc sign GhDeployWatcher.app
docs/design/mockup-glass.html   # visual reference (fake data)
```

`setup_wizard.py` imports `clean`, `suggest`, `preselect` from `selection.py`
instead of defining them. Behavior and tests for the terminal wizard do not
change.

## Server and API

All responses are JSON except the static files. Errors use
`{"error": {"kind": "<gh error kind or validation|conflict|bad_request>", "message": "..."}}`.

| Method and path | Purpose |
|---|---|
| `GET /` , `/app.css`, `/app.js` | Static page. Nothing else is served (404). |
| `GET /api/session` | `{login, config, config_hash}`; `login` from `gh api user`. |
| `GET /api/repos` | Accessible repos via `github.list_repos`: `[{name, display}]` (`display` is sanitized). |
| `GET /api/repos/<owner>/<name>/workflows` | Workflows via `github.list_workflows`: `{file, name, state, suggested:{env,label}, preselected, tracked:null\|{env,label}}`. Repo name must match the repo pattern. |
| `PUT /api/config` | Body `{base_hash, repos:[{repo, workflows:[{file, env, label}]}]}`. Validated by `config.parse_config`; labels sanitized with `clean`; saved with `save_config` (atomic). `409` if `base_hash` differs from the file on disk now; `422` with the validation message otherwise. Returns the new `config_hash`. |
| `POST /api/done` | Responds, then shuts the server down. |

`config_hash` is the SHA-256 of the `config.json` bytes (of the empty string
when the file does not exist). Request bodies are limited to 256 KB. `gh`
failures map to their `GhError.kind` and a sanitized message; the page shows
them with a retry.

## Security

The server is reachable by any local process and by any web page the user
visits (via the browser), so:

- Bind `127.0.0.1` only, random port.
- A random 32-byte session token, passed to the page in the URL fragment
  (`http://127.0.0.1:<port>/#<token>`). Fragments are never sent to the
  server, logs, or `Referer`. The page sends it as `X-Token` on every `/api`
  call; the server compares with `secrets.compare_digest`. Missing or wrong
  token: `401`.
- `Host` must equal `127.0.0.1:<port>` (defeats DNS rebinding). `Origin`, when
  present, and always for non-`GET` requests, must equal
  `http://127.0.0.1:<port>`. No CORS headers are ever sent.
- `PUT`/`POST` require `Content-Type: application/json`.
- Response headers: `Content-Security-Policy: default-src 'none'; script-src
  'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri
  'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. This is
  why the page uses separate `.js` and `.css` files.
- Everything that comes from GitHub (repo names, workflow names) is sanitized
  server-side with `clean`, and the page renders it with `textContent`/DOM
  APIs only, never `innerHTML`. Raw values are used for API calls and for the
  stored `file`/`repo` identifiers.
- Request logging is disabled (no URLs or tokens in output).
- The native window receives the URL on **stdin**, not in `argv`, so the token
  does not appear in the process list.

## Lifecycle

- One instance at a time: an advisory `flock` on `ui.lock` in the config dir.
  A second launch sends a notification ("The selection window is already
  open") and exits 0.
- The server stops on `POST /api/done`, when the native window process exits,
  or after 15 minutes without any request (injectable clock for tests).
- Python waits on the window process. If the window cannot be started, or exits
  non-zero within 3 seconds, the launcher falls back to opening the URL in the
  default browser and keeps the server running until done or idle timeout. If
  even that fails, it prints the URL to stderr and sends a notification.

## The page

Single page, two panels, floating header and action bar, as in
`docs/design/mockup-glass.html` (that file is the visual reference; the real
page replaces its fake data with API calls).

- **Header:** logo, title, live counters (tracked repos, tracked workflows),
  `gh` login.
- **Repos panel:** search box filters client-side; "Tracked" group first with
  per-repo counts. The list loads once with a progress indicator.
- **Workflows panel:** fetched when a repo is selected, only for that repo.
  Each row: toggle, name and file, label input, `prd`/`dev` segmented control.
  Deploy-looking workflows are preselected for repos with nothing tracked;
  `*-old`, CodeQL, Dependabot and disabled workflows are listed unselected
  (disabled ones dimmed). "Select suggested" re-applies the suggestion.
- **Action bar:** live summary ("+N added, −N removed"), Cancel, Save (disabled
  without changes). After Save the page offers Done.
- **States:** loading, per-repo load error with Retry, not-signed-in banner
  with the `gh auth login` instruction, `409` conflict ("config changed on
  disk — reload"), validation errors inline.
- **Dirty flag:** `window.ghdwDirty` is `true` while there are unsaved changes.
- **Accessibility and system settings:** keyboard operable with visible focus,
  light and dark via `prefers-color-scheme`, solid fallback under
  `prefers-reduced-transparency`, no animation under `prefers-reduced-motion`.
  No external assets; works offline.

## Native window (`ui-shell/`)

A Swift package using AppKit and `WKWebView`, built with `swift build` (works
with only the Command Line Tools) and assembled by `build.sh` into
`build/GhDeployWatcher.app` (git-ignored), ad-hoc signed locally. The bundle
sets a bundle id, name, and `NSAllowsLocalNetworking` so the web view may load
`http://127.0.0.1`.

- Reads one line (the URL) from stdin; refuses anything that is not
  `http://127.0.0.1:<port>/…`.
- Navigation policy: only the app origin is allowed; links to other origins
  open in the default browser; no JavaScript-to-native bridge; non-persistent
  website data store.
- Closing the window with unsaved changes asks for confirmation (reads
  `window.ghdwDirty`).
- Exits 0 when the window closes.
- The pure URL/navigation policy is unit-tested. If `swift test` is not
  available with only the Command Line Tools, the binary exposes `--selftest`
  for the same checks (decided during implementation, recorded in the issue).

## Menu and CLI changes

- Menu: "Add / remove repos…" runs `ui` with `terminal=false`. `setup` remains
  a command-line subcommand and is documented as the fallback.
- `actions.main` gains the `ui` subcommand and routes its errors the same way
  as other subcommands (warning menu / exit codes, no tracebacks).

## Installer and docs

- New optional step in `install.sh`: if `swift` and the SDK are available,
  run `ui-shell/build.sh` (skipped when `build/GhDeployWatcher.app` is newer
  than every file in `ui-shell/`). Failure or absence of `swift` prints that
  the page will open in the browser and does not fail the install. New flag
  `--no-window` skips it. `--dry-run` prints the step. `--uninstall` does not
  remove `build/`.
- README: describes the window, the browser fallback, `setup` as the terminal
  fallback, and how to rebuild the window.

## Testing

- Server (real HTTP over a loopback port inside the test, fake `gh` runner):
  every route and method; rejection of missing/wrong token, wrong `Host`,
  wrong `Origin`, wrong `Content-Type`, oversized body, unknown paths; the
  security headers on every response; hostile repo and workflow names are
  sanitized in responses; `PUT` validation (bad env, bad repo, duplicate
  workflow, empty label); `409` when the file changed under the page;
  `done` shutdown; idle timeout with a fake clock; single instance; no
  request logging.
- `selection.py`: the existing wizard tests for `clean`/`suggest`/`preselect`
  move with the code and keep passing.
- Launcher: fake spawner for window success, spawn failure, quick non-zero
  exit (browser fallback), and server shutdown when the window exits.
- Installer: the existing hermetic stub-based tests extend to the new step
  (skipped without `swift`, `--no-window`, dry-run output, build failure is
  non-fatal, rebuild only when sources are newer).
- Swift: policy tests as described above; `build.sh` is exercised for real in
  the final manual verification.
- Frontend: kept thin (logic lives in the server). Verified manually, checking
  each state listed above, since the project avoids requiring Node.
- Final manual verification on the author's machine (menu → window → search →
  save → menu shows the new workflows → browser fallback by temporarily moving
  the app).

## Errors

No traceback may reach SwiftBar from the `ui` subcommand. A port bind failure,
a missing `ui/` directory, an unreadable config, a failed `gh` call, or a
failed window spawn each end in a notification or the browser fallback with a
clear message.

## Public repo hygiene

Same rule as the core tool: no real org, repo, workflow, or PR names in code,
tests, fixtures, docs or the mockup (placeholders such as `acme/api`).

## Open decisions

- Idle timeout is 15 minutes; easy to change.
- Whether `swift test` works with only the Command Line Tools (otherwise
  `--selftest`), settled in the window issue.
- A later phase could replace the web view with a SwiftUI front end using real
  Liquid Glass, reusing the same API and Python backend.
