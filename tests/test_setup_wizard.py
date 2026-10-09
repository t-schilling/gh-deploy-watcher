from __future__ import annotations

import os
import tempfile
import unicodedata
import unittest

from gh_deploy_watcher import setup_wizard as sw
from gh_deploy_watcher.config import Config, RepoConfig, Workflow, load_config
from gh_deploy_watcher.github import GhError, WorkflowInfo


def wf(name, path, state="active"):
    return WorkflowInfo(name, ".github/workflows/" + path, state)


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
            self.assertEqual(sw.suggest(wf(name, "x.yaml")), want, name)


class PreselectTests(unittest.TestCase):
    def test_filters(self):
        a = wf("🚀 PRD - Deploy to EU", "deploy-prd-eu.yaml")
        old = wf("PRD - Deploy to EU", "deploy-prd-eu-old.yaml")
        codeql = wf("CodeQL", "codeql.yaml")
        dep = wf("Dependabot Updates", "deploy-dependabot.yaml")
        off = wf("Deploy DEV", "deploy-dev.yaml", "disabled_manually")
        path_only = wf("Release", "deploy-x.yaml")
        lint = wf("Lint", "lint.yaml")
        got = sw.preselect([a, old, codeql, dep, off, path_only, lint])
        self.assertEqual(got, [a, path_only])


class FakeGh:
    def __init__(self, repos, workflows, error=None):
        self.repos, self.workflows, self.error = repos, workflows, error
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if self.error:
            raise self.error
        if args[:2] == ["api", "--paginate"] and args[2].startswith("user/repos"):
            return "\n".join(self.repos)
        repo = args[2].split("/actions")[0][len("repos/"):]
        return "\n".join("\t".join((w.name.replace("\r", "\\r").replace("\n", "\\n"), w.path, w.state)) for w in self.workflows[repo])


class Script:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


class Picks:
    """Scripted picker: each item is a callable(options, preselected) -> list."""
    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen = []

    def __call__(self, options, multi, preselected, *a, **k):
        self.seen.append((list(options), multi, list(preselected)))
        return self.steps.pop(0)(options, preselected)


def first(n):
    return lambda opts, pre: opts[:n]


def containing(*subs):
    return lambda opts, pre: [o for o in opts if any(s in o for s in subs)]


def defaults(opts, pre):
    return list(pre)


class WizardBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = os.environ.get("GH_DEPLOY_WATCHER_HOME")
        os.environ["GH_DEPLOY_WATCHER_HOME"] = self._tmp.name
        self.addCleanup(self._restore)
        self.lines = []

    def _restore(self):
        if self._old is None:
            os.environ.pop("GH_DEPLOY_WATCHER_HOME", None)
        else:
            os.environ["GH_DEPLOY_WATCHER_HOME"] = self._old

    def run_wiz(self, cfg, gh, picker, answers):
        ask = Script(answers)
        res = sw.run_wizard(cfg, gh, picker, ask, self.lines.append)
        return res, ask

    def saved(self):
        return load_config()


def existing():
    return Config([
        RepoConfig("acme/api", [Workflow("deploy-prd-eu.yaml", "prd", "PRD · EU")]),
        RepoConfig("acme/web", [Workflow("deploy-dev.yaml", "dev", "DEV")]),
    ])


WFS = {
    "acme/api": [wf("🚀 PRD - Deploy to EU", "deploy-prd-eu.yaml"),
                 wf("DEV - Deploy to MX", "deploy-dev-mx.yaml"),
                 wf("CodeQL", "codeql.yaml")],
    "acme/new": [wf("🚀 PRD - Deploy to EU", "deploy-prd-eu.yaml"),
                 wf("CodeQL", "codeql.yaml")],
    "acme/other": [wf("x", "deploy-x.yaml")],
}


class WizardAddTests(WizardBase):
    def test_workflows_fetched_only_for_selected_repos(self):
        gh = FakeGh(["acme/api", "acme/new", "acme/other"], WFS)
        picker = Picks(containing("acme/new"), defaults)
        # label Enter, env Enter, then done
        res, _ = self.run_wiz(Config(), gh, picker, ["1", "", "", "4"])
        fetched = [c for c in gh.calls if "actions/workflows" in c[2]]
        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0][2], "repos/acme/new/actions/workflows")
        self.assertEqual(res.repos[0].repo, "acme/new")
        self.assertEqual(res.repos[0].workflows,
                         [Workflow("deploy-prd-eu.yaml", "prd", "PRD · EU")])
        self.assertEqual(self.saved(), res)

    def test_merge_without_duplicates_preserves_untouched(self):
        gh = FakeGh(["acme/api"], WFS)
        # pick both deploy workflows (existing prd-eu is excluded from options)
        picker = Picks(containing("acme/api"), containing("deploy-dev-mx"))
        cfg = existing()
        res, _ = self.run_wiz(cfg, gh, picker, ["1", "MX label", "dev", "4"])
        api = [r for r in res.repos if r.repo == "acme/api"][0]
        files = [w.file for w in api.workflows]
        self.assertEqual(files.count("deploy-prd-eu.yaml"), 1)
        self.assertIn("deploy-dev-mx.yaml", files)
        self.assertEqual(api.workflows[0], Workflow("deploy-prd-eu.yaml", "prd", "PRD · EU"))
        self.assertEqual([r.repo for r in res.repos], ["acme/api", "acme/web"])
        self.assertEqual(res.repos[1], existing().repos[1])

    def test_invalid_env_is_reasked(self):
        gh = FakeGh(["acme/new"], WFS)
        picker = Picks(first(1), defaults)
        res, ask = self.run_wiz(Config(), gh, picker, ["1", "", "staging", "", "prd", "4"])
        self.assertEqual(res.repos[0].workflows[0].env, "prd")
        env_prompts = [p for p in ask.prompts if "nv" in p]
        self.assertEqual(len(env_prompts), 2)

    def test_gh_error_leaves_config_unchanged(self):
        gh = FakeGh([], {}, error=GhError("auth", "not logged in"))
        res, _ = self.run_wiz(existing(), gh, Picks(), ["1", "4"])
        self.assertEqual(res, existing())
        self.assertTrue(any("not logged in" in l for l in self.lines))
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "config.json")))

    def test_error_midway_discards_partial_add(self):
        class Flaky(FakeGh):
            def __call__(self, args):
                if "repos/acme/other" in args[2]:
                    raise GhError("network", "boom")
                return super().__call__(args)
        gh = Flaky(["acme/new", "acme/other"], WFS)
        picker = Picks(lambda o, p: list(o), defaults)
        res, _ = self.run_wiz(Config(), gh, picker, ["1", "", "", "4"])
        self.assertEqual(res.repos, [])
        self.assertTrue(any("boom" in l for l in self.lines))


class WizardOtherTests(WizardBase):
    def test_remove_workflow_and_drop_empty_repo(self):
        picker = Picks(containing("deploy-prd-eu.yaml"))
        res, _ = self.run_wiz(existing(), FakeGh([], {}), picker, ["2", "4"])
        self.assertEqual([r.repo for r in res.repos], ["acme/web"])
        self.assertEqual(self.saved(), res)

    def test_remove_keeps_repo_with_remaining_workflows(self):
        cfg = Config([RepoConfig("acme/api", [Workflow("a.yaml", "prd", "A"),
                                              Workflow("b.yaml", "dev", "B")])])
        picker = Picks(containing("a.yaml"))
        res, _ = self.run_wiz(cfg, FakeGh([], {}), picker, ["2", "4"])
        self.assertEqual(res.repos[0].workflows, [Workflow("b.yaml", "dev", "B")])

    def test_list_prints_and_does_not_save(self):
        res, _ = self.run_wiz(existing(), FakeGh([], {}), Picks(), ["3", "4"])
        text = "\n".join(self.lines)
        self.assertIn("acme/api", text)
        self.assertIn("PRD · EU", text)
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "config.json")))

    def test_keyboard_interrupt_aborts_without_saving(self):
        cfg = existing()
        gh = FakeGh(["acme/new"], WFS)
        picker = Picks(first(1), defaults)
        res, _ = self.run_wiz(cfg, gh, picker, ["1", "", KeyboardInterrupt()])
        self.assertEqual(res, existing())
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "config.json")))

    def test_eof_aborts(self):
        res, _ = self.run_wiz(existing(), FakeGh([], {}), Picks(), [EOFError()])
        self.assertEqual(res, existing())

    def test_invalid_menu_choice_reasked(self):
        res, ask = self.run_wiz(existing(), FakeGh([], {}), Picks(), ["9", "x", "4"])
        self.assertEqual(len(ask.prompts), 3)


class PickTests(unittest.TestCase):
    OPTS = ["acme/api", "acme/web", "beta/svc", "beta/lib", "gamma/x"]

    def run_pick(self, answers, multi=True, pre=None, **kw):
        ask = Script(answers)
        lines = []
        res = sw.pick(self.OPTS, multi, pre or [], ask=ask, out=lines.append,
                      which=lambda: None, **kw)
        return res, lines

    def test_fzf_used_when_available_and_no_preselection(self):
        calls = []

        def runner(args, text):
            calls.append((args, text))
            return 0, "acme/web\nbeta/lib\n"
        res = sw.pick(self.OPTS, True, [], runner=runner, ask=Script([]),
                      which=lambda: "/x/fzf")
        self.assertEqual(res, ["acme/web", "beta/lib"])
        self.assertEqual(calls[0][0], ["/x/fzf", "-m"])
        self.assertEqual(calls[0][1], "\n".join(self.OPTS))

    def test_fzf_single_has_no_m_and_cancel_returns_empty(self):
        calls = []

        def runner(args, text):
            calls.append(args)
            return 130, ""
        res = sw.pick(self.OPTS, False, [], runner=runner, ask=Script([]),
                      which=lambda: "/x/fzf")
        self.assertEqual(res, [])
        self.assertEqual(calls[0], ["/x/fzf"])

    def test_no_fzf_falls_back(self):
        def runner(*a):
            raise AssertionError("no fzf")
        res = sw.pick(self.OPTS, True, [], runner=runner, ask=Script(["2"]),
                      out=lambda s: None, which=lambda: None)
        self.assertEqual(res, ["acme/web"])

    def test_preselection_skips_fzf(self):
        def runner(*a):
            raise AssertionError("fzf must not run")
        res = sw.pick(self.OPTS, True, ["beta/svc"], runner=runner, ask=Script([""]),
                      out=lambda s: None, which=lambda: "/x/fzf")
        self.assertEqual(res, ["beta/svc"])

    def test_numbers_ranges_and_all(self):
        self.assertEqual(self.run_pick(["1,3,5"])[0], ["acme/api", "beta/svc", "gamma/x"])
        self.assertEqual(self.run_pick(["2-4"])[0], ["acme/web", "beta/svc", "beta/lib"])
        self.assertEqual(self.run_pick(["1, 4-5"])[0], ["acme/api", "beta/lib", "gamma/x"])
        self.assertEqual(self.run_pick(["all"])[0], self.OPTS)

    def test_text_filter_then_select(self):
        res, lines = self.run_pick(["beta", "all"])
        self.assertEqual(res, ["beta/svc", "beta/lib"])
        res, _ = self.run_pick(["beta", "1"])
        self.assertEqual(res, ["beta/svc"])

    def test_out_of_range_reprompts(self):
        res, lines = self.run_pick(["9", "0", "3-99", "1"])
        self.assertEqual(res, ["acme/api"])
        self.assertTrue(any("out of range" in l for l in lines))

    def test_enter_without_defaults_returns_empty(self):
        self.assertEqual(self.run_pick([""])[0], [])

    def test_single_mode_takes_one(self):
        res, lines = self.run_pick(["1,2", "2"], multi=False)
        self.assertEqual(res, ["acme/web"])

    def test_filter_without_matches_reprompts(self):
        res, _ = self.run_pick(["zzz", "/", "1"])
        self.assertEqual(res, ["acme/api"])


HOSTILE = "PRD\x1b[2J\x1b]0;pwn\x07 | Deploy\r\nto\u202e EU"


def no_ctrl(text):
    return all(unicodedata.category(c)[0] != "C" for c in text)


class SanitizeTests(WizardBase):
    def test_clean(self):
        self.assertEqual(sw.clean("a\x1b[0m\tb | c\u202e  d\r\n"), "a[0mb c d")

    def test_suggest_label_clean(self):
        env, label = sw.suggest(wf(HOSTILE, "deploy-x.yaml"))
        self.assertEqual(env, "prd")
        self.assertTrue(no_ctrl(label) and "|" not in label)

    def test_empty_label_falls_back_to_file_stem(self):
        self.assertEqual(sw.suggest(wf("\U0001f680", "deploy-prd-eu.yaml")),
                         ("dev", "deploy-prd-eu"))
        self.assertEqual(sw.suggest(wf("\x1b\x07", "my\x1bflow.yml"))[1], "myflow")

    def test_hostile_workflow_end_to_end(self):
        gh = FakeGh(["acme/new"], {"acme/new": [wf(HOSTILE, "deploy-x.yaml")]})
        picker = Picks(first(1), defaults)
        res, ask = self.run_wiz(Config(), gh, picker, ["1", "\x1b[31mmine\r|x", "", "4"])
        shown = [o for step in picker.seen for o in step[0]] + self.lines + ask.prompts
        self.assertTrue(all(no_ctrl(t) for t in shown), shown)
        w = res.repos[0].workflows[0]
        self.assertEqual(w.file, "deploy-x.yaml")
        self.assertEqual(w.label, "[31mminex")
        self.assertEqual(self.saved(), res)

    def test_hostile_repo_not_printed_raw_and_maps_back(self):
        bad = "acme/\x1b[2Jevil"
        gh = FakeGh(["acme/ok", bad], {bad: [wf("Deploy", "deploy.yaml")]})
        picker = Picks(containing("evil"), defaults)
        res, ask = self.run_wiz(Config(), gh, picker, ["1", "", "", "3", "4"])
        self.assertTrue(all(no_ctrl(o) for o in picker.seen[0][0]))
        self.assertEqual(res.repos[0].repo, bad)  # raw identifier stored
        self.assertIn("repos/%s/actions/workflows" % bad, [c[2] for c in gh.calls])
        self.assertTrue(all(no_ctrl(l) for l in self.lines), self.lines)

    def test_remove_and_list_clean_existing_config(self):
        cfg = Config([RepoConfig("acme/a", [Workflow("a.yaml", "prd", "L\x1b[2J1")])])
        picker = Picks(first(1))
        res, _ = self.run_wiz(cfg, FakeGh([], {}), picker, ["3", "2", "4"])
        self.assertTrue(all(no_ctrl(o) for o in picker.seen[0][0]))
        self.assertTrue(all(no_ctrl(l) for l in self.lines))
        self.assertEqual(res.repos, [])

    def test_gh_error_message_cleaned(self):
        gh = FakeGh([], {}, error=GhError("other", "bad\x1b[2J thing"))
        self.run_wiz(Config(), gh, Picks(), ["1", "4"])
        self.assertTrue(all(no_ctrl(l) for l in self.lines))


if __name__ == "__main__":
    unittest.main()
