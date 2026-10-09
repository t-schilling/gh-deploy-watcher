"""Thin wrapper around the gh CLI. The only module that calls gh."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional

from .model import Run, parse_attempt

_EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin"]
_TIMEOUT = 30
_RUN_FIELDS = "databaseId,status,conclusion,createdAt,displayTitle,url,headBranch,attempt"


_NETWORK_HINTS = (
    "could not resolve host", "timeout", "network", "error connecting to",
    "check your internet connection", "no such host", "connection refused", "tls handshake",
)


class GhError(Exception):
    """Raised when a gh invocation fails; kind is a coarse error category."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__("%s: %s" % (kind, message))
        self.kind = kind
        self.message = message


Runner = Callable[[List[str]], str]


def classify_error(returncode: int, stderr: str) -> str:
    low = stderr.lower()
    if "http 404" in low or "not found" in low:
        return "not_found"
    if "gh auth login" in low or "authentication" in low:
        return "auth"
    if "rate limit" in low:
        return "rate_limit"
    if any(s in low for s in _NETWORK_HINTS):
        return "network"
    return "other"


def run_gh(args: List[str]) -> str:
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p] + _EXTRA_PATH
    env["PATH"] = os.pathsep.join(parts)
    try:
        proc = subprocess.run(
            ["gh"] + list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        raise GhError("missing", "gh CLI not found")
    except subprocess.TimeoutExpired:
        raise GhError("network", "gh timed out after %ds" % _TIMEOUT)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise GhError(classify_error(proc.returncode, stderr), stderr)
    return proc.stdout


def latest_run(repo: str, workflow_file: str, runner: Runner = run_gh) -> Optional[Run]:
    out = runner([
        "run", "list", "--repo", repo, "--workflow", workflow_file,
        "--limit", "1", "--json", _RUN_FIELDS,
    ])
    try:
        data = json.loads(out)
        if not data:
            return None
        d = data[0]
        return Run(
            id=int(d["databaseId"]),
            status=d["status"],
            conclusion=d.get("conclusion") or None,
            created_at=d["createdAt"],
            title=d["displayTitle"],
            url=d["url"],
            branch=d["headBranch"],
            attempt=parse_attempt(d.get("attempt")),
        )
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise GhError("other", "unexpected gh output: %s" % e)


def rerun_failed_args(repo: str, run_id: int) -> List[str]:
    return ["run", "rerun", str(run_id), "--failed", "--repo", repo]


def rerun_failed(repo: str, run_id: int, runner: Runner = run_gh) -> None:
    runner(rerun_failed_args(repo, run_id))


def list_repos(runner: Runner = run_gh) -> List[str]:
    out = runner([
        "api", "--paginate",
        "user/repos?per_page=100&affiliation=owner,collaborator,organization_member",
        "--jq", ".[].full_name",
    ])
    return [line.strip() for line in out.splitlines() if line.strip()]


@dataclass
class WorkflowInfo:
    name: str
    path: str
    state: str


def list_workflows(repo: str, runner: Runner = run_gh) -> List[WorkflowInfo]:
    out = runner([
        "api", "--paginate", "repos/%s/actions/workflows" % repo,
        "--jq", ".workflows[]|[.name,.path,.state]|@tsv",
    ])
    result: List[WorkflowInfo] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) != 3:
            raise GhError("other", "unexpected workflow line: %r" % line)
        result.append(WorkflowInfo(cols[0], cols[1], cols[2]))
    return result


def auth_ok(runner: Runner = run_gh) -> bool:
    try:
        runner(["auth", "status"])
    except GhError:
        return False
    return True


def current_login(runner: Runner = run_gh) -> str:
    return runner(["api", "user", "--jq", ".login"]).strip()
