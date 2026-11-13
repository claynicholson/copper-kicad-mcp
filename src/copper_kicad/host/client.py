"""Engine-side client for the live host (the app). docs/internal/HOST_PROTOCOL.md section 5 is the authority.

    class Host                      the interface: one method per ``host.*`` request
    class HostClient(Host)          the same calls over any Transport
    class HostError(Exception)      .code (STALE_BASE, BUSY, PARSE, ...), .message, .data
    connect(project_dir, name)      -> HostClient | None, through the rendezvous file (for child processes)

Two transports exist:
  * in-process: copper-agent's own stdio peer (``agent.server.AgentServer`` implements ``Transport``);
  * loopback:   ``LoopbackTransport``, a token-authenticated TCP line protocol to ``host/relay.py`` inside
                copper-agent, used by an engine that runs as a grandchild (an MCP server started by an agent CLI).

All coordinates are millimetres. ``rev`` is an integer the host increments on every change of a document.
"""
from __future__ import annotations

import json
import socket
import threading

DOCUMENTS = ("schematic", "board")
HOST_METHODS = ("host.checkout", "host.apply", "host.reveal", "host.checkpoint", "host.save", "host.reload",
                "host.verify", "host.ask_permission")

# Seconds an engine waits for the app to answer. ask_permission waits for a human.
TIMEOUTS = {"host.checkout": 60, "host.apply": 120, "host.reveal": 15, "host.checkpoint": 60, "host.save": 120,
            "host.reload": 120, "host.verify": 300, "host.ask_permission": 900}

# Error codes a host may answer with (JSON-RPC error.data.code), plus the ones this side synthesises.
E_STALE_BASE = "STALE_BASE"      # base_rev is not the document's current rev: check out again
E_BUSY = "BUSY"                  # the editor is in a modal state / mid-tool / in review: retry later
E_PARSE = "PARSE"                # the patch's s-expression did not parse; nothing was applied
E_UNKNOWN_ITEM = "UNKNOWN_ITEM"  # a uuid in `remove` is not in the document; nothing was applied
E_NO_DOCUMENT = "NO_DOCUMENT"    # that document is not open in the app
E_DENIED = "DENIED"              # the user said no
E_UNSUPPORTED = "UNSUPPORTED"    # this host build does not implement the method
E_INTERNAL = "INTERNAL"
E_TIMEOUT = "TIMEOUT"            # synthesised: no answer in time
E_HOST_GONE = "HOST_GONE"        # synthesised: the app closed the channel
E_CANCELLED = "CANCELLED"        # synthesised: the turn was cancelled while waiting
E_NO_HOST = "NO_HOST"            # synthesised: no live host for this project


class HostError(Exception):
    """A host call failed. ``code`` is one of the E_* strings above (or a host-specific one)."""

    def __init__(self, code: str, message: str = "", data: dict | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message or code
        self.data = dict(data or {})

    @classmethod
    def from_rpc(cls, error: dict) -> "HostError":
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        return cls(str(data.get("code") or f"RPC{error.get('code')}"), str(error.get("message") or ""), data)


class Transport:
    """What a HostClient needs from its channel."""

    def request(self, method: str, params: dict, timeout: float | None = None, turn: str | None = None) -> dict:
        """Send one request and block for its result. Raises HostError (incl. TIMEOUT, HOST_GONE, CANCELLED)."""
        raise NotImplementedError


class Host:
    """The live document's owner. Every method blocks until the app answered and raises HostError on refusal."""

    def checkout(self, document: str = "schematic") -> dict:
        """Write the LIVE document (unsaved edits included) to a shadow project folder.
        -> {'shadow_dir': str, 'rev': int, 'document': str, 'sheets': [{'sheet_path': '/', 'file': 'x.kicad_sch'}],
            'project': 'name', 'dirty': bool}. The folder is a complete project the engine can open; the host
        never reads it back."""
        raise NotImplementedError

    def apply(self, *, base_rev: int, document: str, patches: list[dict], lib_symbols: list[str] | None = None,
              files: list[dict] | None = None, summary: str = "", expect: dict | None = None) -> dict:
        """Land one change in the live document as ONE undoable commit.
        patches: [{'sheet_path', 'file', 'remove': [uuid], 'add': '<s-expr items>'}].
        -> {'proposal_id', 'state': 'review'|'applied', 'rev', 'counts': {'added','changed','removed'},
            'erc_new': [finding], 'netlist_ok': bool|None}. Errors: STALE_BASE, BUSY, PARSE, UNKNOWN_ITEM.
        All or nothing."""
        raise NotImplementedError

    def reveal(self, *, refs: list[str] | None = None, uuids: list[str] | None = None, net: str | None = None,
               box: list[float] | None = None, document: str | None = None) -> dict:
        """Select / centre / highlight. -> {}"""
        raise NotImplementedError

    def checkpoint(self, label: str, document: str | None = None) -> dict:
        """Take a restorable snapshot of the project ("Before Copper: <label>"). -> {'id': str}"""
        raise NotImplementedError

    def save(self, document: str) -> dict:
        """Save the live document to its file. -> {'rev': int}"""
        raise NotImplementedError

    def reload(self, document: str) -> dict:
        """Re-read the document from its file without prompting (undo history is lost). -> {'rev': int}"""
        raise NotImplementedError

    def verify(self, document: str, baseline: str | None = None) -> dict:
        """Run ERC (schematic) / DRC (board) on the live document.
        -> {'document', 'findings': [finding], 'counts': {'errors','warnings'}, 'new': [finding]?, 'baseline': str}"""
        raise NotImplementedError

    def ask_permission(self, kind: str, detail: dict) -> dict:
        """Ask the user. -> {'granted': bool, 'remember': bool}"""
        raise NotImplementedError


class HostClient(Host):
    """``Host`` over a ``Transport``. ``methods`` is the host's advertised list (hello.host_methods); a call to a
    method the host did not list fails locally with UNSUPPORTED."""

    def __init__(self, transport: Transport, methods=None, turn: str | None = None, timeouts: dict | None = None):
        self.transport = transport
        self.methods = tuple(methods) if methods else HOST_METHODS
        self.turn = turn
        self.timeouts = dict(TIMEOUTS, **(timeouts or {}))

    def for_turn(self, turn: str | None) -> "HostClient":
        """The same client, with its requests tied to a turn (so turn.cancel can abandon them)."""
        return HostClient(self.transport, self.methods, turn, self.timeouts)

    def call(self, method: str, params: dict) -> dict:
        if method not in self.methods:
            raise HostError(E_UNSUPPORTED, f"this host does not implement {method}")
        res = self.transport.request(method, {k: v for k, v in (params or {}).items() if v is not None},
                                     timeout=self.timeouts.get(method, 60), turn=self.turn)
        return res if isinstance(res, dict) else {}

    def checkout(self, document="schematic"):
        _doc(document)
        res = self.call("host.checkout", {"document": document})
        if not isinstance(res.get("shadow_dir"), str) or not isinstance(res.get("rev"), int):
            raise HostError(E_INTERNAL, "host.checkout answered without shadow_dir / rev", {"result": res})
        res.setdefault("document", document)
        res.setdefault("sheets", [])
        return res

    def apply(self, *, base_rev, document, patches, lib_symbols=None, files=None, summary="", expect=None):
        _doc(document)
        return self.call("host.apply", {"base_rev": base_rev, "document": document, "patches": list(patches),
                                        "lib_symbols": list(lib_symbols or []), "files": list(files or []) or None,
                                        "summary": summary, "expect": expect})

    def reveal(self, *, refs=None, uuids=None, net=None, box=None, document=None):
        return self.call("host.reveal", {"refs": refs, "uuids": uuids, "net": net, "box": box, "document": document})

    def checkpoint(self, label, document=None):
        return self.call("host.checkpoint", {"label": label, "document": document})

    def save(self, document):
        return self.call("host.save", {"document": _doc(document)})

    def reload(self, document):
        return self.call("host.reload", {"document": _doc(document)})

    def verify(self, document, baseline=None):
        return self.call("host.verify", {"document": _doc(document), "baseline": baseline})

    def ask_permission(self, kind, detail):
        return self.call("host.ask_permission", {"kind": kind, "detail": detail or {}})


def _doc(document: str) -> str:
    if document not in DOCUMENTS:
        raise HostError("BAD_ARGUMENT", f"document must be one of {DOCUMENTS}, got {document!r}")
    return document


class LoopbackTransport(Transport):
    """JSON-RPC lines over a loopback TCP socket to host/relay.py. The first request is ``auth {token}``."""

    def __init__(self, port: int, token: str, connect_timeout: float = 5.0):
        self.lock = threading.Lock()
        self.next_id = 0
        try:
            self.sock = socket.create_connection(("127.0.0.1", int(port)), timeout=connect_timeout)
        except OSError as e:
            raise HostError(E_NO_HOST, f"cannot reach the live host on port {port}: {e}") from None
        self.rfile = self.sock.makefile("rb")
        self.request("auth", {"token": token}, timeout=connect_timeout)

    def request(self, method, params, timeout=None, turn=None):
        with self.lock:                      # one call at a time: the reply is the next line with our id
            self.next_id += 1
            mid = self.next_id
            try:
                self.sock.settimeout(timeout)
                self.sock.sendall((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params},
                                              ensure_ascii=False) + "\n").encode("utf-8"))
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        raise HostError(E_HOST_GONE if method != "auth" else E_NO_HOST,
                                        "the live host closed the connection")
                    msg = json.loads(raw.decode("utf-8"))
                    if isinstance(msg, dict) and msg.get("id") == mid:
                        break
            except socket.timeout:
                raise HostError(E_TIMEOUT, f"{method}: no answer from the live host in {timeout:.0f} s") from None
            except (OSError, ValueError) as e:
                raise HostError(E_HOST_GONE, f"{method}: {e}") from None
        if msg.get("error"):
            raise HostError.from_rpc(msg["error"])
        return msg.get("result") or {}

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def connect(project_dir, name: str | None = None) -> HostClient | None:
    """The live host of a project, or None when no app has it open (or the rendezvous entry is stale).

    Order: the environment the agent gave its child (COPPER_HOST_PORT + COPPER_HOST_TOKEN), then the
    rendezvous file. This is the hook for engines that run outside copper-agent (HOST_PROTOCOL.md section 9)."""
    import os
    from . import rendezvous
    port, token = os.environ.get("COPPER_HOST_PORT"), os.environ.get("COPPER_HOST_TOKEN")
    if port and port.isdigit() and token:
        info = {"port": int(port), "token": token}
    else:
        info = rendezvous.lookup(project_dir, name)
    if not info:
        return None
    try:
        return HostClient(LoopbackTransport(info["port"], info["token"]), info.get("methods") or None)
    except HostError:
        return None
