"""Driver for any agent CLI that speaks the Agent Client Protocol (ACP) on stdio. Vendor-neutral.

Account kind ``cli-acp``; the account's ``command`` is the program and its arguments that start the agent in ACP
mode (the user's own, unmodified, signed in by its own flow). Copper is the ACP *client*.

ACP is JSON-RPC 2.0, one message per line, both directions (protocol pages read on 2026-11-10; no ACP agent is
installed on the build machine, so behaviour against a real one is UNVERIFIED, see docs/internal/AGENT_DRIVERS.md):

    -> initialize {protocolVersion: 1, clientCapabilities: {fs: {readTextFile: false, writeTextFile: false},
                   terminal: false}, clientInfo}
          Copper advertises NO file and NO terminal capability: the agent has no way to ask us to read, write or run.
    -> session/new {cwd, mcpServers: [{name: "copper", command, args, env: []}]}        only Copper's bridge (_gate.py)
          or session/resume {sessionId, cwd, mcpServers} when the agent offers it and the conversation has an id
    -> session/prompt {sessionId, prompt: [{type: "text", text}]}
    <- session/update {update: {sessionUpdate: ...}}
          agent_message_chunk -> text       agent_thought_chunk -> reasoning text
          tool_call / tool_call_update -> Copper's own tools are reported by the tool gate; any other tool shows as
          a row named ``cli:<title>``
    <- session/request_permission {toolCall, options}
          answered at once, never shown to the user: a Copper tool gets the agent's allow-once option, anything
          else its reject option. A tool counts as Copper's only when its name / title carries one of the catalogue's
          tool names (or names the ``copper`` server and is not classed edit / delete / move / execute by the
          agent); when in doubt it is rejected.
    <- fs/*, terminal/*, anything else: JSON-RPC error -32601 (not offered)
    <- result of session/prompt {stopReason}     THE TERMINAL MARKER
          end_turn -> done; cancelled -> cancelled; max_tokens / max_turn_requests -> error(engine);
          refusal -> error(engine, REFUSED)
    (EOF before that result)                     error(network, TRUNCATED), or auth / protocol from stderr / exit code

Cancel: ``session/cancel`` is sent (permission requests are answered the moment they arrive, so none is pending)
and the process tree is killed when the turn ends. An ``auth_required`` error means the user has to sign in with the agent's own CLI.

Without session/resume a session starts fresh each turn; Copper's instructions and the earlier turns then open the
message. ACP reports context size, not billed tokens, so no usage event is produced.
"""
from __future__ import annotations

import json
import re

from .. import prompt
from . import register
from ._cli import Child, classify, cli_tool_name, earlier_turns, find, text_of_content, work_dir
from ._gate import SERVER_NAME, ToolGate
from .base import Cancelled, Driver, DriverError

WHAT = "the ACP agent"
PROTOCOL_VERSION = 1


@register
class AcpDriver(Driver):
    kind = "cli-acp"
    label = "Agent CLI over ACP (your own sign-in)"
    needs = ("command",)

    def test(self, account: dict) -> dict:
        if not account.get("command"):
            return {"ok": False, "message": "set the command that starts the agent in ACP mode", "kind": "engine",
                    "code": "CLI_MISSING"}
        if not find(account, ()):
            return {"ok": False, "message": "the program of this account's command was not found", "kind": "engine",
                    "code": "CLI_MISSING"}
        return {"ok": True, "message": "program found; sign-in is checked on first use", "models": []}

    def run(self, turn) -> None:
        req = turn.request
        argv = find(req.account, ()) if req.account.get("command") else None
        if not argv:
            raise DriverError("engine", "the program of this account's command was not found", code="CLI_MISSING",
                              retryable=False, fix="set the command that starts the agent in ACP mode")
        with ToolGate(turn) as gate, Child(argv, work_dir(turn), turn, what=WHAT) as child:
            _Session(self.kind, turn, child, gate).run()


class _Session:
    def __init__(self, kind: str, turn, child: Child, gate: ToolGate):
        self.kind, self.turn, self.child, self.gate = kind, turn, child, gate
        self.next_id = 0
        self.rows: dict = {}                 # toolCallId -> our call id (the agent's own tools only)
        self.known: dict = {}                # toolCallId -> (text that names the tool, the agent's kind for it)
        self.names = [t["name"] for t in prompt.tool_catalogue(turn.request.mode)]
        self.session = None

    # ── wire ──────────────────────────────────────────────────────────────
    def send(self, msg: dict) -> None:
        self.child.send(json.dumps(dict({"jsonrpc": "2.0"}, **msg), ensure_ascii=False, separators=(",", ":")) + "\n")

    def call(self, method: str, params: dict) -> dict:
        """Send a request and serve the agent's traffic until its answer arrives."""
        self.next_id += 1
        mid = self.next_id
        self.send({"id": mid, "method": method, "params": params})
        try:
            for msg in self.child.json_lines():
                if "method" in msg:
                    self.incoming(msg)
                elif msg.get("id") == mid:
                    if msg.get("error"):
                        raise self.failure(method, msg["error"])
                    return msg.get("result") if isinstance(msg.get("result"), dict) else {}
        except Cancelled:
            if self.session and method == "session/prompt":
                self.send({"method": "session/cancel", "params": {"sessionId": self.session}})
                self.child.wait(0.3)                 # a moment to stop by itself; the tree is killed right after
            raise
        raise self.child.truncated()

    def failure(self, method: str, err) -> DriverError:
        err = err if isinstance(err, dict) else {"message": str(err)}
        text = f"{err.get('message') or 'refused'}"
        if "auth" in text.lower() or err.get("code") == -32000:
            return DriverError("auth", f"{WHAT} needs you to sign in: {text[:200]}", code="CLI_AUTH", retryable=False,
                               fix="run the agent's own CLI once in a terminal and sign in")
        return classify(f"{method}: {text}", WHAT, code="CLI_REFUSED")

    # ── the agent's side ──────────────────────────────────────────────────
    def is_ours(self, text: str, kind: str | None = None) -> bool:
        """Is this tool call one of Copper's? A catalogue tool name in the label decides; the bare server name counts
        only for calls the agent does not itself class as editing, deleting, moving or executing."""
        low = text.lower()
        if any(re.search(rf"(?<![a-z0-9_]){re.escape(n)}(?![a-z0-9_])", low) for n in self.names):
            return True
        return SERVER_NAME in low and kind not in ("edit", "delete", "move", "execute")

    def incoming(self, msg: dict) -> None:
        method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if method == "session/update":
            self.update(params.get("update") or {})
        elif method == "session/request_permission" and "id" in msg:
            call = params.get("toolCall") or {}
            seen = self.known.get(call.get("toolCallId")) or ("", None)
            label = " ".join(str(call.get(k) or "") for k in ("title", "name")).strip() or seen[0]
            allow = self.is_ours(label, call.get("kind") or seen[1])
            wanted = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
            options = [o for o in params.get("options") or [] if isinstance(o, dict)]
            pick = next((o for w in wanted for o in options if o.get("kind") == w), None)
            outcome = {"outcome": "selected", "optionId": pick.get("optionId")} if pick else {"outcome": "cancelled"}
            self.send({"id": mid, "result": {"outcome": outcome}})
        elif "id" in msg:                    # fs/*, terminal/*, elicitation: not offered
            self.send({"id": mid, "error": {"code": -32601, "message": f"{method} is not available: use the copper tools"}})

    def update(self, u: dict) -> None:
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = text_of_content(u.get("content"))
            if text:
                self.turn.text(text)
        elif kind == "agent_thought_chunk":
            text = text_of_content(u.get("content"))
            if text:
                self.turn.text(text, channel="reasoning")
        elif kind in ("tool_call", "tool_call_update"):
            tid = u.get("toolCallId")
            label = " ".join(str(u.get(k) or "") for k in ("title", "name")).strip()
            old = self.known.get(tid) or ("", None)
            label, kind_ = label or old[0], u.get("kind") or old[1]
            self.known[tid] = (label, kind_)
            if self.is_ours(label, kind_):
                return
            status = u.get("status")
            if tid not in self.rows and status not in ("completed", "failed"):
                raw = u.get("rawInput")
                self.rows[tid] = self.turn.tool_start(cli_tool_name(label or kind_), raw if isinstance(raw, dict) else None)
            elif tid in self.rows and status in ("completed", "failed"):
                body = text_of_content([c.get("content") for c in u.get("content") or [] if isinstance(c, dict)])
                self.turn.tool_end(self.rows.pop(tid), ok=status == "completed", summary=(body.splitlines() or [""])[0],
                                   error=None if status == "completed" else {"code": "CLI_TOOL", "message": body[:300] or "failed"})

    # ── one turn ──────────────────────────────────────────────────────────
    def run(self) -> None:
        turn, req = self.turn, self.turn.request
        init = self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "copper", "title": "Copper", "version": "1"}})
        if init.get("protocolVersion") not in (None, PROTOCOL_VERSION):
            raise DriverError("protocol", f"{WHAT} speaks ACP version {init.get('protocolVersion')}, Copper speaks "
                              f"{PROTOCOL_VERSION}", code="CLI_VERSION", retryable=False)
        caps = init.get("agentCapabilities") or {}
        py, bridge, gate_file = self.gate.server_argv()
        setup = {"cwd": work_dir(turn), "mcpServers": [{"name": SERVER_NAME, "command": py, "args": [bridge, gate_file], "env": []}]}
        conv, quick = req.conversation, req.mode == "quick"
        stored = turn.sessions.resume_id(conv, self.kind) if conv and not quick else None
        resumed = False
        if stored and (caps.get("sessionCapabilities") or {}).get("resume") is not None:
            try:
                self.call("session/resume", dict(setup, sessionId=stored))
                self.session, resumed = stored, True
            except DriverError as e:
                if e.kind == "auth":
                    raise
        if not resumed:
            self.session = self.call("session/new", setup).get("sessionId")
            if not self.session:
                raise DriverError("protocol", f"{WHAT} did not return a session id", code="CLI_NO_SESSION", retryable=False)
            if conv and not quick:
                turn.sessions.set_resume_id(conv, self.kind, str(self.session))
        head = "" if resumed else ("[instructions]\n" + prompt.system_prefix(req.mode) + "\n[end of instructions]\n\n"
                                   + earlier_turns(turn))
        text = head + prompt.user_turn(req.text, req.focus, turn.notes())
        stop = self.call("session/prompt", {"sessionId": self.session, "prompt": [{"type": "text", "text": text}]}).get("stopReason")
        for call in list(self.rows.values()):
            turn.tool_end(call, ok=False, error={"code": "CLI_TOOL", "message": "the agent did not report the end of this call"})
        if stop == "cancelled":
            raise Cancelled()
        if stop == "refusal":
            raise DriverError("engine", "the agent declined this request", code="REFUSED", retryable=False)
        if stop in ("max_tokens", "max_turn_requests"):
            raise DriverError("engine", f"the agent stopped early ({stop})", code=stop.upper(), retryable=False,
                              fix="continue in a new message")
        if stop not in (None, "end_turn"):
            raise DriverError("protocol", f"{WHAT} ended the turn with an unknown stop reason {stop!r}", code="CLI_STOP",
                              retryable=False)
        if stop is None:
            raise self.child.truncated()
