from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from gh_deploy_watcher.ui_launcher import find_window, run_ui
from gh_deploy_watcher.ui_server import UiServer, try_lock_instance

URL_TOKEN = "tok-secret"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeStdin:
    def __init__(self) -> None:
        self.data = ""
        self.closed = False

    def write(self, data):
        if not isinstance(data, bytes):  # a real Popen stdin pipe is binary
            raise TypeError("a bytes-like object is required, not %r" % type(data).__name__)
        self.data += data.decode("utf-8")

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    """Exits with `code` once `exit_after` seconds have passed (None: never)."""

    def __init__(self, clock, exit_after=None, code=0):
        self.clock = clock
        self.start = clock()
        self.exit_after = exit_after
        self.code = code
        self.stdin = FakeStdin()
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.terminated:
            return -15
        if self.exit_after is not None and self.clock() - self.start >= self.exit_after:
            return self.code
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.poll()


class Spawner:
    def __init__(self, clock, **proc_kwargs):
        self.clock = clock
        self.proc_kwargs = proc_kwargs
        self.calls = []
        self.proc = None
        self.error = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error:
            raise self.error
        self.proc = FakeProc(self.clock, **self.proc_kwargs)
        return self.proc


class Browser:
    def __init__(self, result=True, error=None):
        self.urls = []
        self.result = result
        self.error = error

    def __call__(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.result


class Notes:
    def __init__(self):
        self.items = []

    def __call__(self, title, message):
        self.items.append((title, message))


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.server = UiServer(Path("."), idle_seconds=900.0, clock=self.clock, token=URL_TOKEN)
        self.browser = Browser()
        self.notes = Notes()
        self.window = Path("/tmp/GhDeployWatcher")

    def run_ui(self, spawner, window="default", **kw):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = run_ui(self.server, self.window if window == "default" else window,
                        spawn=spawner, open_browser=self.browser, notify_fn=self.notes,
                        monotonic=self.clock, sleep=self.clock.sleep, **kw)
        self.err = err.getvalue()
        return rc


class RunUiTests(LauncherCase):
    def test_url_goes_to_stdin_not_argv_or_env(self):
        sp = Spawner(self.clock, exit_after=20, code=0)
        self.assertEqual(self.run_ui(sp), 0)
        argv, kwargs = sp.calls[0]
        self.assertEqual(argv, [str(self.window)])
        self.assertEqual(sp.proc.stdin.data.strip(), self.server.url)
        self.assertTrue(sp.proc.stdin.closed)
        self.assertNotIn(URL_TOKEN, " ".join(argv))
        self.assertNotIn(URL_TOKEN, " ".join(map(str, kwargs.get("env", {}).values())))
        self.assertEqual(self.browser.urls, [])
        self.assertTrue(self.server._stopped.is_set())
        self.assertNotIn(URL_TOKEN, self.err)

    def test_spawn_does_not_leak_descriptors(self):
        sp = Spawner(self.clock, exit_after=1, code=0)
        self.run_ui(sp)
        # Python 3 default is close_fds=True; the lock fd must not reach the window.
        self.assertTrue(sp.calls[0][1].get("close_fds", True))

    def test_spawn_oserror_falls_back_to_browser_and_waits_for_done(self):
        sp = Spawner(self.clock)
        sp.error = OSError("no exec")
        calls = {"n": 0}
        real_check = self.server.check_idle

        def done_later():
            calls["n"] += 1
            if calls["n"] == 5:
                self.server.shutdown()
            return real_check()
        self.server.check_idle = done_later
        self.assertEqual(self.run_ui(sp), 0)
        self.assertEqual(self.browser.urls, [self.server.url])
        self.assertGreaterEqual(calls["n"], 5)

    def test_no_window_goes_straight_to_browser(self):
        self.server.shutdown()
        self.assertEqual(self.run_ui(Spawner(self.clock), window=None), 0)
        self.assertEqual(self.browser.urls, [self.server.url])

    def test_quick_nonzero_exit_falls_back_to_browser(self):
        sp = Spawner(self.clock, exit_after=1, code=1)
        self.server.idle_seconds = 5.0  # browser session ends via idle timeout
        self.assertEqual(self.run_ui(sp), 0)
        self.assertEqual(self.browser.urls, [self.server.url])
        self.assertTrue(self.server._stopped.is_set())

    def test_late_nonzero_exit_is_user_closing_no_browser(self):
        sp = Spawner(self.clock, exit_after=10, code=1)
        self.assertEqual(self.run_ui(sp), 0)
        self.assertEqual(self.browser.urls, [])
        self.assertTrue(self.server._stopped.is_set())

    def test_browser_failure_prints_url_and_notifies(self):
        self.browser.result = False
        self.server.idle_seconds = 5.0
        sp = Spawner(self.clock)
        sp.error = OSError("x")
        self.assertEqual(self.run_ui(sp), 0)
        self.assertIn(self.server.url, self.err)
        self.assertEqual(len(self.notes.items), 1)
        for part in self.notes.items[0]:
            self.assertNotIn(URL_TOKEN, part)

    def test_browser_exception_prints_url_and_notifies(self):
        self.browser.error = OSError("boom")
        self.server.idle_seconds = 5.0
        sp = Spawner(self.clock)
        sp.error = OSError("x")
        self.assertEqual(self.run_ui(sp), 0)
        self.assertIn(self.server.url, self.err)
        self.assertEqual(len(self.notes.items), 1)

    def test_server_shutdown_first_terminates_window(self):
        sp = Spawner(self.clock)  # never exits
        self.server.idle_seconds = 30.0
        self.assertEqual(self.run_ui(sp), 0)
        self.assertTrue(sp.proc.terminated)
        self.assertEqual(self.browser.urls, [])

    def test_stdin_write_failure_falls_back(self):
        sp = Spawner(self.clock)

        class Broken(FakeStdin):
            def write(self, data):
                raise BrokenPipeError()
        orig = sp.__call__

        def spawn(argv, **kw):
            proc = orig(argv, **kw)
            proc.stdin = Broken()
            return proc
        self.server.idle_seconds = 5.0
        self.assertEqual(self.run_ui(spawn), 0)
        self.assertEqual(self.browser.urls, [self.server.url])
        self.assertTrue(sp.proc.terminated)


    def test_non_oserror_on_write_falls_back_without_traceback(self):
        sp = Spawner(self.clock)

        class Weird(FakeStdin):
            def write(self, data):
                raise RuntimeError("weird")
        orig = sp.__call__

        def spawn(argv, **kw):
            proc = orig(argv, **kw)
            proc.stdin = Weird()
            return proc
        self.server.idle_seconds = 5.0
        self.assertEqual(self.run_ui(spawn), 0)
        self.assertEqual(self.browser.urls, [self.server.url])
        self.assertTrue(sp.proc.terminated)

    def test_kill_is_followed_by_wait(self):
        import subprocess as sp_mod
        events = []

        class Stubborn(FakeProc):
            def terminate(self):
                events.append("terminate")

            def kill(self):
                events.append("kill")

            def wait(self, timeout=None):
                events.append("wait")
                if "kill" not in events:
                    raise sp_mod.TimeoutExpired("w", timeout)
                return -9

            def poll(self):
                return None
        self.server.idle_seconds = 5.0
        self.run_ui(lambda argv, **kw: Stubborn(self.clock))
        self.assertEqual(events[-1], "wait")
        self.assertIn("kill", events)


class RealPopenTests(unittest.TestCase):
    def test_real_child_receives_url_on_stdin_only(self):
        import sys
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "line.txt"
            script = Path(d) / "window"
            script.write_text("#!%s\nimport sys\nopen(%r, 'w').write(sys.stdin.readline())\n"
                              "open(%r, 'a').write('|' + repr(sys.argv[1:]))\n"
                              % (sys.executable, str(out), str(out)))
            script.chmod(0o755)
            server = UiServer(Path(d), token=URL_TOKEN)
            browser = Browser()
            err = io.StringIO()
            with redirect_stderr(err):
                rc = run_ui(server, script, open_browser=browser, notify_fn=Notes())
            self.assertEqual(rc, 0)
            line, argv = out.read_text().split("|")
            self.assertEqual(line, server.url + "\n")
            self.assertEqual(argv, "[]")
            self.assertEqual(browser.urls, [])


class FindWindowTests(unittest.TestCase):
    def test_find_window(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(find_window(root))
            macos = root / "build" / "GhDeployWatcher.app" / "Contents" / "MacOS"
            macos.mkdir(parents=True)
            exe = macos / "GhDeployWatcher"
            exe.write_text("#!/bin/sh\n")
            self.assertIsNone(find_window(root))  # not executable
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(find_window(root), exe)


class LockInheritTests(unittest.TestCase):
    def test_lock_fd_not_inheritable(self):
        with tempfile.TemporaryDirectory() as d:
            handle = try_lock_instance(Path(d))
            self.assertIsNotNone(handle)
            self.assertFalse(os.get_inheritable(handle.fileno()))
            handle.close()


if __name__ == "__main__":
    unittest.main()
