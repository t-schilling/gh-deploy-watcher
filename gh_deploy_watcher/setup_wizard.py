"""Interactive setup wizard: pick repos and workflows, confirm env and label."""
from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

from .config import VALID_ENVS, Config, RepoConfig, Workflow, save_config
from .github import GhError, Runner, WorkflowInfo, list_repos, list_workflows, run_gh
from .selection import clean, preselect, suggest

_EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin"]
_SELECTION_RE = re.compile(r"^\d+(-\d+)?(\s*,\s*\d+(-\d+)?)*$")

T = TypeVar("T")
FzfRunner = Callable[[List[str], str], Tuple[int, str]]


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
    return "%s [%s]" % (clean(w.name), clean(os.path.basename(w.path)))


def _unique_map(items: List[T], display: Callable[[T], str]) -> Dict[str, T]:
    """Map cleaned display strings back to the raw items, keeping order and uniqueness."""
    result: Dict[str, T] = {}
    for item in items:
        text = display(item)
        base, n = text, 1
        while text in result:
            n += 1
            text = "%s #%d" % (base, n)
        result[text] = item
    return result


def _ask_env(ask: Callable[[str], str], default: str) -> str:
    while True:
        ans = ask("Env (%s) [%s]: " % ("/".join(VALID_ENVS), default)).strip().lower()
        if not ans:
            return default
        if ans in VALID_ENVS:
            return ans


def _add(config: Config, runner: Runner, picker, ask, out) -> Config:
    new = copy.deepcopy(config)
    repo_map = _unique_map(list_repos(runner), clean)
    for key in picker(list(repo_map), True, []):
        repo = repo_map[key]
        have = next((r for r in new.repos if r.repo == repo), None)
        known = {w.file for w in have.workflows} if have else set()
        infos = [w for w in list_workflows(repo, runner)
                 if os.path.basename(w.path) not in known]
        if not infos:
            out("%s: no new workflows." % clean(repo))
            continue
        by_option = _unique_map(infos, _wf_option)
        wanted = [id(w) for w in preselect(infos)]
        defaults = [o for o, w in by_option.items() if id(w) in wanted]
        added: List[Workflow] = []
        for option in picker(list(by_option), True, defaults):
            info = by_option[option]
            env, label = suggest(info)
            typed = clean(ask("Label for %s [%s]: " % (option, label)))
            env = _ask_env(ask, env)
            added.append(Workflow(os.path.basename(info.path), env, typed or label))
        if not added:
            continue
        if have is None:
            have = RepoConfig(repo, [])
            new.repos.append(have)
        have.workflows.extend(added)
    return new


def _remove(config: Config, picker) -> Config:
    new = copy.deepcopy(config)
    pairs = [(r, w) for r in new.repos for w in r.workflows]
    options = _unique_map(pairs, lambda p: "%s :: %s (%s)" % (
        clean(p[0].repo), clean(p[1].file), clean(p[1].label)))
    if not options:
        return new
    chosen = [id(options[o][1]) for o in picker(list(options), True, [])]
    for r in new.repos:
        r.workflows = [w for w in r.workflows if id(w) not in chosen]
    new.repos = [r for r in new.repos if r.workflows]
    return new


def _list(config: Config, out: Callable[[str], None]) -> None:
    if not config.repos:
        out("No repos configured.")
    for r in config.repos:
        out(clean(r.repo))
        for w in r.workflows:
            out("  [%s] %s (%s)" % (clean(w.env), clean(w.label), clean(w.file)))


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
                out("GitHub error: %s" % clean(str(exc.message or exc)))
    except (KeyboardInterrupt, EOFError):
        out("Aborted; nothing saved.")
        return start
