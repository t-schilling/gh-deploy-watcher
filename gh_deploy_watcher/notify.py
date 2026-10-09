"""macOS notifications and confirmation dialogs via osascript."""
from __future__ import annotations

import subprocess
from typing import Callable, List, Optional

Runner = Callable[[List[str]], int]

_NOTIFY_TIMEOUT = 10
_CONFIRM_TIMEOUT = 120  # a person has to click
_FAILED = 1


def _run_osascript(args: List[str], timeout: Optional[int] = None) -> int:
    """Run osascript and return its exit code; never raises."""
    try:
        proc = subprocess.run(
            ["osascript"] + list(args),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout or _NOTIFY_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return _FAILED
    return proc.returncode


def _run_confirm(args: List[str]) -> int:
    return _run_osascript(args, timeout=_CONFIRM_TIMEOUT)


def _quote(text: str) -> str:
    """Build an AppleScript string literal (backslash escaped first)."""
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return '"%s"' % text


def notify(title: str, message: str, runner: Runner = _run_osascript) -> None:
    """Show a macOS notification. Failures are swallowed."""
    script = "display notification %s with title %s" % (_quote(message), _quote(title))
    try:
        runner(["-e", script])
    except Exception:
        pass


def confirm(message: str, runner: Runner = _run_confirm) -> bool:
    """Ask for confirmation; True only if the Re-run button was chosen.

    Cancel is also the default button, so it makes osascript exit non-zero.
    """
    script = (
        "display dialog %s buttons {\"Cancel\", \"Re-run\"} "
        "default button \"Cancel\" cancel button \"Cancel\"" % _quote(message)
    )
    try:
        return runner(["-e", script]) == 0
    except Exception:
        return False
