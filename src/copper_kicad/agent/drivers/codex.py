"""Driver that runs the user's own Codex CLI headless (``codex exec --json``), signed in by its own login.

Account kind ``cli-exec-json``. Copper passes no credential and sees none.

The command (flags checked against the non-interactive-mode page and the configuration reference on 2026-11-10;
the CLI is not installed on the build machine, so the behaviour of a real CLI is UNVERIFIED, see
docs/internal/AGENT_DRIVERS.md):

    codex exec --json --skip-git-repo-check
          --sandbox read-only                               nothing the CLI runs itself can write
          --ignore-user-config                              the user's config.toml (their own MCP servers) is not loaded;
                                                            sign-in lives elsewhere and is kept. COPPER_CLI_USER_CONFIG=1
                                                            keeps the user's config (a custom provider set up there)
          -c features.shell_tool=false                      no command execution tool
          -c features.view_image=false -c features.multi_agent=false -c features.apps=false -c tools.web_search=false
          -c approval_policy=never                          non-interactive: nothing can prompt
          -c mcp_servers.copper.command=<python>            Copper's bridge (_gate.py) is the only MCP server
          -c mcp_servers.copper.args=['<bridge>','<gate file>']
          -c mcp_servers.copper.default_tools_approval_mode=approve
          -c mcp_servers.copper.tool_timeout_sec=1800 -c mcp_servers.copper.startup_timeout_sec=30
          -c mcp_servers.copper.required=true
          [-m <model>] [resume <thread id>] -                  the prompt is read from stdin

``-c`` values are TOML; the two paths are written as TOML literal strings (single quotes, no escapes), so no
double quote ever appears on the command line. The CLI has no flag for extra system text that is safe to pass
this way, so Copper's instructions open the first message of a session instead.

The stream (one JSON object per line) and what becomes of it:

    thread.started {thread_id}             stored as the conversation's resume id
    item.completed agent_message           text (whole messages; this CLI does not stream deltas)
    item.completed reasoning               reasoning text
    item.* mcp_tool_call (server copper)   already reported by the tool gate: skipped
    item.* command_execution, file_change, web_search, other mcp_tool_call
                                           shown as tool rows named ``cli:<kind>`` (they should not occur)
    error {message}                        remembered ("Reconnecting..." lines are transient)
    turn.completed {usage}                 THE TERMINAL MARKER. usage is the thread's running total: this turn's
                                           share = total - the total stored after the previous turn
    turn.failed {error.message}            error, kind chosen from the message
    (EOF without either)                   error(network, TRUNCATED), or auth / protocol from stderr / exit code

``budget_tokens`` cannot be enforced mid-run (usage arrives only at the end); it is checked when the turn ends.
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import prompt
from . import register
from ._cli import Child, Totals, available, classify, cli_tool_name, earlier_turns, find, safe_arg, text_of_content, work_dir
from ._gate import SERVER_NAME, ToolGate
from .base import Driver, DriverError

NAMES = ("codex",)
WHAT = "Codex"


def _toml_literal(value: str, what: str) -> str:
    value = safe_arg(value, what)
    if "'" in value:
        raise DriverError("engine", f"{what} contains a single quote and cannot be passed to the CLI: {value[:80]!r}",
                          code="UNSAFE_ARGUMENT", retryable=False)
    return f"'{value}'"


@register
class ExecJsonCliDriver(Driver):
    kind = "cli-exec-json"
    label = "Codex CLI (your own sign-in; runs codex exec)"
    needs = ()
    streaming = False                         # whole messages, not deltas

    def available(self):
        return available(NAMES)

    def test(self, account: dict) -> dict:
        argv = find(account, NAMES)
        if not argv:
            return {"ok": False, "message": available(NAMES)[1], "kind": "engine", "code": "CLI_MISSING"}
        return {"ok": True, "message": f"found {Path(argv[0]).name}; sign-in is checked on first use", "models": []}

    def command(self, gate: ToolGate, model: str | None, resume: str | None) -> list[str]:
        py, bridge, gate_file = gate.server_argv()
        srv = f"mcp_servers.{SERVER_NAME}"
        cfg = ["features.shell_tool=false", "features.view_image=false", "features.multi_agent=false",
               "features.apps=false", "tools.web_search=false", "approval_policy=never",
               f"{srv}.command={_toml_literal(py, 'the Python path')}",
               f"{srv}.args=[{_toml_literal(bridge, 'the install path')},{_toml_literal(gate_file, 'the temp folder')}]",
               f"{srv}.default_tools_approval_mode=approve", f"{srv}.tool_timeout_sec=1800",
               f"{srv}.startup_timeout_sec=30", f"{srv}.required=true"]
        args = ["exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only"]
        if os.environ.get("COPPER_CLI_USER_CONFIG") != "1":
            args.append("--ignore-user-config")
        for c in cfg:
            args += ["-c", c]
        if model:
            args += ["-m", safe_arg(model, "the model id")]
        if resume:
            args += ["resume", safe_arg(resume, "the session id")]
        return args + ["-"]

    def run(self, turn) -> None:
        req = turn.request
        argv = find(req.account, NAMES)
        if not argv:
            raise DriverError("engine", available(NAMES)[1], code="CLI_MISSING", retryable=False)
        conv, quick = req.conversation, req.mode == "quick"
        resume = turn.sessions.resume_id(conv, self.kind) if conv and not quick else None
        head = "" if resume else ("[instructions]\n" + prompt.system_prefix(req.mode) + "\n[end of instructions]\n\n"
                                  + earlier_turns(turn))
        text = head + prompt.user_turn(req.text, req.focus, turn.notes())
        with ToolGate(turn) as gate:
            with Child(argv + self.command(gate, req.model, resume), work_dir(turn), turn, what=WHAT) as child:
                child.send(text)
                child.close_stdin()
                self._read(turn, child)

    def _read(self, turn, child: Child) -> None:
        req = turn.request
        totals = Totals(turn, self.kind)
        rows: dict = {}
        thread, last_error, said = None, "", False
        for msg in child.json_lines():
            kind = msg.get("type") or ""
            if kind == "thread.started":
                thread = msg.get("thread_id") or thread
                if thread and req.conversation and req.mode != "quick":
                    turn.sessions.set_resume_id(req.conversation, self.kind, thread)
            elif kind.startswith("item."):
                item = msg.get("item") or {}
                itype, iid = item.get("type") or item.get("item_type"), item.get("id")
                done = kind == "item.completed"
                if itype == "agent_message":
                    if done and item.get("text"):
                        turn.text(("\n\n" if said else "") + item["text"])
                        said = True
                elif itype == "reasoning":
                    if done and item.get("text"):
                        turn.text(item["text"] + "\n", channel="reasoning")
                elif itype == "error":
                    last_error = str(item.get("message") or last_error)
                elif itype in ("command_execution", "file_change", "web_search", "mcp_tool_call", "collab_tool_call"):
                    if itype == "mcp_tool_call" and item.get("server") == SERVER_NAME:
                        continue
                    if iid not in rows and not done:
                        rows[iid] = turn.tool_start(cli_tool_name(_row_name(item, itype)), _row_args(item))
                    elif done:
                        call = rows.pop(iid, None) or turn.tool_start(cli_tool_name(_row_name(item, itype)), _row_args(item))
                        ok = item.get("status") in (None, "completed") and item.get("exit_code") in (None, 0)
                        body = str(item.get("aggregated_output") or text_of_content((item.get("result") or {}).get("content"))
                                   or (item.get("error") or {}).get("message") or "")
                        turn.tool_end(call, ok=ok, summary=(body.splitlines() or [""])[0],
                                      error=None if ok else {"code": "CLI_TOOL", "message": body[:300] or str(item.get("status"))})
            elif kind == "error":
                last_error = str(msg.get("message") or last_error)
            elif kind == "turn.failed":
                raise classify((msg.get("error") or {}).get("message") or last_error, WHAT, code="CLI_TURN_FAILED")
            elif kind == "turn.completed":
                u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                i = int(totals.delta("in", u.get("input_tokens"), thread))
                o = int(totals.delta("out", u.get("output_tokens"), thread))
                c = int(totals.delta("cached", u.get("cached_input_tokens"), thread))
                totals.save(thread)
                if u:
                    turn.usage(i, o, c)
                if req.budget_tokens and i + o > req.budget_tokens:
                    raise DriverError("rate", f"the run used {i + o} tokens, over its budget of {req.budget_tokens}",
                                      code="BUDGET", retryable=False)
                return
        if last_error:
            child.wait(3)
            raise classify(last_error, WHAT)
        raise child.truncated()


def _row_name(item: dict, itype: str) -> str:
    if itype == "mcp_tool_call":
        return f"{item.get('server')}.{item.get('tool')}"
    return {"command_execution": "shell", "file_change": "file change", "web_search": "web search"}.get(itype, itype)


def _row_args(item: dict) -> dict | None:
    for key in ("command", "query", "changes", "arguments"):
        if item.get(key):
            return {key: item[key]}
    return None
