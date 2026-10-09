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
    config_hash,
    load_config,
    parse_config,
    save_config,
    valid_repo_name,
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

    def test_utf8_encoding_used_for_read_and_write(self):
        cfg = Config([RepoConfig("acme/api", [Workflow("d.yaml", "prd", "PRD \u00b7 EU")])])
        seen = []
        real_w, real_r = Path.write_text, Path.read_text
        def w(self_, data, *a, **k):
            seen.append(k.get("encoding"))
            return real_w(self_, data, *a, **k)
        def r(self_, *a, **k):
            seen.append(k.get("encoding"))
            return real_r(self_, *a, **k)
        with mock.patch.object(Path, "write_text", w), mock.patch.object(Path, "read_text", r):
            save_config(cfg, self.path)
            load_config(self.path)
        self.assertEqual(seen, ["utf-8", "utf-8"])

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


class ParseConfigTests(unittest.TestCase):
    def test_parse_config_valid_and_alias(self):
        from gh_deploy_watcher import config as cfg
        c = parse_config(_cfg())
        self.assertEqual(c.repos[0].workflows[0], Workflow("deploy.yaml", "prd", "PRD"))
        self.assertIs(cfg._parse, parse_config)

    def test_parse_config_rejects_bad_shapes(self):
        for bad in ([], {"repos": "x"}, _cfg(env="qa"), _cfg(repo="acme")):
            with self.assertRaises(ConfigError):
                parse_config(bad)


class ConfigHashTests(unittest.TestCase):
    def test_hash_of_file_and_missing(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            self.assertEqual(config_hash(p), hashlib.sha256(b"").hexdigest())
            p.write_bytes(b'{"repos": []}')
            self.assertEqual(config_hash(p), hashlib.sha256(b'{"repos": []}').hexdigest())

    def test_default_path_uses_home(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {"GH_DEPLOY_WATCHER_HOME": d}):
            (Path(d) / "config.json").write_bytes(b"x")
            self.assertEqual(config_hash(), config_hash(Path(d) / "config.json"))


class ValidRepoNameTests(unittest.TestCase):
    def test_valid(self):
        for name in ("acme/api", "a.b/c_d-e", "acme/.github"):
            self.assertTrue(valid_repo_name(name), name)

    def test_invalid(self):
        for name in ("acme", "", "/api", "acme/", "-x/api", "acme/-y", "./api",
                     "acme/..", "../api", "acme/api\n", "acme/a b", "a/b/c", "acme/\u202eapi"):
            self.assertFalse(valid_repo_name(name), repr(name))
