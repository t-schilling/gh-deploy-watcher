"""Interactive setup wizard: pick repos and workflows, confirm env and label."""
from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Tuple

from .config import VALID_ENVS, Config, RepoConfig, Workflow, save_config
from .github import GhError, Runner, WorkflowInfo, list_repos, list_workflows, run_gh

_EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin"]
_PRD_WORDS = ("prd", "prod", "production")
_DEV_WORDS = ("dev", "development", "stg", "staging", "qa")
_SKIP_NAMES = ("codeql", "dependabot updates")
_SELECTION_RE = re.compile(r"^\d+(-\d+)?(\s*,\s*\d+(-\d+)?)*$")

FzfRunner = Callable[[List[str], str], Tuple[int, str]]


def _strip_leading(name: str) -> str:
    i = 0
    while i < len(name) and not name[i].isalnum():
        i += 1
    return name[i:]


def suggest(workflow: WorkflowInfo) -> Tuple[str, str]:
    name = _strip_leading(workflow.name)
    first = re.split(r"[^A-Za-z0-9]+", name, maxsplit=1)[0].lower()
    env = "prd" if first in _PRD_WORDS else "dev" if first in _DEV_WORDS else None
    if env is None:
        return "dev", name
    prefix = re.match(r"[A-Za-z0-9]+", name).group(0)  # type: ignore[union-attr]
    rest = name[len(prefix):].lstrip(" :-\u2013\u2014")
    to = re.search(r"\bto\s+(.+)$", rest, re.IGNORECASE)
    tail = (to.group(1) if to else rest).strip()
    return env, "%s \u00b7 %s" % (prefix, tail) if tail else prefix


def preselect(workflows: List[WorkflowInfo]) -> List[WorkflowInfo]:
    keep = []
    for w in workflows:
        text = (w.name + " " + w.path).lower()
        if w.state != "active" or "deploy" not in text or "-old" in text:
            continue
        if w.name.lower() in _SKIP_NAMES:
            continue
        keep.append(w)
    return keep


def _which_fzf() -> Optional[str]:
    parts = _EXTRA_PATH + [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return shutil.which("fzf", path=os.pathsep.join(parts))


def _run_fzf(args: List[str], text: str) -> Tuple[int, str]:
    proc = subprocess.run(args, input=text, stdout=subprocess.PIPE,
                          universal_newlines=True)
    return proc.returncode, proc.stdout


def _parse_selection(text: str, count: int) -> Optional[List[int]]:
    """Return zero-based indexes for '1,3,5-7', or None if out of range/invalid."""
    picked: List[int] = []
    for part in text.replace(" ", "").split(","):
        lo, _, hi = part.partition("-")
        a, b = int(lo), int(hi or lo)
        if a < 1 or b > count or a > b:
            return None
        picked.extend(i for i in range(a - 1, b) if i not in picked)
    return picked


def pick(options: List[str], multi: bool, preselected: List[str],
         runner: FzfRunner = _run_fzf, ask: Callable[[str], str] = input,
         out: Callable[[str], None] = print,
         which: Callable[[], Optional[str]] = _which_fzf) -> List[str]:
    fzf = which()
    if fzf and not preselected:
        args = [fzf, "-m"] if multi else [fzf]
        code, stdout = runner(args, "\n".join(options))
        return [l for l in stdout.splitlines() if l] if code == 0 else []
    shown = list(options)
    while True:
        for i, opt in enumerate(shown, 1):
            out("%3d) %s%s" % (i, "* " if opt in preselected else "", opt))
        hint = "Numbers (1,3,5-7)" if multi else "Number"
        if multi:
            hint += ", 'all'"
        hint += ", text to filter"
        if preselected:
            hint += ", Enter for * defaults"
        ans = ask("%s: " % hint).strip()
        if not ans:
            return [o for o in options if o in preselected]
        if multi and ans.lower() == "all":
            return list(shown)
        if _SELECTION_RE.match(ans):
            idx = _parse_selection(ans, len(shown))
            if idx is None:
                out("Number out of range (1-%d)." % len(shown))
            elif not multi and len(idx) != 1:
                out("Pick exactly one.")
            else:
                return [shown[i] for i in idx]
            continue
        found = [o for o in options if ans.lower() in o.lower()]
        if found:
            shown = found
        else:
            out("No match for %r." % ans)


def _wf_option(w: WorkflowInfo) -> str:
    return "%s [%s]" % (w.name, os.path.basename(w.path))


def _ask_env(ask: Callable[[str], str], default: str) -> str:
    while True:
        ans = ask("Env (%s) [%s]: " % ("/".join(VALID_ENVS), default)).strip().lower()
        if not ans:
            return default
        if ans in VALID_ENVS:
            return ans


def _add(config: Config, runner: Runner, picker, ask, out) -> Config:
    new = copy.deepcopy(config)
    repos = picker(list_repos(runner), True, [])
    for repo in repos:
        have = next((r for r in new.repos if r.repo == repo), None)
        known = {w.file for w in have.workflows} if have else set()
        infos = [w for w in list_workflows(repo, runner)
                 if os.path.basename(w.path) not in known]
        if not infos:
            out("%s: no new workflows." % repo)
            continue
        by_option: Dict[str, WorkflowInfo] = {_wf_option(w): w for w in infos}
        defaults = [_wf_option(w) for w in preselect(infos)]
        chosen = picker(list(by_option), True, defaults)
        added: List[Workflow] = []
        for option in chosen:
            info = by_option[option]
            env, label = suggest(info)
            label = ask("Label for %s [%s]: " % (option, label)).strip() or label
            env = _ask_env(ask, env)
            added.append(Workflow(os.path.basename(info.path), env, label))
        if not added:
            continue
        if have is None:
            have = RepoConfig(repo, [])
            new.repos.append(have)
        have.workflows.extend(added)
    return new


def _remove(config: Config, picker) -> Config:
    new = copy.deepcopy(config)
    options = ["%s :: %s (%s)" % (r.repo, w.file, w.label)
               for r in new.repos for w in r.workflows]
    if not options:
        return new
    chosen = set(picker(options, True, []))
    for r in new.repos:
        r.workflows = [w for w in r.workflows
                       if "%s :: %s (%s)" % (r.repo, w.file, w.label) not in chosen]
    new.repos = [r for r in new.repos if r.workflows]
    return new


def _list(config: Config, out: Callable[[str], None]) -> None:
    if not config.repos:
        out("No repos configured.")
    for r in config.repos:
        out(r.repo)
        for w in r.workflows:
            out("  [%s] %s (%s)" % (w.env, w.label, w.file))


def run_wizard(config: Config, runner: Runner = run_gh, picker=pick,
               ask: Callable[[str], str] = input,
               out: Callable[[str], None] = print) -> Config:
    start = copy.deepcopy(config)
    current = copy.deepcopy(config)
    try:
        while True:
            out("1) Add repos/workflows  2) Remove repos/workflows  3) List config  4) Done")
            choice = ask("Choose 1-4: ").strip()
            try:
                if choice == "1":
                    current = _add(current, runner, picker, ask, out)
                elif choice == "2":
                    current = _remove(current, picker)
                elif choice == "3":
                    _list(current, out)
                elif choice == "4":
                    if current != start:
                        save_config(current)
                        out("Saved.")
                    return current
                else:
                    out("Please enter 1, 2, 3 or 4.")
            except GhError as exc:
                out("GitHub error: %s" % (exc.message or exc))
    except (KeyboardInterrupt, EOFError):
        out("Aborted; nothing saved.")
        return start
