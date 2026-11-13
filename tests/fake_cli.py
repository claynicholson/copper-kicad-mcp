"""Fake agent CLIs for tests/test_agent_drivers.py. They replay streams in the formats the real programs document
(the real programs are not installed on the build machine) and behave like them where it matters to a driver:
they read the prompt from stdin, start the ONE MCP server they were configured with, call tools through it over
real MCP stdio, and print one JSON object per line.

    python fake_cli.py claude <flags of `claude -p ...`>
    python fake_cli.py codex exec <flags of `codex exec ...`>
    python fake_cli.py acp

The scenario is the word after ``scenario=`` in the prompt (default ``ok``). With FAKE_CLI_LOG=<dir> every run
writes <dir>/<mode>-<n>.json = {'argv', 'stdin', 'pid', 'mcp': {...}} so the test can check flags and that a
cancelled run's process is gone. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

EDIT_OPS = {"ops": [{"op": "set_field", "ref": "R1", "field": "Value", "value": "4k7"}]}


def out(obj) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def log(mode: str, record: dict) -> None:
    d = os.environ.get("FAKE_CLI_LOG")
    if not d:
        return
    os.makedirs(d, exist_ok=True)
    n = len([f for f in os.listdir(d) if f.startswith(mode + "-")]) + 1
    with open(os.path.join(d, f"{mode}-{n}.json"), "w", encoding="utf-8") as f:
        json.dump(dict(record, pid=os.getpid()), f)


def scenario(text: str) -> str:
    m = re.findall(r"scenario=(\w+)", text)
    return m[-1] if m else "ok"


class Mcp:
    """A minimal MCP stdio client: what a CLI does with the server it was given."""

    def __init__(self, command: str, args: list, env: dict | None = None):
        self.p = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=dict(os.environ, **(env or {})))
        self.n = 0
        self.init = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "fake-cli", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = [t["name"] for t in self.call("tools/list", {}).get("tools", [])]

    def send(self, msg):
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()

    def call(self, method, params):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise SystemExit("mcp server closed")
            msg = json.loads(line)
            if msg.get("id") == self.n:
                if "error" in msg:
                    return {"content": [{"type": "text", "text": msg["error"].get("message", "")}], "isError": True}
                return msg["result"]

    def tool(self, name, args):
        return self.call("tools/call", {"name": name, "arguments": args})

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(5)
        except Exception:  # noqa: BLE001
            self.p.kill()


def first_line(res) -> str:
    text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    return (text.splitlines() or [""])[0][:80]


# ── claude -p --output-format stream-json ────────────────────────────────
def claude(argv: list) -> int:
    flags, i = {}, 0
    takes = ("--output-format", "--mcp-config", "--tools", "--disallowedTools", "--allowedTools", "--permission-mode",
             "--append-system-prompt-file", "--model", "--resume")
    while i < len(argv):
        if argv[i] in takes:
            flags[argv[i]] = argv[i + 1]
            i += 2
        else:
            flags[argv[i]] = True
            i += 1
    text = sys.stdin.read()
    sc = scenario(text)
    cfg = json.load(open(flags["--mcp-config"], encoding="utf-8"))["mcpServers"]
    system = open(flags["--append-system-prompt-file"], encoding="utf-8").read()
    log("claude", {"argv": argv, "flags": {k: v for k, v in flags.items()}, "stdin": text, "servers": sorted(cfg),
                   "system_chars": len(system)})
    sid = flags.get("--resume") or "sess-0001"
    base = {"session_id": sid, "parent_tool_use_id": None}

    def delta(t):
        out(dict(base, type="stream_event", event={"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": t}}))

    def assistant(content, mid="msg_1", usage=None):
        out(dict(base, type="assistant", message={"id": mid, "type": "message", "role": "assistant", "model": "fake-model",
                                                  "content": content, "usage": usage or {"input_tokens": 10, "output_tokens": 5}}))

    def result(text_, is_error=False, subtype="success", cost=0.01):
        out({"type": "result", "subtype": subtype, "is_error": is_error, "result": text_, "session_id": sid,
             "total_cost_usd": cost, "num_turns": 1,
             "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 20}})

    if sc == "stale" and flags.get("--resume"):
        result(f"No conversation found with session ID: {flags['--resume']}", True, "error_during_execution")
        return 1
    if sc == "auth":
        out({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 1, "retry_delay_ms": 10, "error_status": 401,
             "error": "authentication_failed", "session_id": sid})
        result("Invalid API key · Please run /login", True)
        return 1
    srv = cfg["copper"]
    mcp = Mcp(srv["command"], srv.get("args", []), srv.get("env"))
    out({"type": "system", "subtype": "init", "session_id": sid, "model": flags.get("--model") or "fake-model", "cwd": os.getcwd(),
         "tools": ["mcp__copper__" + t for t in mcp.tools], "mcp_servers": [{"name": "copper", "status": "connected"}]})
    out(dict(base, type="stream_event", event={"type": "message_start", "message": {"id": "msg_1"}}))
    try:
        if sc == "cut":
            delta("Hel")
            return 0
        if sc == "stall":
            delta("thinking")
            time.sleep(120)
            return 0
        if sc == "own":
            assistant([{"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "del *.kicad_sch"}}])
            out(dict(base, type="user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_9",
                 "is_error": True, "content": "Permission to use Bash has been denied."}]}))
            delta("I cannot run commands here.")
            assistant([{"type": "text", "text": "I cannot run commands here."}], "msg_2")
            result("I cannot run commands here.")
            return 0
        if sc == "notext":
            result("Only the result carries this.")
            return 0
        tool, args = ("sch_apply", EDIT_OPS) if sc == "edit" else ("copper_help", {})
        delta("Looking. ")
        assistant([{"type": "text", "text": "Looking. "}])
        assistant([{"type": "tool_use", "id": "toolu_1", "name": "mcp__copper__" + tool, "input": args}])
        res = mcp.tool(tool, args)
        out(dict(base, type="user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
             "content": res.get("content"), "is_error": bool(res.get("isError"))}]}))
        final = (f"tools={len(mcp.tools)} tool_ok={not res.get('isError')} resume={flags.get('--resume') or 'none'} "
                 f"instructions={'yes' if mcp.init.get('instructions') else 'no'} live={'yes' if 'live' in json.dumps(res) else 'no'}")
        delta(final)
        assistant([{"type": "text", "text": final}], "msg_2", {"input_tokens": 12, "cache_read_input_tokens": 100, "output_tokens": 15})
        result("Looking. " + final, cost=0.025 if flags.get("--resume") else 0.01)
        return 0
    finally:
        mcp.close()


# ── codex exec --json ─────────────────────────────────────────────────────
def _toml(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_toml(x) for x in re.findall(r"'[^']*'|[^,\[\]]+", v[1:-1]) if x.strip()]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1]
    return {"true": True, "false": False}.get(v, int(v) if v.isdigit() else v)


def codex(argv: list) -> int:
    assert argv[0] == "exec", argv
    cfg, flags, pos, i = {}, {}, [], 1
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "--config"):
            k, _, v = argv[i + 1].partition("=")
            cfg[k] = _toml(v)
            i += 2
        elif a in ("--sandbox", "-m", "--model"):
            flags[a] = argv[i + 1]
            i += 2
        elif a.startswith("-") and a != "-":
            flags[a] = True
            i += 1
        else:
            pos.append(a)
            i += 1
    resume = pos[1] if pos[:1] == ["resume"] and len(pos) > 1 else None
    text = sys.stdin.read() if pos and pos[-1] == "-" else ""
    sc = scenario(text)
    log("codex", {"argv": argv, "config": cfg, "flags": flags, "positional": pos, "stdin": text})
    thread = resume or "0199a213-81c0-7800-8aa1-bbab2a035a53"
    out({"type": "thread.started", "thread_id": thread})
    out({"type": "turn.started"})
    if sc == "failed":
        out({"type": "error", "message": "Reconnecting... 1/5"})
        out({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized: not logged in"}})
        return 1
    if sc == "cut":
        out({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hel"}})
        return 0
    if sc == "stall":
        time.sleep(120)
        return 0
    mcp = Mcp(cfg["mcp_servers.copper.command"], cfg["mcp_servers.copper.args"])
    try:
        if sc == "own":
            out({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "bash -lc ls",
                                                  "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
            out({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "bash -lc ls",
                                                    "aggregated_output": "sandbox denied", "exit_code": 1, "status": "failed"}})
        out({"type": "item.completed", "item": {"id": "item_2", "type": "reasoning", "text": "**Planning** the call"}})
        out({"type": "item.started", "item": {"id": "item_3", "type": "mcp_tool_call", "server": "copper", "tool": "copper_help",
                                              "arguments": {}, "result": None, "error": None, "status": "in_progress"}})
        res = mcp.tool("copper_help", {})
        out({"type": "item.completed", "item": {"id": "item_3", "type": "mcp_tool_call", "server": "copper", "tool": "copper_help",
                                                "arguments": {}, "result": {"content": res.get("content"), "structured_content": None},
                                                "error": None, "status": "completed"}})
        out({"type": "item.completed", "item": {"id": "item_4", "type": "agent_message", "phase": "final_answer",
             "text": f"tools={len(mcp.tools)} tool_ok={not res.get('isError')} resume={resume or 'none'} "
                     f"instructions={'yes' if '[instructions]' in text else 'no'}"}})
        usage = ({"input_tokens": 2500, "cached_input_tokens": 2000, "output_tokens": 120} if resume else
                 {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50})
        out({"type": "turn.completed", "usage": dict(usage, reasoning_output_tokens=10)})
        return 0
    finally:
        mcp.close()


# ── an ACP agent ──────────────────────────────────────────────────────────
def acp(argv: list) -> int:
    state = {"n": 100, "mcp": None, "session": None}
    log("acp", {"argv": argv})

    def read():
        line = sys.stdin.readline()
        if not line:
            raise SystemExit(0)
        return json.loads(line)

    def ask(method, params):
        """A request from the agent to the client; serves nothing else meanwhile."""
        state["n"] += 1
        out({"jsonrpc": "2.0", "id": state["n"], "method": method, "params": params})
        while True:
            msg = read()
            if msg.get("id") == state["n"] and "method" not in msg:
                return msg

    def update(u):
        out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": state["session"], "update": u}})

    while True:
        msg = read()
        mid, method, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            state["caps"] = p.get("clientCapabilities")
            out({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {
                "loadSession": False, "sessionCapabilities": {"resume": {}}, "promptCapabilities": {"image": False}},
                "agentInfo": {"name": "fake-agent", "version": "0"}, "authMethods": []}})
        elif method in ("session/new", "session/resume"):
            srv = p["mcpServers"][0]
            state["mcp"] = Mcp(srv["command"], srv["args"])
            state["session"] = p.get("sessionId") or "sess_abc123"
            state["resumed"] = method == "session/resume"
            state["servers"] = [s["name"] for s in p["mcpServers"]]
            out({"jsonrpc": "2.0", "id": mid, "result": {} if method == "session/resume" else {"sessionId": state["session"]}})
        elif method == "session/prompt":
            text = "".join(b.get("text", "") for b in p["prompt"])
            sc = scenario(text)
            mcp = state["mcp"]
            if sc == "stall":
                update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "thinking"}})
                while True:
                    m = read()
                    if m.get("method") == "session/cancel":
                        log("acp-cancel", {"got": "session/cancel"})
                        out({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "cancelled"}})
                        time.sleep(120)                      # a badly behaved agent that does not exit
            if sc == "cut":
                update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hel"}})
                return 0
            update({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "plan"}})
            update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Looking. "}})
            # 1. a Copper tool: ask for permission, then really call it through MCP
            update({"sessionUpdate": "tool_call", "toolCallId": "call_1", "title": "copper_help (copper MCP)", "kind": "other",
                    "status": "pending", "rawInput": {}})
            options = [{"optionId": "a1", "name": "Allow once", "kind": "allow_once"},
                       {"optionId": "a2", "name": "Always allow", "kind": "allow_always"},
                       {"optionId": "r1", "name": "Reject", "kind": "reject_once"}]
            a = ask("session/request_permission", {"sessionId": state["session"], "toolCall": {"toolCallId": "call_1"},
                                                   "options": options})["result"]["outcome"]
            res = mcp.tool("copper_help", {}) if a.get("optionId") == "a1" else {"isError": True, "content": []}
            update({"sessionUpdate": "tool_call_update", "toolCallId": "call_1", "status": "completed"})
            # 2. the agent's own file tool: must be rejected
            update({"sessionUpdate": "tool_call", "toolCallId": "call_2", "title": "Write copper_notes.kicad_sch", "kind": "edit",
                    "status": "pending", "rawInput": {"path": "x.kicad_sch"}})
            b = ask("session/request_permission", {"sessionId": state["session"],
                                                   "toolCall": {"toolCallId": "call_2", "title": "Write copper_notes.kicad_sch", "kind": "edit"},
                                                   "options": options})["result"]["outcome"]
            update({"sessionUpdate": "tool_call_update", "toolCallId": "call_2", "status": "failed",
                    "content": [{"type": "content", "content": {"type": "text", "text": "rejected by the client"}}]})
            # 3. the client's file system: must not be offered
            c = ask("fs/read_text_file", {"sessionId": state["session"], "path": "C:/x"})
            final = (f"tools={len(mcp.tools)} tool_ok={not res.get('isError')} copper={a.get('optionId')} write={b.get('optionId')} "
                     f"fs={'error' if 'error' in c else 'served'} caps={json.dumps(state['caps'], sort_keys=True)} "
                     f"servers={state['servers']} resumed={state['resumed']} "
                     f"instructions={'yes' if '[instructions]' in text else 'no'}")
            update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": final}})
            out({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "refusal" if sc == "refuse" else "end_turn"}})
        elif mid is not None:
            out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


def main() -> int:
    mode, argv = sys.argv[1], sys.argv[2:]
    return {"claude": claude, "codex": codex, "acp": acp}[mode](argv)


if __name__ == "__main__":
    sys.exit(main())
