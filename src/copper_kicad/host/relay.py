"""Loopback relay: lets an engine in ANOTHER process reach the live host through copper-agent.

    app  <== stdio ==>  copper-agent  <== 127.0.0.1:<port> + token ==>  engine in a child / external process

copper-agent starts one Relay per project when a driver needs it (an agent CLI that spawns Copper's MCP server)
and publishes it in the rendezvous file. The wire is the same newline-delimited JSON-RPC as the stdio channel,
restricted to ``auth`` and the ``host.*`` requests; the first request on a connection must be

    {"jsonrpc":"2.0","id":1,"method":"auth","params":{"token":"<token>"}}

Bound to 127.0.0.1 on a random port; a wrong or missing token closes the connection without an answer.

    Relay(host, project_dir, name=None).start() -> {'port', 'token'}     .env() -> {'COPPER_HOST_PORT', ...}
    .stop()
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socketserver
import threading

from . import rendezvous
from .client import HOST_METHODS, Host, HostError


class Relay:
    def __init__(self, host: Host, project_dir=None, name: str | None = None):
        self.host = host
        self.project_dir = project_dir
        self.name = name
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self._srv = None

    def start(self) -> dict:
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                authed = False
                for raw in self.rfile:
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
                    except (ValueError, AttributeError):
                        return
                    if not authed:
                        if method != "auth" or not hmac.compare_digest(str(params.get("token") or ""), relay.token):
                            return                                    # close without a word
                        authed = True
                        self._send({"jsonrpc": "2.0", "id": mid, "result": {}})
                        continue
                    self._send(dict({"jsonrpc": "2.0", "id": mid}, **relay.dispatch(method, params)))

            def _send(self, msg):
                self.wfile.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._srv = Server(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, name="copper-relay", daemon=True).start()
        if self.project_dir:
            rendezvous.publish(self.project_dir, self.name, port=self.port, token=self.token,
                               methods=getattr(self.host, "methods", None))
        return {"port": self.port, "token": self.token}

    def dispatch(self, method, params) -> dict:
        if method not in HOST_METHODS:
            return {"error": {"code": -32601, "message": f"method not found: {method}"}}
        try:
            call = getattr(self.host, "call", None)
            if call is not None:
                return {"result": call(method, params)}
            return {"result": getattr(self.host, method.split(".", 1)[1])(**params)}
        except HostError as e:
            return {"error": {"code": -32000, "message": e.message, "data": dict(e.data, code=e.code)}}
        except Exception as e:  # noqa: BLE001
            return {"error": {"code": -32603, "message": f"{type(e).__name__}: {e}", "data": {"code": "INTERNAL"}}}

    def env(self) -> dict:
        """Environment for a child process whose engine should find this host without the rendezvous file."""
        env = {"COPPER_HOST_PORT": str(self.port), "COPPER_HOST_TOKEN": self.token}
        if self.project_dir:
            env["COPPER_HOST_PROJECT"] = str(self.project_dir)        # host/route.registered_for
        return env

    def stop(self):
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        if self.project_dir:
            rendezvous.clear(self.project_dir, self.name, pid=os.getpid())
