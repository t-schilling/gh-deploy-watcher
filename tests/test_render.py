from __future__ import annotations

import unittest
from datetime import datetime, timezone

from gh_deploy_watcher.config import Config, RepoConfig, Workflow
from gh_deploy_watcher.model import Run
from gh_deploy_watcher.render import _ICONS, error_menu, render_menu, sanitize
from gh_deploy_watcher.state import State

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
SCRIPT = "/Users/x y/plugins/gh-deploy-watcher.1m.py"


def run(id=1, status="completed", conclusion="success", title="Merge pull request #7 from acme/x",
        created="2026-10-09T11:48:00Z"):
    return Run(id, status, conclusion, created, title, "https://example.com/run/%d" % id, "main")


def config():
    return Config([RepoConfig("acme/api", [
        Workflow("deploy-prd.yaml", "prd", "PRD · EU"),
        Workflow("deploy-dev.yaml", "dev", "DEV · EU"),
    ])])


def state(prd=None, dev=None, **kw):
    s = State(polling=True, **kw)
    if prd is not None:
        s.last["acme/api/deploy-prd.yaml"] = prd if isinstance(prd, dict) else prd.to_dict()
    if dev is not None:
        s.last["acme/api/deploy-dev.yaml"] = dev if isinstance(dev, dict) else dev.to_dict()
    return s


def render(st, cfg=None, error=None):
    return render_menu(cfg or config(), st, error, NOW, SCRIPT)


def first(text):
    return text.splitlines()[0]


class IconTests(unittest.TestCase):
    def test_paused(self):
        st = state(prd=run())
        st.polling = False
        self.assertEqual(first(render(st)), "GH ⏸")

    def test_error(self):
        self.assertEqual(first(render(state(), error="no network")), "GH ⚠")
        self.assertIn("no network", render(state(), error="no network"))

    def test_prd_failed(self):
        self.assertEqual(first(render(state(prd=run(conclusion="failure")))), "GH 🔴")

    def test_dev_failed(self):
        self.assertEqual(first(render(state(dev=run(conclusion="failure")))), "GH 🟠")

    def test_running(self):
        self.assertEqual(first(render(state(prd=run(status="in_progress", conclusion=None)))), "GH 🟡")

    def test_ok(self):
        self.assertEqual(first(render(state(prd=run(), dev=run(2)))), "GH 🟢")

    def test_header_separator(self):
        self.assertEqual(render(state()).splitlines()[1], "---")


class TitleTests(unittest.TestCase):
    def test_every_state_renders_gh_title(self):
        cases = {
            "prd_failed": lambda: render(state(prd=run(conclusion="failure"))),
            "dev_failed": lambda: render(state(dev=run(conclusion="failure"))),
            "running": lambda: render(state(prd=run(status="in_progress", conclusion=None))),
            "ok": lambda: render(state(prd=run(), dev=run(2))),
            "paused": lambda: render(State(polling=False)),
            "error": lambda: error_menu("x"),
        }
        self.assertEqual(set(cases), set(_ICONS))
        for key, make in cases.items():
            with self.subTest(key):
                self.assertEqual(first(make()), "GH " + _ICONS[key])


class MenuTests(unittest.TestCase):
    def test_rerun_only_when_failed(self):
        failed = render(state(prd=run(7, conclusion="failure")))
        lines = [l for l in failed.splitlines() if "Re-run failed jobs" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("param1=rerun param2=acme/api param3=7 param4=prd", lines[0])
        self.assertIn("bash='%s'" % SCRIPT, lines[0])
        self.assertIn("terminal=false refresh=true", lines[0])
        running = render(state(prd=run(status="in_progress", conclusion=None)))
        self.assertNotIn("Re-run", running)
        self.assertNotIn("param1=rerun", render(state(prd=run())))

    def test_open_run_href(self):
        self.assertIn("href=https://example.com/run/1", render(state(prd=run())))

    def test_filter_hides_dev(self):
        out = render(state(prd=run(), dev=run(2), filter="prd"))
        self.assertIn("PRD · EU", out)
        self.assertNotIn("DEV · EU", out)

    def test_filter_marks_current(self):
        out = render(state(filter="prd"))
        self.assertIn("● PRD only", out)
        self.assertIn("○ Both", out)
        self.assertIn("param1=filter param2=dev", out)

    def test_open_config_only_when_file_exists(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "config.json"
            out = render_menu(config(), state(), None, NOW, SCRIPT, config_path=p)
            self.assertNotIn("Open config", out)
            p.write_text("{}")
            out = render_menu(config(), state(), None, NOW, SCRIPT, config_path=p)
            self.assertIn("Open config", out)
            self.assertIn("bash=/usr/bin/open", out)

    def test_static_entries(self):
        out = render(state())
        for s in ("Stop polling", "Poll now", "param1=refresh", "Add / remove repos…",
                  "param1=ui terminal=false refresh=true"):
            self.assertIn(s, out)
        st = state()
        st.polling = False
        self.assertNotIn("param1=setup", render(state()))
        self.assertIn("param1=start", render(st))
        self.assertIn("param1=stop", render(state()))

    def test_last_poll(self):
        self.assertIn("Last poll: never", render(state()))
        st = state(last_poll=NOW.timestamp() - 40)
        self.assertIn("Last poll: 40s ago", render(st))

    def test_line_content(self):
        out = render(state(prd=run(7)))
        self.assertIn("PRD · EU", out)
        self.assertIn("12m", out)
        self.assertIn("#7", out)

    def test_no_runs_yet(self):
        for st in (state(), state(prd={"run": None}, dev={"run": None})):
            out = render(st)
            self.assertEqual(out.count("no runs yet"), 2)
            self.assertEqual(first(out), "GH 🟢")

    def test_unreadable(self):
        bad = [{"id": 1}, {"id": "x", "status": "s", "created_at": "c", "title": "t",
                           "url": "u", "branch": "b"}]
        for b in bad:
            out = render(state(prd=b, dev=run(2)))
            self.assertIn("unreadable", out)
            self.assertNotIn("Re-run", out)
            self.assertIn("DEV · EU", out)
            self.assertEqual(first(out), "GH 🟢")

    def test_malformed_date_unreadable(self):
        r = run().to_dict()
        r["created_at"] = "not-a-date"
        out = render(state(prd=r, dev=run(2)))
        self.assertIn("unreadable", out)

    def test_error_entry_only_that_workflow(self):
        out = render(state(prd={"error": "repo not found"}, dev=run(2)))
        self.assertEqual(out.count("repo not found"), 1)
        self.assertEqual(first(out), "GH ⚠")
        self.assertIn("DEV · EU", out)
        self.assertEqual(out.count("repo not found"), 1)

    def test_error_entry_with_known_failure_keeps_red(self):
        out = render(state(prd={"error": "x"}, dev=run(2, conclusion="failure")))
        self.assertEqual(first(out), "GH 🟠")
        out = render(state(prd=run(2, conclusion="failure"), dev={"error": "x"}))
        self.assertEqual(first(out), "GH 🔴")

    def test_error_entry_never_green_or_yellow(self):
        out = render(state(prd={"error": "x"}, dev=run(2, status="in_progress", conclusion=None)))
        self.assertEqual(first(out), "GH ⚠")

    def test_error_entry_other_repo_unaffected(self):
        cfg = Config([RepoConfig("acme/api", [Workflow("a.yaml", "prd", "A")]),
                      RepoConfig("acme/web", [Workflow("b.yaml", "prd", "B")])])
        st = State(polling=True, last={"acme/api/a.yaml": {"error": "boom"},
                                       "acme/web/b.yaml": run(3).to_dict()})
        out = render(st, cfg)
        self.assertEqual(out.count("boom"), 1)
        self.assertIn("acme/web", out)
        self.assertIn("#7", out)

    def test_title_injection(self):
        evil = "fix | bash=/bin/rm param1=-rf\nsecond 'q' \"d\""
        out = render(state(prd=run(title=evil)))
        for line in out.splitlines():
            if "fix" in line:
                self.assertEqual(line.count("|"), 0, line)
        self.assertNotIn("\nsecond", out)

    def test_error_message_sanitized(self):
        out = render(state(prd={"error": "bad | href=http://evil\nx"}))
        for line in out.splitlines():
            if "bad" in line:
                self.assertEqual(line.count("|"), 1, line)

    def test_single_quote_in_script_path_refused(self):
        with self.assertRaises(ValueError):
            render_menu(config(), state(), None, NOW, "/tmp/it's/x.py")


class RobustnessTests(unittest.TestCase):
    def test_non_dict_entries_unreadable(self):
        for bad in (5, [1, 2], "str", 1.5, True):
            st = State(polling=True)
            st.last["acme/api/deploy-prd.yaml"] = bad
            st.last["acme/api/deploy-dev.yaml"] = run(2).to_dict()
            out = render(st)
            self.assertIn("unreadable", out, bad)
            self.assertIn("DEV · EU", out)
            self.assertEqual(first(out), "GH 🟢")

    def test_quote_in_repo_degrades_one_workflow(self):
        cfg = Config([RepoConfig("acme/ap'i", [Workflow("a.yaml", "prd", "A")]),
                      RepoConfig("acme/web", [Workflow("b.yaml", "prd", "B")])])
        st = State(polling=True, last={
            "acme/ap'i/a.yaml": run(5, conclusion="failure").to_dict(),
            "acme/web/b.yaml": run(6, conclusion="failure").to_dict()})
        out = render(st, cfg)
        self.assertEqual(first(out), "GH 🔴")
        self.assertIn("failed", out)
        self.assertEqual(out.count("Re-run failed jobs"), 1)
        self.assertIn("param2=acme/web", out)
        self.assertIn("Open run", out)


def header(out, repo="acme/api"):
    return [l for l in out.splitlines() if repo in l and l[:1] in "▾▸"][0]


def lines_of(out, text):
    return [l for l in out.splitlines() if text in l]


class CollapseTests(unittest.TestCase):
    def test_expanded_header_and_workflows(self):
        out = render(state(prd=run(), dev=run(2)))
        self.assertTrue(header(out).startswith("▾ acme/api | "))
        self.assertIn("bash='%s' param1=toggle param2=acme/api terminal=false refresh=true"
                      % SCRIPT, header(out))
        self.assertIn("PRD · EU", out)
        self.assertIn("DEV · EU", out)

    def test_collapsed_all_green(self):
        out = render(state(prd=run(), dev=run(2), collapsed=["acme/api"]))
        self.assertTrue(header(out).startswith("▸ acme/api  🟢 2/2 | "))
        self.assertIn("param1=toggle param2=acme/api", header(out))
        self.assertNotIn("PRD · EU", out)
        self.assertNotIn("Open run", out)
        self.assertEqual(first(out), "GH 🟢")

    def test_collapsed_failing(self):
        st = state(prd=run(conclusion="failure"), dev=run(2), collapsed=["acme/api"])
        out = render(st)
        self.assertTrue(header(out).startswith("▸ acme/api  🔴 1 failed · 🟢 1 | "))
        self.assertNotIn("Re-run", out)

    def test_collapsed_running(self):
        st = state(prd=run(status="in_progress", conclusion=None), dev=run(2), collapsed=["acme/api"])
        self.assertTrue(header(render(st)).startswith("▸ acme/api  🟡 1 running · 🟢 1 | "))

    def test_collapsed_error_and_other(self):
        st = state(prd={"error": "boom"}, dev={"run": None}, collapsed=["acme/api"])
        out = render(st)
        self.assertTrue(header(out).startswith("▸ acme/api  ⚠ 1 error · ⚪ 1 other | "))
        self.assertNotIn("boom", out)
        st = state(prd=run(conclusion="cancelled"), dev={"id": 1}, collapsed=["acme/api"])
        self.assertTrue(header(render(st)).startswith("▸ acme/api  ⚪ 2 other | "))

    def test_collapsed_everything_mixed_order(self):
        cfg = Config([RepoConfig("acme/api", [
            Workflow("a.yaml", "prd", "A"), Workflow("b.yaml", "prd", "B"),
            Workflow("c.yaml", "prd", "C"), Workflow("d.yaml", "prd", "D")])])
        st = State(polling=True, collapsed=["acme/api"], last={
            "acme/api/a.yaml": run(1).to_dict(),
            "acme/api/b.yaml": run(2, conclusion="failure").to_dict(),
            "acme/api/c.yaml": run(3, status="queued", conclusion=None).to_dict(),
            "acme/api/d.yaml": {"error": "x"}})
        self.assertTrue(header(render(st, cfg)).startswith(
            "▸ acme/api  🔴 1 failed · 🟡 1 running · ⚠ 1 error · 🟢 1 | "))

    def test_collapsed_no_runs_yet_not_green(self):
        out = render(state(collapsed=["acme/api"]))
        self.assertTrue(header(out).startswith("▸ acme/api  ⚪ 2 other | "))

    def test_filter_applies_to_summary(self):
        st = state(prd=run(), dev=run(2, conclusion="failure"), collapsed=["acme/api"], filter="prd")
        self.assertTrue(header(render(st)).startswith("▸ acme/api  🟢 1/1 | "))
        st.filter = "dev"
        self.assertTrue(header(render(st)).startswith("▸ acme/api  🔴 1 failed | "))

    def test_filter_leaves_nothing(self):
        cfg = Config([RepoConfig("acme/api", [Workflow("a.yaml", "prd", "A")])])
        st = State(polling=True, filter="dev", collapsed=["acme/api"])
        self.assertTrue(header(render(st, cfg)).startswith("▸ acme/api  no workflows in this view | "))
        self.assertTrue(header(render(State(polling=True, filter="dev"), cfg)).startswith("▾ acme/api | "))

    def test_collapsed_failure_still_turns_bar_red(self):
        self.assertEqual(first(render(state(prd=run(conclusion="failure"), collapsed=["acme/api"]))), "GH 🔴")
        self.assertEqual(first(render(state(dev=run(conclusion="failure"), collapsed=["acme/api"]))), "GH 🟠")
        st = state(prd=run(status="in_progress", conclusion=None), collapsed=["acme/api"])
        self.assertEqual(first(render(st)), "GH 🟡")

    def test_only_named_repo_collapses_and_separators_kept(self):
        cfg = Config([RepoConfig("acme/api", [Workflow("a.yaml", "prd", "A")]),
                      RepoConfig("acme/web", [Workflow("b.yaml", "prd", "B")])])
        st = State(polling=True, collapsed=["acme/web", "gone/repo"],
                   last={"acme/api/a.yaml": run(1).to_dict(), "acme/web/b.yaml": run(2).to_dict()})
        out = render(st, cfg)
        self.assertIn("▾ acme/api", out)
        self.assertIn("▸ acme/web  🟢 1/1", out)
        self.assertIn("A   ok", out)
        self.assertNotIn("B   ok", out)
        self.assertNotIn("gone/repo", out)
        self.assertEqual(out.count("\n---\n"), 4)

    def test_hostile_repo_name_cannot_inject_parameters(self):
        for name in ("acme/ap'i param9=x", "acme/ap\nparam9=x", "acme/ap\rparam9=x"):
            cfg = Config([RepoConfig(name, [Workflow("a.yaml", "prd", "A")])])
            for collapsed in ([], [name]):
                out = render_menu(cfg, State(polling=True, collapsed=collapsed), None, NOW, SCRIPT)
                self.assertNotIn("param9", "\n".join(l.split(" | ", 1)[-1] for l in out.splitlines()
                                                      if "toggle" in l))
                self.assertNotIn("param1=toggle", out)
                for l in out.splitlines():
                    self.assertNotIn("'param9", l)
                    self.assertFalse(l.startswith("param9"))
        cfg = Config([RepoConfig("acme/a b param9=x", [Workflow("a.yaml", "prd", "A")])])
        h = header(render_menu(cfg, State(polling=True), None, NOW, SCRIPT), "acme/a")
        self.assertIn("param2=acme/a%20b%20param9=x terminal=false", h)
        self.assertEqual(h.count("param2="), 1)
        self.assertNotIn(" param3=", h)

    def test_hostile_header_degrades_to_plain_line(self):
        cfg = Config([RepoConfig("acme/ap'i", [Workflow("a.yaml", "prd", "A")])])
        out = render_menu(cfg, State(polling=True, collapsed=["acme/ap'i"]), None, NOW, SCRIPT)
        self.assertIn("▸ acme/api  ", out)
        self.assertNotIn("param1=toggle", out)


class SanitizeTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize("a|b\nc\r\nd"), "a b c  d")
        self.assertEqual(sanitize("it's \"x\""), "its x")


class ErrorMenuActionsTests(unittest.TestCase):
    def test_actions_present_with_script(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "config.json"
            p.write_text("{bad")
            text = error_menu("bad | config", SCRIPT, p)
        self.assertTrue(text.startswith("GH \u26a0\n---\n"))
        for s in ("Add / remove repos", "param1=ui terminal=false", "Stop polling",
                  "param1=stop", "Open config", "bash=/usr/bin/open"):
            self.assertIn(s, text)
        self.assertNotIn("bad |", text)
        self.assertRegex(text, r"Add / remove repos… \| .*param1=ui terminal=false refresh=true")

    def test_no_open_config_when_file_missing(self):
        text = error_menu("x", SCRIPT, "/nonexistent/dir/config.json")
        self.assertNotIn("Open config", text)
        self.assertIn("Add / remove repos", text)

    def test_quote_in_script_path_degrades_to_plain_menu(self):
        text = error_menu("x", "/tmp/it's/x.py", None)
        self.assertEqual(text.splitlines(), ["GH \u26a0", "---", "x"])


class ErrorMenuTests(unittest.TestCase):
    def test_error_menu_is_warning_without_actions(self):
        text = error_menu("bad | config\nline 'two'")
        lines = text.splitlines()
        self.assertEqual(lines[0], "GH \u26a0")
        self.assertEqual(lines[1], "---")
        self.assertEqual(len(lines), 3)
        self.assertNotIn("|", text)
        self.assertNotIn("bash=", text)


if __name__ == "__main__":
    unittest.main()
