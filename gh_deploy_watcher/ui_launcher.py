"""Start the native selection window, with a browser fallback."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from gh_deploy_watcher.notify import notify
from gh_deploy_watcher.ui_server import UiServer

_QUICK_EXIT_SECONDS = 3.0
_POLL_SECONDS = 0.25
_OPEN = "/usr/bin/open"
_WINDOW_NAME = "GhDeployWatcher"


def find_window(repo_root: Path) -> Optional[Path]:
    exe = Path(repo_root) / "build" / (_WINDOW_NAME + ".app") / "Contents" / "MacOS" / _WINDOW_NAME
    return exe if exe.is_file() and os.access(str(exe), os.X_OK) else None


def _open_in_browser(url: str) -> bool:
    return subprocess.run([_OPEN, url], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=10).returncode == 0


def _stop(proc: Any) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    except OSError:
        pass


def run_ui(server: UiServer, window: Optional[Path], spawn: Callable[..., Any] = subprocess.Popen,
           open_browser: Callable[[str], bool] = _open_in_browser,
           notify_fn: Callable[[str, str], None] = notify,
           monotonic: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> int:
    proc: Any = None
    started = monotonic()
    try:
        if window is not None:
            try:
                # close_fds=True is the Python 3 default; stated so the instance
                # lock fd can never leak into a window that outlives the server.
                proc = spawn([str(window)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
                proc.stdin.write(server.url + "\n")  # stdin only: never argv or env
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, ValueError):
                if proc is not None:
                    _stop(proc)
                proc = None
        if proc is None:
            _browser_fallback(server, open_browser, notify_fn)
        while not server.stopped:
            if proc is not None:
                code = proc.poll()
                if code is not None:
                    proc = None
                    if code != 0 and monotonic() - started < _QUICK_EXIT_SECONDS:
                        _browser_fallback(server, open_browser, notify_fn)
                    else:
                        server.shutdown()  # window closed (or crashed late): user is done
                        break
            if server.check_idle():
                break
            sleep(_POLL_SECONDS)
    finally:
        server.shutdown()
        if proc is not None and proc.poll() is None:
            _stop(proc)
    return 0


def _browser_fallback(server: UiServer, open_browser: Callable[[str], bool],
                      notify_fn: Callable[[str, str], None]) -> None:
    try:
        opened = bool(open_browser(server.url))
    except Exception:
        opened = False
    if opened:
        return
    # Last resort, the user's explicit choice: the URL (with its token) on stderr.
    sys.stderr.write("Open this URL in a browser: %s\n" % server.url)
    try:
        notify_fn("gh-deploy-watcher", "Could not open the selection window; the URL was printed to the terminal")
    except Exception:
        pass
