from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gh_deploy_watcher.config import (
    Config,
    ConfigError,
    RepoConfig,
    Workflow,
    config_dir,
    load_config,
    save_config,
)


def _write(path: Path, data) -> None:
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def _cfg(env="prd", repo="acme/api", extra=None):
    wfs = [{"file": "deploy.yaml", "env": env, "label": "PRD"}]
    wfs += extra or []
    return {"repos": [{"repo": repo, "workflows": wfs}]}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.json"

    def test_missing_file_gives_empty_config(self):
        self.assertEqual(load_config(self.path), Config([]))

    def test_roundtrip_save_load(self):
        cfg = Config([RepoConfig("acme/api", [
            Workflow("deploy-prd-eu.yaml", "prd", "PRD · EU"),
            Workflow("deploy-dev-eu.yaml", "dev", "DEV · EU"),
        ])])
        save_config(cfg, self.path)
        self.assertEqual(load_config(self.path), cfg)

    def test_invalid_env_raises(self):
        _write(self.path, _cfg(env="stg"))
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_bad_repo_name_raises(self):
        _write(self.path, _cfg(repo="acme"))
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_duplicate_workflow_raises(self):
        _write(self.path, _cfg(extra=[{"file": "deploy.yaml", "env": "dev", "label": "x"}]))
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_corrupt_json_raises_config_error(self):
        _write(self.path, "{not json")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_config_dir_respects_env_var(self):
        with mock.patch.dict(os.environ, {"GH_DEPLOY_WATCHER_HOME": self._tmp.name}):
            self.assertEqual(config_dir(), Path(self._tmp.name))


if __name__ == "__main__":
    unittest.main()
