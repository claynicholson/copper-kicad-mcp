"""Tool gate: how an agent CLI's tool calls come back into copper-agent. Not a driver: it registers nothing.

    app <== stdio ==> copper-agent  <== 127.0.0.1:<port> + token ==>  _bridge.py (MCP stdio server)  <== stdio ==> the CLI

A CLI driver opens one ToolGate per turn and gives the CLI exactly one MCP server: ``python _bridge.py <gate file>``.
The bridge is a thin relay with no engine of its own; every ``tools/call`` it receives is run HERE through
``turn.call_tool``, so a CLI-driven edit takes the same road as an API-driven one (tool events, shadow checkout,
uuid patch, ``host.apply``, cancel) and there is one engine and one checkout state per app, not two.

    gate = ToolGate(turn).start()
    gate.server_argv() -> [python, bridge.py, gate_file]      the MCP server command to hand to the CLI
    gate.calls                                                 number of tool calls served
    gate.close()

Wire: newline-delimited JSON-RPC on a loopback socket. The first request must be ``auth {token}``; a wrong token
closes the connection without an answer. Then:

    tools.list {}                        -> {"tools": [{"name","description","inputSchema"}], "instructions": str}
    tools.call {"name","arguments"}      -> {"content": [{"type":"text","text"} | {"type":"image","data","mimeType"}],
                                             "isError": bool}

The port and token are written to a file in a private temp folder (never on a command line, never in an
environment that other programs inherit) and the file is removed when the turn ends. Calls are served one at a
time: the engine has process-wide state.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

from .. import prompt
from .base import Cancelled

SERVER_NAME = "copper"
BRIDGE = Path(__file__).with_name("_bridge.py")

INSTRUCTIONS = ("These tools edit the design that is open in the Copper app. Use only these tools: every change must "
                "go through them so the user gets one undoable change with a Keep / Undo bar.")


class ToolGate:
    def __init__(self, turn, mode: str | None = None):
        self.turn = turn
        self.mode = mode or turn.request.mode
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.calls = 0
        self.connected = threading.Event()
        self._lock = threading.Lock()
        self._srv = None
        self.dir: Path | None = None
        self.file: Path | None = None

    def start(self) -> "ToolGate":
        gate = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    self.serve()
                except OSError:
                    pass                               # the CLI (and its bridge) was killed: nothing to answer

            def serve(self):
                authed = False
                for raw in self.rfile:
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
                    except (ValueError, AttributeError):
                        return
                    if not authed:
                        if method != "auth" or not hmac.compare_digest(str(params.get("token") or ""), gate.token):
                            return
                        authed = True
                        gate.connected.set()
                        out = {"result": {}}
                    else:
                        out = gate.dispatch(method, params)
                    try:
                        self.wfile.write((json.dumps(dict({"jsonrpc": "2.0", "id": mid}, **out), ensure_ascii=False)
                                          + "\n").encode("utf-8"))
                        self.wfile.flush()
                    except OSError:
                        return

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._srv = Server(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, name="copper-toolgate", daemon=True).start()
        self.dir = Path(tempfile.mkdtemp(prefix="copper-gate-"))
        self.file = self.dir / "gate.json"
        fd = os.open(str(self.file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"port": self.port, "token": self.token}, f)
        return self

    def server_argv(self) -> list[str]:
        return [sys.executable, str(BRIDGE), str(self.file)]

    def tools(self) -> list[dict]:
        return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                for t in prompt.tool_catalogue(self.mode)]

    def dispatch(self, method, params) -> dict:
        if method == "tools.list":
            return {"result": {"tools": self.tools(), "instructions": INSTRUCTIONS}}
        if method != "tools.call":
            return {"error": {"code": -32601, "message": f"method not found: {method}"}}
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or (args is not None and not isinstance(args, dict)):
            return {"error": {"code": -32602, "message": "tools.call needs name (string) and arguments (object)"}}
        with self._lock:
            if self.turn.cancelled.is_set() or self.turn.finished:
                return {"result": _text("the turn was cancelled; nothing was run", True)}
            self.calls += 1
            try:
                out = self.turn.call_tool(name, args or {})
            except Cancelled:
                return {"result": _text("the turn was cancelled; nothing was applied", True)}
            except Exception as e:  # noqa: BLE001 — the CLI must get an answer, the turn's driver reports the rest
                return {"result": _text(f"internal error: {type(e).__name__}: {e}", True)}
        content = [{"type": "text", "text": out["text"] or ("ok" if out["ok"] else "failed")}]
        for img in out.get("images") or []:
            if img.get("data"):
                content.append({"type": "image", "data": img["data"], "mimeType": img.get("mime") or "image/png"})
        return {"result": {"content": content, "isError": not out["ok"]}}

    def close(self) -> None:
        if self._srv is not None:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except OSError:
                pass
            self._srv = None
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


def _text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}
