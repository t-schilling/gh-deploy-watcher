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


class _Httpd(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._inflight: set = set()
        self._inflight_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        thread = threading.Thread(target=self._run_request,
                                  args=(request, client_address), daemon=True)
        with self._inflight_lock:
            self._inflight.add(thread)
        try:
            thread.start()
        except BaseException:
            with self._inflight_lock:
                self._inflight.discard(thread)
            raise

    def _run_request(self, request: Any, client_address: Any) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            with self._inflight_lock:
                self._inflight.discard(threading.current_thread())

    def join_inflight(self, seconds: float) -> None:
        """Wait for in-flight replies against one shared deadline."""
        deadline = time.monotonic() + seconds
        with self._inflight_lock:
            threads = list(self._inflight)
        for thread in threads:
            if thread is not threading.current_thread() and thread.ident is not None:
                thread.join(max(0.0, deadline - time.monotonic()))

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "gh-deploy-watcher"
    sys_version = ""
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

    def send_error(self, code: int, message: Optional[str] = None,
                   explain: Optional[str] = None) -> None:
        self.close_connection = True
        self._send(_error(code, "bad_request", "bad request"))

    def _send(self, resp: Response) -> None:
        if not isinstance(resp, Response) or not isinstance(resp.status, int) \
                or not 100 <= resp.status <= 599:
            raise TypeError("invalid response")
        if resp.raw is not None:
            data = resp.raw
        else:
            data = json.dumps(resp.body, ensure_ascii=True).encode("ascii")
        self.request_version = "HTTP/1.0"  # never fall back to header-less 0.9
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
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(data)

    def _handle(self) -> None:
        try:
            resp = self._dispatch()
        except Exception:
            resp = _error(500, "internal", "internal error")
        try:
            self._send(resp)
        except OSError:
            return  # client went away while writing
        except Exception:
            try:
                self._send(_error(500, "internal", "internal error"))
            except Exception:
                pass

    def _dispatch(self) -> Response:
        ui = self.ui
        method = self.command
        for name in ("host", "origin", "content-length", "content-type", "x-token"):
            if len(self.headers.get_all(name) or []) > 1:
                return _error(400, "bad_request", "duplicate header")
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
        else:
            return _error(404, "not_found", "not found")
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
            if not re.fullmatch(r"[0-9]+", declared):
                return _error(400, "bad_request", "invalid content length")
            length = int(declared)
            if length > MAX_BODY:
                return _error(413, "too_large", "request body too large")
            data = self.rfile.read(length)
            try:
                body = json.loads(data.decode("utf-8"))
            except (ValueError, RecursionError):
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
        self._httpd: Optional[_Httpd] = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._stop_thread: Optional[threading.Thread] = None
        self._last = clock()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/#%s" % (self.port, self.token)

    def add_route(self, method: str, pattern: str, handler: Handler) -> None:
        if not pattern.startswith("/api/"):
            raise ValueError("routes must start with /api/")
        self.routes.append((method, re.compile(pattern), handler))

    def start(self) -> None:
        handler_cls = type("BoundHandler", (_RequestHandler,), {"ui": self})
        httpd = _Httpd(("127.0.0.1", 0), handler_cls)
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
        stopper = self._stop_thread
        if stopper is not None and stopper is not threading.current_thread():
            stopper.join(5)

    def shutdown(self) -> None:
        with self._lock:
            if self._stopped.is_set():
                return
            httpd = self._httpd
            if httpd is not None:
                # Helper thread: safe when called from a request handler.
                self._stop_thread = threading.Thread(
                    target=self._stop_httpd, args=(httpd,), daemon=True)
                self._stop_thread.start()
            self._stopped.set()

    @staticmethod
    def _stop_httpd(httpd: _Httpd) -> None:
        httpd.shutdown()
        httpd.server_close()
        httpd.join_inflight(3.0)


def try_lock_instance(directory: Path) -> Optional[IO[str]]:
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(str(directory / "ui.lock"), "a")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except OSError:
        handle.close()
        raise
    return handle
