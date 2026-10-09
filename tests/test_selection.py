from __future__ import annotations

import unicodedata
import unittest

from gh_deploy_watcher.github import WorkflowInfo
from gh_deploy_watcher.selection import clean, preselect, suggest

HOSTILE = "PRD\x1b[2J\x1b]0;pwn\x07 | Deploy\r\nto\u202e EU"


def wf(name, path, state="active"):
    return WorkflowInfo(name, ".github/workflows/" + path, state)


def no_ctrl(text):
    return all(unicodedata.category(c)[0] != "C" for c in text)


class SuggestTests(unittest.TestCase):
    def test_mappings(self):
        cases = [
            ("🚀 PRD - Deploy to EU", ("prd", "PRD · EU")),
            ("DEV - Deploy to MX", ("dev", "DEV · MX")),
            ("Production - Deploy to US", ("prd", "Production · US")),
            ("staging - deploy to eu", ("dev", "staging · eu")),
            ("prod: Deploy", ("prd", "prod · Deploy")),
            ("🔥 Nightly build", ("dev", "Nightly build")),
        ]
        for name, want in cases:
            self.assertEqual(suggest(wf(name, "x.yaml")), want, name)


class PreselectTests(unittest.TestCase):
    def test_filters(self):
        a = wf("🚀 PRD - Deploy to EU", "deploy-prd-eu.yaml")
        old = wf("PRD - Deploy to EU", "deploy-prd-eu-old.yaml")
        codeql = wf("CodeQL", "codeql.yaml")
        dep = wf("Dependabot Updates", "deploy-dependabot.yaml")
        off = wf("Deploy DEV", "deploy-dev.yaml", "disabled_manually")
        path_only = wf("Release", "deploy-x.yaml")
        lint = wf("Lint", "lint.yaml")
        got = preselect([a, old, codeql, dep, off, path_only, lint])
        self.assertEqual(got, [a, path_only])


class SanitizeTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(clean("a\x1b[0m\tb | c\u202e  d\r\n"), "a[0mb c d")

    def test_suggest_label_clean(self):
        env, label = suggest(wf(HOSTILE, "deploy-x.yaml"))
        self.assertEqual(env, "prd")
        self.assertTrue(no_ctrl(label) and "|" not in label)

    def test_empty_label_falls_back_to_file_stem(self):
        self.assertEqual(suggest(wf("\U0001f680", "deploy-prd-eu.yaml")),
                         ("dev", "deploy-prd-eu"))
        self.assertEqual(suggest(wf("\x1b\x07", "my\x1bflow.yml"))[1], "myflow")


if __name__ == "__main__":
    unittest.main()
