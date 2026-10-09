import unittest
from datetime import datetime, timedelta, timezone

from gh_deploy_watcher.model import (
    Run,
    age_text,
    classify,
    env_visible,
    new_failures,
    overall_state,
    run_ref,
    workflow_key,
)


def mk(id=1, status="completed", conclusion="success", created_at="2026-01-01T00:00:00Z"):
    return Run(id, status, conclusion, created_at, "t", "https://example.com/r/1", "main")


class ClassifyTests(unittest.TestCase):
    def test_none(self):
        self.assertEqual(classify(None), "none")

    def test_running(self):
        self.assertEqual(classify(mk(status="in_progress", conclusion=None)), "running")
        self.assertEqual(classify(mk(status="queued", conclusion=None)), "running")

    def test_success(self):
        self.assertEqual(classify(mk()), "success")

    def test_failed(self):
        for c in ("failure", "timed_out", "startup_failure"):
            self.assertEqual(classify(mk(conclusion=c)), "failed")

    def test_cancelled_skipped(self):
        self.assertEqual(classify(mk(conclusion="cancelled")), "cancelled")
        self.assertEqual(classify(mk(conclusion="skipped")), "skipped")

    def test_unknown(self):
        self.assertEqual(classify(mk(conclusion="weird")), "unknown")
        self.assertEqual(classify(mk(conclusion=None)), "unknown")


class RunTests(unittest.TestCase):
    def test_roundtrip(self):
        r = mk(id=5)
        self.assertEqual(Run.from_dict(r.to_dict()), r)


class EnvVisibleTests(unittest.TestCase):
    def test_visibility(self):
        self.assertTrue(env_visible("dev", "both"))
        self.assertTrue(env_visible("prd", "both"))
        self.assertTrue(env_visible("prd", "prd"))
        self.assertFalse(env_visible("dev", "prd"))
        self.assertFalse(env_visible("prd", "dev"))
        self.assertTrue(env_visible("dev", "dev"))


class OverallStateTests(unittest.TestCase):
    def test_empty_ok(self):
        self.assertEqual(overall_state([], True, None), "ok")

    def test_paused_beats_all(self):
        self.assertEqual(overall_state([("prd", "failed")], False, "boom"), "paused")

    def test_error_beats_failed(self):
        self.assertEqual(overall_state([("prd", "failed")], True, "boom"), "error")

    def test_workflow_error_never_green_or_yellow(self):
        self.assertEqual(overall_state([("prd", "error"), ("dev", "success")], True, None), "error")
        self.assertEqual(overall_state([("prd", "error"), ("dev", "running")], True, None), "error")

    def test_known_failure_beats_workflow_error(self):
        self.assertEqual(overall_state([("prd", "error"), ("dev", "failed")], True, None), "dev_failed")
        self.assertEqual(overall_state([("prd", "failed"), ("dev", "error")], True, None), "prd_failed")

    def test_global_error_and_paused_beat_workflow_error(self):
        self.assertEqual(overall_state([("prd", "error")], False, None), "paused")

    def test_prd_beats_dev(self):
        items = [("dev", "failed"), ("prd", "failed")]
        self.assertEqual(overall_state(items, True, None), "prd_failed")

    def test_dev_failed_beats_running(self):
        items = [("dev", "failed"), ("prd", "running")]
        self.assertEqual(overall_state(items, True, None), "dev_failed")

    def test_running(self):
        self.assertEqual(overall_state([("prd", "running")], True, None), "running")

    def test_cancelled_skipped_not_red(self):
        items = [("prd", "cancelled"), ("dev", "skipped"), ("dev", "none"), ("prd", "unknown")]
        self.assertEqual(overall_state(items, True, None), "ok")


class NewFailuresTests(unittest.TestCase):
    def setUp(self):
        self.cur = {
            "a/b/x.yml": mk(id=1, conclusion="failure"),
            "a/b/y.yml": mk(id=2),
            "a/b/z.yml": mk(id=3, conclusion="timed_out"),
        }

    def test_dedup(self):
        self.assertEqual(new_failures(self.cur, {"1:1"}, False), ["a/b/z.yml"])

    def test_all(self):
        self.assertEqual(sorted(new_failures(self.cur, set(), False)), ["a/b/x.yml", "a/b/z.yml"])

    def test_attempt_is_part_of_the_key(self):
        cur = {"a/b/x.yml": Run(1, "completed", "failure", "c", "t", "u", "main", 2)}
        self.assertEqual(new_failures(cur, {"1:1"}, False), ["a/b/x.yml"])
        self.assertEqual(new_failures(cur, {"1:2"}, False), [])

    def test_run_attempt_roundtrip_and_legacy(self):
        r = Run(1, "completed", "failure", "c", "t", "u", "main", 3)
        self.assertEqual(Run.from_dict(r.to_dict()).attempt, 3)
        d = r.to_dict()
        del d["attempt"]
        self.assertEqual(Run.from_dict(d).attempt, 1)
        d["attempt"] = "x"
        self.assertEqual(Run.from_dict(d).attempt, 1)

    def test_baseline(self):
        self.assertEqual(new_failures(self.cur, set(), True), [])


class AgeTextTests(unittest.TestCase):
    now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)

    def _age(self, delta):
        ts = (self.now - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
        return age_text(ts, self.now)

    def test_units(self):
        self.assertEqual(self._age(timedelta(seconds=45)), "45s")
        self.assertEqual(self._age(timedelta(minutes=12)), "12m")
        self.assertEqual(self._age(timedelta(hours=3)), "3h")
        self.assertEqual(self._age(timedelta(days=2)), "2d")

    def test_future_clamped(self):
        self.assertEqual(self._age(timedelta(seconds=-30)), "0s")


class RunRefTests(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(run_ref("Merge pull request #2183 from acme/branch"), "#2183")

    def test_pr(self):
        self.assertEqual(run_ref("PR #2183"), "#2183")

    def test_fallback(self):
        self.assertEqual(run_ref("short title"), "short title")
        self.assertEqual(run_ref("x" * 50), "x" * 30)


class WorkflowKeyTests(unittest.TestCase):
    def test_key(self):
        self.assertEqual(workflow_key("acme/api", "deploy.yml"), "acme/api/deploy.yml")


if __name__ == "__main__":
    unittest.main()
