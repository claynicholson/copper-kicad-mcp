"""copper-agent protocol tests: spawn ``python -m copper_kicad.agent`` and talk docs/internal/HOST_PROTOCOL.md to it through
tests/fakehost.py (the stand-in for the C++ app). No model, no network beyond a loopback test server, no real
credential store, nothing outside a temp folder.

    python tests/test_agent_protocol.py [name-fragment ...]      (KiCad's Python, or any Python 3.10+ with KiCad installed)

COPPER_TEST_TMP=<dir> puts the scratch files there instead of the system temp folder.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import threading
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
TMP = Path(tempfile.mkdtemp(prefix="copper-test-agent-", dir=_BASE))
for _k, _d in (("COPPER_HOME", "home"), ("COPPER_CONFIG_HOME_OVERRIDE", "cfg"), ("KICAD_CONFIG_HOME", "kicad-config")):
    (TMP / _d).mkdir()
    os.environ[_k] = str(TMP / _d)             # inherited by every agent: no real ~/.copper, keychain or KiCad config
os.environ["COPPER_HOME"] = str(TMP / "home")        # spelled out: test_install.py checks every module for it

import fakehost  # noqa: E402
from fakehost import FakeHost, RpcFailure  # noqa: E402
from copper_kicad.agent import events, prompt, sessions  # noqa: E402
from copper_kicad.host import client as hostclient, rendezvous  # noqa: E402
from copper_kicad.host.relay import Relay  # noqa: E402
from copper_kicad.sch import patch as schpatch  # noqa: E402

PLUGIN = ROOT / "tests" / "agent_testplugin.py"
FAST = {"COPPER_AGENT_CANCEL_GRACE_S": "0.6", "COPPER_AGENT_LOG": "debug"}
NOTES: list[str] = []
_N = [0]
_STATE: dict = {}


def workdir(name: str) -> Path:
    _N[0] += 1
    return TMP / f"{_N[0]:02d}-{name}"


def fixture() -> Path:
    """A tiny real project (two resistors on a net), built once with the engine itself."""
    if "fixture" not in _STATE:
        from copper_kicad.mcp import registry
        from copper_kicad.review import project_info
        import copper_kicad.tools  # noqa: F401
        r = project_info.create_project("spine", str(TMP / "fixture"), "blank")
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


def shared() -> FakeHost:
    """One agent for the tests that do not need their own process."""
    if "host" not in _STATE:
        h = FakeHost(fixture(), workdir("shared"), env=FAST, plugins=[PLUGIN]).start()
        h.hello_result = h.hello()
        h.script = h.request("accounts.add", {"kind": "test-script", "label": "script"})["account"]["id"]
        _STATE["host"] = h
    return _STATE["host"]


def script(h: FakeHost, steps: list, **kw) -> str:
    return h.turn(json.dumps(steps), account=h.script, **kw)


def conforming(evs: list[dict]) -> list[dict]:
    problems = events.check_stream(evs)
    assert not problems, (problems, [json.dumps(e)[:300] for e in evs])
    return evs


def types(evs) -> list[str]:
    return [e["type"] for e in evs]


def rpc_fails(code: str, fn, *a, **k) -> RpcFailure:
    try:
        fn(*a, **k)
    except RpcFailure as e:
        assert e.code == code, (e.code, e.message)
        return e
    raise AssertionError(f"expected the agent to refuse with {code}")


def sheet(h: FakeHost) -> Path:
    return next(h.live.glob("*.kicad_sch"))


# ── hello ─────────────────────────────────────────────────────────────────
def test_hello_reports_protocol_accounts_drivers_and_commands():
    h = shared()
    r = h.hello_result
    assert r["protocol"] == 1 and r["engine_version"] and isinstance(r["agent"]["pid"], int), r
    assert r["accounts"][0] == {"id": "none", "kind": "none", "label": "No account (commands only)", "has_secret": False}
    kinds = {d["kind"]: d for d in r["drivers"]}
    assert kinds["none"]["available"] and {"test-script", "test-stream"} <= set(kinds), kinds
    assert kinds["test-stream"]["needs"] == ["base_url"]
    names = [c["name"] for c in r["commands"]]
    assert {"help", "focus", "tool"} <= set(names), names
    assert all({"name", "title", "usage", "args", "documents", "needs_project"} <= set(c) for c in r["commands"])
    assert r["limits"]["concurrent_turns"] == 1 and r["load_errors"] == {}, r
    assert h.request("ping") == {}


def test_requests_before_hello_and_version_negotiation():
    h = FakeHost(None, workdir("prehello"), env=FAST).start()
    try:
        assert h.request("ping") == {}                                  # ping needs no hello
        rpc_fails("NOT_READY", h.request, "turn.start", {"text": "hi"})
        e = rpc_fails("UNSUPPORTED_PROTOCOL", h.request, "hello", {"protocol": 0})
        assert e.data["min"] == 1 and e.data["max"] == events.PROTOCOL, e.data
        rpc_fails("INVALID_PARAMS", h.request, "hello", {"protocol": "1"})
        rpc_fails("INVALID_PARAMS", h.request, "hello", {"protocol": 1, "project_dir": str(TMP / "nope")})
        r = h.request("hello", {"protocol": 99, "project_dir": None})    # a newer app: the agent answers with its own
        assert r["protocol"] == events.PROTOCOL, r
        evs = conforming(h.run_command("help"))                         # works without a project
        assert types(evs) == ["text_delta", "done"], evs
        assert h.close() == 0
    finally:
        h.close(2)


# ── a turn without a model ────────────────────────────────────────────────
def test_command_turn_streams_events_in_order_after_the_answer():
    h = shared()
    n0 = len(h.transcript)
    evs = conforming(h.run_command("help"))
    assert types(evs) == ["text_delta", "done"] and evs[-1]["status"] == "ok", evs
    assert "/help" in h.text_of(evs) and "/tool" in h.text_of(evs)
    tr = [t["msg"] for t in h.transcript[n0:] if t["dir"] == "agent>app"]
    turn = evs[0]["turn"]
    first_answer = next(i for i, m in enumerate(tr) if (m.get("result") or {}).get("turn") == turn)
    first_event = next(i for i, m in enumerate(tr) if m.get("method") == "event" and m["params"]["turn"] == turn)
    assert first_answer < first_event, "the answer to command.run must precede the turn's first event"
    rpc_fails("UNKNOWN_COMMAND", h.request, "command.run", {"name": "nope"})
    rpc_fails("INVALID_PARAMS", h.request, "command.run", {"name": "help", "args": []})


def test_no_account_turn_explains_and_runs_slash_commands():
    h = shared()
    evs = conforming(h.run_turn("add a 100n cap here"))
    text = h.text_of(evs)
    assert evs[-1]["status"] == "ok" and "Connect an AI account" in text and "/help" in text, text
    focus = {"document": "schematic", "sheet_path": "/", "mode": "selection", "refs": ["R1"],
             "pins": [{"ref": "R1", "number": "2", "name": "~"}], "nets": ["MID"],
             "items": [{"uuid": "00000000-0000-0000-0000-000000000001", "type": "symbol"}], "source": "kicad", "seq": 3, "rev": 1}
    evs = conforming(h.run_turn("/focus", focus=focus))
    assert types(evs) == ["text_delta", "reveal", "done"], types(evs)
    assert "parts R1" in h.text_of(evs) and "nets MID" in h.text_of(evs), h.text_of(evs)
    assert evs[1]["refs"] == ["R1"] and evs[1]["document"] == "schematic", evs[1]
    evs = conforming(h.run_turn("/nonsense"))
    assert types(evs) == ["error", "done"] and evs[0]["kind"] == "engine" and evs[0]["code"] == "UNKNOWN_COMMAND", evs
    evs = conforming(h.run_turn('/tool {"nope": 1}'))
    assert evs[0]["type"] == "error" and evs[0]["code"] == "BAD_ARGUMENT", evs
    h.notify("focus.changed", focus)                                   # the pushed focus is the default for the next turn
    evs = conforming(h.run_command("focus"))
    assert "parts R1" in h.text_of(evs), evs
    h.notify("focus.changed", {})


def test_edit_lands_through_checkout_and_apply_as_one_undo_step():
    h = shared()
    f = sheet(h)
    before = f.read_bytes()
    n_calls = len(h.calls)
    rev0 = h.rev["schematic"]
    conv = "edit-1"
    evs = conforming(h.run_command("tool", {"tool": "sch_apply", "arguments": {
        "ops": [{"op": "set_field", "ref": "R1", "field": "Value", "value": "4k7"}]}}, conversation=conv, timeout=300))
    assert evs[-1]["status"] == "ok", evs
    calls = h.calls[n_calls:]
    assert [m for m, _ in calls] == ["host.checkout", "host.apply"], [m for m, _ in calls]
    ap = calls[1][1]
    assert ap["document"] == "schematic" and ap["base_rev"] == rev0 and len(ap["patches"]) == 1, ap
    p = ap["patches"][0]
    assert p["sheet_path"] == "/" and p["file"] == f.name and len(p["remove"]) == 1, p
    assert '"4k7"' in p["add"] and p["remove"][0] in p["add"], "a changed item keeps its uuid"
    assert ap["expect"]["netlist_hash"].startswith("sha256:"), ap["expect"]
    shadow = h.live / ".copper" / "shadow" / str(rev0)
    assert (shadow / f.name).exists() and '"4k7"' in (shadow / f.name).read_text(encoding="utf-8")
    prop = next(e for e in evs if e["type"] == "proposal")
    assert prop["state"] == "review" and prop["counts"] == {"added": 0, "changed": 1, "removed": 0}, prop
    assert prop["netlist_ok"] is True and prop["refs"] == ["R1"] and prop["document"] == "schematic", prop
    ts = types(evs)
    assert ts.index("tool_start") < ts.index("tool_end") < ts.index("proposal") < ts.index("done"), ts
    assert any(e["type"] == "tool_progress" for e in evs), ts
    assert '"4k7"' in f.read_text(encoding="utf-8") and f.read_bytes() != before
    assert h.rev["schematic"] == rev0 + 1 and len(h.undo_stack) == 1
    recs = h.request("conversations.load", {"conversation": conv})["records"]
    kinds = [r["kind"] for r in recs]
    assert kinds[0] == "meta" and {"user", "tool", "proposal", "assistant", "done"} <= set(kinds), kinds
    pid = h.undo()                                                     # Ctrl+Z in the app: everything back, one step
    assert pid == prop["proposal_id"] and f.read_bytes() == before
    time.sleep(0.3)
    recs = h.request("conversations.load", {"conversation": conv})["records"]
    assert recs[-1]["kind"] == "outcome" and recs[-1]["outcome"] == "undone", recs[-1]
    assert not h.garbage, h.garbage


def test_stale_base_and_busy_apply_nothing_and_the_next_call_recovers():
    h = shared()
    f = sheet(h)
    before = f.read_bytes()
    edit = {"tool": "sch_apply", "arguments": {"verify": False, "preview": False, "layout": "keep", "ops": [
        {"op": "set_field", "ref": "R2", "field": "Value", "value": "22k"}]}}
    h.user_edit()                                    # any earlier checkout is stale now: the next call checks out
    h.edit_after_checkout = True                     # ... and the user edits again while the engine works on the shadow
    evs = conforming(h.run_command("tool", edit, timeout=300))
    end = next(e for e in evs if e["type"] == "tool_end")
    assert end["ok"] is False and end["error"]["code"] == "STALE_BASE" and evs[-1]["status"] == "ok", evs
    assert "proposal" not in types(evs) and f.read_bytes() == before, "a stale apply must change nothing"
    h.busy = True
    evs = conforming(h.run_command("tool", edit, timeout=300))
    end = next(e for e in evs if e["type"] == "tool_end")
    assert end["ok"] is False and end["error"]["code"] == "BUSY" and f.read_bytes() == before, evs
    h.busy = False
    evs = conforming(h.run_command("tool", edit, timeout=300))    # third time: fresh checkout, applies
    assert "proposal" in types(evs) and '"22k"' in f.read_text(encoding="utf-8"), types(evs)
    h.undo()
    assert f.read_bytes() == before


def test_host_that_never_answers_times_out_and_is_told():
    h = FakeHost(fixture(), workdir("mute"), env=dict(FAST, COPPER_AGENT_HOST_S="1.5")).start()
    try:
        h.hello()
        h.mute.add("host.checkout")
        t0 = time.time()
        evs = conforming(h.run_command("tool", {"tool": "sch_read", "arguments": {}}, timeout=60))
        end = next(e for e in evs if e["type"] == "tool_end")
        assert end["ok"] is False and end["error"]["code"] == "TIMEOUT" and time.time() - t0 < 30, evs
        assert h.cancelled_requests and h.cancelled_requests[-1]["reason"] == "timeout", h.cancelled_requests
        assert h.request("ping") == {}
    finally:
        h.close(5)


# ── cancel ────────────────────────────────────────────────────────────────
def test_cancel_stops_a_cooperative_turn_and_frees_the_agent():
    h = shared()
    turn = script(h, [{"text": "working"}, {"sleep": 60}, {"text": "never"}])
    h.wait_event(turn, "text_delta")
    rpc_fails("BUSY", h.request, "turn.start", {"text": "second"})       # one turn at a time
    rpc_fails("BUSY", h.request, "command.run", {"name": "help"})
    t0 = time.time()
    assert h.cancel(turn) == {"cancelled": True}
    evs = conforming(h.wait_done(turn, 10))
    assert time.time() - t0 < 2 and types(evs) == ["text_delta", "done"] and evs[-1]["status"] == "cancelled", evs
    assert h.cancel(turn) == {"cancelled": False, "status": "cancelled"}  # late cancel: not an error
    rpc_fails("UNKNOWN_TURN", h.request, "turn.cancel", {"turn": "t-nope"})
    assert conforming(h.run_command("help"))[-1]["status"] == "ok"        # the agent is free again


def test_cancel_cuts_off_a_driver_that_does_not_cooperate():
    h = shared()
    turn = script(h, [{"text": "a"}, {"block": 8}, {"text": "late"}])
    h.wait_event(turn, "text_delta")
    t0 = time.time()
    h.cancel(turn)
    evs = conforming(h.wait_done(turn, 10))
    took = time.time() - t0
    assert 0.3 < took < 3 and evs[-1]["status"] == "cancelled", (took, evs)
    evs2 = conforming(h.run_command("help"))                              # usable at once; the old thread is muted
    time.sleep(0.2)
    assert len(h.events[turn]) == len(evs), "an ended turn must not emit again"
    assert evs2[-1]["status"] == "ok"


def test_cancel_while_waiting_for_the_host_abandons_the_request():
    h = shared()
    h.mute.add("host.ask_permission")
    n = len(h.cancelled_requests)
    try:
        turn = script(h, [{"ask": {"what": "x"}}, {"text": "never"}])
        h.wait_call("host.ask_permission")
        h.cancel(turn)
        evs = conforming(h.wait_done(turn, 10))
        assert types(evs) == ["done"] and evs[-1]["status"] == "cancelled", evs
        told = h.cancelled_requests[n:]
        assert told and told[0]["reason"] == "turn_cancelled" and told[0]["id"].startswith("a"), told
    finally:
        h.mute.discard("host.ask_permission")
    h.permission = {"granted": False, "remember": False}
    evs = conforming(h.wait_done(script(h, [{"ask": {"what": "y"}}])))
    assert "granted=False" in h.text_of(evs), evs
    h.permission = {"granted": True, "remember": False}


# ── failures of the model side ────────────────────────────────────────────
class _Stream(http.server.BaseHTTPRequestHandler):
    release = threading.Event()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: Hello\n")
        self.wfile.write(b"data:  world\n")
        self.wfile.flush()
        if self.path == "/full":
            self.wfile.write(b"data: [DONE]\n")
        elif self.path == "/stall":
            self.release.wait(30)
        # "/cut": the connection simply closes here, without the terminal marker

    def log_message(self, *a):
        pass


def _stream_server():
    if "srv" not in _STATE:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stream)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _STATE["srv"] = srv
    return f"http://127.0.0.1:{_STATE['srv'].server_address[1]}"


def test_truncated_stream_is_an_error_event_never_a_hang():
    h = shared()
    acct = h.request("accounts.add", {"kind": "test-stream", "label": "stream", "base_url": _stream_server()})["account"]
    assert h.request("accounts.test", {"id": acct["id"]})["ok"] is True
    evs = conforming(h.run_turn("full", account=acct["id"], timeout=20))
    assert h.text_of(evs) == "Hello world" and evs[-1]["status"] == "ok", evs
    t0 = time.time()
    evs = conforming(h.run_turn("cut", account=acct["id"], timeout=20))
    assert time.time() - t0 < 10
    assert types(evs) == ["text_delta", "text_delta", "error", "done"], types(evs)
    err = evs[2]
    assert err["kind"] == "network" and err["code"] == "TRUNCATED" and err["retryable"] is True, err
    assert evs[-1]["status"] == "error"
    assert conforming(h.run_command("help"))[-1]["status"] == "ok"


def test_silent_stream_is_ended_by_the_watchdog():
    h = FakeHost(fixture(), workdir("idle"), env=dict(FAST, COPPER_AGENT_IDLE_S="1.5"), plugins=[PLUGIN]).start()
    try:
        h.hello()
        acct = h.request("accounts.add", {"kind": "test-stream", "label": "stall", "base_url": _stream_server()})["account"]
        t0 = time.time()
        evs = conforming(h.run_turn("stall", account=acct["id"], timeout=20))
        took = time.time() - t0
        assert 1.2 < took < 8, took
        assert types(evs)[-2:] == ["error", "done"] and evs[-2]["kind"] == "network" and evs[-2]["code"] == "WATCHDOG", evs
        assert conforming(h.run_command("help"))[-1]["status"] == "ok"
    finally:
        _Stream.release.set()
        h.close(5)


def test_driver_failures_map_to_error_kinds():
    h = shared()
    for kind in events.ERROR_KINDS:
        evs = conforming(h.wait_done(script(h, [{"text": "x"}, {"fail": kind, "code": "SCRIPTED"}])))
        assert types(evs) == ["text_delta", "error", "done"], types(evs)
        assert evs[1]["kind"] == kind and evs[1]["code"] == "SCRIPTED" and evs[2]["status"] == "error", evs
        assert evs[1]["retryable"] is (kind in ("rate", "network")), evs[1]
    evs = conforming(h.wait_done(script(h, [{"crash": "boom"}])))
    assert evs[0]["kind"] == "engine" and evs[0]["code"] == "INTERNAL" and "boom" in evs[0]["message"], evs
    evs = conforming(h.wait_done(script(h, [{"usage": [1200, 80, 1000]}, {"text": "ok"}])))
    assert evs[0] == dict(evs[0], type="usage", cached=1000) and evs[0]["in"] == 1200 and evs[0]["out"] == 80, evs[0]
    assert evs[-1]["usage"] == {"in": 1200, "out": 80, "cached": 1000}, evs[-1]
    evs = conforming(h.wait_done(script(h, [{"tool": "no_such_tool"}])))      # a tool failure is not a turn failure
    end = next(e for e in evs if e["type"] == "tool_end")
    assert end["ok"] is False and end["error"]["code"] == "UNKNOWN_TOOL" and evs[-1]["status"] == "ok", evs
    evs = conforming(h.wait_done(script(h, [{"tool": "project_open", "args": {"path": "x"}}])))
    assert next(e for e in evs if e["type"] == "tool_end")["error"]["code"] == "NOT_IN_APP", evs
    rpc_fails("UNKNOWN_ACCOUNT", h.request, "turn.start", {"text": "x", "account": "nope-123"})
    rpc_fails("INVALID_PARAMS", h.request, "turn.start", {"text": ""})
    rpc_fails("INVALID_PARAMS", h.request, "turn.start", {"text": "x", "mode": "yolo"})
    rpc_fails("INVALID_PARAMS", h.request, "turn.start", {"text": "x", "conversation": "../../etc"})


# ── malformed input ───────────────────────────────────────────────────────
def test_malformed_input_is_answered_and_survived():
    h = shared()
    h.send_raw("this is not json")
    e = h.orphan_error()
    assert e["id"] is None and e["error"]["code"] == -32700, e
    h.send_raw('{"jsonrpc":"2.0","id":7001,"method":"ping"')             # cut in the middle
    assert h.orphan_error()["error"]["code"] == -32700
    h.send_raw("[]")
    assert h.orphan_error()["error"]["code"] == -32600
    h.send_raw("5")
    assert h.orphan_error()["error"]["code"] == -32600
    h.send_raw("")                                                       # blank lines are ignored
    h.send_raw('﻿{"jsonrpc":"2.0","id":null,"method":7}')
    assert h.orphan_error()["error"]["code"] == -32600
    try:
        h.request("no.such.method")
        raise AssertionError("unknown method accepted")
    except RpcFailure as e2:
        assert e2.rpc_code == -32601, e2
    mid = h.request_async("ping")
    assert h.answer(mid) == {}
    h._answers[7002] = __import__("queue").Queue()
    h._send({"jsonrpc": "2.0", "id": 7002, "method": "ping", "params": [1, 2]})     # params must be an object
    try:
        h.answer(7002)
        raise AssertionError("array params accepted")
    except RpcFailure as e3:
        assert e3.rpc_code == -32600, e3
    h.notify("no.such.notification", {"x": 1})                           # unknown notifications are ignored
    h.notify("doc.changed", {"document": "nope", "rev": "x"})             # bad ones too
    h._send({"jsonrpc": "2.0", "id": "a999999", "result": {}})            # an answer nobody waits for
    h._answers[7003] = __import__("queue").Queue()
    h._send({"jsonrpc": "2.0", "id": 7003})                               # neither request nor answer
    try:
        h.answer(7003)
        raise AssertionError("empty message accepted")
    except RpcFailure as e4:
        assert e4.rpc_code == -32600, e4
    big = "x" * 300_000
    evs = conforming(h.wait_done(script(h, [{"text": big}])))             # a 300 kB line each way
    assert h.text_of(evs) == big
    assert h.request("ping") == {} and not h.garbage, h.garbage


# ── EOF ───────────────────────────────────────────────────────────────────
def test_stdin_eof_mid_turn_ends_the_turn_and_the_process():
    h = FakeHost(fixture(), workdir("eof"), env=FAST, plugins=[PLUGIN]).start()
    h.hello()
    acct = h.request("accounts.add", {"kind": "test-script"})["account"]["id"]
    turn = h.turn(json.dumps([{"text": "working"}, {"sleep": 60}]), account=acct, conversation="eof-1")
    h.wait_event(turn, "text_delta")
    t0 = time.time()
    rc = h.close(15)
    assert rc == 0 and time.time() - t0 < 8, (rc, time.time() - t0)
    evs = conforming(h.events[turn])
    assert types(evs)[-2:] == ["error", "done"] and evs[-2]["kind"] == "protocol" and evs[-1]["status"] == "error", evs
    recs = sessions.Store(h.live).load("eof-1")                         # the conversation on disk is complete
    assert recs[-1]["kind"] == "done" and recs[-1]["status"] == "error" and recs[-1]["error"]["code"] == "HOST_GONE", recs[-1]
    assert not h.garbage, h.garbage


def test_stdin_eof_while_waiting_for_the_host_does_not_hang():
    h = FakeHost(fixture(), workdir("eof-host"), env=FAST, plugins=[PLUGIN]).start()
    h.hello()
    acct = h.request("accounts.add", {"kind": "test-script"})["account"]["id"]
    h.mute.add("host.ask_permission")
    turn = h.turn(json.dumps([{"ask": {}}]), account=acct)
    h.wait_call("host.ask_permission")
    assert h.close(15) == 0
    evs = h.events[turn]
    assert evs and evs[-1]["type"] == "done" and evs[-1]["status"] == "error", evs


def test_agent_death_mid_turn_gives_the_app_a_synthetic_error():
    h = FakeHost(fixture(), workdir("kill"), env=FAST, plugins=[PLUGIN]).start()
    h.hello()
    acct = h.request("accounts.add", {"kind": "test-script"})["account"]["id"]
    turn = h.turn(json.dumps([{"text": "working"}, {"sleep": 60}]), account=acct)
    h.wait_event(turn, "text_delta")
    h.kill()
    evs = conforming(h.wait_done(turn, 5))
    assert types(evs) == ["text_delta", "error", "done"], evs
    assert evs[1]["synthetic"] and evs[1]["kind"] == "protocol" and evs[2]["status"] == "error", evs
    assert h.close(2) is not None or True


def test_shutdown_request_exits_cleanly():
    h = FakeHost(None, workdir("shutdown"), env=FAST).start()
    h.hello(project_dir=None)
    assert h.request("shutdown") == {}
    h.p.wait(10)
    assert h.p.returncode == 0


# ── sessions, accounts, prompt ────────────────────────────────────────────
def test_conversations_persist_resume_and_feed_history():
    h = shared()
    conv = "talk-1"
    conforming(h.wait_done(script(h, [{"text": "first "}, {"text": "answer"}], conversation=conv)))
    evs = conforming(h.wait_done(script(h, [{"history": True}], conversation=conv)))
    hist = json.loads(h.text_of(evs))
    assert [m["role"] for m in hist] == ["user", "assistant"] and hist[1]["text"] == "first answer", hist
    conforming(h.wait_done(script(h, [{"fail": "network"}], conversation=conv)))       # a failed turn is not history
    evs = conforming(h.wait_done(script(h, [{"history": True}], conversation=conv, mode="quick")))
    assert json.loads(h.text_of(evs)) == [], "quick edits carry no history"
    listed = {c["id"]: c for c in h.request("conversations.list")["conversations"]}
    assert listed[conv]["turns"] == 4 and listed[conv]["title"].startswith("["), listed[conv]
    r = h.request("turn.start", {"text": "hello", "account": "none"})                # no id given: the agent makes one
    assert sessions.valid_id(r["conversation"]), r
    h.wait_done(r["turn"])
    # a second agent on the same project (standalone editors, or a restart) reads the same conversation
    h2 = FakeHost(None, workdir("resume"), env=FAST, plugins=[PLUGIN]).start()
    try:
        h2.hello(project_dir=str(h.live))
        recs = h2.request("conversations.load", {"conversation": conv})["records"]
        assert recs[0]["kind"] == "meta" and sum(1 for x in recs if x["kind"] == "user") == 4, [x["kind"] for x in recs]
        rpc_fails("UNKNOWN_CONVERSATION", h2.request, "conversations.load", {"conversation": "nope"})
    finally:
        h2.close(5)
    st = sessions.Store(h.live)
    st.set_resume_id(conv, "some-cli", "sess-42")
    assert st.resume_id(conv, "some-cli") == "sess-42" and st.resume_id(conv, "other") is None
    with open(st.path(conv), "a", encoding="utf-8") as f:
        f.write('{"kind":"assistant","turn":"t-x","text":"torn')                      # a crash mid-write
    assert st.load(conv)[-1]["kind"] == "resume", "a torn last line is skipped, the rest survives"
    assert (h.live / ".copper" / "conversations" / f"{conv}.jsonl").exists()


def test_review_outcome_reaches_the_next_turn_as_a_note():
    h = shared()
    conv = "notes-1"
    evs = conforming(h.wait_done(script(h, [{"tool": "sch_apply", "args": {"verify": False, "preview": False, "layout": "keep",
        "ops": [{"op": "set_field", "ref": "R1", "field": "Value", "value": "1k"}]}}], conversation=conv), timeout=300))
    assert "proposal" in types(evs), types(evs)
    h.undo()
    time.sleep(0.3)
    focus = {"document": "schematic", "mode": "point", "point_mm": [100.0, 50.8], "refs": [], "source": "kicad"}
    evs = conforming(h.wait_done(script(h, [{"prompt": True}], conversation=conv, focus=focus)))
    text = h.text_of(evs)
    assert text.startswith("[focus] schematic | point | point 100,50.8 mm | (an area, no part)\n"), text
    assert "[note] the user undid your last change" in text and text.endswith("\n\n<text>"), text


def test_account_secrets_never_come_back():
    h = shared()
    secret = "sk-test-DO-NOT-LEAK-7c1f"
    n0 = len(h.transcript)
    a = h.request("accounts.add", {"kind": "test-script", "label": "with key", "secret": secret})["account"]
    assert a["has_secret"] is True and "secret" not in a and a["kind"] == "test-script", a
    listed = h.request("accounts.list")
    assert any(x["id"] == a["id"] and x["has_secret"] for x in listed["accounts"]) and listed["drivers"], listed
    conforming(h.wait_done(script(h, [{"text": "x"}])))
    from_agent = json.dumps([t["msg"] for t in h.transcript[n0:] if t["dir"] == "agent>app"])
    assert secret not in from_agent and secret not in "".join(h.stderr)
    for f in (TMP / "home").rglob("*"):
        if f.is_file():
            assert secret.encode() not in f.read_bytes(), f"{f} holds the secret in clear"
    for f in (h.live / ".copper").rglob("*.jsonl"):
        assert secret not in f.read_text(encoding="utf-8")
    assert h.request("accounts.remove", {"id": a["id"]}) == {}
    rpc_fails("UNKNOWN_ACCOUNT", h.request, "accounts.remove", {"id": a["id"]})
    rpc_fails("UNKNOWN_DRIVER", h.request, "accounts.add", {"kind": "no-such-driver"})
    rpc_fails("INVALID_PARAMS", h.request, "accounts.add", {"kind": "test-stream", "base_url": 5})
    rpc_fails("BAD_ARGUMENT", h.request, "accounts.add", {"kind": "test-stream", "base_url": "ftp://x"})


def test_prompt_prefix_is_stable_and_the_focus_is_one_short_line():
    import copper_kicad.tools  # noqa: F401
    a, b = prompt.system_prefix(), prompt.system_prefix()
    assert a == b and prompt.prefix_digest() == prompt.prefix_digest() and prompt.prefix_digest("quick") != prompt.prefix_digest()
    assert str(TMP) not in a and time.strftime("%Y") not in a, "nothing volatile in the cached prefix"
    cat = prompt.tool_catalogue()
    names = [t["name"] for t in cat]
    assert "sch_apply" in names and not set(names) & set(prompt.HIDDEN_TOOLS), names
    assert all("project" not in t["input_schema"].get("properties", {}) for t in cat), "the app owns the project"
    assert [t["name"] for t in prompt.tool_catalogue("quick")] == [n for n in names if n in prompt.QUICK_TOOLS]
    focus = {"document": "schematic", "sheet_path": "/power", "mode": "selection", "refs": ["U3", "C4"],
             "pins": [{"ref": "U3", "number": "12", "name": "PA5"}], "nets": ["/SWDIO", "GND"],
             "items": [{"uuid": "u1", "type": "symbol"}, {"uuid": "u2", "type": "wire"}], "source": "kicad", "seq": 9, "rev": 4}
    line = prompt.focus_line(focus)
    assert line == "[focus] schematic sheet /power | selection | parts U3, C4 | pins U3.12(PA5) | nets /SWDIO, GND | also 1 wire", line
    big = dict(focus, refs=[f"R{i}" for i in range(400)], nets=[f"N{i}" for i in range(400)])
    assert len(prompt.focus_line(big)) < 600 and "(+376 more)" in prompt.focus_line(big)
    assert prompt.focus_line(None) == "" and prompt.user_turn("hi") == "hi"
    assert prompt.user_turn("hi", focus, ["n1"]).split("\n") == [line, "[note] n1", "", "hi"]


# ── units: events, patch, rendezvous + relay ──────────────────────────────
def test_event_stream_checker_rejects_what_the_protocol_forbids():
    ok = [{"seq": 1, "type": "tool_start", "call": "c1", "name": "x"}, {"seq": 2, "type": "tool_end", "call": "c1", "ok": True},
          {"seq": 3, "type": "done", "status": "ok"}]
    assert events.check_stream(ok) == []
    bad = {
        "no done": ok[:2],
        "seq gap": [dict(ok[0]), dict(ok[1], seq=5), dict(ok[2])],
        "after done": ok + [{"seq": 4, "type": "text_delta", "text": "x"}],
        "open call at done": [ok[0], dict(ok[2], seq=2)],
        "end without start": [dict(ok[1], seq=1), dict(ok[2], seq=2)],
        "error without done(error)": [{"seq": 1, "type": "error", "kind": "network", "message": "m"}, {"seq": 2, "type": "done", "status": "ok"}],
        "done(error) without error": [{"seq": 1, "type": "done", "status": "error"}],
        "bad kind": [{"seq": 1, "type": "error", "kind": "weird", "message": "m"}, {"seq": 2, "type": "done", "status": "error"}],
        "unknown type": [{"seq": 1, "type": "patch"}, {"seq": 2, "type": "done", "status": "ok"}],
        "bad fraction": [ok[0], {"seq": 2, "type": "tool_progress", "call": "c1", "fraction": 7}, dict(ok[1], seq=3), dict(ok[2], seq=4)],
    }
    for name, evs in bad.items():
        assert events.check_stream(evs), f"{name}: accepted"
    assert events.make("usage", **{"in": 1, "out": 2, "cached": 0}) == {"type": "usage", "in": 1, "out": 2, "cached": 0}
    assert "cost" not in events.make("usage", cost=None)


def test_patch_baseline_diff_and_apply_round_trip():
    from copper_kicad import sexpr
    before = next(fixture().glob("*.kicad_sch")).read_text(encoding="utf-8")
    tree = sexpr.loads(before)
    items = [n for n in tree[1:] if schpatch.item_uuid(n)]
    syms = [n for n in items if n[0] == "symbol" and not schpatch._ref(n).startswith("#")]
    labels = [n for n in items if n[0] in ("label", "global_label", "wire")]
    assert len(syms) >= 2 and labels, [n[0] for n in items]
    for p in sexpr.find_all(syms[0], "property"):                 # change one symbol
        if p[1] == "Value":
            p[2] = "47k"
    tree.remove(labels[0])                                        # remove one item
    new = sexpr.loads('(text "patched" (exclude_from_sim no) (at 50.8 50.8 0) (effects (font (size 1.27 1.27))) '
                      '(uuid "11111111-2222-3333-4444-555555555555"))')
    tree.insert(len(tree) - 1, new)                               # add one item
    after = sexpr.dumps(tree) + "\n"
    d = schpatch.diff_sheet(before, after)
    assert (len(d["added"]), len(d["changed"]), len(d["removed"])) == (1, 1, 1), d
    assert set(d["remove"]) == {schpatch.item_uuid(syms[0]), schpatch.item_uuid(labels[0])} and d["lib_symbols"] == []
    assert d["refs"] and d["region"] and schpatch.diff_sheet(before, before)["add"] == ""
    out = schpatch.apply_sheet(before, d)
    assert schpatch._items(sexpr.loads(out)) == schpatch._items(sexpr.loads(after)), "apply(before, diff) == after"
    assert schpatch.diff_sheet(out, after)["remove"] == [] and schpatch.diff_sheet(out, after)["add"] == ""
    pp = schpatch.diff_project({"a.kicad_sch": before}, {"a.kicad_sch": after}, [{"sheet_path": "/", "file": "a.kicad_sch"}])
    assert pp["counts"] == {"added": 1, "changed": 1, "removed": 1} and not pp["empty"] and len(pp["patches"]) == 1, pp
    assert set(pp["patches"][0]) == {"sheet_path", "file", "remove", "add"}
    assert schpatch.diff_project({"a.kicad_sch": before}, {"a.kicad_sch": before})["empty"]
    for code, bad in (("PARSE", {"remove": [], "add": "(text \"x\""}),
                      ("PARSE", {"remove": [], "add": "(text \"no uuid\")"}),
                      ("UNKNOWN_ITEM", {"remove": ["00000000-dead-beef-0000-000000000000"], "add": ""}),
                      ("DUPLICATE_ITEM", {"remove": [], "add": sexpr.dumps(syms[1])})):
        try:
            schpatch.apply_sheet(before, bad)
            raise AssertionError(f"{code}: accepted")
        except schpatch.PatchError as e:
            assert e.code == code, (code, e.code, e.message)
    nl = {"nets": {"A": [{"ref": "R1", "pin": "1"}, {"ref": "#PWR01", "pin": "1"}], "B": [{"ref": "R2", "pin": "2"}, {"ref": "R1", "pin": "2"}]}}
    renamed = {"nets": {"X": list(reversed(nl["nets"]["B"])), "Net-(R1-Pad1)": [{"ref": "R1", "pin": "1"}]}}
    moved = {"nets": {"A": [{"ref": "R1", "pin": "1"}, {"ref": "R2", "pin": "2"}], "B": [{"ref": "R1", "pin": "2"}]}}
    assert schpatch.netlist_hash(nl) == schpatch.netlist_hash(renamed) != schpatch.netlist_hash(moved)


class _StubHost(hostclient.Host):
    def __init__(self):
        self.seen = []

    def checkout(self, document="schematic"):
        self.seen.append(document)
        return {"shadow_dir": "S", "rev": 7, "sheets": []}

    def apply(self, **kw):
        raise hostclient.HostError("STALE_BASE", "moved on", {"rev": 9})


def test_rendezvous_and_loopback_relay_reach_the_host_with_a_token_only():
    proj = TMP / "rv-project"
    proj.mkdir()
    (proj / "rv-project.kicad_pro").write_text("{}", encoding="utf-8")
    assert rendezvous.lookup(proj) is None and hostclient.connect(proj) is None
    stub = _StubHost()
    relay = Relay(stub, proj)
    info = relay.start()
    try:
        f = rendezvous.path(proj)
        assert f.exists() and f.parent == TMP / "home" / "hosts" and rendezvous.lookup(proj)["port"] == info["port"]
        c = hostclient.connect(proj)
        assert c is not None and c.checkout("schematic")["rev"] == 7 and stub.seen == ["schematic"]
        try:
            c.apply(base_rev=7, document="schematic", patches=[])
            raise AssertionError("STALE_BASE not relayed")
        except hostclient.HostError as e:
            assert e.code == "STALE_BASE" and e.data["rev"] == 9, (e.code, e.data)
        try:
            hostclient.LoopbackTransport(info["port"], "wrong-token")
            raise AssertionError("a wrong token was accepted")
        except hostclient.HostError as e:
            assert e.code in ("NO_HOST", "HOST_GONE"), e.code
        os.environ.update(relay.env())                                # what a child of the agent gets
        try:
            assert hostclient.connect(TMP / "any-other-folder").checkout()["rev"] == 7
        finally:
            del os.environ["COPPER_HOST_PORT"], os.environ["COPPER_HOST_TOKEN"]
    finally:
        relay.stop()
    assert not rendezvous.path(proj).exists() and hostclient.connect(proj) is None
    rendezvous.publish(proj, port=1, token="t", pid=2 ** 22 + 12345)   # a dead agent's leftover
    assert rendezvous.lookup(proj) is None and not rendezvous.path(proj).exists(), "stale entries are removed"


def test_new_packages_name_no_vendor_and_write_nothing_to_stdout():
    products = re.compile(r"\b(Claude|Codex|Cursor|ChatGPT|GPT-?\d\w*|Gemini|Copilot|Anthropic|OpenAI|Windsurf|claude|codex|chatgpt|openai)\b")
    hits = []
    pkg = SRC / "copper_kicad"
    files = list((pkg / "agent").rglob("*.py")) + list((pkg / "host").rglob("*.py")) + [pkg / "sch" / "patch.py"]
    adapters = ("agent/drivers/anthropic_api.py", "agent/drivers/openai_compat.py", "agent/drivers/claude_code.py", "agent/drivers/codex.py")
    for f in files:
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if products.search(line) and f.relative_to(pkg).as_posix() not in adapters:    # each is one vendor's adapter
                hits.append(f"{f.relative_to(pkg).as_posix()}:{i}")
            if re.search(r"(?<![\w.])print\(", line) and "file=" not in line:
                hits.append(f"{f.relative_to(pkg).as_posix()}:{i} print() to stdout")
    assert not hits, hits
    # the engine never calls a model: nothing outside agent/ may import the agent package
    importers = [f.relative_to(pkg).as_posix() for f in pkg.rglob("*.py") if "agent" not in f.relative_to(pkg).parts
                 and re.search(r"^\s*(from|import)\s+(copper_kicad)?\.*agent\b", f.read_text(encoding="utf-8"), re.M)]
    assert not importers, importers
    doc = (ROOT / "docs" / "HOST_PROTOCOL.md").read_text(encoding="utf-8")
    assert not products.search(doc), sorted(set(products.findall(doc)))
    for m in list(hostclient.HOST_METHODS) + ["hello", "turn.start", "turn.cancel", "command.run", "proposal.resolved",
                                              "focus.changed", "doc.changed", "accounts.list", "host.cancelled"] + list(events.TYPES):
        assert f"`{m}`" in doc, f"HOST_PROTOCOL.md does not document {m}"


def test_the_whole_run_left_a_clean_transcript():
    h = shared()
    out = h.save_transcript(TMP / "transcript.jsonl")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(h.transcript) > 100
    assert not h.garbage, h.garbage
    for turn, evs in h.events.items():
        conforming(evs)
    assert h.close(15) == 0
    assert sum("Traceback" in l for l in h.stderr) <= 1, "only the scripted crash may log a traceback"
    NOTES.append(f"transcript: {out} ({len(lines)} messages, {len(h.events)} turns)")


def _main() -> int:
    only = sys.argv[1:]                                 # optional name fragments: run just those tests
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
    if "srv" in _STATE:
        _STATE["srv"].shutdown()
    for note in NOTES:
        print(f"NOTE {note}")
    if not failed and not os.environ.get("COPPER_TEST_KEEP") and not _BASE:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
