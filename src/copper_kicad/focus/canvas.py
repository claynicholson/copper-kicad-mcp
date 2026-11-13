"""Channel A — Copper Canvas: a local, zero-dependency viewer the user points in.

    start(pr, launch=False) -> {'url', 'port', 'reused', 'pid'}     lazy, daemon thread in this process
    serve(project=None, port=0, launch=True)                       blocking CLI entry (`copper canvas`)
    running(pr) -> info | None                                     a canvas that is alive for this project

Security model (all enforced in `_Handler._gate`):
  * binds 127.0.0.1 only, on a random free port;
  * a random 192-bit token is required on EVERY request (``?t=`` or ``X-Copper-Token``), compared in
    constant time; the Host header must be the loopback address we bound (stops DNS rebinding);
    cross-origin POSTs are refused;
  * no filesystem routes: the only files ever read are the project's design files (through KiCad's
    exporters) and this package's three static assets; sheets are addressed by index, layers by a
    whitelist; nothing is executed, no shell;
  * the page loads nothing from the network (CSP ``default-src 'none'``).
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import project as projmod
from ..mcp.registry import ToolError
from . import api, resolver as R, scene as S, store, svg

STATIC = Path(__file__).with_name("static")
_servers: dict = {}
_lock = threading.Lock()
MAX_BODY = 64 * 1024


def page_html(nonce: str) -> bytes:
    """The whole app as one document (CSS + JS inlined) — nothing else is fetched except /api and /svg."""
    html = (STATIC / "canvas.html").read_text(encoding="utf-8")
    css = (STATIC / "canvas.css").read_text(encoding="utf-8")
    js = (STATIC / "canvas.js").read_text(encoding="utf-8")
    return (html.replace("/*__CSS__*/", css).replace("/*__JS__*/", js).replace("__NONCE__", nonce)).encode("utf-8")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A browser tab that closes mid-response is not an error worth a traceback on the MCP server's stderr."""
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, OSError)):
            return
        super().handle_error(request, client_address)


class Canvas:
    def __init__(self, pr, port: int = 0):
        self.pr = pr
        self.token = secrets.token_urlsafe(24)
        self.httpd = _Server(("127.0.0.1", port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.canvas = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.thread: threading.Thread | None = None
        self.started = time.time()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def info(self) -> dict:
        return {"port": self.port, "token": self.token, "pid": os.getpid(), "started": round(self.started, 1),
                "project": str(self.pr.pro)}

    def start_thread(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="copper-canvas", daemon=True)
        self.thread.start()
        store.set_canvas(self.pr, self.info())

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            if store.canvas_info(self.pr).get("token") == self.token:
                store.set_canvas(self.pr, None)

    # ── state the page polls ──────────────────────────────────────────────
    def state(self) -> dict:
        pr = self.pr
        f, seq = store.get(pr)
        docs: dict = {}
        if pr.sch.exists():
            docs["schematic"] = {"stamp": S.stamp(pr, "schematic"),
                                 "sheets": [{"index": e["index"], "name": e["name"], "title": e["title"],
                                             "file": e["file"].name} for e in S.sheets(pr)]}
        if pr.pcb.exists():
            docs["pcb"] = {"stamp": S.stamp(pr, "pcb")}
        return {"project": pr.name, "documents": docs, "seq": seq, "focus": api.public(f), "chip": R.chip(f)}

    def scene(self, document: str, sheet) -> dict:
        sc = S.load(self.pr, document, sheet)
        out = scene_json(sc)
        if document == "schematic":
            out["sheet"] = {"index": sc.sheet["index"], "name": sc.sheet["name"]}
            out["subsheets"] = sc.subsheets
        else:
            layers = svg.canvas_layers(sc.layers)
            have = svg.pcb_svgs(self.pr, layers)
            out["layers"] = [ly for ly in layers if ly in have]
        if sc.warnings:
            out["warnings"] = sc.warnings
        return out

    def svg(self, document: str, sheet, layer) -> bytes:
        if document == "schematic":
            e = S.select_sheet(self.pr, sheet)
            p = svg.schematic_svgs(self.pr).get(e["path"])
            if p is None:
                raise ToolError("no SVG for that sheet", code="NOT_FOUND")
            # kicad-cli rounds the page to its own internal units (viewBox 297.0022 for a 297 mm sheet); the page
            # stretches the image over the scene's view, so pin the viewBox to exactly that view (no µm drift)
            view = S.load(self.pr, "schematic", e["index"]).view
            return svg.with_view(p.read_text(encoding="utf-8", errors="replace"), view).encode("utf-8")
        sc = S.load(self.pr, "pcb")
        layers = svg.canvas_layers(sc.layers)
        if layer not in layers:
            raise ToolError(f"unknown layer {layer!r}", code="BAD_ARGUMENT")
        p = svg.pcb_svgs(self.pr, layers).get(layer)
        if p is None:
            raise ToolError(f"layer {layer} was not plotted", code="NOT_FOUND")
        text = svg.with_view(p.read_text(encoding="utf-8", errors="replace"), sc.view)
        if layer.endswith(".Cu"):
            text = svg.dark_holes(text)
        return text.encode("utf-8")


def scene_json(sc: S.Scene) -> dict:
    """What the page hit-tests against. A pin is [num, name, x, y, net, ix, iy, hw, hh, a] (see canvas.js)."""
    items = []
    for it in sc.items:
        d = {"ref": it["ref"], "value": it.get("value", ""), "bbox": it["bbox"],
             "pins": [[p["num"], p.get("name", ""), p["x"], p["y"], p.get("net", ""),
                       p.get("ix", p["x"]), p.get("iy", p["y"]), p.get("hw", 0), p.get("hh", 0), p.get("a", 0)]
                      for p in it["pins"]]}
        if it.get("side"):
            d["side"] = it["side"]
        if it.get("unit"):
            d["unit"] = it["unit"]
        items.append(d)
    return {"document": sc.document, "view": sc.view, "stamp": sc.stamp, "items": items,
            "radius": R.DEFAULT_RADIUS[sc.document], "pin_tol": R.PIN_TOL_SCH, "pad_grow": R.PAD_GROW}


class _Handler(BaseHTTPRequestHandler):
    timeout = 20                    # a connection that stops talking must not pin a thread forever
    server_version = "CopperCanvas"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # stdout belongs to MCP — never write to it
        pass

    # ── plumbing ──────────────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _gate(self, q: dict) -> bool:
        cv: Canvas = self.server.canvas  # type: ignore[attr-defined]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in (f"127.0.0.1:{cv.port}", f"localhost:{cv.port}"):
            self.close_connection = True
            self._send(403, b"forbidden host", "text/plain")
            return False
        tok = self.headers.get("X-Copper-Token") or (q.get("t") or [""])[0]
        if not hmac.compare_digest(tok.encode("utf-8", "ignore"), cv.token.encode()):
            self.close_connection = True
            self._send(403, b"missing or wrong token - open the URL that focus(action='open') printed", "text/plain")
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin and origin.lower() not in (
                f"http://127.0.0.1:{cv.port}", f"http://localhost:{cv.port}"):
            self.close_connection = True
            self._send(403, b"cross-origin request refused", "text/plain")
            return False
        return True

    def _fail(self, e: Exception):
        if isinstance(e, (ConnectionError, TimeoutError)):       # the peer went away: nobody to answer
            self.close_connection = True
            return
        if isinstance(e, ToolError):
            self._json(e.to_dict(), 400 if e.code in ("BAD_ARGUMENT",) else 409)
        else:
            self._json({"error": "INTERNAL", "message": f"{type(e).__name__}: {e}"}, 500)

    # ── routes ────────────────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._gate(q):
            return
        cv: Canvas = self.server.canvas  # type: ignore[attr-defined]
        one = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
        try:
            if u.path == "/":
                nonce = secrets.token_urlsafe(12)
                csp = ("default-src 'none'; img-src 'self' data:; connect-src 'self'; "
                       f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; "
                       "frame-ancestors 'none'")
                return self._send(200, page_html(nonce), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": csp})
            if u.path == "/api/state":
                return self._json(cv.state())
            if u.path == "/api/scene":
                return self._json(cv.scene(one("doc", "schematic"), one("sheet")))
            if u.path == "/svg":
                return self._send(200, cv.svg(one("doc", "schematic"), one("sheet"), one("layer")),
                                  "image/svg+xml", {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
            self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            self._fail(e)

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._gate(q):
            return
        cv: Canvas = self.server.canvas  # type: ignore[attr-defined]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:           # read(-1) would block until the peer hangs up
                self.close_connection = True
                return self._send(413 if n > 0 else 400, b"bad Content-Length", "text/plain")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ToolError("body must be a JSON object", code="BAD_ARGUMENT")
            if u.path == "/api/focus":
                doc = body.get("document")
                mode = "box" if body.get("mode") == "box" else "point"
                side = body.get("side") if body.get("side") in ("top", "bottom") else None
                f = api.point(cv.pr, doc, sheet=body.get("sheet"), mode=mode, point=body.get("point"),
                              box=body.get("box"), radius=body.get("radius"), add=bool(body.get("add")), side=side,
                              prefer_side=body.get("prefer") if body.get("prefer") in ("top", "bottom") else None)
                return self._json({"focus": api.public(f), "chip": R.chip(f), "seq": store.get(cv.pr)[1]})
            if u.path == "/api/clear":
                return self._json({"focus": None, "chip": "", "seq": api.clear(cv.pr)})
            self._send(404, b"not found", "text/plain")
        except ValueError:
            self._json({"error": "BAD_ARGUMENT", "message": "invalid JSON"}, 400)
        except Exception as e:  # noqa: BLE001
            self._fail(e)


# ── lifecycle ─────────────────────────────────────────────────────────────
def _alive(info: dict) -> bool:
    if not info.get("port") or not info.get("token"):
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{int(info['port'])}/api/state",
                                     headers={"X-Copper-Token": str(info["token"])})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def running(pr) -> dict | None:
    """{'url', 'port', 'pid'} of a live canvas for this project (in this or another process), else None."""
    info = store.canvas_info(pr)
    if info and _alive(info):
        return {"url": f"http://127.0.0.1:{info['port']}/?t={info['token']}", "port": info["port"],
                "pid": info.get("pid")}
    return None


def open_browser(url: str) -> bool:
    if os.environ.get("COPPER_NO_BROWSER"):
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001
        return False


def start(pr, launch: bool = False, port: int = 0) -> dict:
    """Make sure a canvas serves `pr` and return {'url', 'port', 'pid', 'reused', 'launched'}.
    Re-uses a canvas that is already alive (even in another process, e.g. `copper canvas`)."""
    if not pr.sch.exists() and not pr.pcb.exists():
        raise ToolError(f"{pr.name} has no schematic or board to show", code="NO_DESIGN",
                        fix="project_open(path=...) an existing design, or create one with sch_apply first")
    with _lock:
        live = running(pr)
        if live:
            out = dict(live, reused=True)
        else:
            cv = Canvas(pr, port)
            cv.start_thread()
            _servers[store.key(pr)] = cv
            out = {"url": cv.url, "port": cv.port, "pid": os.getpid(), "reused": False}
    out["launched"] = open_browser(out["url"]) if launch else False
    return out


def stop(pr) -> bool:
    with _lock:
        cv = _servers.pop(store.key(pr), None)
    if cv:
        cv.stop()
    return bool(cv)


def serve(project: str | None = None, port: int = 0, launch: bool = True, out=None) -> int:
    """Blocking entry point for the CLI: `copper canvas [project] [--port N] [--no-open]`.
    Prints the URL (to stderr-safe `out`, default stdout) and serves until Ctrl-C."""
    import sys
    out = out or sys.stdout
    try:
        pr = projmod.resolve(project)
        info = start(pr, launch=launch, port=port)
    except ToolError as e:
        print(f"copper canvas: {e.message}" + (f"\n  fix: {e.fix}" if e.fix else ""), file=sys.stderr)
        return 1
    print(f"Copper Canvas for {pr.name}\n  {info['url']}\n"
          f"  click a part = point at it · drag = box · shift = add · Esc = clear\n"
          f"  your AI reads it with the `focus` tool. Ctrl-C to stop.", file=out, flush=True)
    if info.get("reused"):
        print("  (already running in another process — nothing more to do)", file=out, flush=True)
        return 0
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stop(pr)
    return 0
