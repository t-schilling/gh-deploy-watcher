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

    def test_missing_actions_falls_back(self):
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

    def test_apostrophe_in_path_gives_warning_menu(self):
        r = self.run_plugin(self.plugin_dir("it's here"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("\u26a0"), r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
