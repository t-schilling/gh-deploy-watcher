#!/usr/bin/env bash
# Hermetic tests for install.sh: fake HOME, fake PATH holding only stubs.
# Nothing real is installed or changed; no network.
set -u

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/ghdw-install-test.XXXXXX")" && pwd)"
trap 'rm -rf "$ROOT"' EXIT

FAILS=0
PASSES=0
OUT=""
RC=0

ok() { PASSES=$((PASSES + 1)); }
bad() { FAILS=$((FAILS + 1)); printf 'FAIL: %s\n' "$1" >&2; }

assert_rc() { # expected description
  if [ "$RC" -eq "$1" ]; then ok; else bad "$2: expected exit $1, got $RC; output: $OUT"; fi
}
assert_rc_nonzero() {
  if [ "$RC" -ne 0 ]; then ok; else bad "$1: expected non-zero exit; output: $OUT"; fi
}
assert_out_has() { # needle description
  case "$OUT" in *"$1"*) ok ;; *) bad "$2: output lacks '$1'; output: $OUT" ;; esac
}
assert_out_lacks() {
  case "$OUT" in *"$1"*) bad "$2: output unexpectedly has '$1'; output: $OUT" ;; *) ok ;; esac
}
assert_log_lacks() { # needle description
  if grep -q -- "$1" "$LOG" 2>/dev/null; then bad "$2: log has '$1': $(cat "$LOG")"; else ok; fi
}
assert_log_has() {
  if grep -q -- "$1" "$LOG" 2>/dev/null; then ok; else bad "$2: log lacks '$1'"; fi
}
assert_true() { # description, command...
  local d="$1"; shift
  if "$@"; then ok; else bad "$d"; fi
}

REAL_UTILS="ln mkdir chmod rm readlink dirname find touch"

# new_case <name> [stubs-to-omit...]: builds $CASE with home/, bin/, repo/, apps/
new_case() {
  local name="$1"; shift
  CASE="$ROOT/$name"
  HOME_="$CASE/home"
  BIN="$CASE/bin"
  REPO="$CASE/repo"
  LOG="$CASE/calls.log"
  PLUGINS="$CASE/plugins"
  mkdir -p "$HOME_" "$BIN" "$REPO" "$CASE/apps"
  : > "$LOG"
  local u p
  for u in $REAL_UTILS; do
    p="$(command -v "$u")" && ln -s "$p" "$BIN/$u"
  done
  cp "$SRC_DIR/install.sh" "$REPO/install.sh"
  write_entry "$REPO"
  stub uname 'echo "${STUB_UNAME:-Darwin}"'
  stub python3 '[ "${STUB_PY:-3.12}" = 3.8 ] && exit 1; exit 0'
  stub brew 'exit 0'
  stub gh '[ "$1 $2" = "auth status" ] && { [ "${STUB_GH_AUTH:-ok}" = ok ]; exit; }; exit 0'
  stub fzf 'exit 0'
  stub defaults '[ -n "${STUB_SWIFTBAR_DIR:-}" ] && { echo "$STUB_SWIFTBAR_DIR"; exit 0; }; exit 1'
  mkdir -p "$CASE/apps/SwiftBar.app"
  # Window build: stub swift/xcrun, plus a fake ui-shell/build.sh that calls the stub swift
  # and drops a fake executable where the real build.sh puts it. No real Swift build ever runs.
  stub swift '[ "${STUB_SWIFT_RC:-0}" -eq 0 ] || { echo "swift: boom" >&2; exit "$STUB_SWIFT_RC"; }; exit 0'
  stub xcrun 'echo /fake/sdk'
  mkdir -p "$REPO/ui-shell/Sources"
  printf 'x\n' > "$REPO/ui-shell/Sources/main.swift"
  printf '#!/bin/sh\nswift build -c release || exit 1\nd="$(cd "$(dirname "$0")/.." && pwd)/build/GhDeployWatcher.app/Contents/MacOS"\nmkdir -p "$d"\nprintf "#!/bin/sh\\n" > "$d/GhDeployWatcher"\nchmod +x "$d/GhDeployWatcher"\n' > "$REPO/ui-shell/build.sh"
  touch -t 202001010000 "$REPO/ui-shell/build.sh" "$REPO/ui-shell/Sources/main.swift"
  local o
  for o in "$@"; do
    case "$o" in
      swiftbar) rm -rf "$CASE/apps/SwiftBar.app" ;;
      *) rm -f "$BIN/$o" ;;
    esac
  done
}

# fake entrypoint: logs its arguments, exit code from STUB_SETUP_RC
write_entry() {
  printf '#!/bin/sh\necho "entry $*" >> "$STUB_LOG"\nexit "${STUB_SETUP_RC:-0}"\n' > "$1/gh-deploy-watcher.1m.py"
  chmod +x "$1/gh-deploy-watcher.1m.py"
}

stub() { # name body
  printf '#!/bin/sh\necho "%s $*" >> "$STUB_LOG"\n%s\n' "$1" "$2" > "$BIN/$1"
  chmod +x "$BIN/$1"
}

# run_install <args...>; extra env via EXTRA_ENV (space separated K=V)
run_install() {
  RC=0
  # shellcheck disable=SC2086
  OUT="$(cd "$CASE" && env -i HOME="$HOME_" PATH="$BIN" STUB_LOG="$LOG" STUB_APPS="$CASE/apps" \
    GH_DEPLOY_WATCHER_APPS_DIR="$CASE/apps" ${EXTRA_ENV:-} \
    /bin/bash "$REPO/install.sh" "$@" 2>&1 <<< "${STDIN_TEXT:-}")" || RC=$?
}

TARGET_NAME="gh-deploy-watcher.1m.py"

# 1. dry-run, everything missing
new_case dry_missing gh fzf swiftbar brew
run_install --dry-run --plugin-dir "$PLUGINS"
assert_rc 1 "dry-run with brew missing"   # brew absent is fatal even in dry-run
assert_out_has "brew.sh" "missing brew guidance"

# 1b. dry-run, brew present but gh, fzf and SwiftBar missing
new_case dry_missing2 gh fzf swiftbar
run_install --dry-run --plugin-dir "$PLUGINS"
assert_rc 0 "dry-run lists steps"
assert_out_has "would run: brew install gh" "dry-run gh"
assert_out_has "would run: brew install fzf" "dry-run fzf"
assert_out_has "would run: brew install --cask swiftbar" "dry-run swiftbar"
assert_out_has "would run: gh auth login" "dry-run gh auth (gh not installed yet)"
assert_out_has "would run: ln -s" "dry-run symlink"
assert_log_lacks "brew install" "dry-run never installs"
assert_true "dry-run created no plugin dir" test ! -e "$PLUGINS"

# 2. dry-run, everything present
new_case dry_present
mkdir -p "$PLUGINS"
ln -s "$REPO/$TARGET_NAME" "$PLUGINS/$TARGET_NAME"
run_install --dry-run --plugin-dir "$PLUGINS"
assert_rc 0 "dry-run all present"
assert_out_has "Nothing needs installing" "all present message"
assert_out_lacks "would run: brew install" "no installs when present"
assert_log_lacks "brew install" "brew install never called"

# 3. python too old
new_case py_old
EXTRA_ENV="STUB_PY=3.8" run_install --dry-run --plugin-dir "$PLUGINS"
assert_rc_nonzero "python 3.8"
assert_out_has "xcode-select --install" "python guidance"

# 3b. python3 missing entirely
new_case py_none python3
run_install --dry-run --plugin-dir "$PLUGINS"
assert_rc_nonzero "python3 missing"
assert_out_has "xcode-select --install" "python missing guidance"

# 3c. not macOS
new_case not_mac
EXTRA_ENV="STUB_UNAME=Linux" run_install --dry-run --plugin-dir "$PLUGINS"
assert_rc_nonzero "non-macOS"
assert_out_has "macOS" "non-macOS message"

# 4. missing brew
new_case no_brew brew
run_install --yes --plugin-dir "$PLUGINS"
assert_rc_nonzero "brew missing"
assert_out_has "https://brew.sh" "brew guidance"

# 5. --yes with unauthenticated gh
new_case unauth
EXTRA_ENV="STUB_GH_AUTH=fail" run_install --yes --plugin-dir "$PLUGINS"
assert_rc_nonzero "unauthenticated gh with --yes"
assert_out_has "gh auth login" "auth guidance"
assert_true "no symlink created on auth failure" test ! -e "$PLUGINS/$TARGET_NAME"

# 5b. --yes without a findable plugin dir
new_case no_dir
run_install --yes
assert_rc_nonzero "no plugin dir with --yes"
assert_out_has "--plugin-dir" "plugin dir guidance"

# 6. symlink created, entrypoint made executable, setup command printed
new_case link
chmod -x "$REPO/$TARGET_NAME"
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "link created"
assert_true "symlink exists" test -L "$PLUGINS/$TARGET_NAME"
assert_true "symlink target" test "$(readlink "$PLUGINS/$TARGET_NAME")" = "$REPO/$TARGET_NAME"
assert_true "entrypoint executable" test -x "$REPO/$TARGET_NAME"
assert_out_has "$REPO/$TARGET_NAME setup" "setup command printed"
assert_log_lacks "brew install" "nothing to install when all present"

# 7. idempotent re-run
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "second run"
assert_out_has "Nothing changed" "second run says nothing changed"
assert_true "symlink intact" test "$(readlink "$PLUGINS/$TARGET_NAME")" = "$REPO/$TARGET_NAME"

# 7b. plugin dir via defaults
new_case via_defaults
EXTRA_ENV="STUB_SWIFTBAR_DIR=$CASE/sb" run_install --yes
assert_rc 0 "plugin dir from defaults"
assert_true "symlink in defaults dir" test -L "$CASE/sb/$TARGET_NAME"

# 8. symlink pointing elsewhere is replaced
new_case replace
mkdir -p "$PLUGINS"
ln -s "/somewhere/else/$TARGET_NAME" "$PLUGINS/$TARGET_NAME"
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "replace stale symlink"
assert_true "now points to repo" test "$(readlink "$PLUGINS/$TARGET_NAME")" = "$REPO/$TARGET_NAME"

# 9. regular file refused
new_case regular
mkdir -p "$PLUGINS"
echo "mine" > "$PLUGINS/$TARGET_NAME"
run_install --yes --plugin-dir "$PLUGINS"
assert_rc_nonzero "regular file refused"
assert_out_has "regular file" "refusal message"
assert_true "regular file untouched" test "$(cat "$PLUGINS/$TARGET_NAME")" = "mine"
assert_true "still not a symlink" test ! -L "$PLUGINS/$TARGET_NAME"

# 10. uninstall removes only the symlink
new_case uninstall
mkdir -p "$HOME_/.config/gh-deploy-watcher"
echo '{"repos": []}' > "$HOME_/.config/gh-deploy-watcher/config.json"
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "install before uninstall"
assert_true "link exists before uninstall" test -L "$PLUGINS/$TARGET_NAME"
run_install --uninstall --plugin-dir "$PLUGINS"
assert_rc 0 "uninstall"
assert_out_has "Removed symlink" "uninstall says removed"
assert_true "symlink removed" test ! -e "$PLUGINS/$TARGET_NAME" -a ! -L "$PLUGINS/$TARGET_NAME"
assert_true "config kept" test -f "$HOME_/.config/gh-deploy-watcher/config.json"
assert_true "repo kept" test -f "$REPO/$TARGET_NAME"
run_install --uninstall --plugin-dir "$PLUGINS"
assert_rc 0 "uninstall twice"
# uninstall leaves a foreign regular file alone
echo "mine" > "$PLUGINS/$TARGET_NAME"
run_install --uninstall --plugin-dir "$PLUGINS"
assert_true "foreign file kept on uninstall" test "$(cat "$PLUGINS/$TARGET_NAME")" = "mine"

# 10b. uninstall leaves a symlink pointing elsewhere (exit 0, says so)
new_case uninstall_other
mkdir -p "$PLUGINS"
ln -s "/somewhere/else/$TARGET_NAME" "$PLUGINS/$TARGET_NAME"
run_install --uninstall --plugin-dir "$PLUGINS"
assert_rc 0 "uninstall foreign symlink"
assert_out_has "points elsewhere" "says it points elsewhere"
assert_true "foreign symlink kept" test "$(readlink "$PLUGINS/$TARGET_NAME")" = "/somewhere/else/$TARGET_NAME"

# 10c. dry-run uninstall claims nothing as done
new_case uninstall_dry
run_install --yes --plugin-dir "$PLUGINS"
run_install --uninstall --dry-run --plugin-dir "$PLUGINS"
assert_rc 0 "dry-run uninstall"
assert_out_has "would run: rm" "dry-run uninstall lists rm"
assert_out_lacks "Removed" "dry-run does not claim removal"
assert_true "link still there after dry-run" test -L "$PLUGINS/$TARGET_NAME"

# 13. setup prompt (interactive hook: GH_DEPLOY_WATCHER_INTERACTIVE=1 stands in for a tty)
new_case setup_no
STDIN_TEXT="n" EXTRA_ENV="GH_DEPLOY_WATCHER_INTERACTIVE=1" run_install --plugin-dir "$PLUGINS"
assert_rc 0 "setup declined"
assert_out_has "Run setup now?" "prompted"
assert_out_has "Summary" "summary after declining"
assert_log_lacks "entry setup" "setup not run on n"

new_case setup_yes
STDIN_TEXT="y" EXTRA_ENV="GH_DEPLOY_WATCHER_INTERACTIVE=1" run_install --plugin-dir "$PLUGINS"
assert_rc 0 "setup accepted"
assert_log_has "entry setup" "setup run on y"
assert_out_has "Summary" "summary after setup"

new_case setup_default
STDIN_TEXT="" EXTRA_ENV="GH_DEPLOY_WATCHER_INTERACTIVE=1" run_install --plugin-dir "$PLUGINS"
assert_rc 0 "setup default answer"
assert_log_has "entry setup" "Enter means yes"

new_case setup_fails
STDIN_TEXT="y" EXTRA_ENV="GH_DEPLOY_WATCHER_INTERACTIVE=1 STUB_SETUP_RC=3" run_install --plugin-dir "$PLUGINS"
assert_rc 0 "failing setup does not abort"
assert_out_has "setup did not finish" "setup failure message"
assert_out_has "Summary" "summary after failing setup"

new_case setup_yes_flag
EXTRA_ENV="GH_DEPLOY_WATCHER_INTERACTIVE=1" run_install --yes --plugin-dir "$PLUGINS"
assert_out_lacks "Run setup now?" "--yes never prompts"
assert_out_has "$REPO/$TARGET_NAME setup" "--yes prints command"
assert_log_lacks "entry setup" "--yes never runs setup"
EXTRA_ENV="GH_DEPLOY_WATCHER_INTERACTIVE=1" run_install --dry-run --plugin-dir "$PLUGINS"
assert_out_lacks "Run setup now?" "--dry-run never prompts"
assert_log_lacks "entry setup" "--dry-run never runs setup"
assert_out_lacks "  changed:" "dry-run summary not claiming done"

# 14. tilde expansion in all plugin-dir sources
new_case tilde_flag
run_install --yes --plugin-dir "~/x"
assert_rc 0 "tilde in --plugin-dir"
assert_true "expanded to fake HOME" test -L "$HOME_/x/$TARGET_NAME"
new_case tilde_defaults
EXTRA_ENV="STUB_SWIFTBAR_DIR=~/sb" run_install --yes
assert_rc 0 "tilde in defaults result"
assert_true "defaults tilde expanded" test -L "$HOME_/sb/$TARGET_NAME"
new_case tilde_typed
STDIN_TEXT="~/typed" EXTRA_ENV="GH_DEPLOY_WATCHER_INTERACTIVE=1" run_install
assert_true "typed tilde expanded" test -L "$HOME_/typed/$TARGET_NAME"

# 15. EOF on the plugin-dir prompt
new_case eof_prompt
STDIN_TEXT="" EXTRA_ENV="GH_DEPLOY_WATCHER_INTERACTIVE=1" run_install
assert_rc_nonzero "EOF at folder prompt"
assert_out_has "no folder given" "EOF message"

# 11. repo path with a space
new_case spaced
REPO="$CASE/my repo"
mkdir -p "$REPO"
cp "$SRC_DIR/install.sh" "$REPO/install.sh"
write_entry "$REPO"
run_install --yes --plugin-dir "$CASE/plug ins"
assert_rc 0 "path with space"
assert_true "symlink with space" test "$(readlink "$CASE/plug ins/$TARGET_NAME")" = "$REPO/$TARGET_NAME"

# 12. --help and unknown flag
new_case help
run_install --help
assert_rc 0 "help"
assert_out_has "--dry-run" "help lists flags"
run_install --bogus
assert_rc_nonzero "unknown flag"

# 14. native window step
APP_EXE() { printf '%s/build/GhDeployWatcher.app/Contents/MacOS/GhDeployWatcher' "$REPO"; }
count_swift() { grep -c '^swift build' "$LOG" 2>/dev/null || true; }

new_case win_build
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "window build"
assert_true "app built" test -x "$(APP_EXE)"
assert_true "swift ran once" test "$(count_swift)" = 1
assert_out_has "window: built" "summary says built"
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "window second run"
assert_true "swift not re-run" test "$(count_swift)" = 1
assert_out_has "window: already built" "summary says already built"

# touching a file under ui-shell/ triggers a rebuild
touch -t 202101010000 "$(APP_EXE)"
touch "$REPO/ui-shell/Sources/main.swift"
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "window rebuild"
assert_true "swift ran again" test "$(count_swift)" = 2
assert_out_has "window: built" "rebuild says built"

# files under ui-shell/.build do not trigger a rebuild
mkdir -p "$REPO/ui-shell/.build"
touch "$REPO/ui-shell/.build/artifact"
run_install --yes --plugin-dir "$PLUGINS"
assert_true ".build ignored" test "$(count_swift)" = 2
assert_out_has "window: already built" ".build ignored says already built"

new_case win_noswift swift
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "no swift"
assert_out_has "window: skipped" "no swift skipped"
assert_out_has "browser" "no swift mentions browser"
assert_true "no app without swift" test ! -e "$(APP_EXE)"

new_case win_nosdk xcrun
run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "no sdk"
assert_out_has "window: skipped" "no sdk skipped"
assert_log_lacks "^swift " "swift not called without sdk"

new_case win_fail
EXTRA_ENV="STUB_SWIFT_RC=1" run_install --yes --plugin-dir "$PLUGINS"
assert_rc 0 "failing swift does not fail install"
assert_out_has "will open in the browser" "failure note"
assert_out_has "window: skipped" "failure summary skipped"
assert_true "symlink still created" test -L "$PLUGINS/$TARGET_NAME"

new_case win_flag
run_install --yes --no-window --plugin-dir "$PLUGINS"
assert_rc 0 "--no-window"
assert_log_lacks "^swift " "--no-window never calls swift"
assert_out_has "window: skipped" "--no-window skipped"
assert_out_has "--no-window" "--no-window reason"

new_case win_dry
run_install --dry-run --plugin-dir "$PLUGINS"
assert_rc 0 "dry-run window"
assert_out_has "would run:" "dry-run lists step"
assert_out_has "build.sh" "dry-run names build.sh"
assert_log_lacks "^swift " "dry-run builds nothing"
assert_true "dry-run no build dir" test ! -e "$REPO/build"

new_case win_uninstall
run_install --yes --plugin-dir "$PLUGINS"
run_install --uninstall --plugin-dir "$PLUGINS"
assert_rc 0 "uninstall after build"
assert_true "uninstall leaves build/" test -x "$(APP_EXE)"

new_case win_help
run_install --help
assert_out_has "--no-window" "help lists --no-window"

printf '%d passed, %d failed\n' "$PASSES" "$FAILS"
[ "$FAILS" -eq 0 ]
