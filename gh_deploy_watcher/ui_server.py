"""Local HTTP server for the selection UI: security checks and lifecycle only."""
from __future__ import annotations

import fcntl
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import unquote

MAX_BODY = 262144
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


@dataclass
class Request:
    method: str
    path: str
    params: Tuple[str, ...]
    headers: Dict[str, str]
    body: Any


@dataclass
class Response:
    status: int
    body: Any = None
    content_type: str = "application/json"
    raw: Optional[bytes] = None


Handler = Callable[[Request], Response]


def _error(status: int, kind: str, message: str, allow: Optional[str] = None) -> Response:
    resp = Response(status, {"error": {"kind": kind, "message": message}})
    resp.allow = allow  # type: ignore[attr-defined]
    return resp


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = 10
    ui: "UiServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def _send(self, resp: Response) -> None:
        if resp.raw is not None:
            data = resp.raw
        else:
            data = json.dumps(resp.body, ensure_ascii=True).encode("ascii")
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for key, value in SECURITY_HEADERS:
            self.send_header(key, value)
        allow = getattr(resp, "allow", None)
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _handle(self) -> None:
        try:
            resp = self._dispatch()
        except Exception:
            resp = _error(500, "internal", "internal error")
        try:
            self._send(resp)
        except OSError:
            pass

    def _dispatch(self) -> Response:
        ui = self.ui
        method = self.command
        headers = {k.lower(): v for k, v in self.headers.items()}
        if headers.get("host") != "127.0.0.1:%d" % ui.port:
            return _error(403, "forbidden", "bad host")
        origin = headers.get("origin")
        if (method != "GET" or origin is not None) and origin != "http://127.0.0.1:%d" % ui.port:
            return _error(403, "forbidden", "bad origin")
        path = unquote(self.path.split("?", 1)[0].split("#", 1)[0])
        if ".." in path or "\\" in path or "\x00" in path or not path.startswith("/"):
            return _error(404, "not_found", "not found")
        is_api = path.startswith("/api/")
        if is_api:
            supplied = headers.get("x-token", "")
            if not secrets.compare_digest(supplied.encode("utf-8"), ui.token.encode("utf-8")):
                return _error(401, "unauthorized", "missing or invalid token")
            ui.touch()
        elif path in STATIC:
            if method != "GET":
                return _error(405, "method_not_allowed", "method not allowed", "GET")
            return self._static(path)
        return self._route(method, path, headers)

    def _static(self, path: str) -> Response:
        name, ctype = STATIC[path]
        try:
            raw = (self.ui.static_dir / name).read_bytes()
        except OSError:
            return _error(404, "not_found", "not found")
        return Response(200, content_type=ctype, raw=raw)

    def _route(self, method: str, path: str, headers: Dict[str, str]) -> Response:
        allowed: List[str] = []
        match = None
        handler = None
        for m, regex, fn in self.ui.routes:
            found = regex.fullmatch(path)
            if not found:
                continue
            if m == method:
                match, handler = found, fn
                break
            allowed.append(m)
        if handler is None or match is None:
            if allowed:
                return _error(405, "method_not_allowed", "method not allowed",
                              ", ".join(sorted(set(allowed))))
            return _error(404, "not_found", "not found")
        body: Any = None
        if method in ("PUT", "POST"):
            ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _error(415, "unsupported_media_type", "content type must be application/json")
            declared = headers.get("content-length")
            if declared is None:
                return _error(411, "length_required", "content length required")
            if not declared.isdigit():
                return _error(400, "bad_request", "invalid content length")
            length = int(declared)
            if length > MAX_BODY:
                return _error(413, "too_large", "request body too large")
            data = self.rfile.read(length)
            try:
                body = json.loads(data.decode("utf-8"))
            except ValueError:
                return _error(400, "bad_request", "invalid JSON")
        return handler(Request(method, path, tuple(match.groups()), headers, body))


class UiServer:
    def __init__(self, static_dir: Path, idle_seconds: float = 900.0,
                 clock: Callable[[], float] = time.monotonic,
                 token: Optional[str] = None) -> None:
        self.static_dir = Path(static_dir)
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.token = token or secrets.token_urlsafe(32)
        self.port = 0
        self.routes: List[Tuple[str, Pattern[str], Handler]] = []
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._last = clock()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/#%s" % (self.port, self.token)

    def add_route(self, method: str, pattern: str, handler: Handler) -> None:
        self.routes.append((method, re.compile(pattern), handler))

    def start(self) -> None:
        handler_cls = type("BoundHandler", (_RequestHandler,), {"ui": self})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self.touch()
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def touch(self) -> None:
        self._last = self.clock()

    def check_idle(self) -> bool:
        if self.clock() - self._last > self.idle_seconds:
            self.shutdown()
            return True
        return False

    def wait(self) -> None:
        while not self._stopped.wait(1.0):
            self.check_idle()

    def shutdown(self) -> None:
        with self._lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
            httpd = self._httpd
        if httpd is not None:
            # Run from a helper thread: safe when called from a request handler.
            threading.Thread(target=self._stop_httpd, args=(httpd,), daemon=True).start()

    @staticmethod
    def _stop_httpd(httpd: ThreadingHTTPServer) -> None:
        httpd.shutdown()
        httpd.server_close()


def try_lock_instance(directory: Path) -> Optional[IO[str]]:
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(str(directory / "ui.lock"), "a")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle
