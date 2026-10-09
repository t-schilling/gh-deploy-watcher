# Selection UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A native macOS window (Swift `WKWebView` shell) showing a local page where the user picks repos, workflows, environments and labels, with the browser as fallback and the terminal wizard kept.

**Architecture:** An on-demand Python HTTP server on `127.0.0.1` (token + Host/Origin checks + CSP) serves a static page and a small JSON API that reuses `github.py`, `config.py` and the selection logic. `gh-deploy-watcher.1m.py ui` starts the server, launches the window, waits for it to exit, and shuts down.

**Tech Stack:** Python 3.9 stdlib only (server, launcher, tests via `unittest`), plain HTML/CSS/JS (no build step, no Node), Swift 5.9+ built with `swift build` using only the Command Line Tools (AppKit, WebKit), bash installer.

**Spec:** `docs/superpowers/specs/2026-10-09-selection-ui-design.md` (builds on `2026-10-09-gh-deploy-watcher-design.md`; visual reference `docs/design/mockup-glass.html`)

## Global Constraints

- Python 3.9 compatible, standard library only; `from __future__ import annotations`; no `X | Y` unions at runtime.
- The server binds `127.0.0.1` only, on a random port. Session token: 32 random bytes (`secrets.token_urlsafe(32)`), delivered only in the URL fragment (`http://127.0.0.1:<port>/#<token>`), sent as header `X-Token`, compared with `secrets.compare_digest`.
- `Host` must equal `127.0.0.1:<port>`; `Origin` (always checked for non-`GET`, and for any request that carries it) must equal `http://127.0.0.1:<port>`; no CORS headers; `PUT`/`POST` need `Content-Type: application/json`; request bodies at most 262144 bytes.
- Every response carries: `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- No inline `<script>`, no inline event handlers, no `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write` in the page; GitHub-supplied text is set with `textContent`/DOM APIs. The page has no external assets.
- Idle timeout 900 seconds (injectable clock). One instance at a time (`flock` on `ui.lock` in the config dir). Request logging disabled.
- The window receives the URL on **stdin** (one line), never in `argv`; it only accepts `http://127.0.0.1:<port>/...`.
- Swift: Command Line Tools only (no Xcode.app, no third-party packages), AppKit + WebKit, output `build/GhDeployWatcher.app` ad-hoc signed; `.build/` and `build/` git-ignored.
- User data stays in `~/.config/gh-deploy-watcher/` (override `GH_DEPLOY_WATCHER_HOME`, used by tests). Tests never call real `gh`, `osascript`, `open`, or `swift build`, and never open a real window or browser (inject fakes), except Task 6's explicit `build.sh` check and Task 8.
- The repo is public: no real org, repo, workflow, or PR names anywhere (code, tests, fixtures, docs, issues). Placeholders like `acme/api` only.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` — EVERY commit, including RED test commits.

## Review Focus

1. A second `ui` launch while a window is open: no second server, no stray process, and a clear notification.
2. The page opened without the token (user types `http://127.0.0.1:<port>/`): it shows an "invalid session" message, every `/api` call returns 401, and no data leaks.
3. `config.json` changes on disk while the page is open (for example via the terminal wizard): Save returns 409 and never overwrites.
4. The window is closed with unsaved changes or while a Save is in flight: no half-written config, the dirty prompt appears, the server shuts down, and no orphan Python or Swift process remains.
5. Hostile names from GitHub (ESC sequences, `<script>`, `|`, newlines, U+202E) in repo or workflow names: never executed or rendered as markup, and stored labels are clean.

---

### Task 1: Extract shared selection logic

**Files:**
- Create: `gh_deploy_watcher/selection.py`, `tests/test_selection.py`
- Modify: `gh_deploy_watcher/setup_wizard.py`, `tests/test_setup_wizard.py`

**Interfaces:**
- Produces (moved verbatim from `setup_wizard.py`, behavior unchanged): `clean(text: str) -> str`, `suggest(workflow: WorkflowInfo) -> Tuple[str, str]`, `preselect(workflows: List[WorkflowInfo]) -> List[WorkflowInfo]`, plus the private helpers and constants they need (`_strip_leading`, `_PRD_WORDS`, `_DEV_WORDS`, `_SKIP_NAMES`).
- Consumes: `WorkflowInfo` from `github.py`.

- [ ] **Step 1: Move the existing tests** for `clean`, `suggest`, `preselect` (including the hostile-name and empty-label cases) into `tests/test_selection.py` importing from `gh_deploy_watcher.selection`; run them: expected FAIL (module missing).
- [ ] **Step 2: Create `selection.py`** by moving the three functions and their helpers out of `setup_wizard.py`; `setup_wizard.py` imports them (`from .selection import clean, suggest, preselect`) and keeps working unchanged.
- [ ] **Step 3: Run the full suite** (`python3 -m unittest discover -s tests`). Expected: all pass, with no test deleted that is not re-homed.
- [ ] **Step 4: Commit** `refactor: extract selection logic into selection.py`.

### Task 2: Local server core, security and lifecycle

**Depends on:** Task 1

**Files:**
- Create: `gh_deploy_watcher/ui_server.py`, `tests/test_ui_server.py`

**Interfaces:**
- Produces:
  - `@dataclass Request(method: str, path: str, params: Tuple[str, ...], headers: Dict[str, str], body: Any)` — `body` is the parsed JSON value or `None`; `headers` keys are lower-case.
  - `@dataclass Response(status: int, body: Any = None, content_type: str = "application/json", raw: Optional[bytes] = None)` — `body` is JSON-serialized unless `raw` is given.
  - `Handler = Callable[[Request], Response]`
  - `class UiServer`: `__init__(self, static_dir: Path, idle_seconds: float = 900.0, clock: Callable[[], float] = time.monotonic, token: Optional[str] = None)`; attributes `port: int`, `token: str`; property `url -> str` (`http://127.0.0.1:{port}/#{token}`); `add_route(method: str, pattern: str, handler: Handler) -> None` (`pattern` is a regex matched with `fullmatch` against the path; capture groups become `Request.params`); `start() -> None` (binds, serves on a thread); `wait() -> None` (blocks until shut down, and calls `check_idle()` about once per second while waiting, so the idle timeout needs no extra thread); `shutdown() -> None` (idempotent); `touch() -> None` (resets idle); `check_idle() -> bool` (shuts down and returns True when idle exceeded).
  - `try_lock_instance(directory: Path) -> Optional[IO[str]]` — non-blocking `flock` on `directory/ui.lock`; returns the open handle holding the lock, or `None` if another instance holds it.
- Serves `GET /`, `/app.css`, `/app.js` from `static_dir` (404 for anything else, no directory traversal); other paths dispatch to registered routes. Static files and unknown routes need no token; every `/api/` path requires `X-Token`.

- [ ] **Step 1: Write failing tests** (real loopback server inside the test, `http.client`): token missing/wrong on `/api/x` → 401; wrong `Host` → 403; wrong `Origin` on `POST` → 403; `POST`/`PUT` without `Content-Type: application/json` → 415; body over 262144 bytes → 413; unknown path → 404; `GET /../x` and encoded traversal → 404; unsupported method → 405; every response (including errors and static) carries the five headers with the exact CSP string; a route handler that raises returns 500 JSON with a generic message and no traceback text; invalid JSON body → 400; `url` contains the token only after `#`; no request line is written to stdout/stderr (capture); `check_idle` with a fake clock shuts down after 900s and `touch()` resets it; `shutdown()` twice is safe; `try_lock_instance` returns `None` for a second call while the first handle is open and succeeds again after it is closed (Review Focus 1); the index served without a token contains no data (Review Focus 2).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_ui_server -v`. Expected: FAIL.
- [ ] **Step 3: Implement `ui_server.py`** on `http.server.ThreadingHTTPServer` with a custom `BaseHTTPRequestHandler` (override `log_message` to do nothing); never use `shell`; read at most the declared `Content-Length`.
- [ ] **Step 4: Run tests.** Expected: PASS. Full suite still green.
- [ ] **Step 5: Commit** `feat: add local UI server with security checks and lifecycle`.

### Task 3: API endpoints

**Depends on:** Tasks 1, 2

**Files:**
- Create: `gh_deploy_watcher/ui_api.py` (the endpoint handlers; `ui_server.py` keeps only transport and security, which the spec lists together), `tests/test_ui_api.py`
- Modify: `gh_deploy_watcher/config.py` (+`parse_config`, `config_hash`, `valid_repo_name`), `gh_deploy_watcher/github.py` (+`current_login`), `tests/test_config.py`, `tests/test_github.py`

**Interfaces:**
- Produces:
  - `config.parse_config(data: Any) -> Config` — public name for the existing `_parse` (keep `_parse` as an alias); raises `ConfigError`.
  - `config.config_hash(path: Optional[Path] = None) -> str` — SHA-256 hex of the file bytes; of `b""` when the file does not exist.
  - `config.valid_repo_name(name: str) -> bool` — `owner/name` with `[A-Za-z0-9_.-]` segments, neither segment empty, starting with `-`, or equal to `.`/`..`; rejects a trailing newline.
  - `github.current_login(runner: Runner = run_gh) -> str` — `gh api user --jq .login`.
  - `ui_api.install_routes(server: UiServer, runner: Runner = run_gh, config_path: Optional[Path] = None) -> None` registering:
    - `GET /api/session` → `{login, config, config_hash}`.
    - `GET /api/repos` → `[{name, display}]` (`display = clean(name)`).
    - `GET /api/repos/<owner>/<name>/workflows` → `[{file, name, state, suggested:{env,label}, preselected, tracked:null|{env,label}}]`, `file` is the basename of the path, `name` cleaned, `preselected` from `selection.preselect`, 400 for an invalid repo name.
    - `PUT /api/config` body `{base_hash, repos:[{repo, workflows:[{file, env, label}]}]}` → 409 (`conflict`) if `base_hash != config_hash()`, 422 (`validation`) on `ConfigError`, otherwise labels cleaned with `clean` (empty label falls back to the cleaned file stem), `save_config`, returns `{config_hash}`; an `OSError` on save → 500 `{"error":{"kind":"io",...}}`.
    - `POST /api/done` → 200 then `server.shutdown()` after the response is sent.
    - Any `GhError` → 502 `{"error":{"kind": <GhError.kind>, "message": <cleaned message>}}`.

- [ ] **Step 1: Write failing tests** (real server on loopback + fake `gh` runner + temp `GH_DEPLOY_WATCHER_HOME`): session returns the login and the current config and its hash; repos list sanitizes a hostile name (ESC, `<script>`, `|`, newline, U+202E) in `display` while `name` stays raw (Review Focus 5); workflows returns suggestions (`"PRD - Deploy to EU"` → `prd`/`PRD · EU`), preselects deploy workflows only, marks tracked ones with their saved env and label, hides nothing; invalid repo names (`acme`, `-x/api`, `acme/..`, `acme/api\n`) → 400; `PUT` saves exactly what `setup_wizard` would produce for the same choices, cleans labels, empty label → file stem; bad env, duplicate `(repo, file)` and bad repo → 422; stale `base_hash` → 409 and the file is untouched (Review Focus 3); a successful `PUT` returns a hash that equals `config_hash()` afterwards and allows a second `PUT` with it; `OSError` on save (read-only dir) → 500 with kind `io`; `GhError` kinds (`auth`, `not_found`, `network`) map to 502 with the kind; `POST /api/done` makes `server.wait()` return; corrupt `config.json` on `session` → 422-style error with the reason, no traceback. Add unit tests for `parse_config`, `config_hash`, `valid_repo_name`, `current_login`.
- [ ] **Step 2: Run** `python3 -m unittest tests.test_ui_api tests.test_config tests.test_github -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the three small additions in `config.py`/`github.py` and `ui_api.py`.
- [ ] **Step 4: Run tests.** Expected: PASS; full suite green.
- [ ] **Step 5: Commit** `feat: add selection UI API endpoints`.

### Task 4: The page (frontend)

**Depends on:** Task 3

**Files:**
- Create: `gh_deploy_watcher/ui/index.html`, `gh_deploy_watcher/ui/app.css`, `gh_deploy_watcher/ui/app.js`, `tests/test_ui_assets.py`

**Interfaces:**
- Consumes: the API from Task 3. Produces: `window.ghdwDirty` (boolean, `true` while there are unsaved changes), the three static files served by Task 2.
- Behavior (from the spec, "The page"): reads the token from `location.hash`, immediately replaces the URL with `history.replaceState` to drop it from the visible address, sends it as `X-Token`; without a token shows an "invalid session" message and makes no API calls (Review Focus 2); loads session then repos; repo search filters client-side; selecting a repo fetches its workflows (once, cached); preselect and "Select suggested" per the spec; per-row toggle, label input and `prd`/`dev` segmented control; header counters and action-bar summary update live; Save sends `PUT /api/config` with the `config_hash` from the last load/save; on 409 shows the reload message; on 422 shows the validation message inline; after a successful Save shows Done, which calls `POST /api/done`; Cancel calls `POST /api/done` without saving; loading, per-repo error with Retry, and not-signed-in (`kind: auth`) states exist. Visual design follows `docs/design/mockup-glass.html` (glass panels, floating header and action bar, switches, segmented control; light/dark; `prefers-reduced-transparency` and `prefers-reduced-motion` fallbacks; keyboard operable with visible focus).

- [ ] **Step 1: Write failing static checks** in `tests/test_ui_assets.py` (no browser, no Node): `index.html` references only `app.css` and `app.js`, has no inline `<script>` body and no `on*=` attributes, no `<style>` block, no `http(s)://` URLs; `app.js` contains none of `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`, `new Function`; it contains `ghdwDirty`, `X-Token`, `replaceState`, and calls each API path from Task 3; `app.css` contains `prefers-color-scheme`, `prefers-reduced-motion`, `prefers-reduced-transparency`; files exist in the package directory and are served by `UiServer` with the CSP headers (using the real server from Task 2).
- [ ] **Step 2: Run** `python3 -m unittest tests.test_ui_assets -v`. Expected: FAIL.
- [ ] **Step 3: Implement the three files.** Keep the JS in small functions with the pure parts (diffing the selection against the loaded config, building the `PUT` body) isolated so they are easy to read; no framework, no build step.
- [ ] **Step 4: Run the static checks and the full suite.** Expected: PASS.
- [ ] **Step 5: Manual verification checklist** (recorded in the PR, run by the implementer against a hermetic server with a fake runner started from a small throwaway script outside the repo, opened in the default browser): every state listed above renders; a hostile repo name from the fake runner is shown as text; the Save/409 flow; keyboard-only operation. State plainly in the report which items could not be observed.
- [ ] **Step 6: Commit** `feat: add selection UI page`.

### Task 5: Launcher, `ui` subcommand and menu

**Depends on:** Tasks 2, 3

**Files:**
- Create: `gh_deploy_watcher/ui_launcher.py`, `tests/test_ui_launcher.py`
- Modify: `gh_deploy_watcher/actions.py`, `gh_deploy_watcher/render.py`, `tests/test_actions.py`, `tests/test_render.py`

**Interfaces:**
- Produces:
  - `ui_launcher.find_window(repo_root: Path) -> Optional[Path]` — the executable inside `repo_root/build/GhDeployWatcher.app/Contents/MacOS/` if present and executable.
  - `ui_launcher.run_ui(server: UiServer, window: Optional[Path], spawn=subprocess.Popen, open_browser=<opens via /usr/bin/open>, notify_fn=notify, monotonic=time.monotonic) -> int` — spawns `window` with the URL written to its stdin; waits for it; on spawn failure or a non-zero exit within 3 seconds falls back to `open_browser(server.url)` and keeps waiting for done/idle; if the browser also fails, prints the URL to stderr and notifies; on window exit shuts the server down; terminates a still-running window when the server shuts down first; returns 0.
  - `actions.main(["ui"])` — acquires the instance lock (second launch → notification `"The selection window is already open"`, exit 0), builds `UiServer(static_dir=<package>/ui)`, `install_routes`, `start`, `run_ui`; any `OSError`/`ConfigError`/`ValueError` becomes the existing error menu path or a notification, never a traceback.
  - `render.py`: the "Add / remove repos…" item uses `param1=ui terminal=false` (both in `render_menu` and `error_menu`).

- [ ] **Step 1: Write failing tests** with fakes only (fake spawner, fake browser opener, fake notifier, fake clock): URL goes to the window's stdin and never into its argv; window success path; spawn raising `OSError` → browser; non-zero exit after 1s → browser; non-zero exit after 10s → no browser (the user closed it) and the server is shut down; browser failure → URL on stderr + notification; server shutdown first (done/idle) terminates the fake window; a second `ui` while the lock is held notifies and never starts a second server (Review Focus 1); the lock is released after exit and after an exception; `actions.main(["ui"])` with a broken config still returns without a traceback; the menu item now carries `param1=ui` and not `terminal=true`.
- [ ] **Step 2: Run** the new and affected tests. Expected: FAIL.
- [ ] **Step 3: Implement** `ui_launcher.py`, the `ui` subcommand, and the render change; update the README's menu description only if a test asserts it (the full README rewrite is Task 7).
- [ ] **Step 4: Run tests.** Expected: PASS; full suite green.
- [ ] **Step 5: Commit** `feat: add ui subcommand, launcher and menu entry`.

### Task 6: Native window (Swift)

**Depends on:** Task 5

**Files:**
- Create: `ui-shell/Package.swift`, `ui-shell/Sources/GhDeployWatcherUI/main.swift`, `ui-shell/Sources/GhDeployWatcherUI/NavigationPolicy.swift`, `ui-shell/build.sh` (executable), `ui-shell/README.md`, Swift tests or `--selftest` (see below)
- Modify: `.gitignore` (add `.build/`), `tests/test_ui_shell_build.py`

**Interfaces:**
- Produces: `build.sh` → `build/GhDeployWatcher.app` (bundle id `dev.gh-deploy-watcher.ui`, name `GhDeployWatcher`, `NSAllowsLocalNetworking` true, ad-hoc signed with `codesign --force --sign -`); the executable reads one line from stdin and opens it in a window (about 1100×720, title `gh-deploy-watcher`, titlebar transparent with full-size content), exits 0 when the window closes.
- `NavigationPolicy`: `func isAppURL(_ url: URL, port: Int) -> Bool` (scheme `http`, host `127.0.0.1`, that port); `func parseStartURL(_ line: String) -> URL?` (accepts only `http://127.0.0.1:<port>/...`, rejects `localhost`, other hosts, `file:`, `javascript:`, empty, over-long, control characters). Navigation to other origins opens in the default browser and is cancelled; no `WKScriptMessageHandler`; `WKWebsiteDataStore.nonPersistent()`.
- On window close it evaluates `window.ghdwDirty`; if true it shows an `NSAlert` (Cancel/Discard) before closing.

- [ ] **Step 1: Decide the test mechanism:** run `swift test` in a scratch package using only the Command Line Tools. If XCTest is available, add a test target for `NavigationPolicy`; otherwise implement `--selftest` in the binary that checks the same cases and exits non-zero on failure. Record the choice in the PR.
- [ ] **Step 2: Write the failing policy tests** (valid URL, wrong host, `localhost`, wrong scheme, wrong port, injected newline, very long input, empty) and a Python test `tests/test_ui_shell_build.py` that is skipped unless `swift` and the SDK exist, builds with `build.sh` into a temp output directory (via an env override `GH_DEPLOY_WATCHER_BUILD_DIR`), and asserts the bundle layout, the `Info.plist` keys, and that the binary refuses a bad URL on stdin with a non-zero exit and never opens a window.
- [ ] **Step 3: Implement** the package, `NavigationPolicy`, `main.swift` (AppKit app delegate, activation policy `.regular`, one window, `WKWebView`) and `build.sh` (`set -euo pipefail`, quoted variables, `swift build -c release`, assemble the bundle, ad-hoc sign, idempotent).
- [ ] **Step 4: Run** the policy tests and the Python build test (the build runs for real here). Expected: PASS.
- [ ] **Step 5: Smoke check** (the implementer, no human needed): start a hermetic `UiServer` from a throwaway script outside the repo with a fake runner, launch the built app with its URL on stdin, confirm via the server that the page was requested with the right `Host`, then terminate the app and confirm exit. State plainly that the window's appearance was not seen.
- [ ] **Step 6: Commit** `feat: add native window shell`.

### Task 7: Installer step and README

**Depends on:** Tasks 5, 6

**Files:**
- Modify: `install.sh`, `tests/test_install.sh`, `README.md`

**Interfaces:**
- Produces: an optional step in `install.sh` after the symlink step: when `swift` and the SDK are available (`command -v swift` and `xcrun --show-sdk-path`) and `build/GhDeployWatcher.app` is missing or older than any file in `ui-shell/`, run `ui-shell/build.sh` through the `run` helper; a failure or missing `swift` prints that the page will open in the browser and does NOT fail the install; new flag `--no-window` skips the step; `--dry-run` prints `would run: ...`; `--uninstall` leaves `build/`; the summary reports `built` / `already built` / `skipped`.
- README: describes the window, the browser fallback, `setup` as the terminal fallback, `--no-window`, how to rebuild (`ui-shell/build.sh`), and the new troubleshooting entries (window does not open → run `./install.sh` again; Gatekeeper is not involved because the app is built locally).

- [ ] **Step 1: Write failing shell tests** (hermetic stubs, fake HOME and PATH): with a stub `swift`/`xcrun` the build runs once and a second run says `already built`; touching a file under `ui-shell/` triggers a rebuild; no `swift` → skipped, exit 0; a failing stub `swift` → warning, exit 0; `--no-window` never calls `swift`; `--dry-run` prints the step and builds nothing; `--uninstall` leaves `build/` untouched.
- [ ] **Step 2: Run** `bash tests/test_install.sh`. Expected: FAIL.
- [ ] **Step 3: Implement** the step; keep `set -euo pipefail` safe (no failing command outside an `if`/`||` guard); quote everything.
- [ ] **Step 4: Update the README**; every claim must be checkable against the code.
- [ ] **Step 5: Run** `bash tests/test_install.sh` and the full suite. Expected: PASS.
- [ ] **Step 6: Commit** `feat: build the native window in the installer and document the UI`.

### Task 8: End-to-end verification on the author's machine

**Depends on:** Tasks 4, 5, 6, 7

**Files:**
- Modify: `README.md` (fixes found here only)

- [ ] **Step 1: Run the full suite and the shell tests.** Expected: all pass.
- [ ] **Step 2: Rebuild and relink** with `./install.sh` (the author runs it, or gives the go-ahead); confirm the window app is built.
- [ ] **Step 3: Verify in SwiftBar** (author present): menu → "Add / remove repos…" opens the window, not a browser tab; search, select, set env and label, Save, Done; the menu shows the new workflows within a minute; the second click while open notifies and opens nothing new.
- [ ] **Step 4: Verify the guard rails:** close the window with unsaved changes (prompt appears); edit `config.json` from the terminal while the window is open, then Save (409 message); `kill` the window process (server stops, lock released).
- [ ] **Step 5: Verify the browser fallback** by temporarily moving `build/GhDeployWatcher.app` aside: the page opens in the browser.
- [ ] **Step 6: Commit any README fixes** `docs: fixes from UI verification`.
