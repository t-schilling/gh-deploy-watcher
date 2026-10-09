from __future__ import annotations

import http.client
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gh_deploy_watcher import config as config_mod
from gh_deploy_watcher.config import Config, RepoConfig, Workflow, config_hash, load_config, save_config
from gh_deploy_watcher.github import GhError
from gh_deploy_watcher.ui_api import install_routes
from gh_deploy_watcher.ui_server import UiServer

HOSTILE = "acme/ev\x1b[2J<script>|\nil‮"
WORKFLOWS = (
    "PRD - Deploy to EU\t.github/workflows/deploy-prd.yml\tactive\n"
    "Dev Deploy\t.github/workflows/deploy-dev.yml\tactive\n"
    "CI\t.github/workflows/ci.yml\tactive\n"
    "Deploy old\t.github/workflows/deploy-old.yml\tdisabled_manually\n"
)


class FakeGh:
    def __init__(self):
        self.repos = "acme/api\n" + HOSTILE.replace("\n", "") + "\n"
        self.workflows = WORKFLOWS
        self.login = "octocat\n"
        self.error = None

    def __call__(self, args):
        if self.error:
            raise self.error
        if args[:2] == ["api", "user"]:
            return self.login
        if "user/repos" in args[2]:
            return self.repos
        if args[2].endswith("/actions/workflows"):
            return self.workflows
        raise AssertionError(args)


class ApiCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "config.json"
        patcher = mock.patch.dict(os.environ, {"GH_DEPLOY_WATCHER_HOME": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = FakeGh()
        self.server = UiServer(self.dir)
        install_routes(self.server, self.gh)
        self.server.start()
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        port = self.server.port
        headers = {"Host": "127.0.0.1:%d" % port, "X-Token": self.server.token}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if method != "GET":
            headers["Origin"] = "http://127.0.0.1:%d" % port
            headers["Content-Type"] = "application/json"
            data = data if data is not None else b""
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(payload)
        except ValueError:
            return resp.status, payload

    def put(self, repos, base_hash=None):
        if base_hash is None:
            base_hash = config_hash()
        return self.call("PUT", "/api/config", {"base_hash": base_hash, "repos": repos})

    def seed(self):
        save_config(Config([RepoConfig("acme/api", [Workflow("deploy-prd.yml", "prd", "Saved")])]))


def wf(file="deploy.yml", env="prd", label="L"):
    return {"file": file, "env": env, "label": label}


class SessionTests(ApiCase):
    def test_session(self):
        self.seed()
        status, body = self.call("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(body["login"], "octocat")
        self.assertEqual(body["config_hash"], config_hash())
        self.assertEqual(body["config"], {"repos": [{"repo": "acme/api", "workflows": [
            {"file": "deploy-prd.yml", "env": "prd", "label": "Saved"}]}]})

    def test_missing_config_is_empty(self):
        status, body = self.call("GET", "/api/session")
        self.assertEqual((status, body["config"]), (200, {"repos": []}))

    def test_corrupt_config_clean_error(self):
        self.path.write_text("{not json")
        status, body = self.call("GET", "/api/session")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["kind"], "config")
        self.assertIn("not valid JSON", body["error"]["message"])
        self.assertNotIn("Traceback", json.dumps(body))

    def test_wrong_shape_config(self):
        self.path.write_text('{"repos": 5}')
        self.assertEqual(self.call("GET", "/api/session")[0], 422)


class ReposTests(ApiCase):
    def test_hostile_name_sanitised_only_in_display(self):
        status, body = self.call("GET", "/api/repos")
        self.assertEqual(status, 200)
        self.assertEqual(body[0], {"name": "acme/api", "display": "acme/api"})
        hostile = body[1]
        self.assertEqual(hostile["name"], HOSTILE.replace("\n", ""))
        self.assertEqual(hostile["display"], "acme/ev[2J<script>il")
        for ch in ("\x1b", "|", "‮"):
            self.assertNotIn(ch, hostile["display"])


class WorkflowsTests(ApiCase):
    def get(self, repo="acme/api"):
        return self.call("GET", "/api/repos/%s/workflows" % repo)

    def test_suggestions_and_preselect(self):
        status, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual([w["file"] for w in body],
                         ["deploy-prd.yml", "deploy-dev.yml", "ci.yml", "deploy-old.yml"])
        first = body[0]
        self.assertEqual(first["name"], "PRD - Deploy to EU")
        self.assertEqual(first["suggested"], {"env": "prd", "label": "PRD · EU"})
        self.assertEqual([w["preselected"] for w in body], [True, True, False, False])
        self.assertEqual(body[3]["state"], "disabled_manually")
        self.assertTrue(all(w["tracked"] is None for w in body))

    def test_tracked_marked(self):
        self.seed()
        _, body = self.get()
        self.assertEqual(body[0]["tracked"], {"env": "prd", "label": "Saved"})
        self.assertIsNone(body[1]["tracked"])

    def test_hostile_workflow_name_cleaned(self):
        self.gh.workflows = "Dep\x1b[2Jloy | x‮\t.github/workflows/d.yml\tactive\n"
        _, body = self.get()
        self.assertEqual(body[0]["name"], "Dep[2Jloy x")

    def test_invalid_repo_names(self):
        for name in ("acme", "-x/api", "acme/api%0A"):
            self.assertEqual(self.get(name)[0], 400, name)
        # the server layer already rejects dot-dot paths
        self.assertIn(self.get("acme/..")[0], (400, 404))

    def test_invalid_name_not_reflected(self):
        _, body = self.get("-x%1b%5b2J%3cscript%3e/api")
        self.assertNotIn("script", json.dumps(body))
        self.assertNotIn("\\u001b", json.dumps(body))


class PutTests(ApiCase):
    def test_saves_like_wizard(self):
        status, body = self.put([{"repo": "acme/api", "workflows": [
            wf("deploy-prd.yml", "prd", "PRD · EU"), wf("deploy-dev.yml", "dev", "Dev")]}])
        self.assertEqual(status, 200)
        self.assertEqual(load_config(), Config([RepoConfig("acme/api", [
            Workflow("deploy-prd.yml", "prd", "PRD · EU"),
            Workflow("deploy-dev.yml", "dev", "Dev")])]))
        self.assertEqual(body, {"config_hash": config_hash()})

    def test_second_put_with_returned_hash(self):
        _, body = self.put([{"repo": "acme/api", "workflows": [wf()]}])
        status, _ = self.put([], base_hash=body["config_hash"])
        self.assertEqual(status, 200)
        self.assertEqual(load_config(), Config([]))

    def test_labels_cleaned_and_empty_falls_back_to_stem(self):
        self.put([{"repo": "acme/api", "workflows": [
            wf("a.yml", label="x\x1b[0m | y‮"), wf("b-c.yml", label=""),
            wf("d.yml", label=" | ")]}])
        labels = [w.label for w in load_config().repos[0].workflows]
        self.assertEqual(labels, ["x[0m y", "b-c", "d"])

    def test_validation_errors(self):
        cases = [
            [{"repo": "acme/api", "workflows": [wf(env="qa")]}],
            [{"repo": "acme/api", "workflows": [wf(), wf()]}],
            [{"repo": "acme", "workflows": [wf()]}],
            [{"repo": "acme/api", "workflows": [wf(file="")]}],
            [{"repo": "acme/api", "workflows": [wf(file="../x.yml")]}],
        ]
        for repos in cases:
            status, body = self.put(repos)
            self.assertEqual((status, body["error"]["kind"]), (422, "validation"), repos)
        self.assertFalse(self.path.exists())

    def test_wrong_types_give_422(self):
        bad_bodies = [
            {"base_hash": config_hash(), "repos": "x"},
            {"base_hash": config_hash(), "repos": ["x"]},
            {"base_hash": config_hash(), "repos": [{"repo": "acme/api", "workflows": "x"}]},
            {"base_hash": config_hash(), "repos": [{"repo": "acme/api", "workflows": ["x"]}]},
            {"base_hash": config_hash(), "repos": [{"repo": "acme/api", "workflows": [wf(label=5)]}]},
            {"base_hash": config_hash(), "repos": [{"repo": "acme/api", "workflows": [wf(env=["prd"])]}]},
            {"base_hash": config_hash(), "repos": [{"repo": "acme/api", "workflows": [wf(env=None)]}]},
            {"base_hash": config_hash(), "repos": [{"repo": 5, "workflows": [wf()]}]},
            {"base_hash": config_hash(), "repos": [{"repo": "acme/api", "workflows": [wf(file=5)]}]},
            {"base_hash": 5, "repos": []},
            {"repos": []},
            [],
            "x",
            None,
        ]
        for body in bad_bodies:
            status, payload = self.call("PUT", "/api/config", raw=json.dumps(body).encode())
            self.assertEqual(status, 422, body)
            self.assertEqual(payload["error"]["kind"], "validation")
        self.assertFalse(self.path.exists())

    def test_malformed_json_is_client_error(self):
        status, _ = self.call("PUT", "/api/config", raw=b"{nope")
        self.assertEqual(status, 400)

    def test_hostile_values_not_reflected(self):
        evil = "ev\x1b[2J<script>‮"
        _, body = self.put([{"repo": "acme/api", "workflows": [wf(env=evil)]}])
        text = json.dumps(body, ensure_ascii=False)
        for bad in ("\x1b", "<script>", "‮"):
            self.assertNotIn(bad, text)
        _, body = self.put([{"repo": evil, "workflows": [wf()]}])
        text = json.dumps(body, ensure_ascii=False)
        for bad in ("\x1b", "‮"):
            self.assertNotIn(bad, text)

    def test_stale_hash_conflict_leaves_file(self):
        self.seed()
        before = self.path.read_bytes()
        status, body = self.put([{"repo": "acme/api", "workflows": [wf()]}], base_hash="0" * 64)
        self.assertEqual((status, body["error"]["kind"]), (409, "conflict"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.dir / "config.json.tmp").exists())

    def test_save_oserror_is_io_500(self):
        with mock.patch("gh_deploy_watcher.ui_api.save_config",
                        side_effect=OSError(13, "denied", "/secret/path")):
            status, body = self.put([{"repo": "acme/api", "workflows": [wf()]}])
        self.assertEqual((status, body["error"]["kind"]), (500, "io"))
        self.assertNotIn("/secret/path", json.dumps(body))

    def test_readonly_dir_is_io_500(self):
        self.dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.dir.chmod, stat.S_IRWXU)
        if os.access(str(self.dir), os.W_OK):
            self.skipTest("cannot make directory read-only (root?)")
        status, body = self.put([{"repo": "acme/api", "workflows": [wf()]}])
        self.assertEqual((status, body["error"]["kind"]), (500, "io"))


class GhErrorTests(ApiCase):
    def test_kinds_map_to_502(self):
        for kind in ("auth", "not_found", "network"):
            self.gh.error = GhError(kind, "boom \x1b[2J<x>|‮")
            for path in ("/api/session", "/api/repos", "/api/repos/acme/api/workflows"):
                status, body = self.call("GET", path)
                self.assertEqual(status, 502, path)
                self.assertEqual(body["error"]["kind"], kind)
                self.assertEqual(body["error"]["message"], "boom [2J<x>")


class DoneTests(ApiCase):
    def test_done_replies_then_stops(self):
        status, body = self.call("POST", "/api/done", {})
        self.assertEqual(status, 200)
        done = []
        import threading
        t = threading.Thread(target=lambda: (self.server.wait(), done.append(1)), daemon=True)
        t.start()
        t.join(5)
        self.assertEqual(done, [1])


if __name__ == "__main__":
    unittest.main()
