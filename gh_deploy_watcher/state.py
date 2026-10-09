"""Persist watcher runtime state (polling flag, filter, cached runs)."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from gh_deploy_watcher.config import config_dir

VALID_FILTERS = ("both", "prd", "dev")
_KEY_RE = re.compile(r"[0-9]+:[0-9]+")


@dataclass
class State:
    polling: bool = False
    filter: str = "both"
    last: Dict[str, dict] = field(default_factory=dict)
    last_poll: Optional[float] = None
    notified: List[str] = field(default_factory=list)


def _default_path() -> Path:
    return config_dir() / "state.json"


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _notify_key(n: Any) -> Optional[str]:
    """Normalise a stored dedupe key; a legacy int id means attempt 1."""
    if _is_int(n):
        return "%d:1" % n
    if isinstance(n, str) and _KEY_RE.fullmatch(n):
        return n
    return None


def _parse(data: Any) -> State:
    if not isinstance(data, dict):
        return State()
    state = State()
    if isinstance(data.get("polling"), bool):
        state.polling = data["polling"]
    if data.get("filter") in VALID_FILTERS:
        state.filter = data["filter"]
    last = data.get("last")
    if isinstance(last, dict):
        state.last = {k: v for k, v in last.items() if isinstance(v, dict)}
    lp = data.get("last_poll")
    if isinstance(lp, (int, float)) and not isinstance(lp, bool):
        state.last_poll = lp
    notified = data.get("notified")
    if isinstance(notified, list):
        state.notified = [k for k in (_notify_key(n) for n in notified) if k]
    return state


def load_state(path: Optional[Path] = None) -> State:
    path = Path(path) if path else _default_path()
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        return State()
    return _parse(data)


def save_state(state: State, path: Optional[Path] = None) -> None:
    path = Path(path) if path else _default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "polling": state.polling,
        "filter": state.filter,
        "last": state.last,
        "last_poll": state.last_poll,
        "notified": state.notified,
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
