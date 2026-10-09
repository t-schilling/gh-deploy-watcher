import subprocess
import unittest
from unittest import mock

from gh_deploy_watcher import notify


class FakeRunner:
    def __init__(self, code=0, exc=None):
        self.code = code
        self.exc = exc
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if self.exc:
            raise self.exc
        return self.code


class NotifyTest(unittest.TestCase):
    def test_notify_builds_osascript_command(self):
        r = FakeRunner()
        self.assertIsNone(notify.notify("acme/api", "Deploy done", runner=r))
        self.assertEqual(
            r.calls,
            [["-e", 'display notification "Deploy done" with title "acme/api"']],
        )

    def test_notify_escapes_quotes(self):
        r = FakeRunner()
        notify.notify("line1\nline2", 'He said "hi" \\ done', runner=r)
        self.assertEqual(
            r.calls,
            [[
                "-e",
                'display notification "He said \\"hi\\" \\\\ done" '
                'with title "line1 line2"',
            ]],
        )

    def test_notify_swallows_failures(self):
        self.assertIsNone(notify.notify("t", "m", runner=FakeRunner(code=1)))
        self.assertIsNone(notify.notify("t", "m", runner=FakeRunner(exc=OSError("x"))))

    def test_confirm_builds_dialog_command(self):
        r = FakeRunner()
        notify.confirm('Re-run "x"?', runner=r)
        self.assertEqual(
            r.calls,
            [[
                "-e",
                'display dialog "Re-run \\"x\\"?" buttons {"Cancel", "Re-run"} '
                'default button "Cancel" cancel button "Cancel"',
            ]],
        )

    def test_confirm_true_on_ok(self):
        self.assertTrue(notify.confirm("Go?", runner=FakeRunner(code=0)))

    def test_confirm_false_on_cancel(self):
        self.assertFalse(notify.confirm("Go?", runner=FakeRunner(code=1)))

    def test_confirm_false_when_runner_raises(self):
        self.assertFalse(notify.confirm("Go?", runner=FakeRunner(exc=RuntimeError("x"))))


class DefaultRunnerTest(unittest.TestCase):
    def test_runs_osascript_without_shell(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(notify._run_osascript(["-e", "x"]), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["osascript", "-e", "x"])
        self.assertFalse(kwargs.get("shell", False))

    def test_missing_osascript_returns_nonzero(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertNotEqual(notify._run_osascript(["-e", "x"]), 0)

    def test_timeout_returns_nonzero(self):
        exc = subprocess.TimeoutExpired("osascript", 1)
        with mock.patch("subprocess.run", side_effect=exc):
            self.assertNotEqual(notify._run_osascript(["-e", "x"]), 0)


if __name__ == "__main__":
    unittest.main()
