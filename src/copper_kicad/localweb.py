"""A tiny gated loopback web server for Copper's local pages (onboarding).

Security model, the same one Copper Canvas uses (``focus/canvas.py``):

* binds 127.0.0.1 only, on a random free port;
* a random 192-bit token is required on EVERY request (``?t=`` or ``X-Copper-Token``), compared in
  constant time;
* the ``Host`` header must be the loopback address that was bound (stops DNS rebinding);
* cross-origin POSTs are refused;
* pages get a per-response CSP nonce: nothing is loaded from the network, no inline ``style=""``
  attributes, no framing.

    httpd, token = make_server(handler_cls, port=0)
    class MyHandler(GatedHandler): ...          # implement route_get / route_post
"""
from __future__ import annotations

import hmac
import json
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 16 * 1024


class HttpError(Exception):
    def __init__(self, code: int, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class Server(ThreadingHTTPServer):
    daemon_threads = True
    token = ""
    port = 0

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, OSError)):
            return                       # a tab that closes mid-response is not worth a traceback
        super().handle_error(request, client_address)


def csp(nonce: str) -> str:
    return ("default-src 'none'; img-src 'self' data:; connect-src 'self'; "
            f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


class GatedHandler(BaseHTTPRequestHandler):
    timeout = 20
    server_version = "Copper"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):           # never write to stdout/stderr (stdout may belong to MCP)
        pass

    # ── plumbing ──────────────────────────────────────────────────────────
    def send_body(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code: int = 200):
        self.send_body(code, json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

    def send_page(self, html_for_nonce):
        nonce = secrets.token_urlsafe(12)
        self.send_body(200, html_for_nonce(nonce), "text/html; charset=utf-8", {"Content-Security-Policy": csp(nonce)})

    def gate(self, q: dict) -> bool:
        srv: Server = self.server  # type: ignore[assignment]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in (f"127.0.0.1:{srv.port}", f"localhost:{srv.port}"):
            self.close_connection = True
            self.send_body(403, b"forbidden host", "text/plain")
            return False
        tok = self.headers.get("X-Copper-Token") or (q.get("t") or [""])[0]
        if not hmac.compare_digest(tok.encode("utf-8", "ignore"), srv.token.encode()):
            self.close_connection = True
            self.send_body(403, b"missing or wrong token - open the URL that Copper printed", "text/plain")
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin and origin.lower() not in (
                f"http://127.0.0.1:{srv.port}", f"http://localhost:{srv.port}"):
            self.close_connection = True
            self.send_body(403, b"cross-origin request refused", "text/plain")
            return False
        return True

    # ── dispatch ──────────────────────────────────────────────────────────
    def route_get(self, path: str, q: dict):
        raise HttpError(404, "not found")

    def route_post(self, path: str, body: dict):
        raise HttpError(404, "not found")

    def _run(self, fn, *a):
        try:
            out = fn(*a)
            if out is not None:
                self.send_json(out)
        except HttpError as e:
            self.send_json({"error": e.message, **e.extra}, e.code)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            from .link import redact
            self.send_json({"error": redact.scrub(f"{type(e).__name__}: {getattr(e, 'message', e)}"),
                            **({"fix": e.fix} if getattr(e, "fix", None) else {})}, 500)

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if self.gate(q):
            self._run(self.route_get, u.path, q)

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        if not self.gate(urllib.parse.parse_qs(u.query)):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            return self.send_body(413 if n > 0 else 400, b"bad Content-Length", "text/plain")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send_json({"error": "invalid JSON"}, 400)
        if not isinstance(body, dict):
            return self.send_json({"error": "body must be a JSON object"}, 400)
        self._run(self.route_post, u.path, body)


def make_server(handler_cls, port: int = 0) -> tuple[Server, str]:
    httpd = Server(("127.0.0.1", port), handler_cls)
    httpd.token = secrets.token_urlsafe(24)
    httpd.port = httpd.server_address[1]
    return httpd, httpd.token
