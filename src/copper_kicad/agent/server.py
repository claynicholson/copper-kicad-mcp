"""copper-agent's stdio peer: newline-delimited JSON-RPC 2.0, both directions (docs/internal/HOST_PROTOCOL.md).

    stdin   requests and notifications from the app, and its answers to our host.* requests
    stdout  our answers, our host.* requests, and `event` notifications          (nothing else, ever)
    stderr  the log

Threads:
    main        reads stdin, answers cheap requests inline, resolves pending host requests
    turn        one per active turn (v1: one at a time); runs the driver or the command
    watchdog    ends turns that went silent, enforces the cancel grace period
    worker      short-lived, for slow non-turn requests (accounts.test)

Guarantees (tests/test_agent_protocol.py):
  * every accepted turn ends with exactly one `done` event, whatever the driver does;
  * a line that is not JSON gets a -32700 answer and the agent keeps running;
  * stdin EOF fails pending host requests (HOST_GONE), ends open turns with error(kind=protocol) + done, exits 0;
  * nothing but protocol lines reaches stdout: fd 1 is re-pointed at stderr for the engine and its children.

Environment (seconds unless noted):
    COPPER_AGENT_IDLE_S          60    a turn with no event and no running tool or host call for this long is ended
    COPPER_AGENT_TOOL_S          1800  the same while an engine tool runs (tool_progress counts as activity)
    COPPER_AGENT_TURN_S          3600  hard limit for one turn
    COPPER_AGENT_CANCEL_GRACE_S  2     after turn.cancel, how long the driver has to stop by itself
    COPPER_AGENT_HOST_S                upper bound for every wait on a host.* answer (default: per method, 15-900)
    COPPER_AGENT_LOG             info  debug | info | warning | error  (stderr; never contains params or secrets)
    COPPER_AGENT_PLUGINS               os.pathsep-separated .py files imported at start (test / development drivers)
"""
from __future__ import annotations

import importlib.util
import json
import os
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path

from .. import __version__
from ..host.client import E_CANCELLED, E_HOST_GONE, E_TIMEOUT, HOST_METHODS, HostClient, HostError, Transport
from ..host.live import LiveSession
from . import accounts, commands, drivers, events, sessions, usage
from .drivers.base import Cancelled, DriverError, TurnRequest
from .turn import Turn

MODES = ("chat", "quick", "agent")
E_APP = -32000          # every application error; the string in error.data.code says which


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def app_error(code: str, message: str, **data) -> RpcError:
    return RpcError(E_APP, message, dict(data, code=code))


def bad_params(message: str) -> RpcError:
    return RpcError(-32602, f"invalid params: {message}", {"code": "INVALID_PARAMS"})


def _env_s(name: str, default: float) -> float:
    try:
        return max(0.05, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _host_timeouts() -> dict:
    """COPPER_AGENT_HOST_S caps every host.* wait (tests; a slow machine may raise the defaults per method)."""
    cap = os.environ.get("COPPER_AGENT_HOST_S")
    if not cap:
        return {}
    from ..host.client import TIMEOUTS
    return {m: min(t, _env_s("COPPER_AGENT_HOST_S", t)) for m, t in TIMEOUTS.items()}


class AgentServer(Transport):
    def __init__(self, rfile, wfile, logfile=None):
        self.rfile, self.wfile, self.logfile = rfile, wfile, logfile or sys.stderr
        self.wlock = threading.Lock()
        self.lock = threading.RLock()
        self.log_level = {"debug": 0, "info": 1, "warning": 2, "error": 3}.get(
            (os.environ.get("COPPER_AGENT_LOG") or "info").lower(), 1)
        self.ready = False
        self.closed = False
        self.stop = False
        self.protocol = events.PROTOCOL
        self.hello: dict = {}
        self.project_dir: str | None = None
        self.sessions = sessions.Store(None)
        self.host = HostClient(self, timeouts=_host_timeouts())
        self.live = LiveSession(self.host, None)
        self.turns: dict = {}                    # active turns by id
        self.recent: dict = {}                   # ended turn id -> status (so a late cancel is not an error)
        self.pending: dict = {}                  # our request id -> {'event','result','error','turn','method'}
        self.next_id = 0
        self.focus: dict | None = None
        self.notes: dict = {}                    # conversation -> [str]
        self.proposals: dict = {}                # proposal_id -> conversation
        self.relay = None
        self.idle_s = _env_s("COPPER_AGENT_IDLE_S", 60)
        self.tool_s = _env_s("COPPER_AGENT_TOOL_S", 1800)
        self.turn_s = _env_s("COPPER_AGENT_TURN_S", 3600)
        self.grace_s = _env_s("COPPER_AGENT_CANCEL_GRACE_S", 2)

    # ── log (stderr only) ─────────────────────────────────────────────────
    def log(self, level: str, message: str) -> None:
        if {"debug": 0, "info": 1, "warning": 2, "error": 3}.get(level, 1) < self.log_level:
            return
        try:
            self.logfile.write(f"{time.strftime('%H:%M:%S')} {level:<7} {message}\n")
            self.logfile.flush()
        except (OSError, ValueError):
            pass

    # ── output ────────────────────────────────────────────────────────────
    def send(self, msg: dict) -> bool:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with self.wlock:
            try:
                self.wfile.write(data)
                self.wfile.flush()
                return True
            except (OSError, ValueError):
                self.closed = True
                return False

    def reply(self, mid, result=None) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "result": result if result is not None else {}})

    def reply_error(self, mid, code: int, message: str, data: dict | None = None) -> None:
        err = {"code": code, "message": message}
        if data:
            err["data"] = data
        self.send({"jsonrpc": "2.0", "id": mid, "error": err})

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    # ── our requests to the app (Transport) ───────────────────────────────
    def request(self, method: str, params: dict, timeout: float | None = None, turn: str | None = None) -> dict:
        t = self.turns.get(turn) if turn else None
        if turn and (t is None or t.cancelled.is_set() or t.finished):
            raise Cancelled()                                    # an ended turn may not touch the document
        if self.closed:
            raise HostError(E_HOST_GONE, "the app closed the channel")
        slot = {"event": threading.Event(), "result": None, "error": None, "turn": turn, "method": method}
        with self.lock:
            self.next_id += 1
            rid = f"a{self.next_id}"
            self.pending[rid] = slot
        if t is not None:
            t.waiting_host += 1
        try:
            self.log("debug", f"-> {method} id={rid}")
            if not self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
                raise HostError(E_HOST_GONE, "the app closed the channel")
            if not slot["event"].wait(timeout):
                self.notify("host.cancelled", {"id": rid, "reason": "timeout"})
                raise HostError(E_TIMEOUT, f"{method}: no answer from the app in {timeout:.0f} s")
        finally:
            with self.lock:
                self.pending.pop(rid, None)
            if t is not None:
                t.waiting_host -= 1
                t.touch()
        err = slot["error"]
        if err is not None:
            if err.get("data", {}).get("code") == E_CANCELLED and err.get("synthetic"):
                raise Cancelled()
            raise HostError.from_rpc(err)
        return slot["result"] if isinstance(slot["result"], dict) else {}

    def _fail_pending(self, code: str, message: str, turn: str | None = None, tell_app: bool = False) -> None:
        with self.lock:
            slots = [(rid, s) for rid, s in self.pending.items() if turn is None or s["turn"] == turn]
        for rid, s in slots:
            s["error"] = {"code": E_APP, "message": message, "data": {"code": code}, "synthetic": True}
            s["event"].set()
            if tell_app:
                self.notify("host.cancelled", {"id": rid, "reason": "turn_cancelled"})

    # ── input ─────────────────────────────────────────────────────────────
    def serve(self) -> int:
        threading.Thread(target=self._watchdog, name="copper-watchdog", daemon=True).start()
        self.log("info", f"copper-agent {__version__} protocol {events.PROTOCOL} pid {os.getpid()}")
        while not self.stop:
            try:
                raw = self.rfile.readline()
            except (OSError, ValueError):
                raw = b""
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip().lstrip("﻿")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.log("warning", f"unparseable line ({len(raw)} bytes)")
                self.reply_error(None, -32700, "parse error: the line is not JSON", {"code": "PARSE_ERROR"})
                continue
            try:
                self.handle(msg)
            except Exception as e:  # noqa: BLE001 — the reader must survive anything
                self.log("error", "handler crashed: " + traceback.format_exc(limit=4))
                if isinstance(msg, dict) and msg.get("id") is not None and "method" in msg:
                    self.reply_error(msg["id"], -32603, f"internal error: {type(e).__name__}: {e}", {"code": "INTERNAL"})
        return self._shutdown()

    def _shutdown(self) -> int:
        """stdin reached EOF (or `shutdown`): nobody is listening any more. End everything, lose nothing on disk."""
        eof = not self.stop
        self.closed = self.closed or eof
        self._fail_pending(E_HOST_GONE, "the app closed the channel")
        turns = list(self.turns.values())
        gone = {"kind": "protocol", "code": E_HOST_GONE, "retryable": False,
                "message": "the app closed the channel before the turn ended"}
        for t in turns:
            t.abort_error = gone
            t.cancelled.set()
        end = time.monotonic() + min(self.grace_s, 2.0)
        for t in turns:
            if t.thread is not None:
                t.thread.join(max(0.0, end - time.monotonic()))
        for t in turns:
            self._finish(t, "error", gone)
        if self.relay is not None:
            try:
                self.relay.stop()
            except Exception:  # noqa: BLE001
                pass
        self.log("info", "stdin closed, exiting" if eof else "shutdown requested, exiting")
        return 0

    def handle(self, msg) -> None:
        if not isinstance(msg, dict):
            return self.reply_error(None, -32600, "invalid request: not a JSON object (batches are not supported)",
                                    {"code": "INVALID_REQUEST"})
        mid, method = msg.get("id"), msg.get("method")
        if method is None:                                         # an answer to one of our requests
            if "result" not in msg and "error" not in msg:
                return self.reply_error(mid, -32600, "invalid request: no method, result or error",
                                        {"code": "INVALID_REQUEST"})
            slot = self.pending.get(mid) if isinstance(mid, str) else None
            if slot is None:
                return self.log("debug", f"answer for unknown or abandoned request {mid!r}")
            if msg.get("error") is not None:
                err = msg["error"] if isinstance(msg["error"], dict) else {"code": -32603, "message": str(msg["error"])}
                err.pop("synthetic", None)
                slot["error"] = err
            else:
                slot["result"] = msg.get("result")
            slot["event"].set()
            return None
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(method, str) or not isinstance(params, dict) \
                or (mid is not None and (isinstance(mid, bool) or not isinstance(mid, (str, int)))):
            if "id" in msg:
                self.reply_error(mid if isinstance(mid, (str, int)) and not isinstance(mid, bool) else None, -32600,
                                 "invalid request: method must be a string, params an object, id a string or integer",
                                 {"code": "INVALID_REQUEST"})
            return None
        self.log("debug", f"<- {method} id={mid!r}")
        if "id" not in msg:                                        # a notification: never answered
            fn = self.NOTIFICATIONS.get(method)
            if fn is not None:
                try:
                    fn(self, params)
                except Exception as e:  # noqa: BLE001
                    self.log("warning", f"notification {method} failed: {type(e).__name__}: {e}")
            return None
        fn = self.METHODS.get(method)
        if fn is None:
            return self.reply_error(mid, -32601, f"method not found: {method}", {"code": "METHOD_NOT_FOUND"})
        if not self.ready and method not in ("hello", "ping", "shutdown"):
            return self.reply_error(mid, E_APP, "send hello first", {"code": "NOT_READY"})
        try:
            result = fn(self, params, mid)
        except RpcError as e:
            return self.reply_error(mid, e.code, e.message, e.data)
        if result is not _DEFERRED:
            self.reply(mid, result)
        return None

    # ── app -> agent: requests ────────────────────────────────────────────
    def m_hello(self, p, mid):
        proto = p.get("protocol")
        if not isinstance(proto, int) or isinstance(proto, bool):
            raise bad_params("protocol (integer) is required")
        if proto < events.PROTOCOL_MIN:
            raise app_error("UNSUPPORTED_PROTOCOL", f"protocol {proto} is older than this agent accepts",
                            min=events.PROTOCOL_MIN, max=events.PROTOCOL)
        if self.turns:
            raise app_error("BUSY", "a turn is running; hello again after it ended")
        pd = p.get("project_dir")
        if pd is not None and (not isinstance(pd, str) or not Path(pd).is_dir()):
            raise bad_params("project_dir must be an existing folder or null")
        methods = p.get("host_methods")
        if methods is not None and (not isinstance(methods, list) or any(m not in HOST_METHODS for m in methods)):
            raise bad_params(f"host_methods must be a list out of {list(HOST_METHODS)}")
        cli = p.get("kicad_cli")
        if isinstance(cli, str) and cli and Path(cli).is_file():
            os.environ["COPPER_KICAD_CLI"] = cli                   # "the host is the fork": use its own kicad-cli
        self.protocol = min(proto, events.PROTOCOL)
        self.hello = {k: p.get(k) for k in ("app_version", "kicad_cli", "python", "project_dir", "frame")}
        self.project_dir = pd
        self.sessions = sessions.Store(pd)
        self.host = HostClient(self, methods, timeouts=_host_timeouts())
        self.live = LiveSession(self.host, pd)
        if self.relay is not None:
            self.relay.stop()
            self.relay = None
        if pd:
            try:                                                   # tools without `project` use the active project
                from .. import project as P
                P.activate(P.resolve(pd, format_check=False))
            except Exception as e:  # noqa: BLE001 — not a KiCad project yet, or unreadable: tools will say so
                self.log("info", f"project not activated: {type(e).__name__}")
        self.ready = True
        return {"protocol": self.protocol, "engine_version": __version__,
                "agent": {"pid": os.getpid(), "python": sys.version.split()[0]},
                "accounts": [accounts.public(a) for a in accounts.list_accounts()],
                "drivers": drivers.describe(), "commands": commands.describe(),
                "limits": {"concurrent_turns": 1, "idle_timeout_s": self.idle_s, "tool_timeout_s": self.tool_s,
                           "turn_timeout_s": self.turn_s, "cancel_grace_s": self.grace_s},
                "load_errors": dict(drivers.LOAD_ERRORS, **commands.LOAD_ERRORS, **_PLUGIN_ERRORS)}

    def m_ping(self, p, mid):
        return {}

    def m_shutdown(self, p, mid):
        self.reply(mid, {})
        self.stop = True
        return _DEFERRED

    def _new_turn(self, p, kind, command=None) -> Turn:
        if self.turns:
            raise app_error("BUSY", "another turn is running", turn=next(iter(self.turns)))
        conv = p.get("conversation")
        if conv is None and kind == "model":
            conv = self.sessions.new_id()
        if conv is not None and not sessions.valid_id(conv):
            raise bad_params("conversation must match [A-Za-z0-9][A-Za-z0-9_-]{0,63} or be null")
        focus = p.get("focus")
        if focus is not None and not isinstance(focus, dict):
            raise bad_params("focus must be an object or null")
        if focus is None:
            focus = self.focus
        mode = p.get("mode") or "chat"
        if mode not in MODES:
            raise bad_params(f"mode must be one of {list(MODES)}")
        text = p.get("text") if kind == "model" else "/" + command["name"]
        if not isinstance(text, str) or not text.strip():
            raise bad_params("text (non-empty string) is required")
        budget = p.get("budget_tokens")
        if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0):
            raise bad_params("budget_tokens must be a positive integer")
        acct = accounts.get(p.get("account")) if kind == "model" else dict(accounts.NONE)
        if acct is None:
            raise app_error("UNKNOWN_ACCOUNT", f"no account {p.get('account')!r}")
        if kind == "model" and drivers.get(acct["kind"]) is None:
            raise app_error("NO_DRIVER", f"no driver for account kind {acct['kind']!r}")
        model = p.get("model")
        if model is not None and not isinstance(model, str):
            raise bad_params("model must be a string or null")
        req = TurnRequest(conversation=conv or "", text=text, focus=focus, mode=mode, account=accounts.public(acct),
                          model=model or acct.get("default_model"), budget_tokens=budget)
        turn = Turn(self, "t-" + secrets.token_hex(4), req, kind=kind, command=command)
        self.turns[turn.id] = turn
        return turn

    def _start(self, turn: Turn, mid, target) -> object:
        rec = {"kind": "user", "text": turn.request.text, "focus": turn.focus, "mode": turn.request.mode,
               "account": turn.request.account.get("id"), "model": turn.request.model, "command": turn.command}
        turn._record(rec)
        res = {"turn": turn.id}
        if turn.request.conversation:
            res["conversation"] = turn.request.conversation
        self.reply(mid, res)                                       # the answer goes out BEFORE the first event
        turn.thread = threading.Thread(target=self._run, args=(turn, target), name=f"copper-turn-{turn.id}", daemon=True)
        turn.thread.start()
        return _DEFERRED

    def m_turn_start(self, p, mid):
        turn = self._new_turn(p, "model")
        driver = drivers.get(turn.request.account["kind"])
        return self._start(turn, mid, lambda: driver.run(turn))

    def m_command_run(self, p, mid):
        name, args = p.get("name"), p.get("args")
        if not isinstance(name, str) or commands.get(name.lstrip("/")) is None:
            raise app_error("UNKNOWN_COMMAND", f"no command {name!r}", commands=[c["name"] for c in commands.describe()])
        if args is not None and not isinstance(args, dict):
            raise bad_params("args must be an object")
        name = name.lstrip("/")
        turn = self._new_turn(p, "command", command={"name": name, "args": args or {}})
        return self._start(turn, mid, lambda: commands.run(turn, name, args or {}))

    def m_turn_cancel(self, p, mid):
        tid = p.get("turn")
        turn = self.turns.get(tid)
        if turn is None:
            if tid in self.recent:
                return {"cancelled": False, "status": self.recent[tid]}        # it had already ended: not an error
            raise app_error("UNKNOWN_TURN", f"no turn {tid!r}")
        if not turn.cancelled.is_set():
            turn.cancel_at = time.monotonic()
            turn.cancelled.set()
            self._fail_pending(E_CANCELLED, "the turn was cancelled", turn=turn.id, tell_app=True)
        return {"cancelled": True}

    def m_accounts_list(self, p, mid):
        return {"accounts": [accounts.public(a) for a in accounts.list_accounts()], "drivers": drivers.describe()}

    def m_accounts_add(self, p, mid):
        for k in ("kind", "label", "base_url", "secret", "command", "default_model"):
            if p.get(k) is not None and not isinstance(p[k], str):
                raise bad_params(f"{k} must be a string")
        if not p.get("kind"):
            raise bad_params("kind is required (one of hello.drivers[].kind)")
        try:
            rec = accounts.add(p["kind"], p.get("label"), p.get("base_url"), p.get("secret"), p.get("command"),
                               p.get("default_model"))
        except accounts.AccountError as e:
            raise app_error(e.code, e.message) from None
        return {"account": accounts.public(rec)}

    def m_accounts_remove(self, p, mid):
        if not accounts.remove(str(p.get("id") or "")):
            raise app_error("UNKNOWN_ACCOUNT", f"no account {p.get('id')!r}")
        return {}

    def m_accounts_test(self, p, mid):
        acct = accounts.get(p.get("id"))
        if acct is None:
            raise app_error("UNKNOWN_ACCOUNT", f"no account {p.get('id')!r}")
        driver = drivers.get(acct["kind"])
        if driver is None:
            raise app_error("NO_DRIVER", f"no driver for account kind {acct['kind']!r}")

        def work():
            try:
                res = driver.test(accounts.public(acct))
                res = res if isinstance(res, dict) else {"ok": bool(res), "message": ""}
                try:
                    res.setdefault("models", driver.models(accounts.public(acct)))
                except Exception:  # noqa: BLE001
                    pass
                self.reply(mid, res)
            except DriverError as e:
                self.reply(mid, {"ok": False, "message": e.message, "kind": e.kind, "code": e.code})
            except Exception as e:  # noqa: BLE001
                self.reply(mid, {"ok": False, "message": f"{type(e).__name__}", "kind": "engine", "code": "INTERNAL"})
        threading.Thread(target=work, name="copper-accounts-test", daemon=True).start()
        return _DEFERRED

    def m_conversations_list(self, p, mid):
        return {"conversations": self.sessions.list()}

    def m_conversations_load(self, p, mid):
        conv = p.get("conversation")
        if not sessions.valid_id(conv):
            raise bad_params("conversation is required")
        recs = self.sessions.load(conv)
        if not recs:
            raise app_error("UNKNOWN_CONVERSATION", f"no conversation {conv!r}")
        return {"conversation": conv, "records": recs}

    def m_conversations_delete(self, p, mid):
        conv = p.get("conversation")
        if not sessions.valid_id(conv) or not self.sessions.delete(conv):
            raise app_error("UNKNOWN_CONVERSATION", f"no conversation {conv!r}")
        return {}

    def m_usage_get(self, p, mid):
        conv, limit = p.get("conversation"), p.get("limit")
        if conv is not None and not sessions.valid_id(conv):
            raise bad_params("conversation must be a conversation id or null")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 1000):
            raise bad_params("limit must be an integer 0..1000")
        recs = usage.read(self.project_dir)
        out = usage.summary(self.project_dir, conv, recs)
        out["line"] = usage.line(out)
        out["turns"] = recs[-limit:] if limit else []
        return out

    METHODS = {"hello": m_hello, "ping": m_ping, "shutdown": m_shutdown, "turn.start": m_turn_start,
               "turn.cancel": m_turn_cancel, "command.run": m_command_run, "accounts.list": m_accounts_list,
               "accounts.add": m_accounts_add, "accounts.remove": m_accounts_remove, "accounts.test": m_accounts_test,
               "conversations.list": m_conversations_list, "conversations.load": m_conversations_load,
               "conversations.delete": m_conversations_delete, "usage.get": m_usage_get}

    # ── app -> agent: notifications ───────────────────────────────────────
    def n_focus_changed(self, p):
        self.focus = p or None

    def n_doc_changed(self, p):
        if p.get("document") in ("schematic", "board") and isinstance(p.get("rev"), int):
            self.live.doc_changed(p["document"], p["rev"])

    def n_proposal_resolved(self, p):
        pid, outcome = p.get("proposal_id"), p.get("outcome")
        if not isinstance(pid, str) or outcome not in ("kept", "undone", "stale"):
            return
        conv = self.proposals.pop(pid, None)
        if outcome != "kept":
            self.live.invalidate()                 # the document is no longer what the shadow holds
        if conv:
            try:
                self.sessions.append(conv, {"kind": "outcome", "proposal_id": pid, "outcome": outcome})
            except Exception:  # noqa: BLE001
                pass
            if outcome != "kept":
                self.notes.setdefault(conv, []).append(
                    "the user undid your last change" if outcome == "undone"
                    else "your last change was dropped because the document changed")

    NOTIFICATIONS = {"focus.changed": n_focus_changed, "doc.changed": n_doc_changed,
                     "proposal.resolved": n_proposal_resolved}

    def take_notes(self, conversation: str) -> list[str]:
        return self.notes.pop(conversation, [])

    def start_relay(self) -> dict:
        """Start (once) the loopback relay for child processes; -> environment for the child (host/relay.py)."""
        from ..host.relay import Relay
        if self.relay is None:
            self.relay = Relay(self.host, self.project_dir)
            self.relay.start()
        return self.relay.env()

    # ── turn lifecycle ────────────────────────────────────────────────────
    def _run(self, turn: Turn, target) -> None:
        status, error = "ok", None
        try:
            target()
            if turn.cancelled.is_set():
                status = "cancelled"
        except Cancelled:
            status = "cancelled"
        except DriverError as e:
            error = {"kind": e.kind, "message": e.message, "code": e.code, "retryable": e.retryable, "fix": e.fix,
                     "retry_after_s": e.retry_after_s}
        except HostError as e:
            gone = e.code in (E_HOST_GONE, E_TIMEOUT)
            error = {"kind": "protocol" if gone else "engine", "message": e.message, "code": e.code, "retryable": not gone}
        except BaseException as e:  # noqa: BLE001 — a driver bug must end the turn, not the agent. BaseException:
            # a tool that calls sys.exit() would otherwise end this thread silently and leave the turn to the watchdog
            self.log("error", f"turn {turn.id} crashed: " + traceback.format_exc(limit=6))
            error = {"kind": "engine", "message": f"{type(e).__name__}: {e}", "code": "INTERNAL", "retryable": False}
        if turn.abort_error is not None:                       # the channel closed under the turn: that is its end
            status, error = "error", turn.abort_error
        self._finish(turn, status, error)

    def _finish(self, turn: Turn, status: str, error: dict | None = None) -> None:
        if error is not None:
            error = {k: v for k, v in error.items() if v is not None}
        with self.lock:                                # free the slot BEFORE `done` goes out: the app may start
            self.turns.pop(turn.id, None)              # the next turn the moment it reads it
            self.recent.setdefault(turn.id, "error" if error else status)
            while len(self.recent) > 64:
                self.recent.pop(next(iter(self.recent)))
            meter, turn.metered = not getattr(turn, "metered", False) and not turn.finished, True
        if meter:                                      # <project>/.copper/usage.jsonl, written BEFORE `done`:
            usage.record_turn(self.project_dir, turn, "error" if error else status)   # usage.get after done sees it
        if turn.finish(status, error):
            self.log("info", f"turn {turn.id} {turn.status}" + (f" ({error.get('kind')}/{error.get('code')})" if error else ""))
        self._fail_pending(E_CANCELLED, "the turn ended", turn=turn.id, tell_app=True)

    def note_proposal(self, proposal_id: str, conversation: str) -> None:
        if proposal_id and conversation:
            self.proposals[proposal_id] = conversation

    def _watchdog(self) -> None:
        while not self.stop:
            time.sleep(0.1)
            now = time.monotonic()
            for t in list(self.turns.values()):
                if t.finished:
                    continue
                if t.cancelled.is_set():
                    if t.cancel_at is not None and now - t.cancel_at > self.grace_s:
                        self._finish(t, "cancelled")           # the driver did not stop in time; its thread is muted
                    continue
                if now - t.started > self.turn_s:
                    err = ("engine", "TURN_TIMEOUT", f"the turn ran longer than {self.turn_s:.0f} s")
                elif t.waiting_host:
                    continue                                    # host timeouts govern (host/client.TIMEOUTS)
                elif t.in_tool:
                    if now - t.last_activity <= self.tool_s:
                        continue
                    err = ("engine", "WATCHDOG_TOOL", f"a tool made no progress for {self.tool_s:.0f} s")
                elif now - t.last_activity > self.idle_s:
                    err = ("network", "WATCHDOG", f"nothing arrived for {self.idle_s:.0f} s")
                else:
                    continue
                self._finish(t, "error", {"kind": err[0], "code": err[1], "message": err[2], "retryable": err[0] == "network"})


_DEFERRED = object()
_PLUGIN_ERRORS: dict = {}


def _load_plugins() -> None:
    for i, f in enumerate(filter(None, (os.environ.get("COPPER_AGENT_PLUGINS") or "").split(os.pathsep))):
        try:
            spec = importlib.util.spec_from_file_location(f"copper_agent_plugin_{i}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:  # noqa: BLE001
            _PLUGIN_ERRORS[f"plugin {Path(f).name}"] = traceback.format_exc(limit=3).strip().splitlines()[-1]


def _claim_stdio():
    """Take stdin/stdout for the protocol and make sure nothing else can use them: the engine and its child
    processes (kicad-cli, pcbnew workers) get NUL as stdin and stderr as stdout."""
    try:
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        in_fd, out_fd = os.dup(0), os.dup(1)
        nul = os.open(os.devnull, os.O_RDONLY)
        os.dup2(nul, 0)
        os.close(nul)
        os.dup2(2, 1)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.setmode(in_fd, os.O_BINARY)
            msvcrt.setmode(out_fd, os.O_BINARY)
        rfile, wfile = os.fdopen(in_fd, "rb"), os.fdopen(out_fd, "wb")
        sys.stdout = sys.stderr
        return rfile, wfile
    except (OSError, ValueError, AttributeError):
        return sys.stdin.buffer, sys.stdout.buffer


def main(argv=None) -> int:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    rfile, wfile = _claim_stdio()
    drivers.load()
    commands.load()
    _load_plugins()
    return AgentServer(rfile, wfile).serve()
