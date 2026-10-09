from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def plugin_dir(self, name="plugin"):
        d = Path(self._tmp.name) / name
        d.mkdir()
        shutil.copy(str(ROOT / "gh-deploy-watcher.1m.py"), str(d))
        os.symlink(str(ROOT / "gh_deploy_watcher"), str(d / "gh_deploy_watcher"))
        return d

    def run_plugin(self, d):
        env = dict(os.environ, GH_DEPLOY_WATCHER_HOME=str(self.home))
        return subprocess.run([sys.executable, str(d / "gh-deploy-watcher.1m.py")],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)

    def test_bad_config_gives_warning_menu(self):
        (self.home / "config.json").write_text("{not json")
        r = self.run_plugin(self.plugin_dir())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("\u26a0"), r.stdout)
        self.assertIn("not valid JSON", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_paused_with_no_config_renders_menu(self):
        r = self.run_plugin(self.plugin_dir())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("\u23f8"), r.stdout)

    def test_real_import_error_in_actions_propagates(self):
        d = self.plugin_dir()
        pkg = d / "pkg_real"
        # replace symlink by a real package dir with a broken actions module
        os.unlink(str(d / "gh_deploy_watcher"))
        shutil.copytree(str(ROOT / "gh_deploy_watcher"), str(d / "gh_deploy_watcher"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        (d / "gh_deploy_watcher" / "actions.py").write_text(
            "import definitely_not_a_module_xyz\n")
        r = self.run_plugin(d)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("definitely_not_a_module_xyz", r.stderr)

    def test_offline_keeps_red_and_cached_run(self):
        import json
        (self.home / "config.json").write_text(json.dumps({"repos": [{"repo": "acme/api", "workflows": [
            {"file": "deploy.yaml", "env": "prd", "label": "PRD"}]}]}))
        (self.home / "state.json").write_text(json.dumps({
            "polling": True, "filter": "both", "notified": ["5:1"],
            "last": {"acme/api/deploy.yaml": {
                "id": 5, "status": "completed", "conclusion": "failure",
                "created_at": "2026-10-09T11:00:00Z", "title": "Merge pull request #7 from acme/x",
                "url": "https://example.com/r/5", "branch": "main", "attempt": 1}}}))
        stubs = Path(self._tmp.name) / "stubs"
        stubs.mkdir()
        gh = stubs / "gh"
        gh.write_text("#!/bin/sh\necho 'error connecting to api.github.com' >&2\n"
                      "echo 'check your internet connection or https://githubstatus.com' >&2\n"
                      "exit 1\n")
        gh.chmod(0o755)
        d = self.plugin_dir()
        env = dict(os.environ, GH_DEPLOY_WATCHER_HOME=str(self.home),
                   PATH=str(stubs) + os.pathsep + "/usr/bin:/bin")
        r = subprocess.run([sys.executable, str(d / "gh-deploy-watcher.1m.py")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("\u26a0"), r.stdout)  # offline: global warning
        self.assertIn("failed", r.stdout)  # cached red run still listed
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("5", json.loads((self.home / "state.json").read_text())["last"]
                      ["acme/api/deploy.yaml"]["id"].__str__())

    def test_apostrophe_in_path_gives_warning_menu(self):
        r = self.run_plugin(self.plugin_dir("it's here"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("\u26a0"), r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
