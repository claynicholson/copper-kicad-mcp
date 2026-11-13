"""Model-driver tests: the two API-key drivers against a local fake provider (tests/fake_models.py) and the three
agent-CLI drivers against fake CLIs (tests/fake_cli.py), all through a real copper-agent process and
tests/fakehost.py. No model provider is contacted, no real credential store or config is touched, nothing
leaves a temp folder.

    python tests/test_agent_drivers.py [name-fragment ...]      (KiCad's Python, or any Python 3.10+ with KiCad installed)

COPPER_TEST_TMP=<dir> puts the scratch files there instead of the system temp folder.
The real CLIs are not installed on the build machine: what these tests prove is that each driver builds the
documented command line, survives a Windows ``.cmd`` shim, parses the documented stream format, and that tool
calls made by a CLI through its one MCP server land in the app as a normal proposal.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tests"))

_BASE = os.environ.get("COPPER_TEST_TMP")
if _BASE:
    Path(_BASE).mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="copper-test-drivers-", dir=_BASE))
for _k, _d in (("COPPER_HOME", "home"), ("COPPER_CONFIG_HOME_OVERRIDE", "cfg"), ("KICAD_CONFIG_HOME", "kicad-config")):
    (TMP / _d).mkdir()
    os.environ[_k] = str(TMP / _d)             # inherited by every agent: no real ~/.copper, keychain or KiCad config
os.environ["COPPER_HOME"] = str(TMP / "home")        # spelled out: test_install.py checks every module for it
for _k in list(os.environ):                    # no real key of this machine may reach a test process
    if _k.upper().endswith(("_API_KEY", "_AUTH_TOKEN")):
        del os.environ[_k]

from fakehost import FakeHost  # noqa: E402
from fake_models import KEY, FakeModels  # noqa: E402
from copper_kicad.agent import events, prompt  # noqa: E402

CLI_LOG = TMP / "cli-log"
FAKE_CLI = ROOT / "tests" / "fake_cli.py"
ENV = {"COPPER_AGENT_CANCEL_GRACE_S": "1.0", "COPPER_AGENT_LOG": "debug", "COPPER_AGENT_BACKOFF_S": "0.05",
       "FAKE_CLI_LOG": str(CLI_LOG)}
IS_WIN = sys.platform == "win32"
NOTES: list[str] = []
_STATE: dict = {}
_N = [0]


def fixture() -> Path:
    """A tiny real project (two resistors on a net), built once with the engine itself."""
    if "fixture" not in _STATE:
        from copper_kicad.mcp import registry
        from copper_kicad.review import project_info
        import copper_kicad.tools  # noqa: F401
        r = project_info.create_project("drv", str(TMP / "fixture"), "blank")
        d = r["project"]["dir"]
        out = registry.call_tool("sch_apply", {"project": d, "ops": [
            {"op": "place", "lib_id": "Device:R", "ref": "R1", "value": "10k"},
            {"op": "place", "lib_id": "Device:R", "ref": "R2", "value": "10k"},
            {"op": "connect", "pins": ["R1.2", "R2.1"], "net": "MID"},
            {"op": "connect", "pins": ["R1.1"], "net": "TOP"},
            {"op": "connect", "pins": ["R2.2"], "net": "GND"}]})
        assert not out.get("isError"), out["content"][0]["text"][:800]
        _STATE["fixture"] = Path(d)
    return _STATE["fixture"]


def models() -> FakeModels:
    if "models" not in _STATE:
        _STATE["models"] = FakeModels().start()
    return _STATE["models"]


def host() -> FakeHost:
    if "host" not in _STATE:
        _N[0] += 1
        h = FakeHost(fixture(), TMP / "shared", env=ENV).start()
        h.hello_result = h.hello()
        srv = models()
        h.msg = h.request("accounts.add", {"kind": "api-messages", "label": "messages", "base_url": srv.url, "secret": KEY})["account"]["id"]
        h.chat = h.request("accounts.add", {"kind": "api-chat-completions", "label": "chat", "base_url": srv.url + "/v1",
                                            "secret": KEY})["account"]["id"]
        _STATE["host"] = h
    return _STATE["host"]


def conforming(evs: list[dict]) -> list[dict]:
    problems = events.check_stream(evs)
    assert not problems, (problems, [json.dumps(e)[:300] for e in evs])
    return evs


def types(evs) -> list[str]:
    return [e["type"] for e in evs]


def answer(evs) -> str:
    return "".join(e["text"] for e in evs if e["type"] == "text_delta" and not e.get("channel"))


def reasoning(evs) -> str:
    return "".join(e["text"] for e in evs if e["type"] == "text_delta" and e.get("channel") == "reasoning")


def error_of(evs) -> dict:
    assert types(evs)[-2:] == ["error", "done"] and evs[-1]["status"] == "error", types(evs)
    return evs[-2]


def run(h, account, model, text="hi", timeout=60, **kw) -> list[dict]:
    return conforming(h.run_turn(text, account=account, model=model, timeout=timeout, **kw))


def sheet(h: FakeHost) -> Path:
    return next(h.live.glob("*.kicad_sch"))


def both(h):
    return (("messages", h.msg, "/v1/messages"), ("chat", h.chat, "/v1/chat/completions"))


def last(srv, path, model):
    return [r for r in srv.of(model) if r["path"] == path]


# ── registration ──────────────────────────────────────────────────────────
def test_all_drivers_register_with_neutral_kinds():
    h = host()
    kinds = {d["kind"]: d for d in h.hello_result["drivers"]}
    assert {"none", "api-messages", "api-chat-completions", "cli-stream-json", "cli-exec-json", "cli-acp"} <= set(kinds), sorted(kinds)
    assert h.hello_result["load_errors"] == {}, h.hello_result["load_errors"]
    for k in kinds:
        assert not any(w in k for w in ("claude", "codex", "openai", "anthropic", "gpt")), k
    assert kinds["api-messages"]["needs"] == ["secret"] and "base_url" in kinds["api-chat-completions"]["needs"]
    assert kinds["cli-acp"]["needs"] == ["command"]
    for k in ("cli-stream-json", "cli-exec-json"):                       # not installed here: says so, does not fail
        if not kinds[k]["available"]:
            assert "not on PATH" in kinds[k]["reason"], kinds[k]


def test_models_come_from_the_provider_not_from_the_code():
    h = host()
    res = h.request("accounts.test", {"id": h.msg})
    assert res["ok"] is True and [m["id"] for m in res["models"]] == ["model-a", "model-b"], res
    assert res["models"][0]["label"] == "Model A"
    res = h.request("accounts.test", {"id": h.chat})
    assert res["ok"] is True and [m["id"] for m in res["models"]] == ["local-small", "local-large"], res
    bad = h.request("accounts.add", {"kind": "api-messages", "label": "bad", "base_url": models().url, "secret": "sk-wrong-000000"})["account"]["id"]
    res = h.request("accounts.test", {"id": bad})
    assert res["ok"] is False and res["kind"] == "auth" and "sk-wrong" not in json.dumps(res), res
    nolist = h.request("accounts.add", {"kind": "api-chat-completions", "label": "nolist", "base_url": models().url + "/nolist"})["account"]["id"]
    res = h.request("accounts.test", {"id": nolist})
    assert res["ok"] is True and res["models"] == [], res
    for acct in (h.msg, h.chat):                                         # no model chosen -> a clear error, no request sent
        n = len(models().requests)
        evs = conforming(h.run_turn("hi", account=acct, timeout=30))
        err = error_of(evs)
        assert err["kind"] == "engine" and err["code"] == "NO_MODEL" and err["fix"], err
        assert len(models().requests) == n
    for f in list((SRC / "copper_kicad" / "agent" / "drivers").glob("*.py")):
        text = f.read_text(encoding="utf-8")
        import re
        assert not re.search(r"claude-(opus|sonnet|haiku|fable|\d)|gpt-\d|o\d-(mini|preview)", text), f"{f.name} hard-codes a model id"


# ── text streaming ────────────────────────────────────────────────────────
def test_text_streams_with_usage_and_a_cacheable_prefix():
    h, srv = host(), models()
    for name, acct, path in both(h):
        evs = run(h, acct, "text", "say hello")
        assert answer(evs) == "Hello world" and evs[-1]["status"] == "ok", (name, evs)
        assert [e["text"] for e in evs if e["type"] == "text_delta"] == ["Hello", " world"], name
        usage = evs[-1]["usage"]
        req = last(srv, path, "text")[-1]
        body = req["body"]
        assert body["stream"] is True and body["model"] == "text"
        for k in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
            assert k not in body, (name, k)
        cat = prompt.tool_catalogue("chat")
        if name == "messages":
            assert usage == {"in": 190, "out": 5, "cached": 80, "model": "text"}, usage
            assert req["headers"]["x-api-key"] == KEY and req["headers"]["anthropic-version"] == "2023-06-01"
            assert "authorization" not in req["headers"]
            assert body["system"] == [{"type": "text", "text": prompt.system_prefix("chat"), "cache_control": {"type": "ephemeral"}}]
            assert [t["name"] for t in body["tools"]] == [t["name"] for t in cat] and body["tools"] == cat
            lastmsg = body["messages"][-1]
            assert lastmsg["role"] == "user" and lastmsg["content"][-1]["cache_control"] == {"type": "ephemeral"}
            assert lastmsg["content"][-1]["text"].endswith("say hello")
            assert json.dumps(body).count("cache_control") == 2 and body["max_tokens"] == 16000
        else:
            assert usage == {"in": 200, "out": 7, "cached": 150, "model": "text"}, usage
            assert req["headers"]["authorization"] == f"Bearer {KEY}"
            assert body["messages"][0] == {"role": "system", "content": prompt.system_prefix("chat")}
            assert body["messages"][-1]["content"].endswith("say hello")
            assert [t["function"]["name"] for t in body["tools"]] == [t["name"] for t in cat]
            assert body["tools"][0]["function"]["parameters"] == cat[0]["input_schema"]
            assert body["stream_options"] == {"include_usage": True}
    # the prefix is byte-identical on the next turn (what makes provider caching work), and history is replayed as text
    conv = "drv-hist"
    run(h, h.msg, "text", "first question", conversation=conv)
    run(h, h.msg, "text", "second question", conversation=conv, focus={"document": "schematic", "mode": "selection", "refs": ["R1"]})
    a, b = [r["body"] for r in last(srv, "/v1/messages", "text")[-2:]]
    assert json.dumps(a["system"]) == json.dumps(b["system"]) and json.dumps(a["tools"]) == json.dumps(b["tools"])
    assert [m["role"] for m in b["messages"]] == ["user", "assistant", "user"], b["messages"]
    assert b["messages"][0]["content"] == "first question" and b["messages"][1]["content"] == "Hello world"
    assert b["messages"][2]["content"][0]["text"].startswith("[focus] schematic | selection | parts R1\n\nsecond question")
    # quick mode: the five-tool subset and no history
    run(h, h.chat, "text", "quick one", conversation=conv, mode="quick")
    q = last(srv, "/v1/chat/completions", "text")[-1]["body"]
    assert sorted(t["function"]["name"] for t in q["tools"]) == sorted(prompt.QUICK_TOOLS) and len(q["messages"]) == 2, q["tools"]


# ── the tool loop ─────────────────────────────────────────────────────────
def test_parallel_tool_calls_run_in_order_and_a_tool_error_goes_back_to_the_model():
    h, srv = host(), models()
    for name, acct, path in both(h):
        evs = run(h, acct, "tools", "use tools")
        ts = types(evs)
        starts = [e for e in evs if e["type"] == "tool_start"]
        ends = [e for e in evs if e["type"] == "tool_end"]
        assert [s["name"] for s in starts] == ["copper_help", "no_such_tool"], (name, starts)
        assert starts[1].get("args") == {"x": 1}, starts[1]
        assert [e["ok"] for e in ends] == [True, False] and ends[1]["error"]["code"] == "UNKNOWN_TOOL", ends
        assert answer(evs) == "Checking. Done: ok,error." and evs[-1]["status"] == "ok", (name, answer(evs))
        assert reasoning(evs) == "plan", name
        assert ts.index("tool_end") < len(ts) - 1 and ts[-1] == "done"
        reqs = last(srv, path, "tools")[-2:]
        second = reqs[1]["body"]
        if name == "messages":
            assert evs[-1]["usage"] == {"in": 2080, "out": 52, "cached": 1000, "model": "tools"}, evs[-1]["usage"]
            asst, results = second["messages"][-2], second["messages"][-1]
            assert asst["role"] == "assistant" and [b["type"] for b in asst["content"]] == ["thinking", "text", "tool_use", "tool_use"]
            assert asst["content"][0] == {"type": "thinking", "thinking": "plan", "signature": "SIG123"}, "thinking goes back unchanged"
            assert asst["content"][2]["input"] == {} and asst["content"][3]["input"] == {"x": 1}
            assert results["role"] == "user" and [b["type"] for b in results["content"]] == ["tool_result", "tool_result"], \
                "every result of one step goes back in ONE user message"
            assert [b["tool_use_id"] for b in results["content"]] == ["toolu_1", "toolu_2"]
            assert "is_error" not in results["content"][0] and results["content"][1]["is_error"] is True
            assert "UNKNOWN_TOOL" in json.dumps(results["content"][1])
            assert "cache_control" in results["content"][-1] and json.dumps(second["messages"]).count("cache_control") == 1
        else:
            assert evs[-1]["usage"] == {"in": 2080, "out": 52, "cached": 1000, "model": "tools"}, evs[-1]["usage"]
            asst = second["messages"][-3]
            assert asst["role"] == "assistant" and asst["content"] == "Checking."
            assert [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in asst["tool_calls"]] == \
                [("call_a", "copper_help", "{}"), ("call_b", "no_such_tool", '{"x": 1}')], asst["tool_calls"]
            tool_msgs = second["messages"][-2:]
            assert [m["role"] for m in tool_msgs] == ["tool", "tool"] and [m["tool_call_id"] for m in tool_msgs] == ["call_a", "call_b"]
            assert "UNKNOWN_TOOL" in tool_msgs[1]["content"] and "reasoning" not in json.dumps(second["messages"])


def test_a_model_driven_edit_lands_as_one_proposal_through_the_live_host():
    h, srv = host(), models()
    f = sheet(h)
    for name, acct, path in both(h):
        before = f.read_bytes()
        n_calls = len(h.calls)
        evs = run(h, acct, "edit", "make R1 4k7", timeout=300)
        assert evs[-1]["status"] == "ok", evs
        assert [m for m, _ in h.calls[n_calls:]] in (["host.checkout", "host.apply"], ["host.apply"]), [m for m, _ in h.calls[n_calls:]]
        prop = next(e for e in evs if e["type"] == "proposal")
        assert prop["counts"] == {"added": 0, "changed": 1, "removed": 0} and prop["netlist_ok"] is True, prop
        assert '"4k7"' in f.read_text(encoding="utf-8")
        result = json.dumps(last(srv, path, "edit")[-1]["body"]["messages"][-1])
        assert "live" in result and prop["proposal_id"] in result, "the model reads that the change is under review"
        assert h.undo() == prop["proposal_id"] and f.read_bytes() == before, name
        time.sleep(0.3)


# ── failures ──────────────────────────────────────────────────────────────
def test_rate_limit_is_retried_after_the_providers_delay_and_a_long_one_is_reported():
    h, srv = host(), models()
    for name, acct, path in both(h):
        n = len(last(srv, path, "rate"))
        t0 = time.time()
        evs = run(h, acct, "rate")
        assert answer(evs) == "Hello world" and evs[-1]["status"] == "ok", (name, evs)
        assert len(last(srv, path, "rate")) == n + 2 and time.time() - t0 >= 0.28, (name, time.time() - t0)
        n = len(last(srv, path, "ratelong"))
        t0 = time.time()
        err = error_of(run(h, acct, "ratelong"))
        assert err["kind"] == "rate" and err["code"] == "HTTP_429" and err["retry_after_s"] == 600 and err["retryable"] is True, err
        assert len(last(srv, path, "ratelong")) == n + 1 and time.time() - t0 < 5, "a ten-minute wait is reported, not slept"
        n = len(last(srv, path, "five"))
        evs = run(h, acct, "five")
        assert answer(evs) == "Hello world" and len(last(srv, path, "five")) == n + 2, name


def test_auth_failure_is_an_auth_error_without_the_key():
    h, srv = host(), models()
    for name, acct, path in both(h):
        n0 = len(h.transcript)
        n = len(last(srv, path, "auth"))
        err = error_of(run(h, acct, "auth"))
        assert err["kind"] == "auth" and err["code"] == "HTTP_401" and err["retryable"] is False and err["fix"], err
        assert len(last(srv, path, "auth")) == n + 1, "auth failures are not retried"
        sent = json.dumps([t["msg"] for t in h.transcript[n0:] if t["dir"] == "agent>app"])
        assert KEY not in sent and KEY not in "".join(h.stderr), name
    for fpath in (TMP / "home").rglob("*"):
        if fpath.is_file():
            assert KEY.encode() not in fpath.read_bytes(), f"{fpath} holds the key in clear"
    for fpath in (h.live / ".copper").rglob("*.jsonl"):
        assert KEY not in fpath.read_text(encoding="utf-8")
    unknown = error_of(run(h, h.msg, "no-such-model"))
    assert unknown["kind"] == "protocol" and unknown["code"] == "HTTP_404" and "model" in unknown["fix"], unknown
    # a key is never sent over plain http to another machine
    far = h.request("accounts.add", {"kind": "api-chat-completions", "label": "far", "base_url": "http://example.invalid/v1", "secret": KEY})["account"]["id"]
    err = error_of(run(h, far, "text"))
    assert err["kind"] == "auth" and err["code"] == "INSECURE_URL", err
    # nothing listens here: a network error after the retries, quickly
    dead = h.request("accounts.add", {"kind": "api-chat-completions", "label": "dead", "base_url": "http://127.0.0.1:9/v1"})["account"]["id"]
    err = error_of(run(h, dead, "text"))
    assert err["kind"] == "network" and err["code"] == "CONNECT" and err["retryable"] is True, err


def test_mid_stream_disconnect_is_truncated_and_only_retried_before_any_text():
    h, srv = host(), models()
    for name, acct, path in both(h):
        n = len(last(srv, path, "cut"))
        t0 = time.time()
        evs = run(h, acct, "cut")
        err = error_of(evs)
        assert answer(evs) == "Hel" and err["kind"] == "network" and err["code"] == "TRUNCATED" and err["retryable"] is True, (name, evs)
        assert len(last(srv, path, "cut")) == n + 1 and time.time() - t0 < 10, "text already shown is never requested twice"
        err = error_of(run(h, acct, "maxtok"))
        assert err["kind"] == "engine" and err["code"] == "MAX_TOKENS", err
    for model in ("cutearly", "streamerr"):                      # nothing shown yet: asked again, then fine
        n = len(last(srv, "/v1/messages", model))
        evs = run(h, h.msg, model)
        assert answer(evs) == "Hello world" and len(last(srv, "/v1/messages", model)) == n + 2, model
    n = len(last(srv, "/v1/chat/completions", "nostreamopts"))    # an older server: asked again without the option
    evs = run(h, h.chat, "nostreamopts")
    reqs = last(srv, "/v1/chat/completions", "nostreamopts")[n:]
    assert answer(evs) == "Hello world" and len(reqs) == 2 and "stream_options" in reqs[0]["body"] and "stream_options" not in reqs[1]["body"]
    local = h.request("accounts.add", {"kind": "api-chat-completions", "label": "local", "base_url": srv.url + "/v1"})["account"]["id"]
    evs = run(h, local, "nokey")                                 # a local server: no key, no Authorization header
    assert answer(evs) == "Hello world" and "authorization" not in last(srv, "/v1/chat/completions", "nokey")[-1]["headers"]


def test_cancel_stops_a_streaming_turn_at_once_and_the_agent_stays_usable():
    h, srv = host(), models()
    for name, acct, path in both(h):
        turn = h.turn("wait", account=acct, model="stall")
        h.wait_event(turn, "text_delta", 20)
        t0 = time.time()
        assert h.cancel(turn) == {"cancelled": True}
        evs = conforming(h.wait_done(turn, 20))
        took = time.time() - t0
        assert evs[-1]["status"] == "cancelled" and "error" not in types(evs), (name, evs)
        assert took < 0.9, f"{name}: the blocked read was not interrupted ({took:.2f}s; the grace period is 1.0 s)"
        assert answer(run(h, acct, "text")) == "Hello world", "the agent is free for the next turn"
    srv.release.set()


def test_budget_stops_the_tool_loop_before_the_next_request():
    h, srv = host(), models()
    for name, acct, path in both(h):
        n = len(last(srv, path, "budget"))
        evs = run(h, acct, "budget", "go", mode="agent", budget_tokens=500)
        err = error_of(evs)
        assert err["kind"] == "rate" and err["code"] == "BUDGET" and err["retryable"] is False, (name, err)
        assert len(last(srv, path, "budget")) == n + 1, "the second request was not sent"
        assert types(evs).count("tool_end") == 2 and evs[-1]["usage"]["in"] == 1000, (name, evs[-1])
        n = len(last(srv, path, "budget"))
        evs = run(h, acct, "budget", "go", mode="agent", budget_tokens=50000)
        assert evs[-1]["status"] == "ok" and len(last(srv, path, "budget")) == n + 2, name
        if name == "messages":
            assert last(srv, path, "budget")[-1]["body"]["max_tokens"] == 16000


# ── agent CLIs ────────────────────────────────────────────────────────────
def cli_command(mode: str, shim: bool) -> str:
    """The account's command for a fake CLI: python + script, or (Windows) a .cmd shim like the ones npm installs."""
    if shim and IS_WIN:
        d = TMP / "shims"
        d.mkdir(exist_ok=True)
        name = {"claude": "claude", "codex": "codex"}.get(mode, mode)
        cmd = d / f"{name}.cmd"
        cmd.write_text(f'@echo off\r\n"{sys.executable}" "{FAKE_CLI}" {mode} %*\r\n', encoding="utf-8")
        return str(cmd)
    return f'"{sys.executable}" "{FAKE_CLI}" {mode}'


def cli_logs(mode: str) -> list[dict]:
    files = sorted(CLI_LOG.glob(f"{mode}-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


def alive(pid: int) -> bool:
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def account(h, kind: str, mode: str, shim: bool = True) -> str:
    key = (kind, shim)
    if key not in _STATE:
        _STATE[key] = h.request("accounts.add", {"kind": kind, "label": mode, "command": cli_command(mode, shim)})["account"]["id"]
    return _STATE[key]


def test_stream_json_cli_runs_with_only_copper_tools_and_its_stream_is_normalised():
    h = host()
    acct = account(h, "cli-stream-json", "claude")
    assert h.request("accounts.test", {"id": acct})["ok"] is True
    conv = "cli-a"
    evs = conforming(h.run_turn("what is here? scenario=ok", account=acct, model="some-model", conversation=conv, timeout=120,
                                focus={"document": "schematic", "mode": "selection", "refs": ["R1"]}))
    assert evs[-1]["status"] == "ok", evs
    text = answer(evs)
    n_tools = len(prompt.tool_catalogue("chat"))
    assert text == f"Looking. tools={n_tools} tool_ok=True resume=none instructions=yes live=no", text
    assert [e["text"] for e in evs if e["type"] == "text_delta"][0] == "Looking. ", "partial messages stream, and are not repeated"
    starts = [e for e in evs if e["type"] == "tool_start"]
    assert [s["name"] for s in starts] == ["copper_help"], "the tool row comes from the agent's own tool gate, once"
    assert evs[-1]["usage"] == {"in": 115, "out": 20, "cached": 100, "cost": 0.01}, evs[-1]["usage"]
    rec = cli_logs("claude")[-1]
    a, fl = rec["argv"], rec["flags"]
    assert a[0] == "-p" and "--bare" not in a and "--dangerously-skip-permissions" not in a, a
    assert fl["--output-format"] == "stream-json" and fl["--verbose"] is True and fl["--include-partial-messages"] is True
    assert fl["--strict-mcp-config"] is True and rec["servers"] == ["copper"], "only Copper's MCP server is attached"
    assert fl["--tools"] == "" and fl["--permission-mode"] == "dontAsk" and fl["--allowedTools"] == "mcp__copper", fl
    assert {"Bash", "Edit", "Write"} <= set(fl["--disallowedTools"].split(",")), fl
    assert fl["--model"] == "some-model" and "--resume" not in fl and rec["system_chars"] == len(prompt.system_prefix("chat"))
    assert rec["stdin"].startswith("[focus] schematic | selection | parts R1\n\nwhat is here?"), rec["stdin"][:80]
    assert not any(KEY in x for x in a)
    # the next turn of the conversation resumes the CLI's own session; its cumulative cost becomes this turn's share
    evs = conforming(h.run_turn("and now? scenario=ok", account=acct, conversation=conv, timeout=120))
    assert "resume=sess-0001" in answer(evs) and evs[-1]["usage"]["cost"] == 0.015, (answer(evs), evs[-1]["usage"])
    assert cli_logs("claude")[-1]["flags"]["--resume"] == "sess-0001" and "[earlier in this conversation]" not in cli_logs("claude")[-1]["stdin"]
    # a session the CLI no longer has: one fresh start, with the earlier turns as text
    n = len(cli_logs("claude"))
    evs = conforming(h.run_turn("again scenario=stale", account=acct, conversation=conv, timeout=120))
    logs = cli_logs("claude")[n:]
    assert evs[-1]["status"] == "ok" and len(logs) == 2 and "--resume" in logs[0]["flags"] and "--resume" not in logs[1]["flags"], evs
    assert "[earlier in this conversation]" in logs[1]["stdin"] and "User: what is here?" in logs[1]["stdin"]
    # the CLI's own tools never run; if one shows up in the stream the user sees it
    evs = conforming(h.run_turn("scenario=own", account=acct, timeout=120))
    row = next(e for e in evs if e["type"] == "tool_start")
    end = next(e for e in evs if e["type"] == "tool_end")
    assert row["name"] == "cli:Bash" and end["ok"] is False and "denied" in end["error"]["message"], (row, end)
    assert answer(evs) == "I cannot run commands here."
    assert answer(conforming(h.run_turn("scenario=notext", account=acct, timeout=120))) == "Only the result carries this."
    quick = conforming(h.run_turn("scenario=ok", account=acct, mode="quick", timeout=120))
    assert f"tools={len(prompt.QUICK_TOOLS)} " in answer(quick) and cli_logs("claude")[-1]["flags"].get("--no-session-persistence") is True


def test_stream_json_cli_failures_cut_auth_and_cancel():
    h = host()
    acct = account(h, "cli-stream-json", "claude")
    evs = conforming(h.run_turn("scenario=cut", account=acct, timeout=60))
    err = error_of(evs)
    assert answer(evs) == "Hel" and err["kind"] == "network" and err["code"] == "TRUNCATED", evs
    err = error_of(conforming(h.run_turn("scenario=auth", account=acct, timeout=60)))
    assert err["kind"] == "auth" and err["code"] == "CLI_AUTHENTICATION_FAILED" and "sign in" in err["fix"], err
    turn = h.turn("scenario=stall", account=acct)
    h.wait_event(turn, "text_delta", 60)
    pid = cli_logs("claude")[-1]["pid"]
    assert alive(pid)
    t0 = time.time()
    h.cancel(turn)
    evs = conforming(h.wait_done(turn, 20))
    assert evs[-1]["status"] == "cancelled" and time.time() - t0 < 3, (evs, time.time() - t0)
    deadline = time.time() + 5
    while alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not alive(pid), "the cancelled CLI (and its shim) must be gone"
    missing = h.request("accounts.add", {"kind": "cli-stream-json", "label": "gone", "command": str(TMP / "no-such-cli.exe")})["account"]["id"]
    assert h.request("accounts.test", {"id": missing})["ok"] is False
    err = error_of(conforming(h.run_turn("x", account=missing, timeout=30)))
    assert err["kind"] == "engine" and err["code"] == "CLI_MISSING", err
    assert conforming(h.run_command("help"))[-1]["status"] == "ok"


def test_a_cli_driven_edit_lands_through_the_bridge_as_a_normal_proposal():
    h = host()
    acct = account(h, "cli-stream-json", "claude")
    f = sheet(h)
    before = f.read_bytes()
    n_calls = len(h.calls)
    evs = conforming(h.run_turn("make R1 4k7 scenario=edit", account=acct, timeout=300))
    assert evs[-1]["status"] == "ok", evs
    assert [m for m, _ in h.calls[n_calls:]][-1] == "host.apply", [m for m, _ in h.calls[n_calls:]]
    prop = next(e for e in evs if e["type"] == "proposal")
    assert prop["counts"] == {"added": 0, "changed": 1, "removed": 0} and prop["netlist_ok"] is True and prop["refs"] == ["R1"], prop
    ts = types(evs)
    assert ts.index("tool_start") < ts.index("tool_end") < ts.index("proposal") < ts.index("done"), ts
    assert "tool_ok=True" in answer(evs) and "live=yes" in answer(evs), answer(evs)
    assert '"4k7"' in f.read_text(encoding="utf-8")
    assert h.undo() == prop["proposal_id"] and f.read_bytes() == before, "one undo step, as for any other driver"
    time.sleep(0.3)


def test_exec_json_cli_command_line_stream_and_cumulative_usage():
    h = host()
    acct = account(h, "cli-exec-json", "codex")
    conv = "cli-b"
    evs = conforming(h.run_turn("what is here? scenario=ok", account=acct, model="m-1", conversation=conv, timeout=120))
    n_tools = len(prompt.tool_catalogue("chat"))
    assert answer(evs) == f"tools={n_tools} tool_ok=True resume=none instructions=yes" and evs[-1]["status"] == "ok", evs
    assert reasoning(evs).strip() == "**Planning** the call"
    assert [e["name"] for e in evs if e["type"] == "tool_start"] == ["copper_help"]
    assert evs[-1]["usage"] == {"in": 1000, "out": 50, "cached": 800}, evs[-1]["usage"]
    rec = cli_logs("codex")[-1]
    cfg, fl = rec["config"], rec["flags"]
    assert rec["argv"][0] == "exec" and fl["--json"] is True and fl["--skip-git-repo-check"] is True and fl["--sandbox"] == "read-only"
    assert fl["--ignore-user-config"] is True and fl["-m"] == "m-1" and rec["positional"] == ["-"], rec["positional"]
    assert cfg["features.shell_tool"] is False and cfg["tools.web_search"] is False and cfg["approval_policy"] == "never", cfg
    assert [k for k in cfg if k.startswith("mcp_servers.")] and all(k.startswith("mcp_servers.copper.") for k in cfg if k.startswith("mcp_servers.")), cfg
    assert Path(cfg["mcp_servers.copper.command"]).name.lower().startswith("python") and len(cfg["mcp_servers.copper.args"]) == 2
    assert Path(cfg["mcp_servers.copper.args"][0]).name == "_bridge.py" and cfg["mcp_servers.copper.tool_timeout_sec"] == 1800
    assert not any('"' in a for a in rec["argv"]), "no double quote on the command line"
    assert "bypass" not in " ".join(rec["argv"]) and "danger" not in " ".join(rec["argv"])
    assert not Path(cfg["mcp_servers.copper.args"][1]).exists(), "the gate file is gone when the turn ends"
    evs = conforming(h.run_turn("and now? scenario=ok", account=acct, conversation=conv, timeout=120))
    assert "resume=0199a213-81c0-7800-8aa1-bbab2a035a53 instructions=no" in answer(evs), answer(evs)
    assert evs[-1]["usage"] == {"in": 1500, "out": 70, "cached": 1200}, "the thread's running total minus the previous turn's"
    assert cli_logs("codex")[-1]["positional"] == ["resume", "0199a213-81c0-7800-8aa1-bbab2a035a53", "-"]
    evs = conforming(h.run_turn("scenario=own", account=acct, timeout=120))
    rows = {e["call"]: e["name"] for e in evs if e["type"] == "tool_start"}
    ended = {rows[e["call"]]: e["ok"] for e in evs if e["type"] == "tool_end"}
    assert ended == {"cli:shell": False, "copper_help": True}, evs       # the CLI's own tool is shown, never hidden
    err = error_of(conforming(h.run_turn("scenario=failed", account=acct, timeout=60)))
    assert err["kind"] == "auth" and err["code"] == "CLI_AUTH", err
    evs = conforming(h.run_turn("scenario=cut", account=acct, timeout=60))
    assert answer(evs) == "Hel" and error_of(evs)["code"] == "TRUNCATED", evs
    turn = h.turn("scenario=stall", account=acct)
    time.sleep(2.5)
    pid = cli_logs("codex")[-1]["pid"]
    h.cancel(turn)
    assert conforming(h.wait_done(turn, 20))[-1]["status"] == "cancelled"
    deadline = time.time() + 5
    while alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not alive(pid)


def test_acp_agent_gets_no_file_or_terminal_access_and_only_copper_tools_are_allowed():
    h = host()
    acct = account(h, "cli-acp", "acp", shim=False)
    conv = "cli-c"
    evs = conforming(h.run_turn("what is here? scenario=ok", account=acct, conversation=conv, timeout=120))
    assert evs[-1]["status"] == "ok", evs
    text = answer(evs)
    n_tools = len(prompt.tool_catalogue("chat"))
    assert text.startswith(f"Looking. tools={n_tools} tool_ok=True copper=a1 write=r1 fs=error "), text
    assert 'caps={"fs": {"readTextFile": false, "writeTextFile": false}, "terminal": false}' in text, text
    assert "servers=['copper'] resumed=False instructions=yes" in text, text
    assert reasoning(evs) == "plan"
    rows = [e["name"] for e in evs if e["type"] == "tool_start"]
    assert rows == ["copper_help", "cli:Write copper_notes.kicad_sch"], rows
    ends = [e for e in evs if e["type"] == "tool_end"]
    assert [e["ok"] for e in ends] == [True, False] and "rejected" in ends[1]["error"]["message"], ends
    evs = conforming(h.run_turn("and now? scenario=ok", account=acct, conversation=conv, timeout=120))
    assert "resumed=True instructions=no" in answer(evs), answer(evs)
    err = error_of(conforming(h.run_turn("scenario=refuse", account=acct, timeout=120)))
    assert err["kind"] == "engine" and err["code"] == "REFUSED", err
    evs = conforming(h.run_turn("scenario=cut", account=acct, timeout=60))
    assert answer(evs) == "Hel" and error_of(evs)["code"] == "TRUNCATED", evs
    turn = h.turn("scenario=stall", account=acct)
    h.wait_event(turn, "text_delta", 60)
    pid = cli_logs("acp")[-1]["pid"]
    t0 = time.time()
    h.cancel(turn)
    assert conforming(h.wait_done(turn, 20))[-1]["status"] == "cancelled" and time.time() - t0 < 3
    deadline = time.time() + 5
    while alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not alive(pid)
    assert list(CLI_LOG.glob("acp-cancel-*.json")), "the agent was told session/cancel before it was killed"
    nocmd = h.request("accounts.add", {"kind": "cli-acp", "label": "nocmd"})["account"]["id"]
    assert h.request("accounts.test", {"id": nocmd})["ok"] is False
    assert error_of(conforming(h.run_turn("x", account=nocmd, timeout=30)))["code"] == "CLI_MISSING"


def test_cli_without_a_shim_and_the_bridge_alone():
    h = host()
    if IS_WIN:                                                   # the same driver, started as "python script" instead
        acct = account(h, "cli-stream-json", "claude", shim=False)
        evs = conforming(h.run_turn("scenario=ok", account=acct, timeout=120))
        assert "tool_ok=True" in answer(evs), answer(evs)
    # the bridge with no gate behind it: answers MCP, reports tools as unavailable, never hangs
    bridge = SRC / "copper_kicad" / "agent" / "drivers" / "_bridge.py"
    gate = TMP / "dead-gate.json"
    gate.write_text(json.dumps({"port": 9, "token": "x"}), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(bridge), str(gate)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "ping"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sch_read", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 4, "method": "resources/list"}]
    out, err = p.communicate(("\n".join(json.dumps(l) for l in lines) + "\nnot json\n").encode(), timeout=60)
    got = [json.loads(l) for l in out.decode().splitlines()]
    assert [g.get("id") for g in got] == [1, 2, 3, 4, None], got
    assert got[0]["result"]["protocolVersion"] == "2024-11-05" and got[0]["result"]["serverInfo"]["name"] == "copper"
    assert got[2]["result"]["isError"] is True and "not reachable" in got[2]["result"]["content"][0]["text"], got[2]
    assert got[3]["error"]["code"] == -32601 and got[4]["error"]["code"] == -32700 and p.returncode == 0
    # a wrong token gets nothing from a live gate
    from copper_kicad.agent.drivers._gate import ToolGate
    import socket

    class _T:
        class request:
            mode = "chat"
        cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})()
        finished = False
    g = ToolGate(_T()).start()
    try:
        assert json.loads(g.file.read_text(encoding="utf-8")) == {"port": g.port, "token": g.token}
        s = socket.create_connection(("127.0.0.1", g.port), timeout=5)
        s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"auth","params":{"token":"wrong"}}\n')
        assert s.recv(100) == b"", "a wrong token closes the connection without an answer"
        s.close()
        s = socket.create_connection(("127.0.0.1", g.port), timeout=5)
        s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"tools.list","params":{}}\n')
        assert s.recv(100) == b"", "no method before auth"
        s.close()
    finally:
        g.close()
    assert not g.file.exists()


def test_the_run_left_no_secret_and_a_clean_stream():
    h = host()
    assert not h.garbage, h.garbage
    for turn, evs in h.events.items():
        conforming(evs)
    sent = json.dumps([t["msg"] for t in h.transcript if t["dir"] == "agent>app"])
    assert KEY not in sent and KEY not in "".join(h.stderr)
    for rec in cli_logs("claude") + cli_logs("codex") + cli_logs("acp"):
        assert KEY not in json.dumps(rec), "no API key of another account reaches a CLI's command line or stdin"
    err = "".join(h.stderr)
    assert "Traceback" not in err, err[max(0, err.index("Traceback") - 300):][:3000]
    NOTES.append(f"{len(h.events)} turns, {len(h.transcript)} messages, {len(models().requests)} provider requests, "
                 f"{len(list(CLI_LOG.glob('*.json')))} CLI runs")
    assert h.close(15) == 0


def _main() -> int:
    only = sys.argv[1:]
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
             and (not only or any(o in n for o in only))]
    failed = 0
    for n, f in tests:
        t0 = time.time()
        try:
            f()
            print(f"PASS {n} ({time.time() - t0:.1f}s)", flush=True)
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}\n{traceback.format_exc()}", flush=True)
    h = _STATE.get("host")
    if h is not None:
        h.close(5)
    if "models" in _STATE:
        _STATE["models"].stop()
    for note in NOTES:
        print(f"NOTE {note}")
    if not failed and not os.environ.get("COPPER_TEST_KEEP") and not _BASE:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
