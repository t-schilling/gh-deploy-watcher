"""Render the SwiftBar menu. The only module that knows the SwiftBar format."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Tuple

from gh_deploy_watcher.config import Config, Workflow, config_dir
from gh_deploy_watcher.model import (
    Run, age_text, classify, env_visible, overall_state, run_ref, workflow_key,
)
from gh_deploy_watcher.state import State

_ICONS = {
    "prd_failed": "🔴",
    "dev_failed": "🟠",
    "running": "🟡",
    "ok": "🟢",
    "paused": "⏸",
    "error": "⚠",
}
_CLASS_ICONS = {
    "failed": "🔴",
    "running": "🟡",
    "success": "🟢",
    "cancelled": "⚪",
    "skipped": "⚪",
}
_CLASS_WORDS = {
    "failed": "failed",
    "running": "running",
    "success": "ok",
    "cancelled": "cancelled",
    "skipped": "skipped",
    "unknown": "unknown",
}
_FILTERS = (("both", "Both"), ("prd", "PRD only"), ("dev", "DEV only"))


def sanitize(text: str) -> str:
    """Make text safe to place before ' | ' in a SwiftBar line."""
    text = str(text).replace("|", " ").replace("\r", " ").replace("\n", " ")
    return text.replace("'", "").replace('"', "")


def _check_arg(value: str) -> str:
    if "'" in value or "\n" in value or "\r" in value:
        raise ValueError("unsafe character in menu action argument: %r" % (value,))
    return value


def _action(script: str, *params: str) -> str:
    parts = ["bash='%s'" % _check_arg(script)]
    for i, p in enumerate(params, 1):
        parts.append("param%d=%s" % (i, _check_arg(p).replace(" ", "%20")))
    return " ".join(parts) + " terminal=false refresh=true"


def _read(entry: Any, now: datetime) -> Tuple[str, Optional[Run], Optional[str], str]:
    """Return (kind, run, message, age) where kind is run|none|error|unreadable."""
    if entry is None or entry == {}:
        return "none", None, None, ""
    if not isinstance(entry, dict):
        return "unreadable", None, None, ""
    if "run" in entry and entry["run"] is None:
        return "none", None, None, ""
    if "error" in entry:
        return "error", None, sanitize(entry["error"]), ""
    try:
        run = Run.from_dict(entry)
        return "run", run, None, age_text(run.created_at, now)
    except (KeyError, TypeError, ValueError, AttributeError):
        return "unreadable", None, None, ""


def _last_poll(state: State, now: datetime) -> str:
    if state.last_poll is None:
        return "never"
    try:
        secs = max(0, int(now.timestamp() - state.last_poll))
    except (OverflowError, ValueError, OSError):
        return "never"
    if secs < 60:
        return "%ds ago" % secs
    if secs < 3600:
        return "%dm ago" % (secs // 60)
    if secs < 86400:
        return "%dh ago" % (secs // 3600)
    return "%dd ago" % (secs // 86400)


def _workflow_lines(repo: str, wf: Workflow, entry: Optional[dict], now: datetime,
                    script: str) -> Tuple[List[str], Tuple[str, str]]:
    label = sanitize(wf.label)
    kind, run, msg, age = _read(entry, now)
    if kind == "none":
        return ["%s %s   no runs yet" % ("⚪", label)], (wf.env, "none")
    if kind == "unreadable":
        return ["⚪ %s   unreadable" % label], (wf.env, "unknown")
    if kind == "error":
        return ["⚠ %s   error" % label, "--%s | color=red" % msg], (wf.env, "unknown")
    assert run is not None
    cls = classify(run)
    head = "%s %s   %s · %s ago   %s" % (
        _CLASS_ICONS.get(cls, "⚪"), label, _CLASS_WORDS[cls], age, sanitize(run_ref(run.title)))
    lines = [head]
    if cls == "failed":
        try:
            action = _action(script, "rerun", repo, str(run.id), wf.env)
        except ValueError:
            action = None  # unsafe value: omit the action, keep the status line
        if action:
            lines.append("--↻ Re-run failed jobs | " + action)
    lines.append("--↗ Open run | href=%s" % sanitize(run.url).replace(" ", "%20"))
    return lines, (wf.env, cls)


def render_menu(config: Config, state: State, error: Optional[str], now: datetime,
                script_path: str) -> str:
    _check_arg(script_path)
    body: List[str] = []
    items: List[Tuple[str, str]] = []
    for repo in config.repos:
        visible = [w for w in repo.workflows if env_visible(w.env, state.filter)]
        body.append(sanitize(repo.repo))
        for wf in visible:
            lines, item = _workflow_lines(
                repo.repo, wf, state.last.get(workflow_key(repo.repo, wf.file)), now, script_path)
            body.extend(lines)
            items.append(item)
        body.append("---")
    overall = overall_state(items, state.polling, error)

    out = [_ICONS[overall], "---"]
    if error:
        out.append("⚠ %s | color=red" % sanitize(error))
    if state.polling:
        out.append("⏸ Stop polling | " + _action(script_path, "stop"))
    else:
        out.append("▶ Start polling | " + _action(script_path, "start"))
    out.append("View:")
    for key, name in _FILTERS:
        mark = "●" if state.filter == key else "○"
        out.append("--%s %s | %s" % (mark, name, _action(script_path, "filter", key)))
    out.append("Last poll: %s" % _last_poll(state, now))
    out.append("---")
    if not config.repos:
        out.append("No repos configured")
        out.append("---")
    out.extend(body)
    out.append("Poll now | " + _action(script_path, "refresh"))
    out.append("Add / remove repos… | bash='%s' param1=setup terminal=true" % script_path)
    out.append("Open config | bash=/usr/bin/open param1=%s terminal=false"
               % str(config_dir() / "config.json").replace(" ", "%20"))
    return "\n".join(out) + "\n"


def error_menu(reason: str) -> str:
    """Minimal warning menu for problems that prevent a normal render."""
    return "%s\n---\n%s\n" % (_ICONS["error"], sanitize(reason))
