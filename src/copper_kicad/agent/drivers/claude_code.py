"""Driver that runs the user's own Claude Code CLI headless (``claude -p``), signed in by its own login.

Account kind ``cli-stream-json``. Copper passes no credential and sees none: the CLI talks to its provider with
whatever sign-in the user did in a terminal. ``--bare`` is never passed (bare mode ignores the subscription login).

The command (flags checked against the CLI reference and the "run programmatically" page on 2026-11-10; the CLI
is not installed on the build machine, so the behaviour of a real CLI is UNVERIFIED, see docs/internal/AGENT_DRIVERS.md):

    claude -p --output-format stream-json --verbose --include-partial-messages
           --mcp-config <file> --strict-mcp-config          only Copper's bridge (_gate.py), no user / project servers
           --tools ""                                       no built-in tools at all
           --disallowedTools Bash,Edit,Write,...            belt and braces: deny rules win over any allow rule
           --allowedTools mcp__copper                       Copper's tools run without a prompt
           --permission-mode dontAsk                        anything else that would prompt is denied
           --append-system-prompt-file <file>               Copper's system text, appended to the CLI's own
           [--model <id>] [--resume <session id>] [--no-session-persistence]      (quick edits are not persisted)

The user message goes in on stdin. Nothing that needs quoting is on the command line.

The stream (one JSON object per line) and what becomes of it:

    system/init            session_id -> stored as the conversation's resume id; a failed Copper MCP server ends the turn
    stream_event           content_block_delta: text_delta -> text, thinking_delta -> reasoning text
    assistant              text blocks (only if no delta carried them), tool_use blocks: Copper's own are already
                           reported by the tool gate; any other tool shows as a row named ``cli:<name>``
    user                   tool_result blocks close those rows
    system/api_retry       remembered: it names the failure category if the run then fails
    result                 THE TERMINAL MARKER. usage / total_cost_usd -> usage; is_error -> error by category
    (EOF without result)   error(network, TRUNCATED), or auth / protocol when stderr or the exit code says more

Budget: with ``budget_tokens`` the run is stopped (process killed) once the usage reported on assistant messages
passes it: error(rate, BUDGET).
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from .. import prompt
from . import register
from ._cli import Child, Totals, available, classify, cli_tool_name, earlier_turns, find, safe_arg, text_of_content, work_dir
from ._gate import SERVER_NAME, ToolGate
from .base import Driver, DriverError

NAMES = ("claude",)
WHAT = "Claude Code"
DENY = "Bash,Edit,Write,Read,Glob,Grep,NotebookEdit,WebFetch,WebSearch,Agent,Task"
MCP_PREFIX = f"mcp__{SERVER_NAME}__"
_NO_SESSION = re.compile(r"no conversation found|session .*not found|could not find session", re.I)
_CATEGORY = {"authentication_failed": "auth", "oauth_org_not_allowed": "auth", "account_on_hold": "auth",
             "billing_error": "rate", "rate_limit": "rate", "overloaded": "network", "server_error": "network",
             "cloud_credential_error": "auth", "invalid_request": "protocol", "model_not_found": "protocol"}


@register
class StreamJsonCliDriver(Driver):
    kind = "cli-stream-json"
    label = "Claude Code (your own sign-in; runs the claude CLI)"
    needs = ()                                # 'command' is optional: the path of the program when not on PATH

    def available(self):
        return available(NAMES)

    def test(self, account: dict) -> dict:
        argv = find(account, NAMES)
        if not argv:
            return {"ok": False, "message": available(NAMES)[1], "kind": "engine", "code": "CLI_MISSING"}
        return {"ok": True, "message": f"found {Path(argv[0]).name}; sign-in is checked on first use", "models": []}

    def run(self, turn) -> None:
        req = turn.request
        argv = find(req.account, NAMES)
        if not argv:
            raise DriverError("engine", available(NAMES)[1], code="CLI_MISSING", retryable=False)
        conv, quick = req.conversation, req.mode == "quick"
        resume = turn.sessions.resume_id(conv, self.kind) if conv and not quick else None
        try:
            self._attempt(turn, argv, resume)
        except _StaleSession:
            self._attempt(turn, argv, None)          # the CLI no longer has that session: start a fresh one

    def _attempt(self, turn, argv: list, resume: str | None) -> None:
        req = turn.request
        tmp = Path(tempfile.mkdtemp(prefix="copper-cli-"))
        try:
            with ToolGate(turn) as gate:
                (tmp / "mcp.json").write_text(json.dumps({"mcpServers": {SERVER_NAME: {
                    "type": "stdio", "command": gate.server_argv()[0], "args": gate.server_argv()[1:]}}}), encoding="utf-8")
                (tmp / "system.txt").write_text(prompt.system_prefix(req.mode), encoding="utf-8")
                args = ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                        "--mcp-config", safe_arg(tmp / "mcp.json", "the temp folder"), "--strict-mcp-config",
                        "--tools", "", "--disallowedTools", DENY, "--allowedTools", f"mcp__{SERVER_NAME}",
                        "--permission-mode", "dontAsk",
                        "--append-system-prompt-file", safe_arg(tmp / "system.txt", "the temp folder")]
                if req.model:
                    args += ["--model", safe_arg(req.model, "the model id")]
                if req.mode == "quick":
                    args += ["--no-session-persistence"]
                elif resume:
                    args += ["--resume", safe_arg(resume, "the session id")]
                text = ("" if resume else earlier_turns(turn)) + prompt.user_turn(req.text, req.focus, turn.notes())
                env = {"MCP_TOOL_TIMEOUT": "1800000", "MCP_TIMEOUT": "30000"}
                with Child(argv + args, work_dir(turn), turn, env=env, what=WHAT) as child:
                    child.send(text)
                    child.close_stdin()
                    self._read(turn, child, bool(resume))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _read(self, turn, child: Child, resumed: bool) -> None:
        req = turn.request
        totals = Totals(turn, self.kind)
        rows: dict = {}                    # the CLI's own tool_use id -> our call id
        per_message: dict = {}             # message id -> (in, out, cached)
        delta_text = False                 # a text delta arrived since the last assistant message
        said = False                       # any answer text reached the user
        session, category = None, None
        for msg in child.json_lines():
            kind, sub = msg.get("type"), msg.get("subtype")
            if kind == "system" and sub == "init":
                session = msg.get("session_id") or session
                if session and req.conversation and req.mode != "quick":
                    turn.sessions.set_resume_id(req.conversation, self.kind, session)
                for srv in msg.get("mcp_servers") or []:
                    if isinstance(srv, dict) and srv.get("name") == SERVER_NAME and srv.get("status") in ("failed", "error"):
                        raise DriverError("protocol", f"{WHAT} could not start Copper's tool server", code="CLI_MCP_FAILED",
                                          retryable=False)
            elif kind == "system" and sub == "api_retry":
                category = msg.get("error") or category
            elif kind == "stream_event":
                if msg.get("parent_tool_use_id"):
                    continue
                ev = msg.get("event") or {}
                d = ev.get("delta") or {}
                if ev.get("type") == "content_block_delta":
                    if d.get("type") == "text_delta" and d.get("text"):
                        delta_text = said = True
                        turn.text(d["text"])
                    elif d.get("type") == "thinking_delta" and d.get("thinking"):
                        turn.text(d["thinking"], channel="reasoning")
            elif kind == "assistant":
                m = msg.get("message") or {}
                top = not msg.get("parent_tool_use_id")
                for block in m.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and top:
                        if not delta_text and block.get("text"):
                            said = True
                            turn.text(block["text"])
                        delta_text = False
                    elif block.get("type") == "tool_use" and not str(block.get("name") or "").startswith(MCP_PREFIX):
                        rows[block.get("id")] = turn.tool_start(cli_tool_name(block.get("name")), block.get("input")
                                                                if isinstance(block.get("input"), dict) else None)
                u = m.get("usage")
                if isinstance(u, dict) and m.get("id"):
                    per_message[m["id"]] = _usage(u)
                    used = [sum(v[i] for v in per_message.values()) for i in range(3)]
                    turn.usage(used[0], used[1], used[2], model=m.get("model"))
                    if req.budget_tokens and used[0] + used[1] >= req.budget_tokens:
                        child.kill()
                        raise DriverError("rate", f"the run reached its budget of {req.budget_tokens} tokens", code="BUDGET",
                                          retryable=False, fix="raise the budget or continue in a new message")
            elif kind == "user":
                for block in (msg.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in rows:
                        body = text_of_content(block.get("content"))
                        failed = bool(block.get("is_error"))
                        turn.tool_end(rows.pop(block["tool_use_id"]), ok=not failed, summary=(body.splitlines() or [""])[0],
                                      error={"code": "CLI_TOOL", "message": body[:300] or "failed"} if failed else None)
            elif kind == "result":
                session = msg.get("session_id") or session
                u = msg.get("usage")
                cost = msg.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    cost = round(totals.delta("cost", cost, session), 6)     # the CLI reports the conversation's total
                    totals.save(session)
                else:
                    cost = None
                if isinstance(u, dict):
                    i, o, c = _usage(u)
                    turn.usage(i, o, c, cost=cost)
                if msg.get("is_error") or (sub not in (None, "success")):
                    text = str(msg.get("result") or "") or str(sub or "failed")
                    if resumed and _NO_SESSION.search(text):
                        raise _StaleSession()
                    if category in _CATEGORY:
                        k = _CATEGORY[category]
                        raise DriverError(k, f"{WHAT}: {' '.join(text.split())[:300]}", code="CLI_" + category.upper(),
                                          retryable=k in ("rate", "network"),
                                          fix="run `claude` in a terminal and sign in" if k == "auth" else None)
                    raise classify(text, WHAT, code="CLI_" + str(sub or "error").upper())
                if not said and isinstance(msg.get("result"), str) and msg["result"]:
                    turn.text(msg["result"])          # a CLI that sent no assistant text: the result carries it
                return
        raise child.truncated()


def _usage(u: dict) -> tuple[int, int, int]:
    read = int(u.get("cache_read_input_tokens") or 0)
    return (int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + read,
            int(u.get("output_tokens") or 0), read)


class _StaleSession(Exception):
    pass
