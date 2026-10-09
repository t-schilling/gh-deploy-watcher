"""Polling, menu actions and the plugin's command-line dispatch."""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TextIO, Tuple

from gh_deploy_watcher.config import Config, ConfigError, load_config
from gh_deploy_watcher.github import GhError, Runner, latest_run, rerun_failed, rerun_failed_args, run_gh
from gh_deploy_watcher.model import Run, classify, env_visible, new_failures, run_ref, workflow_key
from gh_deploy_watcher.notify import confirm, notify
from gh_deploy_watcher.render import error_menu, render_menu
from gh_deploy_watcher.state import VALID_FILTERS, State, load_state, save_state

_GLOBAL_KINDS = ("auth", "missing", "network", "rate_limit")
_NOTIFIED_CAP = 200
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

NotifyFn = Callable[[str, str], None]
ConfirmFn = Callable[[str], bool]


def _cached_run(entry: object) -> Optional[Run]:
    try:
        return Run.from_dict(entry)  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def poll(config: Config, state: State, runner: Runner, now: float,
         baseline: bool = False) -> Tuple[State, List[str], Optional[str]]:
    fresh: Dict[str, Run] = {}
    attempted = 0
    global_errors: List[str] = []
    for repo in config.repos:
        for wf in repo.workflows:
            if not env_visible(wf.env, state.filter):
                continue
            key = workflow_key(repo.repo, wf.file)
            attempted += 1
            try:
                run = latest_run(repo.repo, wf.file, runner)
            except GhError as exc:
                if exc.kind in _GLOBAL_KINDS:
                    global_errors.append(exc.message or str(exc))  # keep last good entry
                else:
                    state.last[key] = {"error": exc.message or str(exc)}
                continue
            if run is None:
                state.last[key] = {"run": None}
            else:
                state.last[key] = run.to_dict()
                fresh[key] = run
    error = global_errors[0] if attempted and len(global_errors) == attempted else None
    state.last_poll = now
    if baseline:
        failed = [r.id for r in fresh.values() if classify(r) == "failed"]
        state.notified.extend(i for i in failed if i not in state.notified)
        keys: List[str] = []
    else:
        keys = new_failures(fresh, set(state.notified), False)
    del state.notified[:-_NOTIFIED_CAP]
    return state, keys, error


def _poll_and_notify(config: Config, state: State, runner: Runner, notify_fn: NotifyFn,
                     now: float, baseline: bool = False) -> Optional[str]:
    _, keys, error = poll(config, state, runner, now, baseline)
    labels = {workflow_key(r.repo, w.file): (r.repo, w.label)
              for r in config.repos for w in r.workflows}
    for key in keys:
        run = _cached_run(state.last.get(key))
        if run is None:
            continue
        repo, label = labels.get(key, ("", key))
        try:
            notify_fn("Deploy failed: %s" % label, "%s · %s" % (repo, run_ref(run.title)))
        except Exception:
            pass
        state.notified.append(run.id)
    del state.notified[:-_NOTIFIED_CAP]
    save_state(state)
    return error


def _fail(message: str, code: int = 2) -> int:
    sys.stderr.write(message + "\n")
    return code


def rerun(repo: str, run_id: object, env: str, dry_run: bool = False,
          confirm_fn: ConfirmFn = confirm, runner: Runner = run_gh,
          notify_fn: NotifyFn = notify, now: Optional[float] = None,
          out: Optional[TextIO] = None) -> int:
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        return _fail("invalid repo %r: expected owner/name" % (repo,))
    try:
        rid = int(run_id)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return _fail("invalid run id %r: expected an integer" % (run_id,))
    if env not in ("prd", "dev"):
        return _fail("invalid env %r: expected prd or dev" % (env,))
    if dry_run:
        (out or sys.stdout).write("gh " + " ".join(rerun_failed_args(repo, rid)) + "\n")
        return 0
    if env == "prd" and not confirm_fn(
            "Re-run failed jobs of run %d in %s? This redeploys to production." % (rid, repo)):
        return 1
    try:
        rerun_failed(repo, rid, runner)
    except GhError as exc:
        try:
            notify_fn("Re-run failed", exc.message or str(exc))
        except Exception:
            pass
        return 1
    config, state = load_config(), load_state()
    _poll_and_notify(config, state, runner, notify_fn, time.time() if now is None else now)
    return 0


def main(argv: List[str], script_path: Optional[str] = None, runner: Runner = run_gh,
         notify_fn: NotifyFn = notify, confirm_fn: ConfirmFn = confirm,
         now: Optional[float] = None, out: Optional[TextIO] = None) -> int:
    out = out or sys.stdout
    script = script_path or os.path.abspath(sys.argv[0])
    ts = time.time() if now is None else now
    try:
        return _dispatch(list(argv), script, runner, notify_fn, confirm_fn, ts, out)
    except (ConfigError, ValueError) as exc:
        out.write(error_menu(str(exc)))
        return 0


def _dispatch(argv: List[str], script: str, runner: Runner, notify_fn: NotifyFn,
              confirm_fn: ConfirmFn, ts: float, out: TextIO) -> int:
    if not argv:
        config, state = load_config(), load_state()
        error = None
        if state.polling:
            error = _poll_and_notify(config, state, runner, notify_fn, ts)
        out.write(render_menu(config, state, error, datetime.fromtimestamp(ts, timezone.utc), script))
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "setup":
        return _fail("The setup wizard is not available yet.", 1)
    if cmd == "rerun":
        flags = [a for a in args if a == "--dry-run"]
        pos = [a for a in args if a != "--dry-run"]
        if len(pos) != 3:
            return _fail("usage: rerun <repo> <run_id> <env> [--dry-run]")
        return rerun(pos[0], pos[1], pos[2], bool(flags), confirm_fn, runner, notify_fn, ts, out)
    if cmd == "filter" and (len(args) != 1 or args[0] not in VALID_FILTERS):
        return _fail("invalid filter %r: expected one of %s" % (args[:1], ", ".join(VALID_FILTERS)))
    if cmd not in ("start", "stop", "filter", "refresh"):
        return _fail("unknown command %r" % cmd)
    config, state = load_config(), load_state()
    if cmd == "stop":
        state.polling = False
        save_state(state)
    elif cmd == "start":
        state.polling = True
        _poll_and_notify(config, state, runner, notify_fn, ts, baseline=True)
    elif cmd == "filter":
        state.filter = args[0]
        _poll_and_notify(config, state, runner, notify_fn, ts)
    else:
        _poll_and_notify(config, state, runner, notify_fn, ts)
    return 0
