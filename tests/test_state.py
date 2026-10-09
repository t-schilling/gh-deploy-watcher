from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gh_deploy_watcher.state import State, load_state, save_state


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.json"

    def test_missing_file_gives_defaults(self):
        self.assertEqual(load_state(self.path), State())

    def test_defaults_not_shared(self):
        a, b = State(), State()
        a.last["x"] = {}
        a.notified.append(1)
        self.assertEqual(b.last, {})
        self.assertEqual(b.notified, [])

    def test_corrupt_file_gives_defaults(self):
        for text in ["", "{not json", "[]", "null", "42"]:
            self.path.write_text(text)
            self.assertEqual(load_state(self.path), State(), text)

    def test_wrong_field_types(self):
        self.path.write_text(json.dumps({
            "polling": "yes",
            "filter": 5,
            "last": {"a": {"id": 1}, "b": "str", "c": [1]},
            "last_poll": "now",
            "notified": [1, "x", 2, None, 3.5, "7:2", "7:x", True, "9:1:1"],
        }))
        s = load_state(self.path)
        self.assertIs(s.polling, False)
        self.assertEqual(s.filter, "both")
        self.assertEqual(s.last, {"a": {"id": 1}})
        self.assertIsNone(s.last_poll)
        self.assertEqual(s.notified, ["1:1", "2:1", "7:2"])
        self.path.write_text(json.dumps({"last": [], "notified": {}}))
        s = load_state(self.path)
        self.assertEqual((s.last, s.notified), ({}, []))

    def test_roundtrip(self):
        s = State(True, "prd", {"acme/api/deploy.yaml": {"id": 7}, "x": {"error": "boom"}}, 12.5, [1, 2])
        save_state(s, self.path)
        self.assertEqual(load_state(self.path), s)

    def test_invalid_filter_resets_to_both(self):
        self.path.write_text(json.dumps({"filter": "staging"}))
        self.assertEqual(load_state(self.path).filter, "both")

    def test_save_is_atomic(self):
        save_state(State(polling=True), self.path)
        self.assertEqual(os.listdir(self._tmp.name), ["state.json"])
        with mock.patch("gh_deploy_watcher.state.json.dump", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                save_state(State(polling=False), self.path)
        self.assertTrue(load_state(self.path).polling)
        self.assertEqual(os.listdir(self._tmp.name), ["state.json"])

    def test_save_creates_parent_dir(self):
        p = Path(self._tmp.name) / "sub" / "state.json"
        save_state(State(), p)
        self.assertTrue(p.exists())

    def test_default_path_uses_env(self):
        with mock.patch.dict(os.environ, {"GH_DEPLOY_WATCHER_HOME": self._tmp.name}):
            save_state(State(filter="dev"))
            self.assertEqual(load_state().filter, "dev")
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
