import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from gh_deploy_watcher import github
from gh_deploy_watcher.github import GhError, WorkflowInfo

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


class FakeRunner:
    def __init__(self, out=""):
        self.out = out
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return self.out


class LatestRunTests(unittest.TestCase):
    def test_success(self):
        r = FakeRunner(fixture("run_list_success.json"))
        run = github.latest_run("acme/api", "deploy.yml", r)
        self.assertEqual(run.id, 1001)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.conclusion, "success")
        self.assertEqual(run.title, "Merge pull request #12 from acme/feature")
        self.assertEqual(run.branch, "main")
        self.assertEqual(run.created_at, "2026-01-15T10:00:00Z")

    def test_exact_args_no_branch_filter(self):
        r = FakeRunner(fixture("run_list_success.json"))
        github.latest_run("acme/api", "deploy.yml", r)
        self.assertEqual(r.calls, [[
            "run", "list", "--repo", "acme/api", "--workflow", "deploy.yml",
            "--limit", "1", "--json",
            "databaseId,status,conclusion,createdAt,displayTitle,url,headBranch,attempt",
        ]])
        self.assertNotIn("--branch", r.calls[0])

    def test_attempt_parsed_default_and_tolerant(self):
        import json as _j
        base = _j.loads(fixture("run_list_failure.json"))
        base[0]["attempt"] = 2
        self.assertEqual(github.latest_run("a/b", "d.yml", FakeRunner(_j.dumps(base))).attempt, 2)
        for bad in (None, "x", 0, -1, True):
            base[0]["attempt"] = bad
            self.assertEqual(github.latest_run("a/b", "d.yml", FakeRunner(_j.dumps(base))).attempt, 1)
        del base[0]["attempt"]
        self.assertEqual(github.latest_run("a/b", "d.yml", FakeRunner(_j.dumps(base))).attempt, 1)

    def test_failure(self):
        run = github.latest_run("acme/api", "d.yml", FakeRunner(fixture("run_list_failure.json")))
        self.assertEqual(run.conclusion, "failure")

    def test_in_progress_empty_conclusion_is_none(self):
        run = github.latest_run("acme/api", "d.yml", FakeRunner(fixture("run_list_in_progress.json")))
        self.assertEqual(run.status, "in_progress")
        self.assertIsNone(run.conclusion)

    def test_never_ran_returns_none(self):
        self.assertIsNone(github.latest_run("acme/api", "d.yml", FakeRunner(fixture("run_list_empty.json"))))

    def test_malformed_json_is_gh_error(self):
        with self.assertRaises(GhError) as cm:
            github.latest_run("acme/api", "d.yml", FakeRunner("not json"))
        self.assertEqual(cm.exception.kind, "other")

    def test_unexpected_shape_is_gh_error(self):
        with self.assertRaises(GhError) as cm:
            github.latest_run("acme/api", "d.yml", FakeRunner('[{"foo": 1}]'))
        self.assertEqual(cm.exception.kind, "other")


class RerunTests(unittest.TestCase):
    def test_args(self):
        self.assertEqual(
            github.rerun_failed_args("acme/api", 42),
            ["run", "rerun", "42", "--failed", "--repo", "acme/api"],
        )

    def test_rerun_uses_runner(self):
        r = FakeRunner()
        self.assertIsNone(github.rerun_failed("acme/api", 42, r))
        self.assertEqual(r.calls, [github.rerun_failed_args("acme/api", 42)])


class ListTests(unittest.TestCase):
    def test_list_repos(self):
        r = FakeRunner("acme/api\nacme/web\n\n")
        self.assertEqual(github.list_repos(r), ["acme/api", "acme/web"])
        self.assertEqual(r.calls, [[
            "api", "--paginate",
            "user/repos?per_page=100&affiliation=owner,collaborator,organization_member",
            "--jq", ".[].full_name",
        ]])

    def test_list_workflows_unfiltered(self):
        r = FakeRunner(
            "Deploy\t.github/workflows/deploy.yml\tactive\n"
            "Deploy old\t.github/workflows/deploy-old.yml\tdisabled_manually\n"
        )
        wfs = github.list_workflows("acme/api", r)
        self.assertEqual(wfs, [
            WorkflowInfo("Deploy", ".github/workflows/deploy.yml", "active"),
            WorkflowInfo("Deploy old", ".github/workflows/deploy-old.yml", "disabled_manually"),
        ])
        self.assertEqual(r.calls, [[
            "api", "--paginate", "repos/acme/api/actions/workflows",
            "--jq", ".workflows[]|[.name,.path,.state]|@tsv",
        ]])

    def test_auth_ok(self):
        r = FakeRunner()
        self.assertTrue(github.auth_ok(r))
        self.assertEqual(r.calls, [["auth", "status"]])

    def test_auth_not_ok(self):
        def bad(args):
            raise GhError("auth", "x")
        self.assertFalse(github.auth_ok(bad))


class ClassifyTests(unittest.TestCase):
    def test_mapping(self):
        c = github.classify_error
        self.assertEqual(c(1, "gh: Not Found (HTTP 404)"), "not_found")
        self.assertEqual(c(1, "HTTP 404"), "not_found")
        self.assertEqual(c(1, "To get started with GitHub CLI, please run: gh auth login"), "auth")
        self.assertEqual(c(1, "bad authentication"), "auth")
        self.assertEqual(c(1, "API Rate Limit exceeded"), "rate_limit")
        self.assertEqual(c(1, "could not resolve host: api.github.com"), "network")
        self.assertEqual(c(1, "i/o timeout"), "network")
        self.assertEqual(c(1, "network is unreachable"), "network")
        self.assertEqual(c(1, "boom"), "other")

    def test_offline_messages_are_network(self):
        c = github.classify_error
        for msg in ("error connecting to api.github.com",
                    "check your internet connection or https://githubstatus.com",
                    "dial tcp: lookup api.github.com: no such host",
                    "dial tcp 1.2.3.4:443: connect: Connection refused",
                    "net/http: TLS handshake timeout"):
            self.assertEqual(c(1, msg), "network", msg)


class RunGhTests(unittest.TestCase):
    def test_path_appended_after_inherited(self):
        cp = subprocess.CompletedProcess(["gh"], 0, stdout="ok", stderr="")
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            with mock.patch("subprocess.run", return_value=cp) as run:
                self.assertEqual(github.run_gh(["auth", "status"]), "ok")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["gh", "auth", "status"])
        parts = kwargs["env"]["PATH"].split(os.pathsep)
        self.assertEqual(parts, ["/usr/bin", "/bin", "/opt/homebrew/bin", "/usr/local/bin"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["encoding"], "utf-8")

    def test_missing_binary(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(GhError) as cm:
                github.run_gh(["x"])
        self.assertEqual(cm.exception.kind, "missing")

    def test_timeout_is_network(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
            with self.assertRaises(GhError) as cm:
                github.run_gh(["x"])
        self.assertEqual(cm.exception.kind, "network")

    def test_nonzero_exit_classified(self):
        cp = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="gh: Not Found (HTTP 404)")
        with mock.patch("subprocess.run", return_value=cp):
            with self.assertRaises(GhError) as cm:
                github.run_gh(["x"])
        self.assertEqual(cm.exception.kind, "not_found")
        self.assertIn("404", cm.exception.message)


if __name__ == "__main__":
    unittest.main()


class CurrentLoginTests(unittest.TestCase):
    def test_current_login(self):
        r = FakeRunner("octocat\n")
        self.assertEqual(github.current_login(r), "octocat")
        self.assertEqual(r.calls, [["api", "user", "--jq", ".login"]])
