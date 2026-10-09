# gh-deploy-watcher

A macOS menu bar tool that watches the GitHub Actions deploy workflows you choose and shows their latest status at a glance. It is a [SwiftBar](https://swiftbar.app) plugin written in Python 3.9+ (standard library only) that polls through your existing `gh` login. It notifies you when a deploy fails and can re-run the failed jobs from the menu.

> **Note: this is NOT a `gh` extension.** Despite the `gh-` prefix, it is not installed with `gh extension install` and `gh deploy-watcher` does not exist. It is a SwiftBar plugin; it only calls `gh` to talk to GitHub.

## What the menu looks like

The menu bar shows one icon for the overall state:

- red: a PRD workflow's latest run failed
- orange: a DEV workflow's latest run failed (and no PRD failure)
- yellow: something is running
- green: everything visible is fine
- pause symbol: polling is stopped
- warning symbol: an error while polling, such as `gh` not being logged in

Clicking it opens a menu with, from top to bottom:

1. An error line in red, when there is one.
2. `Stop polling` or `Start polling`.
3. A `View:` submenu with `Both`, `PRD only` and `DEV only` (the current choice is marked).
4. `Last poll: ...` (for example `2m ago`, or `never`).
5. One section per configured repo, for example `acme/api`, with one line per workflow: a status dot, the label, the status (`failed`, `running`, `ok`, `cancelled`, `skipped`, `unknown`), how long ago it started, and the run's PR or commit reference. Each has a submenu with `Open run` (opens the run in the browser) and, for failed runs, `Re-run failed jobs`.
6. `Poll now`, `Add / remove repos…` (opens the setup wizard in a terminal) and `Open config`.

With no repos configured the menu says `No repos configured`.

## Requirements

- macOS.
- `python3` 3.9 or newer. The macOS Command Line Tools provide it (`xcode-select --install`). The installer does not install Python for you, because the plugin runs with `#!/usr/bin/env python3` and SwiftBar's PATH may not include Homebrew.
- [Homebrew](https://brew.sh).
- `gh` (GitHub CLI), logged in with access to the repos you want to watch.
- `fzf` (used by the setup wizard for picking; without it the wizard falls back to numbered lists).
- SwiftBar.

The installer installs `gh`, `fzf` and SwiftBar with Homebrew when they are missing.

## Install

```sh
git clone <this repository's URL> gh-deploy-watcher
cd gh-deploy-watcher
./install.sh
```

The installer verifies macOS and Python, requires Homebrew (it prints the install instructions if it is missing and never downloads anything into a shell itself), installs what is missing, runs `gh auth login` if `gh` is not authenticated, links the plugin into SwiftBar's plugin folder, and then offers to run the setup wizard.

Options: `--dry-run` (print every step as `would run: ...` and change nothing), `--plugin-dir <path>`, `--yes` (never prompt; fails with a message instead when input is needed), `--uninstall`, `--help`. It is safe to re-run.

### First run

1. Launch SwiftBar. On first launch it asks for a plugin folder; pick or create one (for example `~/swiftbar-plugins`).
2. Run `./install.sh` (again). It reads the folder from SwiftBar's settings. If it cannot find it, it asks you to enter the folder; or pass `--plugin-dir <folder>`.
3. In the setup wizard, pick the repos and workflows to watch, and confirm each one's label and environment (`prd` or `dev`). You can re-run it any time with `./gh-deploy-watcher.1m.py setup` or from the menu.
4. The icon appears in the menu bar. Choose `Start polling`.

If the installer cannot detect SwiftBar's folder the first time (SwiftBar not launched yet), launch SwiftBar, choose the folder, and re-run `./install.sh`.

## Usage

- **Start / stop polling.** `Start polling` takes a baseline (failures that already exist do not notify) and then the plugin refreshes about once a minute (the `1m` in the filename). `Stop polling` pauses it; the icon becomes the pause symbol.
- **Filter.** `View:` switches between `Both`, `PRD only` and `DEV only`. Hidden environments are not polled.
- **Poll now.** Refreshes immediately.
- **Re-run failed jobs.** For a failed run, the submenu has `Re-run failed jobs`. For a `prd` workflow, a confirmation dialog first warns that this redeploys to production; Cancel is the default. For `dev` workflows there is no dialog.
- **Notifications.** When a workflow's latest run newly fails, a macOS notification titled `Deploy failed: <label>` appears, once per run.
- **Adding / removing repos.** `Add / remove repos…` opens the wizard with: add repos/workflows, remove repos/workflows, list config, done. Changes are saved when you choose done.
- **Where things live.** `~/.config/gh-deploy-watcher/` holds `config.json` (what to watch; see `config.example.json`), `state.json` (polling on/off, filter, cached results, notified runs) and `state.lock`. Set the environment variable `GH_DEPLOY_WATCHER_HOME` to use another directory.

Example `config.json`:

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

## Update

```sh
cd gh-deploy-watcher
git pull
```

The plugin is a symlink to this checkout, so a `git pull` is all it takes.

## Uninstall

```sh
./install.sh --uninstall
```

This removes only the symlink in the SwiftBar plugin folder. It never touches `~/.config/gh-deploy-watcher/`; delete that folder yourself if you want your config and state gone. Homebrew packages (`gh`, `fzf`, SwiftBar) are left installed.

## Troubleshooting

- **SwiftBar can't find `gh`.** SwiftBar runs plugins with a minimal PATH. The plugin extends PATH itself with `/opt/homebrew/bin` and `/usr/local/bin`, so a Homebrew `gh` is found. If `gh` lives elsewhere, make sure it is in one of those folders.
- **No notifications.** Notifications are sent with `osascript`. Allow notifications in System Settings, Notifications, for the app that runs it (SwiftBar, or Script Editor).
- **Authentication errors in the menu.** Run `gh auth status` in a terminal; if it fails, run `gh auth login`.
- **Reset state.** Stop polling, delete `~/.config/gh-deploy-watcher/state.json`, and start again. Your `config.json` is not affected.

## Security and privacy

- No tokens are stored by this tool. It uses whatever login `gh` already has and only calls `gh`.
- State and config stay on your machine in `~/.config/gh-deploy-watcher/`.
- `Re-run failed jobs` on a PRD workflow redeploys production, which is why it asks first.

## License

MIT, see [LICENSE](LICENSE).
