"""Shared selection logic: sanitising workflow text, suggesting env/label, preselecting."""
from __future__ import annotations

import os
import re
import unicodedata
from typing import List, Tuple

from .github import WorkflowInfo

_PRD_WORDS = ("prd", "prod", "production")
_DEV_WORDS = ("dev", "development", "stg", "staging", "qa")
_SKIP_NAMES = ("codeql", "dependabot updates")


def clean(text: str) -> str:
    """Make untrusted text safe to print and store: no control/format chars or '|'."""
    kept = "".join(c for c in text if unicodedata.category(c)[0] != "C" and c != "|")
    return " ".join(kept.split())


def _strip_leading(name: str) -> str:
    i = 0
    while i < len(name) and not name[i].isalnum():
        i += 1
    return name[i:]


def suggest(workflow: WorkflowInfo) -> Tuple[str, str]:
    name = _strip_leading(clean(workflow.name))
    stem = clean(os.path.splitext(os.path.basename(workflow.path))[0])
    first = re.split(r"[^A-Za-z0-9]+", name, maxsplit=1)[0].lower()
    env = "prd" if first in _PRD_WORDS else "dev" if first in _DEV_WORDS else None
    if env is None:
        return "dev", name or stem
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
