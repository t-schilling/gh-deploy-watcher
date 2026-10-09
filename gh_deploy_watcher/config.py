"""Load, validate and save the watcher configuration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

VALID_ENVS = ("prd", "dev")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class ConfigError(Exception):
    """Raised when the config file is unreadable or invalid."""


@dataclass
class Workflow:
    file: str
    env: str
    label: str


@dataclass
class RepoConfig:
    repo: str
    workflows: List[Workflow] = field(default_factory=list)


@dataclass
class Config:
    repos: List[RepoConfig] = field(default_factory=list)


def config_dir() -> Path:
    home = os.environ.get("GH_DEPLOY_WATCHER_HOME")
    if home:
        return Path(home)
    return Path.home() / ".config" / "gh-deploy-watcher"


def _default_path() -> Path:
    return config_dir() / "config.json"


def valid_repo_name(name: str) -> bool:
    if not isinstance(name, str) or not _REPO_RE.fullmatch(name):
        return False
    return all(seg not in (".", "..") and not seg.startswith("-")
               for seg in name.split("/"))


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict) or not isinstance(data.get("repos", []), list):
        raise ConfigError("config must be an object with a 'repos' list")
    repos: List[RepoConfig] = []
    seen = set()
    for r in data.get("repos", []):
        if not isinstance(r, dict):
            raise ConfigError("each entry in 'repos' must be an object")
        repo = r.get("repo")
        if not valid_repo_name(repo):
            raise ConfigError("invalid repo %r: expected 'owner/name'" % (repo,))
        wfs_raw = r.get("workflows", [])
        if not isinstance(wfs_raw, list):
            raise ConfigError("'workflows' for %s must be a list" % repo)
        workflows: List[Workflow] = []
        for w in wfs_raw:
            if not isinstance(w, dict):
                raise ConfigError("each workflow in %s must be an object" % repo)
            file = w.get("file")
            if not isinstance(file, str) or not file.strip():
                raise ConfigError("workflow in %s needs a non-empty 'file'" % repo)
            env = w.get("env")
            if env not in VALID_ENVS:
                raise ConfigError(
                    "invalid env %r for %s/%s: expected one of %s"
                    % (env, repo, file, ", ".join(VALID_ENVS))
                )
            label = w.get("label", file)
            if not isinstance(label, str):
                raise ConfigError("label for %s/%s must be a string" % (repo, file))
            if (repo, file) in seen:
                raise ConfigError("duplicate workflow %s in %s" % (file, repo))
            seen.add((repo, file))
            workflows.append(Workflow(file, env, label))
        repos.append(RepoConfig(repo, workflows))
    return Config(repos)


_parse = parse_config


def config_hash(path: Optional[Path] = None) -> str:
    path = Path(path) if path else _default_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    return hashlib.sha256(data).hexdigest()


def load_config(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else _default_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config([])
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    except UnicodeDecodeError:
        raise ConfigError("%s is not valid UTF-8 text" % path)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc))
    return _parse(data)


def save_config(config: Config, path: Optional[Path] = None) -> None:
    path = Path(path) if path else _default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "repos": [
            {
                "repo": r.repo,
                "workflows": [
                    {"file": w.file, "env": w.env, "label": w.label}
                    for w in r.workflows
                ],
            }
            for r in config.repos
        ]
    }
    # Unique temp file in the same directory so concurrent saves never share one.
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
