"""End-to-end MCP protocol test: spawns ``python -m copper_kicad serve`` and
drives it with real newline-delimited JSON-RPC, exactly like Claude/Codex do.

    python tests/test_protocol.py            (or: python -m pytest tests/test_protocol.py)

Set COPPER_TEST_LAUNCHER to an installed launcher (e.g. ...\\Copper\\bin\\copper.exe)
to test an installed build instead of the source tree.

Tool modules still being written by other areas are tolerated: their
LOAD_ERRORS are reported, not failed, but every tool that *is* registered must
have a valid schema and annotations.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
NOTES: list[str] = []


class McpProcess:
    def __init__(self):
        launcher = os.environ.get("COPPER_TEST_LAUNCHER")
        cmd = [launcher, "serve"] if launcher else [sys.executable, "-m", "copper_kicad", "serve"]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.home = tempfile.mkdtemp(prefix="copper-proto-")
        env["COPPER_CONFIG_HOME_OVERRIDE"] = self.home      # never read/write real client configs
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=str(SRC), env=env, bufsize=0)
        self.q: queue.Queue = queue.Queue()
        self.stderr: list[str] = []
        self.next_id = 100
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _read(self):
        for raw in iter(self.p.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self.q.put(line)

    def _err(self):
        for raw in iter(self.p.stderr.readline, b""):
            self.stderr.append(raw.decode("utf-8", errors="replace"))

    def send_raw(self, text: str):
        self.p.stdin.write((text + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def send(self, msg: dict):
        self.send_raw(json.dumps(msg))

    def request(self, method: str, params: dict | None = None, timeout=120, mid=None) -> dict:
        mid = self.next_id if mid is None else mid
        self.next_id += 1
        m = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            m["params"] = params
        self.send(m)
        return self.wait_for(mid, timeout)

    def wait_for(self, mid, timeout=120) -> dict:
        deadline = time.time() + timeout
        stash = []
        try:
            while time.time() < deadline:
                try:
                    line = self.q.get(timeout=max(0.05, deadline - time.time()))
                except queue.Empty:
                    break
                msg = json.loads(line)        # every stdout line MUST be JSON (no stray prints)
                assert msg.get("jsonrpc") == "2.0", msg
                if msg.get("id") == mid and "method" not in msg:
                    return msg
                stash.append(msg)
        finally:
            for s in stash:
                if "id" in s and "method" not in s:
                    self.q.put(json.dumps(s))   # keep other responses for their waiters
        raise AssertionError(f"no response to id={mid} within {timeout}s; stderr: {''.join(self.stderr)[-2000:]}")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=30)
        except Exception:  # noqa: BLE001
            self.p.kill()


_PROC: McpProcess | None = None


def proc() -> McpProcess:
    global _PROC
    if _PROC is None or _PROC.p.poll() is not None:
        _PROC = McpProcess()
        r = _PROC.request("initialize", {"protocolVersion": VERSIONS[0], "capabilities": {},
                                         "clientInfo": {"name": "copper-test", "version": "1"}})
        assert "result" in r, r
        _PROC.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return _PROC


def _tool_payload(res: dict) -> dict:
    sc = res.get("structuredContent")
    if isinstance(sc, dict):
        return sc
    txt = next((c["text"] for c in res.get("content", []) if c.get("type") == "text"), "")
    i = txt.find("{")
    return json.loads(txt[i:]) if i >= 0 else {}


# ── tests ─────────────────────────────────────────────────────────────────
def test_initialize_versions():
    p = McpProcess()
    try:
        for v in VERSIONS:
            r = p.request("initialize", {"protocolVersion": v, "capabilities": {"roots": {}},
                                         "clientInfo": {"name": "t", "version": "0"}})
            assert "result" in r, r
            res = r["result"]
            assert res["protocolVersion"] == v, (v, res["protocolVersion"])
            assert res["serverInfo"]["name"] == "copper" and res["serverInfo"]["version"]
            caps = res["capabilities"]
            assert "tools" in caps and "resources" in caps and "prompts" in caps
            assert isinstance(res.get("instructions"), str) and 100 < len(res["instructions"]) < 4096
        # unknown future/past version → server answers with one it supports
        r = p.request("initialize", {"protocolVersion": "2099-01-01", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        assert r["result"]["protocolVersion"] in VERSIONS
        p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})   # must not produce a response
        r = p.request("ping")
        assert r["result"] == {}
    finally:
        p.close()


def test_ping_and_unknown_method():
    p = proc()
    assert p.request("ping")["result"] == {}
    r = p.request("does/not/exist")
    assert r["error"]["code"] == -32601, r


def test_parse_error():
    p = proc()
    p.send_raw("{this is not json")
    msg = json.loads(p.q.get(timeout=30))
    assert msg["error"]["code"] == -32700 and msg["id"] is None, msg
    assert p.request("ping")["result"] == {}     # still alive


def test_tools_list_schemas_and_annotations():
    p = proc()
    tools = p.request("tools/list")["result"]["tools"]
    assert tools, "no tools registered at all"
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names"
    core = 0
    for t in tools:
        n = t["name"]
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n), n
        assert isinstance(t.get("description"), str) and len(t["description"]) >= 40, f"{n}: description too thin"
        s = t["inputSchema"]
        assert s.get("type") == "object" and isinstance(s.get("properties"), dict), n
        for r in s.get("required", []):
            assert r in s["properties"], f"{n}: required '{r}' not in properties"
        for pn, ps in s["properties"].items():
            assert isinstance(ps, dict) and ("type" in ps or "anyOf" in ps or "oneOf" in ps or "enum" in ps), f"{n}.{pn}"
            if ps.get("type") == "array":
                assert "items" in ps, f"{n}.{pn}: array without items (some clients reject this)"
        # lean annotations (mcp/catalogue.py): the title is top-level only; a read-only tool says so and nothing
        # else, a tool that writes always states destructiveHint (the protocol default would be true)
        ann = t["annotations"]
        assert t.get("title") and "title" not in ann, n
        assert all(isinstance(v, bool) for v in ann.values()), (n, ann)
        assert isinstance(ann.get("openWorldHint"), bool), f"{n}: openWorldHint"
        if ann.get("readOnlyHint"):
            assert "destructiveHint" not in ann, f"{n}: read-only tool marked destructive"
        else:
            assert "readOnlyHint" not in ann and isinstance(ann.get("destructiveHint"), bool), f"{n}: destructiveHint"
        if (t.get("_meta") or {}).get("anthropic/alwaysLoad"):
            core += 1
    assert core <= 8, f"{core} always-loaded tools — keep the upfront context small"
    total_desc = sum(len(t["description"]) for t in tools)
    NOTES.append(f"{len(tools)} tools, {core} always-loaded, {total_desc} chars of descriptions")


def test_copper_status_call():
    p = proc()
    names = [t["name"] for t in p.request("tools/list")["result"]["tools"]]
    if "copper_status" not in names:
        NOTES.append("copper_status not registered yet (project_tools still in progress?) — skipped")
        return
    r = p.request("tools/call", {"name": "copper_status", "arguments": {}}, timeout=180)
    res = r["result"]
    assert res["isError"] is False, res
    assert res["content"][0]["type"] == "text"
    payload = _tool_payload(res)
    assert payload, "copper_status returned no structured payload"
    errs = payload.get("tool_load_errors") or payload.get("load_errors") or {}
    if errs:
        NOTES.append(f"tool modules failing to load: {list(errs) if isinstance(errs, dict) else errs}")


def test_bad_arguments_are_actionable():
    p = proc()
    tools = p.request("tools/list")["result"]["tools"]
    target = next((t for t in tools if t["name"] == "copper_status"), tools[0])
    r = p.request("tools/call", {"name": target["name"], "arguments": {"definitely_not_an_arg": 1}})
    res = r["result"]
    assert res["isError"] is True, res
    d = _tool_payload(res)
    assert d.get("error") == "BAD_ARGUMENT", d
    assert d.get("fix") or "valid:" in d.get("message", ""), f"bad-args error gives the model no way forward: {d}"
    if not d.get("fix"):
        NOTES.append("BAD_ARGUMENT errors carry no `fix` (core registry) — message lists valid args instead")
    # wrong type
    req = [t for t in tools if t["inputSchema"].get("required")]
    if req:
        t = req[0]
        r = p.request("tools/call", {"name": t["name"], "arguments": {}})
        d = _tool_payload(r["result"])
        assert r["result"]["isError"] and "missing required" in d.get("message", ""), d


def test_unknown_tool():
    p = proc()
    r = p.request("tools/call", {"name": "no_such_tool", "arguments": {}})
    res = r["result"]
    assert res["isError"] is True
    d = _tool_payload(res)
    assert d.get("error") == "UNKNOWN_TOOL" and d.get("fix"), d


def test_concurrent_ping_during_tool_call():
    p = proc()
    names = [t["name"] for t in p.request("tools/list")["result"]["tools"]]
    if "copper_status" not in names:
        return
    p.send({"jsonrpc": "2.0", "id": 9001, "method": "tools/call", "params": {"name": "copper_status", "arguments": {}}})
    t0 = time.time()
    assert p.request("ping", timeout=30)["result"] == {}
    assert time.time() - t0 < 10, "ping blocked behind a tool call"
    r = p.wait_for(9001, timeout=180)
    assert "result" in r


def test_resources_and_prompts():
    p = proc()
    r = p.request("resources/list")
    if "error" in r:
        NOTES.append(f"resources/list error: {r['error']}")
        raise AssertionError(r)
    res = r["result"]["resources"]
    for x in res:
        assert x["uri"] and x["name"], x
    r = p.request("resources/templates/list")
    assert "result" in r, r
    r = p.request("prompts/list")
    assert "result" in r, r
    prompts = r["result"]["prompts"]
    for pr in prompts:
        assert pr["name"] and pr.get("description"), pr
    if prompts:
        pr = prompts[0]
        args = {a["name"]: "a USB-C LED blinker" for a in pr.get("arguments", []) if a.get("required")}
        g = p.request("prompts/get", {"name": pr["name"], "arguments": args})
        assert "result" in g, g
        msgs = g["result"]["messages"]
        assert msgs and msgs[0]["content"]["type"] == "text" and len(msgs[0]["content"]["text"]) > 50
    if res:
        rr = p.request("resources/read", {"uri": res[0]["uri"]}, timeout=180)
        assert "result" in rr or rr.get("error", {}).get("code") in (-32002, -32602, -32603), rr
    NOTES.append(f"{len(res)} resources, {len(prompts)} prompts")


def test_stdout_is_pure_jsonrpc():
    """Nothing but JSON-RPC may reach stdout (a stray print breaks every client)."""
    p = McpProcess()
    try:
        p.request("initialize", {"protocolVersion": VERSIONS[1], "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        p.request("tools/list")
        p.request("prompts/list")
        p.request("resources/list")
        time.sleep(0.5)
        while not p.q.empty():
            json.loads(p.q.get())   # raises if any non-JSON line was emitted
    finally:
        p.close()


def test_clean_shutdown_on_eof():
    p = McpProcess()
    p.request("ping")
    p.p.stdin.close()
    try:
        rc = p.p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.p.kill()
        raise AssertionError("server did not exit when stdin closed")
    assert rc == 0, f"exit code {rc}: {''.join(p.stderr)[-1000:]}"


class _BareMcpProcess(McpProcess):
    """`copper` with no arguments, as the .mcpb entry_point / a bare client entry launches it,
    with a hand-broken Claude Desktop config in the sandbox home."""

    def __init__(self):
        launcher = os.environ.get("COPPER_TEST_LAUNCHER")
        cmd = [launcher] if launcher else [sys.executable, "-m", "copper_kicad"]
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.home = tempfile.mkdtemp(prefix="copper-proto-bare-")
        # where clients._claude_desktop_dirs reads under COPPER_CONFIG_HOME_OVERRIDE on this platform
        sub = (("AppData", "Roaming") if sys.platform == "win32" else ("Library", "Application Support")
               if sys.platform == "darwin" else (".config",))
        cfg = Path(self.home).joinpath(*sub, "Claude", "claude_desktop_config.json")
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"mcpServers":{"fs":{"command":"npx","args":["C:\\Users\\me"]}}}', encoding="utf-8")
        env["COPPER_CONFIG_HOME_OVERRIDE"] = self.home
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=str(SRC), env=env, bufsize=0)
        self.q, self.stderr, self.next_id = queue.Queue(), [], 100
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()


def test_bare_launch_serves_and_survives_broken_client_config():
    """Distribution regression: bare `copper` on a pipe serves MCP (and says so on stderr only),
    and copper_status works although a client config has an invalid JSON escape."""
    p = _BareMcpProcess()
    try:
        r = p.request("initialize", {"protocolVersion": VERSIONS[0], "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
        assert "result" in r, r
        p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = p.request("tools/call", {"name": "copper_status", "arguments": {}}, timeout=180)
        assert "result" in r and not r["result"].get("isError"), r
        time.sleep(0.3)
        assert "serving MCP on stdio" in "".join(p.stderr), "".join(p.stderr)[-1000:]
        assert "Traceback" not in "".join(p.stderr), "".join(p.stderr)[-2000:]
    finally:
        p.close()


# ── runner ────────────────────────────────────────────────────────────────
def test_bom_prefixed_line_and_invalid_requests():
    """Core patches: a UTF-8 BOM before the first request is ignored; non-object requests, empty batches and
    non-object params get JSON-RPC errors instead of killing the read loop."""
    p = McpProcess()
    try:
        p.p.stdin.write(("\ufeff" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n").encode("utf-8"))
        p.p.stdin.flush()
        assert p.wait_for(1, timeout=30)["result"] == {}
        p.send_raw("5")
        e = p.wait_for(None, timeout=30)
        assert e["error"]["code"] == -32600, e
        p.send_raw("[]")
        e = p.wait_for(None, timeout=30)
        assert e["error"]["code"] == -32600, e
        p.send({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": [1, 2]})
        assert p.wait_for(7, timeout=30)["error"]["code"] == -32602
        assert p.request("ping", timeout=30)["result"] == {}          # still serving
    finally:
        p.close()


def test_bad_resource_and_prompt_params_are_32602():
    p = proc()
    for method, params in (("resources/read", {}), ("prompts/get", {"name": "no_such_prompt"})):
        r = p.request(method, params)
        assert r.get("error", {}).get("code") == -32602, (method, r)


def test_tool_errors_are_compact_and_actionable():
    p = proc()
    r = p.request("tools/call", {"name": "verify", "arguments": {"what": ["nope"]}})["result"]
    d = _tool_payload(r)
    assert r["isError"] and d["error"] == "BAD_ARGUMENT" and "erc" in d.get("fix", ""), d
    assert "\n" not in r["content"][0]["text"], r["content"][0]["text"]     # compact JSON, not indented
    r = p.request("tools/call", {"name": "verify", "arguments": ["erc"]})["result"]
    assert r["isError"] and _tool_payload(r)["error"] == "BAD_ARGUMENT", r


def test_long_examples_live_in_help_not_in_the_catalogue():
    """tools/list is loaded up front by every client: long examples are registered with tool(help=…) and
    served on demand through copper://help/<tool> and the copper_help tool."""
    p = proc()
    tools = {t["name"]: t for t in p.request("tools/list")["result"]["tools"]}
    total = len(json.dumps({"tools": list(tools.values())}))
    NOTES.append(f"tools/list is {total} bytes")
    mine = ("copper_status", "project_create", "project_open", "project_summary", "checkpoint", "copper_help",
            "verify", "design_review", "render")
    for n in mine:
        assert len(json.dumps(tools[n])) <= 1800, (n, len(json.dumps(tools[n])))
    assert sum(len(json.dumps(tools[n])) for n in mine) <= 10800
    for n in ("verify", "render", "checkpoint", "project_create"):
        d = tools[n]["description"]
        # one example in the catalogue; no per-tool "more: copper_help(...)" line (the server instructions say it once)
        assert "copper_help(" not in d and d.count("Example") == 1, (n, d[-200:])
    # the resource: an index, and one page per tool with description + examples + arguments
    uris = [r["uri"] for r in p.request("resources/list")["result"]["resources"]]
    assert "copper://help" in uris, uris
    tpl = [t["uriTemplate"] for t in p.request("resources/templates/list")["result"]["resourceTemplates"]]
    assert "copper://help/{tool}" in tpl, tpl
    idx = json.loads(p.request("resources/read", {"uri": "copper://help"})["result"]["contents"][0]["text"])
    rows = {r["tool"]: r for r in idx["tools"]}
    assert set(rows) == set(tools) and rows["verify"]["help_chars"] > 500 and rows["verify"]["uri"] == "copper://help/verify"
    c = p.request("resources/read", {"uri": "copper://help/verify"})["result"]["contents"][0]
    assert c["mimeType"] == "text/markdown", c["mimeType"]
    for must in ("# verify", "## Examples", "verify(what=['erc','drc','review'], limit=40)", '"dangling":true', "## Arguments", "`limit`"):
        assert must in c["text"], must
    bad = json.loads(p.request("resources/read", {"uri": "copper://help/nope"})["result"]["contents"][0]["text"])
    assert bad["error"] == "NOT_FOUND" and "verify" in bad["fix"], bad
    # the tool: same text for clients whose model cannot read resources
    r = p.request("tools/call", {"name": "copper_help", "arguments": {"tool": "render"}})["result"]
    assert not r["isError"] and "render(target='pcb3d', view='iso')" in r["content"][0]["text"], r
    r = p.request("tools/call", {"name": "copper_help", "arguments": {}})["result"]
    listed = {x["tool"] for x in _tool_payload(r)["tools"]}
    assert listed == set(tools), listed ^ set(tools)
    # a tool without registered help still gets a page (description + arguments), and no pointer in its description
    plain = next((n for n in tools if rows[n]["help_chars"] == 0), None)
    if plain:
        assert "copper_help(tool='" + plain not in tools[plain]["description"]
        assert "## Arguments" in p.request("resources/read", {"uri": f"copper://help/{plain}"})["result"]["contents"][0]["text"] \
            or not tools[plain]["inputSchema"]["properties"]
    # argument errors point at the examples
    d = _tool_payload(p.request("tools/call", {"name": "render", "arguments": {"target": "nope"}})["result"])
    assert d["error"] == "BAD_ARGUMENT" and "copper_help(tool='render')" in d.get("help", ""), d


def test_add_help_registry_api():
    sys.path.insert(0, str(SRC))
    try:
        from copper_kicad.mcp import registry
    finally:
        sys.path.remove(str(SRC))
    saved_t, saved_h = dict(registry.TOOLS), dict(registry.HELP)
    try:
        @registry.tool("zz_demo", "Does a demo thing. Example: zz_demo(x=1)", {"x": {"type": "integer", "description": "how many"}},
                       ["x"], help="""## Examples
                       zz_demo(x=1)
                         zz_demo(x=2)   # indented on purpose""")
        def zz_demo(x):
            return {"x": x}
        registry.add_help("zz_demo", "## Recipes\nuse it twice")
        registry.add_help("zz_later", "registered before the tool exists")
        assert registry.HELP["zz_demo"] == "## Examples\nzz_demo(x=1)\n  zz_demo(x=2)   # indented on purpose\n\n## Recipes\nuse it twice"
        d = registry.TOOLS["zz_demo"].describe()["description"]
        assert d == "Does a demo thing. Example: zz_demo(x=1)", d      # no pointer line: BAD_ARGUMENT errors name the help
        assert registry.TOOLS["zz_demo"].has_help()
        t = registry.help_text("zz_demo")
        assert t.startswith("# zz_demo\n") and "## Recipes" in t and "- `x` (integer, required): how many" in t, t
        try:
            registry.help_text("zz_missing")
            raise AssertionError("no error for an unknown tool")
        except registry.ToolError as e:
            assert e.code == "NOT_FOUND"
    finally:
        registry.TOOLS.clear(); registry.TOOLS.update(saved_t)
        registry.HELP.clear(); registry.HELP.update(saved_h)


def _main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        t0 = time.time()
        try:
            f()
            print(f"PASS {n} ({time.time() - t0:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}\n{traceback.format_exc()}")
    if _PROC:
        _PROC.close()
    for note in NOTES:
        print(f"NOTE {note}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


def teardown_module(_m=None):   # pytest hook
    if _PROC:
        _PROC.close()


if __name__ == "__main__":
    sys.exit(_main())
