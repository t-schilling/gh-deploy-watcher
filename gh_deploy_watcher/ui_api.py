"""JSON endpoints for the selection UI. Untrusted text is cleaned before it is echoed."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import (Config, ConfigError, RepoConfig, Workflow, config_hash, load_config,
                     parse_config, save_config, valid_repo_name)
from .github import GhError, Runner, current_login, list_repos, list_workflows, run_gh
from .selection import clean, preselect, suggest
from .ui_server import Request, Response, UiServer


def _error(status: int, kind: str, message: str) -> Response:
    return Response(status, {"error": {"kind": kind, "message": clean(message)}})


def _config_json(config: Config) -> Dict[str, Any]:
    return {"repos": [{"repo": r.repo, "workflows": [
        {"file": w.file, "env": w.env, "label": w.label} for w in r.workflows]}
        for r in config.repos]}


def _guarded(fn: Callable[[Request], Response]) -> Callable[[Request], Response]:
    def wrapper(req: Request) -> Response:
        try:
            return fn(req)
        except GhError as exc:
            return _error(502, exc.kind, str(exc.message or exc.kind))
        except ConfigError as exc:
            return _error(422, "config", str(exc))
        except OSError:
            return _error(500, "io", "cannot read or write the config file")
    return wrapper


def _build_config(body: Any) -> Config:
    """Validate the PUT body's shape and clean labels; ConfigError on anything wrong."""
    repos = body.get("repos")
    if not isinstance(repos, list):
        raise ConfigError("'repos' must be a list")
    out: List[Dict[str, Any]] = []
    for r in repos:
        if not isinstance(r, dict) or not isinstance(r.get("workflows"), list):
            raise ConfigError("each repo needs a 'workflows' list")
        wfs: List[Dict[str, Any]] = []
        for w in r["workflows"]:
            if not isinstance(w, dict):
                raise ConfigError("each workflow must be an object")
            file, env, label = w.get("file"), w.get("env"), w.get("label", "")
            if not isinstance(file, str) or not isinstance(env, str) \
                    or not isinstance(label, str):
                raise ConfigError("file, env and label must be strings")
            if file != os.path.basename(file) or clean(file) != file:
                raise ConfigError("invalid workflow file name")
            stem = clean(os.path.splitext(file)[0])
            wfs.append({"file": file, "env": env, "label": clean(label) or stem})
        out.append({"repo": r.get("repo"), "workflows": wfs})
    return parse_config({"repos": out})


def install_routes(server: UiServer, runner: Runner = run_gh,
                   config_path: Optional[Path] = None) -> None:
    def session(req: Request) -> Response:
        return Response(200, {"login": clean(current_login(runner)),
                              "config": _config_json(load_config(config_path)),
                              "config_hash": config_hash(config_path)})

    def repos(req: Request) -> Response:
        return Response(200, [{"name": n, "display": clean(n)} for n in list_repos(runner)])

    def workflows(req: Request) -> Response:
        repo = req.params[0]
        if not valid_repo_name(repo):
            return _error(400, "bad_request", "invalid repository name")
        tracked = {}
        for r in load_config(config_path).repos:
            if r.repo == repo:
                tracked = {w.file: {"env": w.env, "label": w.label} for w in r.workflows}
        infos = list_workflows(repo, runner)
        picked = [id(w) for w in preselect(infos)]
        result = []
        for info in infos:
            file = os.path.basename(info.path)
            env, label = suggest(info)
            result.append({"file": file, "name": clean(info.name), "state": clean(info.state),
                           "suggested": {"env": env, "label": label},
                           "preselected": id(info) in picked,
                           "tracked": tracked.get(file)})
        return Response(200, result)

    def put_config(req: Request) -> Response:
        body = req.body
        if not isinstance(body, dict) or not isinstance(body.get("base_hash"), str):
            return _error(422, "validation", "body must be an object with a base_hash")
        if body["base_hash"] != config_hash(config_path):
            return _error(409, "conflict", "config changed on disk; reload and retry")
        try:
            config = _build_config(body)
        except ConfigError as exc:
            return _error(422, "validation", str(exc))
        save_config(config, config_path)
        return Response(200, {"config_hash": config_hash(config_path)})

    def done(req: Request) -> Response:
        server.shutdown()  # waits for this reply to be sent before closing
        return Response(200, {})

    server.add_route("GET", r"/api/session", _guarded(session))
    server.add_route("GET", r"/api/repos", _guarded(repos))
    server.add_route("GET", r"/api/repos/([\s\S]*)/workflows", _guarded(workflows))
    server.add_route("PUT", r"/api/config", _guarded(put_config))
    server.add_route("POST", r"/api/done", done)
