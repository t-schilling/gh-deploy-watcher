from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from gh_deploy_watcher.ui_server import Response, UiServer, try_lock_instance

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.static = Path(self._tmp.name)
        (self.static / "index.html").write_text("<html>hello</html>")
        (self.static / "app.css").write_text("body{}")
        (self.static / "app.js").write_text("var x=1;")
        self.clock = Clock()
        self.server = UiServer(self.static, clock=self.clock)
        self.server.add_route("GET", r"/api/echo", lambda r: Response(200, {"ok": True}))
        self.server.add_route("POST", r"/api/post", lambda r: Response(200, {"got": r.body}))
        self.server.add_route("PUT", r"/api/item/([a-z]+)", lambda r: Response(200, {"p": list(r.params)}))
        self.ran = []

        def boom(r):
            raise RuntimeError("secret-trace-text")

        def big(r):
            self.ran.append(1)
            return Response(200, {})

        self.server.add_route("GET", r"/api/boom", boom)
        self.server.add_route("POST", r"/api/big", big)
        self.server.add_route("POST", r"/api/stop", lambda r: (self.server.shutdown(), Response(200, {}))[1])
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.port

    def req(self, method, path, body=None, host="default", origin="default",
            token="default", ctype="default", extra=None, raw_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {}
        h["Host"] = "127.0.0.1:%d" % self.port if host == "default" else host
        if origin == "default":
            if method != "GET":
                h["Origin"] = "http://127.0.0.1:%d" % self.port
        elif origin is not None:
            h["Origin"] = origin
        if token == "default":
            h["X-Token"] = self.server.token
        elif token is not None:
            h["X-Token"] = token
        if method in ("POST", "PUT"):
            if ctype == "default":
                h["Content-Type"] = "application/json"
            elif ctype is not None:
                h["Content-Type"] = ctype
        h.update(extra or {})
        data = body.encode() if isinstance(body, str) else body
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        for k, v in (raw_headers or {}).items():
            conn.putheader(k, v)
        if data is not None and "Content-Length" not in (raw_headers or {}):
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data if raw_headers is None else None)
        resp = conn.getresponse()
        payload = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, headers, payload

    def assert_headers(self, headers):
        for k, v in HEADERS.items():
            self.assertEqual(headers.get(k), v, k)


class SecurityTests(ServerCase):
    def test_ok_request_and_headers(self):
        st, h, b = self.req("GET", "/api/echo")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(b), {"ok": True})
        self.assert_headers(h)
        self.assertNotIn("access-control-allow-origin", h)
        self.assertEqual(h["content-length"], str(len(b)))

    def test_token_missing_and_wrong(self):
        for tok in (None, "wrong"):
            st, h, b = self.req("GET", "/api/echo", token=tok)
            self.assertEqual(st, 401)
            self.assert_headers(h)
            self.assertEqual(json.loads(b)["error"]["kind"], "unauthorized")

    def test_wrong_host(self):
        st, h, _ = self.req("GET", "/api/echo", host="evil.example:%d" % self.port)
        self.assertEqual(st, 403)
        self.assert_headers(h)

    def test_host_checked_before_token(self):
        st, _, _ = self.req("GET", "/api/echo", host="localhost:%d" % self.port)
        self.assertEqual(st, 403)

    def test_static_wrong_host(self):
        st, h, _ = self.req("GET", "/app.js", host="evil.example", token=None)
        self.assertEqual(st, 403)
        self.assert_headers(h)

    def test_wrong_origin_post(self):
        st, _, _ = self.req("POST", "/api/post", "{}", origin="http://evil.example")
        self.assertEqual(st, 403)

    def test_absent_origin_post(self):
        st, _, _ = self.req("POST", "/api/post", "{}", origin=None)
        self.assertEqual(st, 403)

    def test_wrong_origin_get_with_origin(self):
        st, _, _ = self.req("GET", "/api/echo", origin="http://evil.example")
        self.assertEqual(st, 403)

    def test_origin_checked_before_token(self):
        st, _, _ = self.req("POST", "/api/post", "{}", origin="http://evil.example", token=None)
        self.assertEqual(st, 403)

    def test_content_type_415(self):
        for m, p in (("POST", "/api/post"), ("PUT", "/api/item/abc")):
            for ct in (None, "text/plain"):
                st, h, _ = self.req(m, p, "{}", ctype=ct)
                self.assertEqual(st, 415)
                self.assert_headers(h)

    def test_content_type_with_charset_ok(self):
        st, _, _ = self.req("POST", "/api/post", "{}", ctype="application/json; charset=utf-8")
        self.assertEqual(st, 200)

    def test_body_too_large_413(self):
        st, h, _ = self.req("POST", "/api/big", None, raw_headers={"Content-Length": "262145"})
        self.assertEqual(st, 413)
        self.assert_headers(h)
        self.assertEqual(self.ran, [])

    def test_body_at_limit_ok(self):
        body = '"' + "a" * (262144 - 2) + '"'
        self.assertEqual(len(body), 262144)
        st, _, _ = self.req("POST", "/api/post", body)
        self.assertEqual(st, 200)

    def test_missing_content_length(self):
        st, _, _ = self.req("POST", "/api/post", None, raw_headers={})
        self.assertIn(st, (400, 411))
        st, _, _ = self.req("POST", "/api/post", None, raw_headers={"Content-Length": "abc"})
        self.assertEqual(st, 400)

    def test_invalid_json_400(self):
        st, h, b = self.req("POST", "/api/post", "{nope")
        self.assertEqual(st, 400)
        self.assert_headers(h)
        self.assertEqual(json.loads(b)["error"]["kind"], "bad_request")

    def test_unknown_path_404(self):
        for p in ("/nope", "/api/nope", "/index.html", "/app.js/x"):
            st, h, _ = self.req("GET", p)
            self.assertEqual(st, 404, p)
            self.assert_headers(h)

    def test_traversal_404(self):
        for p in ("/../x", "/%2e%2e/etc/passwd", "/app.js/../x", "/..%5cx", "/app.js%00"):
            st, h, _ = self.req("GET", p)
            self.assertEqual(st, 404, p)
            self.assert_headers(h)

    def test_unsupported_method_405(self):
        st, h, _ = self.req("DELETE", "/api/echo")
        self.assertEqual(st, 405)
        self.assertIn("GET", h["allow"])
        self.assert_headers(h)

    def test_wrong_method_on_route_405(self):
        st, h, _ = self.req("GET", "/api/post")
        self.assertEqual(st, 405)
        self.assertEqual(h["allow"], "POST")

    def test_handler_exception_500(self):
        st, h, b = self.req("GET", "/api/boom")
        self.assertEqual(st, 500)
        self.assert_headers(h)
        self.assertEqual(json.loads(b), {"error": {"kind": "internal", "message": "internal error"}})
        self.assertNotIn(b"secret-trace-text", b)

    def test_route_params_and_query_ignored(self):
        st, _, b = self.req("PUT", "/api/item/abc?x=1", "{}")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(b), {"p": ["abc"]})

    def test_non_ascii_json_escaped(self):
        self.server.add_route("GET", r"/api/u", lambda r: Response(200, {"s": "é"}))
        _, _, b = self.req("GET", "/api/u")
        self.assertTrue(all(c < 128 for c in b))


class StaticTests(ServerCase):
    def test_static_files(self):
        for path, ctype, content in (
            ("/", "text/html; charset=utf-8", b"<html>hello</html>"),
            ("/app.css", "text/css; charset=utf-8", b"body{}"),
            ("/app.js", "application/javascript; charset=utf-8", b"var x=1;"),
        ):
            st, h, b = self.req("GET", path, token=None)
            self.assertEqual(st, 200, path)
            self.assertEqual(h["content-type"], ctype)
            self.assertEqual(b, content)
            self.assert_headers(h)

    def test_index_contains_no_token(self):
        _, _, b = self.req("GET", "/", token=None)
        self.assertNotIn(self.server.token.encode(), b)

    def test_missing_static_404(self):
        (self.static / "app.js").unlink()
        st, h, _ = self.req("GET", "/app.js", token=None)
        self.assertEqual(st, 404)
        self.assert_headers(h)

    def test_query_string_ignored_for_static(self):
        st, _, _ = self.req("GET", "/app.js?v=1", token=None)
        self.assertEqual(st, 200)


class LifecycleTests(ServerCase):
    def test_url_token_only_in_fragment(self):
        url = self.server.url
        base, frag = url.split("#")
        self.assertEqual(base, "http://127.0.0.1:%d/" % self.port)
        self.assertEqual(frag, self.server.token)
        self.assertNotIn(self.server.token, base)
        self.assertGreaterEqual(len(self.server.token), 43)

    def test_binds_loopback_only(self):
        self.assertEqual(self.server._httpd.server_address[0], "127.0.0.1")

    def test_no_request_logging(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.req("GET", "/api/echo")
            self.req("GET", "/api/boom")
            self.req("DELETE", "/nope")
            self.req("GET", "/api/echo", host="evil")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_idle_shutdown_and_touch(self):
        self.clock.now += 899
        self.assertFalse(self.server.check_idle())
        self.server.touch()
        self.clock.now += 899
        self.assertFalse(self.server.check_idle())
        self.clock.now += 2
        self.assertTrue(self.server.check_idle())

    def test_api_request_touches(self):
        self.clock.now += 800
        self.req("GET", "/api/echo")
        self.clock.now += 800
        self.assertFalse(self.server.check_idle())

    def test_rejected_request_does_not_touch(self):
        self.clock.now += 800
        self.req("GET", "/api/echo", token="wrong")
        self.clock.now += 101
        self.assertTrue(self.server.check_idle())

    def test_wait_returns_after_idle(self):
        done = threading.Event()

        def run():
            self.server.wait()
            done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.clock.now += 901
        self.assertTrue(done.wait(5))

    def test_shutdown_idempotent(self):
        self.server.shutdown()
        self.server.shutdown()

    def test_shutdown_from_handler(self):
        done = threading.Event()
        t = threading.Thread(target=lambda: (self.server.wait(), done.set()), daemon=True)
        t.start()
        st, _, _ = self.req("POST", "/api/stop", "{}")
        self.assertEqual(st, 200)
        self.assertTrue(done.wait(5))


class LockTests(unittest.TestCase):
    def test_lock_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            first = try_lock_instance(Path(d) / "sub")
            self.assertIsNotNone(first)
            second = try_lock_instance(Path(d) / "sub")
            self.assertIsNone(second)
            first.close()
            third = try_lock_instance(Path(d) / "sub")
            self.assertIsNotNone(third)
            third.close()


if __name__ == "__main__":
    unittest.main()
