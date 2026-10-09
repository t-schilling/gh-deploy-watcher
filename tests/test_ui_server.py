from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import socket
import select
import subprocess
import sys
import tempfile
import time
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


class HardeningTests(ServerCase):
    def raw(self, payload: bytes, shutdown_write: bool = False):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                c = sock.recv(65536)
            except OSError:
                break
            if not c:
                break
            chunks.append(c)
        sock.close()
        data = b"".join(chunks)
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        hdrs = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            hdrs[k.strip().lower()] = v.strip()
        return lines[0], hdrs, body

    def hostline(self):
        return ("Host: 127.0.0.1:%d\r\n" % self.port).encode()

    def run_quiet(self, fn):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = fn()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        return result

    def test_unserializable_body_500(self):
        self.server.add_route("GET", r"/api/set", lambda r: Response(200, {1}))
        st, h, b = self.run_quiet(lambda: self.req("GET", "/api/set"))
        self.assertEqual(st, 500)
        self.assert_headers(h)
        self.assertEqual(json.loads(b)["error"]["kind"], "internal")

    def test_none_and_bad_status_500(self):
        self.server.add_route("GET", r"/api/none", lambda r: None)
        self.server.add_route("GET", r"/api/status", lambda r: Response(99999, {}))
        for p in ("/api/none", "/api/status"):
            st, h, _ = self.run_quiet(lambda: self.req("GET", p))
            self.assertEqual(st, 500, p)
            self.assert_headers(h)

    def check_raw_error(self, payload, code):
        status, hdrs, body = self.run_quiet(lambda: self.raw(payload))
        self.assertTrue(status.startswith("HTTP/1.0 %d" % code), status)
        for k, v in HEADERS.items():
            self.assertEqual(hdrs.get(k), v, k)
        self.assertEqual(int(hdrs["content-length"]), len(body))
        self.assertIn("error", json.loads(body))

    def test_bad_request_line(self):
        self.check_raw_error(b"GARBAGE\r\n\r\n", 400)

    def test_long_uri(self):
        self.check_raw_error(b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n", 414)

    def test_too_many_headers(self):
        hdrs = b"".join(b"X-%d: y\r\n" % i for i in range(150))
        self.check_raw_error(b"GET / HTTP/1.1\r\n" + self.hostline() + hdrs + b"\r\n", 431)

    def test_bad_http_version(self):
        self.check_raw_error(b"GET / HTTP/2.0\r\n\r\n", 505)

    def test_duplicate_security_headers_400(self):
        tok = ("X-Token: %s\r\n" % self.server.token).encode()
        good = self.hostline()
        cases = [
            b"GET /api/echo HTTP/1.1\r\nHost: evil\r\n" + good + tok + b"\r\n",
            b"GET /api/echo HTTP/1.1\r\n" + good + tok + tok + b"\r\n",
            b"GET /api/echo HTTP/1.1\r\n" + good + tok
            + b"Origin: http://evil\r\nOrigin: http://127.0.0.1:%d\r\n\r\n" % self.port,
            b"POST /api/post HTTP/1.1\r\n" + good + tok
            + b"Origin: http://127.0.0.1:%d\r\n" % self.port
            + b"Content-Type: application/json\r\nContent-Type: text/plain\r\n"
            + b"Content-Length: 2\r\n\r\n{}",
            b"POST /api/post HTTP/1.1\r\n" + good + tok
            + b"Origin: http://127.0.0.1:%d\r\n" % self.port
            + b"Content-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        ]
        for c in cases:
            self.check_raw_error(c, 400)

    def test_unicode_digit_content_length_400(self):
        st, _, _ = self.req("POST", "/api/post", None,
                            raw_headers={"Content-Length": "\u00b2".encode("latin-1").decode("latin-1")})
        self.assertEqual(st, 400)

    def test_deeply_nested_json_400(self):
        body = "[" * 100000 + "]" * 100000
        st, _, b = self.run_quiet(lambda: self.req("POST", "/api/post", body))
        self.assertEqual(st, 400)

    def test_add_route_requires_api_prefix(self):
        for pat in (r"/x", r"/", r"/app.js", r"/apix/y", r".*"):
            with self.assertRaises(ValueError):
                self.server.add_route("GET", pat, lambda r: Response(200, {}))

    def test_no_python_version_in_server_header(self):
        _, h, _ = self.req("GET", "/api/echo")
        self.assertNotIn("python", h.get("server", "").lower())
        status, hdrs, _ = self.raw(b"GARBAGE\r\n\r\n")
        self.assertNotIn("python", hdrs.get("server", "").lower())

    def test_wait_returns_after_server_fully_stopped(self):
        done = threading.Event()
        t = threading.Thread(target=lambda: (self.server.wait(), done.set()), daemon=True)
        t.start()
        st, _, b = self.req("POST", "/api/stop", "{}")
        self.assertEqual((st, json.loads(b)), (200, {}))
        self.assertTrue(done.wait(5))
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=2)


CHILD = """
import sys, tempfile
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from gh_deploy_watcher.ui_server import Response, UiServer
s = UiServer(Path(tempfile.mkdtemp()), token="tok")
s.add_route("POST", r"/api/stop", lambda r: (s.shutdown(), Response(200, {}))[1])
s.add_route("POST", r"/api/bigstop", lambda r: (s.shutdown(), Response(200, raw=b"x" * 3000000))[1])
s.start()
print(s.port, flush=True)
s.wait()
"""


class RoundTwoTests(ServerCase):
    def child_port(self, proc):
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        self.assertTrue(ready, "child did not start")
        return int(proc.stdout.readline())

    def test_shutdown_reply_is_never_cut(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as d:
            for i in range(30):
                proc = subprocess.Popen([sys.executable, "-c", CHILD, repo], cwd=d,
                                        stdout=subprocess.PIPE, text=True)
                try:
                    port = self.child_port(proc)
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    conn.request("POST", "/api/bigstop", "{}", {
                        "Host": "127.0.0.1:%d" % port,
                        "Origin": "http://127.0.0.1:%d" % port,
                        "X-Token": "tok", "Content-Type": "application/json"})
                    resp = conn.getresponse()
                    time.sleep(0.15)
                    self.assertEqual(len(resp.read()), 3000000, "run %d" % i)
                    self.assertEqual(proc.wait(timeout=8), 0)
                finally:
                    proc.kill()
                    proc.wait()
                    proc.stdout.close()

    def test_oserror_in_handler_gives_500(self):
        calls = []

        def make(exc):
            def h(r):
                calls.append(1)
                raise exc
            return h

        self.server.add_route("GET", r"/api/oserr", make(OSError("disk")))
        self.server.add_route("GET", r"/api/perm", make(PermissionError("nope-secret")))
        self.server.add_route("GET", r"/api/fnf", lambda r: open("/nonexistent/x"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for p in ("/api/oserr", "/api/perm", "/api/fnf"):
                st, h, b = self.req("GET", p)
                self.assertEqual(st, 500, p)
                self.assert_headers(h)
                self.assertEqual(json.loads(b)["error"]["kind"], "internal")
                self.assertNotIn(b"nope-secret", b)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(calls), 2)

    def test_client_disconnect_during_write(self):
        self.server.add_route("GET", r"/api/bigout",
                              lambda r: Response(200, raw=b"x" * 20000000))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            sock.sendall(("GET /api/bigout HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Token: %s\r\n\r\n"
                          % (self.port, self.server.token)).encode())
            sock.recv(10)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            sock.close()
            time.sleep(0.5)
            st, _, _ = self.req("GET", "/api/echo")
        self.assertEqual(st, 200)
        self.assertEqual(err.getvalue(), "")

    def test_non_api_route_never_dispatched(self):
        hit = []

        def h(r):
            hit.append(1)
            return Response(200, {"secret": 1})

        try:
            self.server.add_route("GET", r"/api/x|/secret", h)
        except ValueError:
            pass
        st, _, b = self.req("GET", "/secret", token=None)
        self.assertEqual(st, 404)
        self.assertEqual(hit, [])
        self.assertNotIn(b"secret", b.replace(b"not found", b""))

    def test_trickling_clients_do_not_hold_shutdown(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            proc = subprocess.Popen([sys.executable, "-c", CHILD, repo], cwd=d,
                                    stdout=subprocess.PIPE, text=True)
            self.addCleanup(proc.kill)
            self.addCleanup(proc.stdout.close)
            port = self.child_port(proc)
            stop = threading.Event()

            def trickle():
                sk = socket.create_connection(("127.0.0.1", port), timeout=5)
                sk.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % port)
                while not stop.wait(2):
                    try:
                        sk.sendall(b"X-A: b\r\n")
                    except OSError:
                        break
                sk.close()

            threads = [threading.Thread(target=trickle, daemon=True) for _ in range(2)]
            for t in threads:
                t.start()
            self.addCleanup(stop.set)
            time.sleep(1)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/stop", "{}", {
                "Host": "127.0.0.1:%d" % port, "Origin": "http://127.0.0.1:%d" % port,
                "X-Token": "tok", "Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            resp.read()
            self.assertEqual(proc.wait(timeout=8), 0)


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

    def test_other_oserror_closes_handle_and_raises(self):
        from unittest import mock
        opened = []
        real_open = open

        def spy(*a, **k):
            h = real_open(*a, **k)
            opened.append(h)
            return h
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("gh_deploy_watcher.ui_server.open", spy, create=True), \
                mock.patch("gh_deploy_watcher.ui_server.fcntl.flock", side_effect=OSError(5, "io")):
            with self.assertRaises(OSError):
                try_lock_instance(Path(d))
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)


class InflightTests(unittest.TestCase):
    def _httpd(self):
        from http.server import BaseHTTPRequestHandler
        from gh_deploy_watcher.ui_server import _Httpd
        httpd = _Httpd(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.addCleanup(httpd.server_close)
        return httpd

    def test_start_failure_removes_thread_and_reraises(self):
        from unittest import mock
        httpd = self._httpd()
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")):
            with self.assertRaises(RuntimeError):
                httpd.process_request(None, ("127.0.0.1", 1))
        self.assertEqual(httpd._inflight, set())
        httpd.join_inflight(0.1)

    def test_join_skips_unstarted_threads(self):
        httpd = self._httpd()
        httpd._inflight.add(threading.Thread(target=lambda: None))
        httpd.join_inflight(0.1)  # must not raise RuntimeError


if __name__ == "__main__":
    unittest.main()
