#!/usr/bin/env bash
# gh-deploy-watcher installer. Idempotent; safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY_NAME="gh-deploy-watcher.1m.py"
ENTRY="$REPO_DIR/$ENTRY_NAME"
APPS_DIR="${GH_DEPLOY_WATCHER_APPS_DIR:-/Applications}"

DRY_RUN=0
ASSUME_YES=0
UNINSTALL=0
NO_WINDOW=0
WINDOW_STATUS=""
PLUGIN_DIR=""
CHANGED=()
UNCHANGED=()

usage() {
  printf '%s\n' 'Usage: ./install.sh [options]

Installs gh, fzf and SwiftBar if missing (via Homebrew), checks that gh is
logged in, and symlinks the plugin into SwiftBar'\''s plugin folder.

Options:
  --dry-run             Print every action as "would run: ..." and change nothing.
  --plugin-dir <path>   Use this SwiftBar plugin folder instead of detecting it.
  --yes                 Never prompt; fail with a clear message if input is needed.
  --no-window           Skip building the native window (build/GhDeployWatcher.app);
                        the page then opens in the browser.
  --uninstall           Remove the plugin symlink this installer created.
                        Never touches ~/.config/gh-deploy-watcher/.
  --help                Show this help.

Requires macOS, Homebrew and python3 >= 3.9 (macOS Command Line Tools has it).'
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

# The single place that changes the system; honors --dry-run.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'would run: %s\n' "$*"
    return 0
  fi
  "$@"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --no-window) NO_WINDOW=1 ;;
    --plugin-dir)
      [ $# -ge 2 ] || die "--plugin-dir needs a path"
      PLUGIN_DIR="$2"
      shift
      ;;
    --help | -h) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

# True when we may prompt: stdin is a terminal (GH_DEPLOY_WATCHER_INTERACTIVE=1 is a test hook).
interactive() {
  [ "$ASSUME_YES" -eq 0 ] && { [ -t 0 ] || [ "${GH_DEPLOY_WATCHER_INTERACTIVE:-}" = "1" ]; }
}

expand_tilde() {
  case "$1" in
    "~") printf '%s' "$HOME" ;;
    "~/"*) printf '%s' "$HOME/${1#\~/}" ;;
    *) printf '%s' "$1" ;;
  esac
}

detect_plugin_dir() {
  local found=""
  if [ -z "$PLUGIN_DIR" ] && command -v defaults >/dev/null 2>&1; then
    PLUGIN_DIR="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
  fi
  if [ -z "$PLUGIN_DIR" ]; then
    interactive || die "could not detect the SwiftBar plugin folder. Launch SwiftBar once and pick a folder, then re-run with --plugin-dir <that folder>."
    info "Could not detect the SwiftBar plugin folder."
    info "Launch SwiftBar once and choose a plugin folder when it asks."
    printf "Enter the plugin folder SwiftBar uses: "
    read -r found || die "no folder given"
    [ -n "$found" ] || die "no folder given"
    PLUGIN_DIR="$found"
  fi
  PLUGIN_DIR="$(expand_tilde "$PLUGIN_DIR")"
}

link_path() { printf '%s/%s' "$PLUGIN_DIR" "$ENTRY_NAME"; }

if [ "$UNINSTALL" -eq 1 ]; then
  detect_plugin_dir
  target="$(link_path)"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$ENTRY" ]; then
    run rm "$target"
    if [ "$DRY_RUN" -eq 1 ]; then
      info "Would remove symlink $target. Your config in ~/.config/gh-deploy-watcher/ is never touched."
    else
      info "Removed symlink $target. Your config in ~/.config/gh-deploy-watcher/ was left untouched."
    fi
  elif [ -L "$target" ]; then
    info "$target is a symlink that points elsewhere ($(readlink "$target")), not one made by this installer; leaving it alone."
  elif [ -e "$target" ]; then
    info "$target is a regular file, not a symlink made by this installer; leaving it alone."
  else
    info "Nothing to remove: no symlink at $target."
  fi
  exit 0
fi

# 1. macOS
[ "$(uname)" = "Darwin" ] || die "this tool supports macOS only."

# 2. python3 >= 3.9
if ! command -v python3 >/dev/null 2>&1 \
  || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  info "python3 >= 3.9 is required but was not found."
  info "Install the macOS Command Line Tools, which provide python3 3.9:"
  info "  xcode-select --install"
  exit 1
fi

# 3. Homebrew
if ! command -v brew >/dev/null 2>&1; then
  info "Homebrew is required but was not found."
  info "Install it from https://brew.sh (follow the official instructions there), then re-run ./install.sh."
  exit 1
fi

# 4. gh, fzf, SwiftBar
need_gh=0; need_fzf=0; need_swiftbar=0
command -v gh >/dev/null 2>&1 || need_gh=1
command -v fzf >/dev/null 2>&1 || need_fzf=1
if [ ! -d "$APPS_DIR/SwiftBar.app" ] && [ ! -d "$HOME/Applications/SwiftBar.app" ]; then
  need_swiftbar=1
fi
if [ $((need_gh + need_fzf + need_swiftbar)) -eq 0 ]; then
  info "Nothing needs installing: gh, fzf and SwiftBar are present."
fi
if [ "$need_gh" -eq 1 ]; then run brew install gh; CHANGED+=("installed gh"); else UNCHANGED+=("gh present"); fi
if [ "$need_fzf" -eq 1 ]; then run brew install fzf; CHANGED+=("installed fzf"); else UNCHANGED+=("fzf present"); fi
if [ "$need_swiftbar" -eq 1 ]; then run brew install --cask swiftbar; CHANGED+=("installed SwiftBar"); else UNCHANGED+=("SwiftBar present"); fi

# 5. gh authentication (gh just installed in a dry run cannot be asked)
if [ "$need_gh" -eq 1 ] && [ "$DRY_RUN" -eq 1 ]; then
  run gh auth login
elif gh auth status >/dev/null 2>&1; then
  UNCHANGED+=("gh already authenticated")
elif [ "$ASSUME_YES" -eq 1 ]; then
  info "gh is not logged in. Run this yourself, then re-run the installer:"
  info "  gh auth login"
  exit 1
else
  run gh auth login
  CHANGED+=("logged in to gh")
fi

# 6. plugin directory
detect_plugin_dir

# 7. symlink
target="$(link_path)"
if [ ! -d "$PLUGIN_DIR" ]; then
  run mkdir -p "$PLUGIN_DIR"
  CHANGED+=("created $PLUGIN_DIR")
fi
if [ -L "$target" ] && [ "$(readlink "$target")" = "$ENTRY" ]; then
  UNCHANGED+=("symlink already in place")
elif [ -L "$target" ]; then
  old="$(readlink "$target")"
  run ln -sfn "$ENTRY" "$target"
  CHANGED+=("replaced symlink that pointed to $old")
elif [ -e "$target" ]; then
  die "$target is a regular file; refusing to overwrite it. Move it away and re-run."
else
  run ln -s "$ENTRY" "$target"
  CHANGED+=("linked $target")
fi
if [ ! -x "$ENTRY" ]; then
  run chmod +x "$ENTRY"
  CHANGED+=("made the entrypoint executable")
fi

# 8. native window (optional; a missing toolchain or failed build never fails the install)
WINDOW_EXE="$REPO_DIR/build/GhDeployWatcher.app/Contents/MacOS/GhDeployWatcher"
WINDOW_SRC="$REPO_DIR/ui-shell"
window_stale() {
  [ -x "$WINDOW_EXE" ] || return 0
  [ -n "$(find "$WINDOW_SRC" -path "$WINDOW_SRC/.build" -prune -o -type f -newer "$WINDOW_EXE" -print -quit 2>/dev/null || true)" ]
}
if [ "$NO_WINDOW" -eq 1 ]; then
  WINDOW_STATUS="skipped (--no-window; the page opens in the browser)"
elif ! command -v swift >/dev/null 2>&1; then
  WINDOW_STATUS="skipped (swift not found; the page will open in the browser)"
elif ! xcrun --show-sdk-path >/dev/null 2>&1; then
  WINDOW_STATUS="skipped (no macOS SDK, try: xcode-select --install; the page will open in the browser)"
elif window_stale; then
  if [ "$DRY_RUN" -eq 1 ]; then
    run /bin/bash "$WINDOW_SRC/build.sh"
    WINDOW_STATUS="would build"
  elif run /bin/bash "$WINDOW_SRC/build.sh" >/dev/null; then
    WINDOW_STATUS="built"
  else
    info "Building the native window failed; the page will open in the browser instead."
    info "Fix the error above and re-run ./install.sh to try again."
    WINDOW_STATUS="skipped (build failed; the page will open in the browser)"
  fi
else
  WINDOW_STATUS="already built"
fi

# 9. setup wizard (offered; never aborts the installer)
answer="y"
if interactive && [ "$DRY_RUN" -eq 0 ]; then
  printf "Run setup now? [Y/n] "
  read -r answer || answer="n"
  [ -n "$answer" ] || answer="y"
else
  answer="n"
fi
case "$answer" in
  [Yy]*) run "$ENTRY" setup || info "setup did not finish; re-run it later with: $ENTRY setup" ;;
  *)
    info "Next, choose which repos to watch by running:"
    info "  $ENTRY setup"
    ;;
esac

info ""
info "Summary"
verb="changed"
[ "$DRY_RUN" -eq 1 ] && verb="would change"
if [ ${#CHANGED[@]} -eq 0 ]; then
  info "  Nothing changed."
else
  for item in "${CHANGED[@]}"; do info "  $verb: $item"; done
fi
info "  window: $WINDOW_STATUS"
for ((i = 0; i < ${#UNCHANGED[@]}; i++)); do info "  already: ${UNCHANGED[$i]}"; done
