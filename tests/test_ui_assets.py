"""Static checks for the selection UI page (no browser, no Node)."""
from __future__ import annotations

import http.client
import re
import unittest
from pathlib import Path

from gh_deploy_watcher.ui_server import CSP, UiServer

UI_DIR = Path(__file__).resolve().parent.parent / "gh_deploy_watcher" / "ui"
BANNED_JS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
             "eval(", "new Function")


def read(name: str) -> str:
    return (UI_DIR / name).read_text(encoding="utf-8")


class HtmlTests(unittest.TestCase):
    def test_files_exist(self):
        for name in ("index.html", "app.css", "app.js"):
            self.assertTrue((UI_DIR / name).is_file(), name)

    def test_only_local_assets(self):
        html = read("index.html")
        refs = re.findall(r'(?:src|href)="([^"]*)"', html)
        self.assertEqual(sorted(refs), ["app.css", "app.js"])

    def test_no_inline_script_or_style(self):
        html = read("index.html")
        for body in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S):
            self.assertEqual(body.strip(), "")
        self.assertNotIn("<style", html.lower())
        self.assertIsNone(re.search(r"\sstyle\s*=", html, re.I))

    def test_no_event_handler_attributes(self):
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", read("index.html"), re.I))

    def test_no_urls(self):
        for name in ("index.html", "app.css", "app.js"):
            self.assertIsNone(re.search(r"https?://", read(name)), name)


class JsTests(unittest.TestCase):
    def test_banned_constructs_absent(self):
        js = read("app.js")
        for word in BANNED_JS:
            self.assertNotIn(word, js)

    def test_required_pieces_present(self):
        js = read("app.js")
        for word in ("ghdwDirty", "X-Token", "replaceState", "location.hash"):
            self.assertIn(word, js)

    def test_calls_every_api_path(self):
        js = read("app.js")
        for path in ("/api/session", "/api/repos", "/workflows", "/api/config", "/api/done"):
            self.assertIn(path, js)
        self.assertIn("base_hash", js)

    def test_no_inline_style_attribute_writes(self):
        self.assertNotIn(".style", read("app.js"))
        self.assertNotIn('"style"', read("app.js"))


class CssTests(unittest.TestCase):
    def test_media_features(self):
        css = read("app.css")
        for feature in ("prefers-color-scheme", "prefers-reduced-motion",
                        "prefers-reduced-transparency"):
            self.assertIn(feature, css)

    def test_focus_visible(self):
        self.assertIn(":focus-visible", read("app.css"))


class ServedTests(unittest.TestCase):
    def setUp(self):
        self.server = UiServer(UI_DIR)
        self.server.start()
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.server.port})
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, headers, body

    def test_served_with_csp(self):
        for path, name in (("/", "index.html"), ("/app.css", "app.css"), ("/app.js", "app.js")):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["content-security-policy"], CSP)
            self.assertEqual(body, (UI_DIR / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
