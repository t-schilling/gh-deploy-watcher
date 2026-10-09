from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from gh_deploy_watcher import actions
from gh_deploy_watcher.config import Config, RepoConfig, Workflow, save_config
from gh_deploy_watcher.github import GhError
from gh_deploy_watcher.state import State, load_state, save_state

NOW = 1760000000.0
PRD = "acme/api/deploy-prd.yaml"
DEV = "acme/api/deploy-dev.yaml"


def cfg():
    return Config([
        RepoConfig("acme/api", [Workflow("deploy-prd.yaml", "prd", "PRD"),
                                Workflow("deploy-dev.yaml", "dev", "DEV")]),
        RepoConfig("acme/web", [Workflow("deploy-prd.yaml", "prd", "WEB PRD"),
                                Workflow("deploy-dev.yaml", "dev", "WEB DEV")]),
    ])


def run_json(id, conclusion="success", status="completed"):
    return json.dumps([{
        "databaseId": id, "status": status, "conclusion": conclusion,
        "createdAt": "2026-10-09T11:48:00Z", "displayTitle": "Merge pull request #7 from acme/x",
        "url": "https://example.com/run/%d" % id, "headBranch": "main",
    }])


class FakeRunner:
    """results: {(repo, workflow): json text | Exception | callable}"""

    def __init__(self, results=None, default=None, ids_from=None, conclusion="failure"):
        self.ids_from = ids_from  # distinct run id per workflow, as on GitHub
        self.conclusion = conclusion
        self.calls = []
        self.results = results or {}
        self.default = default if default is not None else run_json(1)

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "run" and args[1] == "list":
            key = (args[3], args[5])
            r = self.results.get(key, self.default)
            if self.ids_from is not None and key not in self.results:
                order = [("acme/api", "deploy-prd.yaml"), ("acme/api", "deploy-dev.yaml"),
                         ("acme/web", "deploy-prd.yaml"), ("acme/web", "deploy-dev.yaml")]
                r = run_json(self.ids_from + order.index(key), self.conclusion)
            if callable(r):
                r = r()
            if isinstance(r, Exception):
                raise r
            return r
        if args[0] == "run" and args[1] == "rerun":
            r = self.results.get("rerun", "")
            if isinstance(r, Exception):
                raise r
            return r
        raise AssertionError("unexpected gh call %r" % (args,))

    def list_calls(self):
        return [(c[3], c[5]) for c in self.calls if c[:2] == ["run", "list"]]


class Notes:
    def __init__(self, boom=False):
        self.items = []
        self.boom = boom

    def __call__(self, title, message):
        self.items.append((title, message))
        if self.boom:
            raise RuntimeError("notify exploded")


class PollTests(unittest.TestCase):
    def test_filter_prd_polls_half(self):
        r = FakeRunner()
        actions.poll(cfg(), State(polling=True, filter="prd"), r, NOW)
        self.assertEqual(r.list_calls(), [("acme/api", "deploy-prd.yaml"),
                                          ("acme/web", "deploy-prd.yaml")])

    def test_both_polls_all_and_sets_last_poll(self):
        r = FakeRunner()
        st, keys, err = actions.poll(cfg(), State(), r, NOW)
        self.assertEqual(len(r.calls), 4)
        self.assertEqual(st.last_poll, NOW)
        self.assertEqual(st.last[PRD]["id"], 1)
        self.assertIsNone(err)

    def test_never_ran_stores_empty_shape(self):
        r = FakeRunner(default="[]")
        st, _, _ = actions.poll(cfg(), State(), r, NOW)
        self.assertEqual(st.last[PRD], {"run": None})

    def test_not_found_isolated(self):
        r = FakeRunner({("acme/api", "deploy-prd.yaml"): GhError("not_found", "HTTP 404"),
                        ("acme/web", "deploy-prd.yaml"): run_json(5, "failure")})
        st, keys, err = actions.poll(cfg(), State(), r, NOW)
        self.assertEqual(len(r.calls), 4)
        self.assertIn("error", st.last[PRD])
        self.assertEqual(st.last["acme/web/deploy-prd.yaml"]["id"], 5)
        self.assertEqual(keys, ["acme/web/deploy-prd.yaml"])
        self.assertIsNone(err)

    def test_global_error_keeps_previous_entry(self):
        s = State(last={PRD: {"id": 9, "status": "completed", "conclusion": "success",
                              "created_at": "x", "title": "t", "url": "u", "branch": "b"}})
        r = FakeRunner(default=GhError("network", "no route"))
        st, keys, err = actions.poll(cfg(), s, r, NOW)
        self.assertEqual(st.last[PRD]["id"], 9)
        self.assertEqual(err, "no route")
        self.assertEqual(keys, [])

    def test_global_error_none_when_some_succeeded(self):
        r = FakeRunner({("acme/api", "deploy-prd.yaml"): GhError("auth", "bad token")})
        _, _, err = actions.poll(cfg(), State(), r, NOW)
        self.assertIsNone(err)

    def test_nothing_attempted_no_error(self):
        _, _, err = actions.poll(Config([]), State(), FakeRunner(), NOW)
        self.assertIsNone(err)

    def test_other_error_stored(self):
        r = FakeRunner(default=GhError("other", "weird"))
        st, _, err = actions.poll(cfg(), State(), r, NOW)
        self.assertEqual(st.last[PRD], {"error": "weird"})
        self.assertIsNone(err)

    def test_corrupt_cached_entry_does_not_crash(self):
        s = State(last={PRD: {"id": "nope"}})
        r = FakeRunner(default=GhError("network", "down"))
        st, keys, err = actions.poll(cfg(), s, r, NOW)  # must not raise
        self.assertEqual(st.last[PRD], {"id": "nope"})  # global error keeps cache
        self.assertEqual(err, "down")
        s2 = State(last={PRD: {"garbage": 1}})
        st2, keys2, _ = actions.poll(cfg(), s2, FakeRunner(default=run_json(2, "failure")), NOW)
        self.assertEqual(st2.last[PRD]["id"], 2)
        self.assertIn(PRD, keys2)

    def test_notify_once_then_not_again(self):
        r = FakeRunner(ids_from=3)
        st, keys, _ = actions.poll(cfg(), State(), r, NOW)
        self.assertEqual(len(keys), 4)
        st.notified = [3, 4, 5, 6]  # poll() no longer records ids; _commit does
        _, keys2, _ = actions.poll(cfg(), st, r, NOW)
        self.assertEqual(keys2, [])

    def test_hidden_env_does_not_notify(self):
        r = FakeRunner(default=run_json(3, "failure"))
        _, keys, _ = actions.poll(cfg(), State(filter="prd"), r, NOW)
        self.assertTrue(keys)
        self.assertIn(PRD, keys)
        self.assertNotIn(DEV, keys)
        self.assertNotIn("acme/web/deploy-dev.yaml", keys)

    def test_baseline_marks_notified_no_keys(self):
        r = FakeRunner(default=run_json(3, "failure"))
        st, keys, _ = actions.poll(cfg(), State(), r, NOW, baseline=True)
        self.assertEqual(keys, [])
        self.assertIn(3, st.notified)

    def test_notified_capped(self):
        s = State(notified=list(range(1000, 1300)))
        st, _, _ = actions.poll(cfg(), s, FakeRunner(default=run_json(5, "failure")), NOW,
                                baseline=True)
        self.assertEqual(len(st.notified), 200)
        self.assertEqual(st.notified[-1], 5)


class ActionBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = os.environ.get("GH_DEPLOY_WATCHER_HOME")
        os.environ["GH_DEPLOY_WATCHER_HOME"] = self._tmp.name
        self.addCleanup(self._restore)
        save_config(cfg())
        self.notes = Notes()
        self.out = io.StringIO()

    def _restore(self):
        if self._old is None:
            os.environ.pop("GH_DEPLOY_WATCHER_HOME", None)
        else:
            os.environ["GH_DEPLOY_WATCHER_HOME"] = self._old

    def main(self, argv, runner, confirm=lambda m: True, notify=None):
        return actions.main(argv, script_path="/tmp/plugin.py", runner=runner,
                            notify_fn=notify or self.notes, confirm_fn=confirm,
                            now=NOW, out=self.out)


class MainTests(ActionBase):
    def test_polling_off_zero_calls_and_renders(self):
        r = FakeRunner()
        self.assertEqual(self.main([], r), 0)
        self.assertEqual(r.calls, [])
        self.assertTrue(self.out.getvalue().startswith("⏸"))

    def test_polling_on_polls_notifies_once(self):
        save_state(State(polling=True))
        r = FakeRunner(ids_from=3)
        self.assertEqual(self.main([], r), 0)
        n = len(self.notes.items)
        self.assertEqual(n, 4)
        self.assertEqual(self.notes.items[0][0], "Deploy failed: PRD")
        self.assertEqual(self.notes.items[0][1], "acme/api · #7")
        self.main([], r)
        self.assertEqual(len(self.notes.items), n)
        self.assertIn(3, load_state().notified)

    def test_notify_raising_does_not_break(self):
        save_state(State(polling=True))
        r = FakeRunner(default=run_json(3, "failure"))
        self.assertEqual(self.main([], r, notify=Notes(boom=True)), 0)
        self.assertEqual(load_state().last[PRD]["id"], 3)

    def test_bad_config_gives_warning_menu(self):
        Path(self._tmp.name, "config.json").write_text("{not json")
        r = FakeRunner()
        self.assertEqual(self.main([], r), 0)
        text = self.out.getvalue()
        self.assertTrue(text.startswith("⚠"))
        self.assertIn("not valid JSON", text)
        self.assertEqual(r.calls, [])

    def test_bad_state_does_not_crash(self):
        Path(self._tmp.name, "state.json").write_text("\x00garbage")
        self.assertEqual(self.main([], FakeRunner()), 0)

    def test_global_error_shown(self):
        save_state(State(polling=True))
        r = FakeRunner(default=GhError("auth", "please log in"))
        self.main([], r)
        self.assertTrue(self.out.getvalue().startswith("⚠"))
        self.assertIn("please log in", self.out.getvalue())

    def test_start_baseline_then_new_failure_notifies(self):
        r = FakeRunner(ids_from=3)
        self.assertEqual(self.main(["start"], r), 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.notes.items, [])
        self.assertTrue(load_state().polling)
        r2 = FakeRunner(ids_from=10)
        self.main([], r2)
        self.assertEqual(len(self.notes.items), 4)

    def test_stop_no_calls(self):
        save_state(State(polling=True))
        r = FakeRunner()
        self.assertEqual(self.main(["stop"], r), 0)
        self.assertEqual(r.calls, [])
        self.assertFalse(load_state().polling)

    def test_filter_polls_immediately(self):
        r = FakeRunner()
        self.assertEqual(self.main(["filter", "dev"], r), 0)
        self.assertEqual(load_state().filter, "dev")
        self.assertEqual(r.list_calls(), [("acme/api", "deploy-dev.yaml"),
                                          ("acme/web", "deploy-dev.yaml")])

    def test_filter_invalid_exit_2(self):
        r = FakeRunner()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(self.main(["filter", "qa"], r), 2)
        self.assertEqual(r.calls, [])
        self.assertIn("qa", err.getvalue())
        self.assertEqual(load_state().filter, "both")

    def test_refresh_while_paused(self):
        r = FakeRunner()
        self.assertEqual(self.main(["refresh"], r), 0)
        self.assertEqual(len(r.calls), 4)
        self.assertFalse(load_state().polling)

    def test_setup_stub(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(self.main(["setup"], FakeRunner()), 1)

    def test_unknown_subcommand(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.main(["bogus"], FakeRunner()), 2)


class ConcurrencyTests(ActionBase):
    def test_lost_pause_and_filter(self):
        save_state(State(polling=True))
        def mid_poll():
            self.main(["stop"], FakeRunner())
            st = load_state()
            st.filter = "prd"
            save_state(st)
            return run_json(1)
        self.main([], FakeRunner(default=mid_poll))
        st = load_state()
        self.assertFalse(st.polling)
        self.assertEqual(st.filter, "prd")
        self.assertEqual(st.last[PRD]["id"], 1)

    def test_double_notify_once_total(self):
        save_state(State(polling=True))
        n1, n2 = Notes(), Notes()
        def other():
            # second poller runs entirely while the first is mid-poll
            self.main([], FakeRunner(ids_from=3), notify=n2)
            return run_json(3, "failure")
        self.main([], FakeRunner(default=other, ids_from=None), notify=n1)
        # n1's only call that is not overridden is "other", which re-enters for all 4
        self.assertEqual(len(n1.items) + len(n2.items), 4)
        self.assertEqual(len(n2.items), 4)  # committed first; the stale poller saw its ids

    def test_notified_not_lost_by_filtered_poller(self):
        save_state(State(polling=True, filter="prd"))
        def mid_poll():
            st = load_state()
            st.notified = [77]
            save_state(st)
            return run_json(1)
        self.main([], FakeRunner(default=mid_poll))
        self.assertIn(77, load_state().notified)

    def test_save_failure_menu_no_notify(self):
        save_state(State(polling=True))
        from unittest import mock
        with mock.patch.object(actions, "save_state", side_effect=PermissionError("denied")):
            rc = self.main([], FakeRunner(ids_from=3))
        self.assertEqual(rc, 0)
        self.assertTrue(self.out.getvalue().startswith("\u26a0"))
        self.assertIn("denied", self.out.getvalue())
        self.assertNotIn("Traceback", self.out.getvalue())
        self.assertEqual(self.notes.items, [])

    def test_save_happens_before_notify(self):
        save_state(State(polling=True))
        from unittest import mock
        order = []
        real = actions.save_state
        def spy(*a, **k):
            order.append("save")
            return real(*a, **k)
        with mock.patch.object(actions, "save_state", side_effect=spy):
            self.main([], FakeRunner(ids_from=3),
                      notify=lambda t, m: order.append("notify"))
        self.assertEqual(order[0], "save")
        self.assertLess(order.index("save"), order.index("notify"))
        self.assertEqual(order.count("notify"), 4)

    def test_lock_released_after_exception(self):
        with self.assertRaises(RuntimeError):
            with actions._locked():
                raise RuntimeError("boom")
        import fcntl
        with open(os.path.join(self._tmp.name, "state.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise if still held


class RerunTests(ActionBase):
    CMD = ["run", "rerun", "123", "--failed", "--repo", "acme/api"]

    def test_prd_declined_never_runs(self):
        r = FakeRunner()
        asked = []
        rc = self.main(["rerun", "acme/api", "123", "prd"], r,
                       confirm=lambda m: asked.append(m) or False)
        self.assertEqual(rc, 1)
        self.assertEqual(len(asked), 1)
        self.assertEqual(r.calls, [])

    def test_dev_does_not_ask(self):
        r = FakeRunner()
        def no(_):
            raise AssertionError("must not ask")
        self.assertEqual(self.main(["rerun", "acme/api", "123", "dev"], r, confirm=no), 0)
        self.assertIn(self.CMD, r.calls)

    def test_prd_confirmed_runs_then_polls(self):
        r = FakeRunner()
        self.assertEqual(self.main(["rerun", "acme/api", "123", "prd"], r), 0)
        self.assertEqual(r.calls[0], self.CMD)
        self.assertEqual(len(r.list_calls()), 4)

    def test_dry_run(self):
        r = FakeRunner()
        def no(_):
            raise AssertionError("must not ask")
        rc = self.main(["rerun", "acme/api", "123", "prd", "--dry-run"], r, confirm=no)
        self.assertEqual(rc, 0)
        self.assertEqual(r.calls, [])
        self.assertEqual(self.out.getvalue(), "gh run rerun 123 --failed --repo acme/api\n")

    def test_dotted_repo_name_accepted(self):
        r = FakeRunner()
        rc = self.main(["rerun", "acme/.github", "1", "dev", "--dry-run"], r)
        self.assertEqual(rc, 0)
        self.assertEqual(self.out.getvalue(), "gh run rerun 1 --failed --repo acme/.github\n")

    def test_bad_args_exit_2(self):
        for argv in (["rerun", "nope", "1", "prd"], ["rerun", "acme/api", "x", "prd"],
                     ["rerun", "acme/api", "1", "qa"], ["rerun", "acme/api"],
                     ["rerun", "acme/api\n", "1", "prd"], ["rerun", "acme/api", "-5", "dev"],
                     ["rerun", "acme/api", " 12 ", "dev"], ["rerun", "acme/api", "1_0", "dev"],
                     ["rerun", "acme/api", "0", "dev"], ["rerun", "-x/api", "1", "dev"],
                     ["rerun", "acme/..", "1", "dev"], ["rerun", "./api", "1", "dev"],
                     ["rerun", "../api", "1", "dev"], ["rerun", "acme/.", "1", "dev"]):
            r = FakeRunner()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(self.main(argv, r), 2, argv)
            self.assertEqual(r.calls, [])

    def test_failed_rerun_notifies_and_returns_1(self):
        r = FakeRunner({"rerun": GhError("other", "run cannot be rerun")})
        self.assertEqual(self.main(["rerun", "acme/api", "123", "dev"], r), 1)
        self.assertEqual(self.notes.items, [("Re-run failed", "run cannot be rerun")])
        self.assertEqual(r.list_calls(), [])


class SetupCommandTests(ActionBase):
    def test_setup_runs_wizard_and_returns_0(self):
        from unittest import mock
        with mock.patch("gh_deploy_watcher.setup_wizard.run_wizard") as wiz:
            rc = self.main(["setup"], FakeRunner())
        self.assertEqual(rc, 0)
        self.assertEqual(wiz.call_count, 1)
        self.assertEqual(wiz.call_args[0][0].repos[0].repo, cfg().repos[0].repo)

    def test_setup_missing_config_starts_empty(self):
        from unittest import mock
        os.remove(os.path.join(self._tmp.name, "config.json"))
        with mock.patch("gh_deploy_watcher.setup_wizard.run_wizard") as wiz:
            self.assertEqual(self.main(["setup"], FakeRunner()), 0)
        self.assertEqual(wiz.call_args[0][0].repos, [])

    def test_setup_corrupt_config_returns_1(self):
        from unittest import mock
        Path(self._tmp.name, "config.json").write_text("{nope")
        err = io.StringIO()
        with mock.patch("gh_deploy_watcher.setup_wizard.run_wizard") as wiz, redirect_stderr(err):
            rc = self.main(["setup"], FakeRunner())
        self.assertEqual(rc, 1)
        self.assertIn("not valid JSON", err.getvalue())
        wiz.assert_not_called()


if __name__ == "__main__":
    unittest.main()
