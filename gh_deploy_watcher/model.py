"""Pure status model: run classification, overall state and text helpers."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

_FAILED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
_PR_RE = re.compile(r"(?:pull request|PR)\s*#(\d+)", re.IGNORECASE)
_REF_MAX = 30


def parse_attempt(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 1


@dataclass
class Run:
    id: int
    status: str
    conclusion: Optional[str]
    created_at: str
    title: str
    url: str
    branch: str
    attempt: int = 1

    @property
    def notify_key(self) -> str:
        return "%d:%d" % (self.id, self.attempt)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Run":
        return Run(
            id=int(d["id"]),
            status=d["status"],
            conclusion=d.get("conclusion"),
            created_at=d["created_at"],
            title=d["title"],
            url=d["url"],
            branch=d["branch"],
            attempt=parse_attempt(d.get("attempt")),
        )


def classify(run: Optional[Run]) -> str:
    if run is None:
        return "none"
    if run.status != "completed":
        return "running"
    if run.conclusion == "success":
        return "success"
    if run.conclusion in _FAILED_CONCLUSIONS:
        return "failed"
    if run.conclusion == "cancelled":
        return "cancelled"
    if run.conclusion == "skipped":
        return "skipped"
    return "unknown"


def env_visible(env: str, flt: str) -> bool:
    return flt == "both" or env == flt


def overall_state(items: List[Tuple[str, str]], polling: bool, error: Optional[str]) -> str:
    if not polling:
        return "paused"
    if error:
        return "error"
    if any(env == "prd" and c == "failed" for env, c in items):
        return "prd_failed"
    if any(env == "dev" and c == "failed" for env, c in items):
        return "dev_failed"
    if any(c == "error" for _, c in items):
        return "error"
    if any(c == "running" for _, c in items):
        return "running"
    return "ok"


def new_failures(current: Dict[str, Run], notified: Set[str], baseline: bool) -> List[str]:
    if baseline:
        return []
    return [k for k, r in current.items() if classify(r) == "failed" and r.notify_key not in notified]


def age_text(created_at: str, now: datetime) -> str:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    secs = max(0, int((now - created).total_seconds()))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def run_ref(title: str) -> str:
    m = _PR_RE.search(title)
    if m:
        return "#" + m.group(1)
    return title[:_REF_MAX]


def workflow_key(repo: str, file: str) -> str:
    return "%s/%s" % (repo, file)
