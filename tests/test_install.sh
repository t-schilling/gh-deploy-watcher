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

REAL_UTILS="ln mkdir chmod rm readlink dirname"

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
  printf '#!/usr/bin/env python3\n' > "$REPO/gh-deploy-watcher.1m.py"
  chmod +x "$REPO/gh-deploy-watcher.1m.py"
  stub uname 'echo "${STUB_UNAME:-Darwin}"'
  stub python3 '[ "${STUB_PY:-3.12}" = 3.8 ] && exit 1; exit 0'
  stub brew 'exit 0'
  stub gh '[ "$1 $2" = "auth status" ] && { [ "${STUB_GH_AUTH:-ok}" = ok ]; exit; }; exit 0'
  stub fzf 'exit 0'
  stub defaults '[ -n "${STUB_SWIFTBAR_DIR:-}" ] && { echo "$STUB_SWIFTBAR_DIR"; exit 0; }; exit 1'
  mkdir -p "$CASE/apps/SwiftBar.app"
  local o
  for o in "$@"; do
    case "$o" in
      swiftbar) rm -rf "$CASE/apps/SwiftBar.app" ;;
      *) rm -f "$BIN/$o" ;;
    esac
  done
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
    /bin/bash "$REPO/install.sh" "$@" 2>&1 < /dev/null)" || RC=$?
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
run_install --uninstall --plugin-dir "$PLUGINS"
assert_rc 0 "uninstall"
assert_true "symlink removed" test ! -e "$PLUGINS/$TARGET_NAME" -a ! -L "$PLUGINS/$TARGET_NAME"
assert_true "config kept" test -f "$HOME_/.config/gh-deploy-watcher/config.json"
assert_true "repo kept" test -f "$REPO/$TARGET_NAME"
run_install --uninstall --plugin-dir "$PLUGINS"
assert_rc 0 "uninstall twice"
# uninstall leaves a foreign regular file alone
echo "mine" > "$PLUGINS/$TARGET_NAME"
run_install --uninstall --plugin-dir "$PLUGINS"
assert_true "foreign file kept on uninstall" test "$(cat "$PLUGINS/$TARGET_NAME")" = "mine"

# 11. repo path with a space
new_case spaced
REPO="$CASE/my repo"
mkdir -p "$REPO"
cp "$SRC_DIR/install.sh" "$REPO/install.sh"
printf '#!/usr/bin/env python3\n' > "$REPO/$TARGET_NAME"
chmod +x "$REPO/$TARGET_NAME"
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

printf '%d passed, %d failed\n' "$PASSES" "$FAILS"
[ "$FAILS" -eq 0 ]
