"""The MCP server an agent CLI is given while it works for the Copper app: a relay with no engine of its own.

    python _bridge.py <gate file>

Runs as a plain script (standard library only, imports nothing from Copper) as the CLI's child. It speaks MCP over
stdio (newline-delimited JSON-RPC: initialize, ping, tools/list, tools/call) and forwards the two tool methods to
copper-agent's tool gate (_gate.py) over a loopback socket, authenticated with the token in the gate file.
The tools therefore run inside the agent that the app started: on the live document, as one undoable change.

stdout carries protocol lines only; diagnostics go to stderr. When the gate is gone (the turn ended, the app
closed) every call answers with a tool error instead of hanging, and the relay exits when its stdin closes.
"""
from __future__ import annotations

import json
import socket
import sys

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
NAME = "copper"


class Gate:
    def __init__(self, path: str):
        self.path = path
        self.sock = None
        self.rfile = None
        self.next_id = 0

    def _connect(self):
        with open(self.path, encoding="utf-8") as f:
            info = json.load(f)
        self.sock = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=10)
        self.sock.settimeout(None)                     # a tool may run for many minutes
        self.rfile = self.sock.makefile("rb")
        self.call("auth", {"token": info["token"]}, connect=False)

    def call(self, method: str, params: dict, connect: bool = True) -> dict:
        if self.sock is None and connect:
            self._connect()
        self.next_id += 1
        mid = self.next_id
        self.sock.sendall((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params},
                                      ensure_ascii=False) + "\n").encode("utf-8"))
        while True:
            raw = self.rfile.readline()
            if not raw:
                raise OSError("the Copper app closed the connection")
            msg = json.loads(raw.decode("utf-8"))
            if isinstance(msg, dict) and msg.get("id") == mid:
                if msg.get("error"):
                    raise RuntimeError(str((msg["error"] or {}).get("message") or "refused"))
                return msg.get("result") or {}

    def safe(self, method: str, params: dict):
        """-> (result | None, problem | None). A broken connection is dropped so the next call reconnects."""
        try:
            return self.call(method, params), None
        except RuntimeError as e:
            return None, str(e)
        except (OSError, ValueError, KeyError) as e:
            try:
                if self.sock is not None:
                    self.sock.close()
            except OSError:
                pass
            self.sock = self.rfile = None
            return None, f"Copper is not reachable ({type(e).__name__}); the turn may have ended"


def serve(gate: Gate, rfile, wfile) -> int:
    def send(msg: dict) -> None:
        wfile.write((json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        wfile.flush()

    for raw in iter(rfile.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            continue                                   # answers to requests we never make
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            continue                                   # notifications (initialized, cancelled, ...)
        if method == "initialize":
            want = params.get("protocolVersion")
            result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": NAME, "title": "Copper", "version": "1"}}
            listed, _ = gate.safe("tools.list", {})
            if listed and listed.get("instructions"):
                result["instructions"] = listed["instructions"]
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            listed, problem = gate.safe("tools.list", {})
            if listed is None:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": problem}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": listed.get("tools") or []}})
        elif method == "tools/call":
            res, problem = gate.safe("tools.call", {"name": params.get("name"), "arguments": params.get("arguments") or {}})
            if res is None:
                res = {"content": [{"type": "text", "text": problem}], "isError": True}
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
    return 0


def main(argv) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: _bridge.py <gate file>\n")
        return 2
    return serve(Gate(argv[1]), sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
