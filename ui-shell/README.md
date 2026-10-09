# ui-shell

A small native macOS window (AppKit + `WKWebView`) that hosts the selection UI served by the local `gh-deploy-watcher ui` server. It holds no logic of its own.

## Build

Needs only the Command Line Tools (no Xcode project):

    bash ui-shell/build.sh

This writes `build/GhDeployWatcher.app` (ad-hoc signed) and prints its path. Set `GH_DEPLOY_WATCHER_BUILD_DIR` to use another output directory. `build/` and `.build/` are git-ignored.

## How the URL arrives

The launcher writes one line, `http://127.0.0.1:<port>/#<token>`, to the app's stdin and closes the pipe. The URL is never passed in argv or the environment. Nothing is created or loaded until that line has passed `parseStartURL`; otherwise the process exits non-zero with a short message on stderr and no window is shown.

## Security model

- Only `http://127.0.0.1:<port>/...` for the given port is accepted and navigable; other links open in the default browser and are cancelled in the window, as are new-window requests.
- No `WKScriptMessageHandler`: the page has no bridge to native code.
- The website data store is non-persistent; the app only calls `load` with the validated http URL.
- Closing with unsaved changes (`window.ghdwDirty`) asks for confirmation.
- XCTest is not available with only the Command Line Tools, so the policy checks run via `GhDeployWatcher --selftest`, which `tests/test_ui_shell_build.py` executes.
