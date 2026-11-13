"""Dependency-free MCP server (stdio, newline-delimited JSON-RPC 2.0).

Implements the parts of the Model Context Protocol Copper needs —
initialize / ping / tools / resources / prompts / logging / progress —
across protocol revisions 2024-11-05 → 2025-11-25. Tool calls run on worker
threads so long jobs (routing, exports) never block pings or cancellation.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from .. import DISPLAY_NAME, SERVER_NAME, __version__
from . import compact, ledger, registry

SUPPORTED = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = """Copper for KiCad: schematic, PCB, parts, checks and the fabrication pack, on KiCad's own files.

New board, short path:
1. project_create(name, template, board_size=[w,h])
2. sch_apply(ops=[...]) with the WHOLE design in one batch. Place parts by spec ("10k 0402"), part number or lib_id;
   connect pins to nets by NAME: {"op":"connect","pins":["U1.PA9","J1.D+"],"net":"USB_DP"}. Never compute coordinates.
3. board_build(width, height): sync, outline, placement, routing, pour and DRC in one call, one report.
   If it returns "pending", call board_build() again.
4. {fab}() writes the fabrication pack.
Existing project: project_open(path), then project_summary.

Results already carry KiCad's ERC/DRC counts, a `fix` per finding and `next`: do not call verify or render
after each step. Pictures are attached only when asked (render, or preview/render=true). Single-step tools
(pcb_place, pcb_route, pcb_pour ...) are for fixing what board_build reports.
Examples for a tool: copper_help(tool=...). Trouble: copper_status.
Assembled parts need the part number the fab profile asks for (default: LCSC); parts placed by spec get it.
If a file is open in KiCad, save and close it first."""


def instructions() -> str:
    """The server instructions."""
    return INSTRUCTIONS.replace("{fab}", "fab_export")


def _project_dir(args) -> str | None:
    """Folder of the project a call worked on (for project-relative paths in the result). No side effects."""
    try:
        from pathlib import Path
        from .. import project as P
        p = args.get("project") if isinstance(args, dict) else None
        if isinstance(p, str) and p.strip():
            q = Path(p).expanduser()
            return str(q if q.is_dir() else q.parent)
        a = P._state().get("active")
        return str(Path(a).parent) if a else None
    except Exception:  # noqa: BLE001
        return None


class Server:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="copper-tool")
        self.protocol = SUPPORTED[0]
        self.client_info: dict = {}
        self.log_level = "info"
        self.cancelled: set = set()

    # ── io ────────────────────────────────────────────────────────────────
    def send(self, msg: dict):
        line = json.dumps(msg, ensure_ascii=False, default=str)
        with self.lock:
            self.stdout.write(line + "\n")
            self.stdout.flush()

    def reply(self, mid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        self.send(msg)

    def notify(self, method: str, params: dict):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def log(self, level: str, message: str, data=None):
        order = ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"]
        if order.index(level) < order.index(self.log_level):
            return
        self.notify("notifications/message", {"level": level, "logger": SERVER_NAME,
                                              "data": data if data is not None else message})

    # ── dispatch ──────────────────────────────────────────────────────────
    def handle(self, msg: dict):
        if not isinstance(msg, dict):
            # JSON-RPC: a non-object request (e.g. `5` or a batch member that is not an object) is an invalid request
            return self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request: not a JSON object"}})
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method is None:
            return  # a response to something we sent; we send no requests
        if not isinstance(method, str) or not isinstance(params, dict):
            if mid is not None:
                self.reply(mid, error={"code": -32600 if not isinstance(method, str) else -32602,
                                       "message": "invalid request: method must be a string and params an object"})
            return
        try:
            if method == "initialize":
                req = params.get("protocolVersion")
                self.protocol = req if req in SUPPORTED else SUPPORTED[0]
                self.client_info = params.get("clientInfo") or {}
                text = instructions()
                led = ledger.current()
                led.set_client(self.client_info)
                led.instructions_bytes = len(text.encode("utf-8"))
                try:
                    from .. import resources as _res  # noqa: F401 — registers resources/prompts
                except Exception:  # noqa: BLE001 — never fail the handshake
                    traceback.print_exc(file=sys.stderr)
                return self.reply(mid, {
                    "protocolVersion": self.protocol,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                        "logging": {},
                    },
                    "serverInfo": {"name": SERVER_NAME, "title": DISPLAY_NAME, "version": __version__,
                                   "websiteUrl": "https://coppereda.com"},
                    "instructions": text,
                })
            if method == "ping":
                return self.reply(mid, {})
            if method.startswith("notifications/"):
                if method == "notifications/cancelled":
                    self.cancelled.add(params.get("requestId"))
                return
            if method == "logging/setLevel":
                self.log_level = params.get("level", "info")
                return self.reply(mid, {})
            if method == "tools/list":
                from .. import tools as _t  # noqa: F401
                listing = {"tools": [t.describe() for t in registry.TOOLS.values()]}
                ledger.current().catalogue_bytes = len(json.dumps(listing, ensure_ascii=False).encode("utf-8"))
                return self.reply(mid, listing)
            if method == "tools/call":
                from .. import tools as _t  # noqa: F401
                self.pool.submit(self._call, mid, params)
                return
            if method in ("resources/list", "resources/templates/list", "resources/read",
                          "prompts/list", "prompts/get", "completion/complete"):
                from .. import resources as res
                try:
                    return self.reply(mid, res.dispatch(method, params))
                except (KeyError, ValueError, TypeError, registry.ToolError) as e:
                    return self.reply(mid, error={"code": -32602, "message": f"invalid params: {getattr(e, 'message', None) or e}"})
            if mid is not None:
                self.reply(mid, error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            if mid is not None:
                self.reply(mid, error={"code": -32603, "message": f"{type(e).__name__}: {e}"})

    def _call(self, mid, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        token = (params.get("_meta") or {}).get("progressToken")

        def progress(fraction: float, message: str = ""):
            if token is not None and mid not in self.cancelled:
                self.notify("notifications/progress", {"progressToken": token, "progress": round(fraction * 100, 1),
                                                       "total": 100, "message": message})
        progress.has_token = token is not None          # tools that time-box themselves read these two
        progress.cancelled = lambda: mid in self.cancelled
        t0 = time.time()
        tool = None
        try:
            # wire-level defaults (image policy, digests) -> the tool -> wire shaping (mcp/compact.py) -> meter
            tool = registry.TOOLS.get(name) if isinstance(name, str) else None
            run_args, defaulted = compact.prepare_args(tool, args)
            result = registry.call_tool(name, run_args, progress)
            result = compact.to_wire(result, _project_dir(args), tool=name, args=args if isinstance(args, dict) else {},
                                     defaulted=defaulted)
        except Exception as e:  # noqa: BLE001
            result = {"content": [{"type": "text", "text": f"internal error: {e}"}], "isError": True}
        try:
            ledger.current().record(name if tool is not None else ledger.UNKNOWN, args, result, time.time() - t0)
        except Exception:  # noqa: BLE001 — the meter must never cost a result
            pass
        if mid in self.cancelled:
            self.cancelled.discard(mid)
            return
        self.reply(mid, result)

    def serve(self):
        for raw in self.stdin:
            raw = raw.strip().lstrip("\ufeff")   # a UTF-8 BOM some Windows clients prepend
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if isinstance(msg, list):
                if not msg:
                    self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request: empty batch"}})
                for m in msg:
                    self.handle(m)
            else:
                self.handle(msg)
        self.pool.shutdown(wait=True)
        ledger.current().flush()


def main():
    # Windows: force UTF-8 and binary-safe newlines on the stdio pipes.
    try:
        sys.stdin.reconfigure(encoding="utf-8", newline="\n")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:  # noqa: BLE001
        pass
    Server().serve()
