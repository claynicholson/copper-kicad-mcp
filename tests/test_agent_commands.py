"""The pane without a model: every no-model command end to end, the accounts.* methods and the usage ledger.

Spawns ``python -m copper_kicad.agent`` and drives it through tests/fakehost.py on a COPY of a real project
(schematic + routed board). No model, no network beyond a loopback test server, no real credential store,
nothing outside a temp folder; the project that is copied is never written.

    python tests/test_agent_commands.py [name-fragment ...]

    COPPER_TEST_PROJECT=<dir>   the real project to copy (default: KiCad's own 'pic_programmer' demo)
    COPPER_TEST_TMP=<dir>       keep the scratch files there instead of the system temp folder
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tests"))

_BASE = os.environ.get("COPPER_TEST_TMP")
if _BASE:
    Path(_BASE).mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="cmd-", dir=_BASE))        # short: deep project copies must stay under MAX_PATH
for _k, _d in (("COPPER_HOME", "home"), ("COPPER_CONFIG_HOME_OVERRIDE", "cfg"), ("KICAD_CONFIG_HOME", "kicad-config")):
    (TMP / _d).mkdir()
    os.environ[_k] = str(TMP / _d)             # inherited by every agent: no real ~/.copper, keychain or KiCad config
os.environ["COPPER_HOME"] = str(TMP / "home")        # spelled out: test_install.py checks every module for it

from fakehost import FakeHost, RpcFailure  # noqa: E402
from copper_kicad.agent import commands, events, jobs, usage  # noqa: E402

PLUGIN = ROOT / "tests" / "agent_cmd_testplugin.py"
GRACE = 3.0
ENV = {"COPPER_AGENT_CANCEL_GRACE_S": str(GRACE), "COPPER_AGENT_LOG": "debug"}
SECRET = "tk-test-DO-NOT-LEAK-91ab"
NOTES: list[str] = []
_N = [0]
_STATE: dict = {}


class Skip(Exception):
    pass


def workdir(name: str) -> Path:
    _N[0] += 1
    return TMP / f"{_N[0]:02d}-{name}"


def source_project() -> Path:
    if "src" not in _STATE:
        cand = os.environ.get("COPPER_TEST_PROJECT")
        if not cand:
            from copper_kicad.kicad import env
            try:
                inst = env.kicad()
                roots = ([Path(inst.share)] if inst.share else []) + [Path(inst.root) / "share" / "kicad"]
            except Exception:  # noqa: BLE001
                roots = []
            cand = next((str(r / "demos" / "pic_programmer") for r in roots if (r / "demos" / "pic_programmer").is_dir()), None)
        if not cand or not any(Path(cand).glob("*.kicad_pcb")) or not any(Path(cand).glob("*.kicad_sch")):
            raise Skip("no real project with a schematic and a board (set COPPER_TEST_PROJECT)")
        _STATE["src"] = Path(cand)
        _STATE["src_sig"] = tree_sig(Path(cand))
    return _STATE["src"]


def tree_sig(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in d.rglob("*")
            if p.is_file() and ".copper" not in p.parts and "copper-out" not in p.parts}


def H() -> FakeHost:
    """One agent on one project copy for the tests that do not need their own."""
    if "host" not in _STATE:
        h = FakeHost(source_project(), workdir("shared"), env=ENV, plugins=[PLUGIN]).start()
        h.hello_result = h.hello()
        _STATE["host"] = h
    return _STATE["host"]


def facts() -> dict:
    """A two-pin part of the project, where it is, and a signal net on it (read from the copy, in-process)."""
    if "facts" not in _STATE:
        h = H()
        from copper_kicad.mcp import registry
        import copper_kicad.tools  # noqa: F401

        def call(name, **a):
            r = registry.call_tool(name, dict(a, project=str(h.live)))
            assert not r.get("isError"), r["content"][0]["text"][:400]
            return r.get("structuredContent") or {}
        comps = call("sch_read", query="components", limit=400)["components"]
        best = None
        for c in sorted(comps, key=lambda c: (not str(c["ref"]).startswith("R"), str(c["ref"]))):
            pins = call("sch_read", query="pins", ref=c["ref"]).get("pins") or []
            nets = [p.get("net") for p in pins if p.get("net")]
            signal = [n for n in nets if n.startswith("/")]
            if len(pins) == 2 and len(nets) == 2 and signal:
                best = {"ref": c["ref"], "at": c["at"], "value": c.get("value"), "nets": nets, "net": signal[0]}
                break
        assert best, "the test project has no two-pin part on a signal net"
        _STATE["facts"] = best
        NOTES.append(f"project {source_project().name}: part {best['ref']} ({best['value']}) on {best['nets']}")
    return _STATE["facts"]


def conforming(evs: list[dict]) -> list[dict]:
    problems = events.check_stream(evs)
    assert not problems, (problems, [json.dumps(e)[:300] for e in evs])
    return evs


def types(evs) -> list[str]:
    return [e["type"] for e in evs]


def of(evs, type_) -> list[dict]:
    return [e for e in evs if e["type"] == type_]


def calls_since(h: FakeHost, n0: int) -> list[str]:
    return [m for m, _ in h.calls[n0:]]


def board(h: FakeHost) -> Path:
    return next(h.live.glob("*.kicad_pcb"))


def sheet(h: FakeHost) -> Path:
    """The root sheet of the live copy."""
    return h.live / (next(h.live.glob("*.kicad_pro")).stem + ".kicad_sch")


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def children(pid: int) -> list[int]:
    """Process ids whose parent is ``pid``."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}').ProcessId"],
                                 capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout
        else:
            out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def wait_until(fn, timeout: float = 10.0, step: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())


def cancel_running(h: FakeHost, turn: str, settle: float = 0.7) -> tuple[list[dict], float]:
    """Wait until the turn's job process is starting, press Stop, return (events, seconds until done)."""
    h.wait_event(turn, "tool_start", 60)
    time.sleep(settle)
    t0 = time.time()
    assert h.cancel(turn) == {"cancelled": True}
    evs = conforming(h.wait_done(turn, 30))
    return evs, time.time() - t0


# ── the fake provider (accounts.test and usage) ───────────────────────────
class _Provider(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("X-Test-Key")))
        if self.headers.get("X-Test-Key") != SECRET:
            body, code = b'{"error":"bad key"}', 401
        elif self.path.startswith("/models"):
            body, code = b'{"models":["test-large","test-small"]}', 200
        elif self.path.startswith("/turn"):
            big = "big" in self.path
            body = json.dumps({"text": "done.", "in": 5000 if big else 1200, "out": 300 if big else 80,
                               "cached": 4000 if big else 1000, "cost": 0.0125 if big else None,
                               "model": "test-large" if big else None}).encode()
            code = 200
        else:
            body, code = b"{}", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def provider() -> str:
    if "srv" not in _STATE:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _STATE["srv"] = srv
    return f"http://127.0.0.1:{_STATE['srv'].server_address[1]}"


# ── the surface ───────────────────────────────────────────────────────────
def test_hello_offers_every_command_and_help_lists_them():
    h = H()
    cmds = {c["name"]: c for c in h.hello_result["commands"]}
    want = {"verify", "tidy", "route", "pour", "export", "parts", "focus", "undo-checkpoint", "help", "usage", "accounts", "tool"}
    assert want <= set(cmds), sorted(want - set(cmds))
    assert not [k for k in h.hello_result["load_errors"] if k.startswith("commands.")], h.hello_result["load_errors"]
    assert cmds["tidy"]["documents"] == ["schematic"] and cmds["route"]["documents"] == ["board"]
    assert cmds["parts"]["needs_project"] is False and cmds["verify"]["needs_project"] is True
    assert cmds["verify"]["args"]["what"]["enum"] == ["erc", "drc", "all"]
    text = h.text_of(conforming(h.run_command("help")))
    for name in want:
        assert f"/{name}" in text, (name, text)


def test_commands_need_a_project_except_the_ones_that_do_not():
    h = FakeHost(None, workdir("noproject"), env=ENV).start()
    try:
        h.hello()
        for name in ("verify", "tidy", "route", "pour", "export", "undo-checkpoint"):
            evs = conforming(h.run_command(name))
            assert types(evs) == ["error", "done"] and evs[0]["code"] == "NO_PROJECT", (name, evs)
        evs = conforming(h.run_command("parts", {"query": "10k 0603", "offline": True}))
        assert evs[-1]["status"] == "ok" and "part(s) for" in h.text_of(evs), evs
        assert "No model usage" in h.text_of(conforming(h.run_command("usage")))
        assert h.text_of(conforming(h.run_command("focus", {"detail": True}))).startswith("Nothing is selected")
        recs = usage.read(None)
        assert recs and all(r["kind"] == "command" and r["in"] == 0 for r in recs), recs      # $COPPER_HOME/agent/usage.jsonl
        assert usage.path(None) == TMP / "home" / "agent" / "usage.jsonl" and usage.path(None).is_file()
    finally:
        assert h.close() == 0
        assert not h.garbage, h.garbage


# ── /verify ───────────────────────────────────────────────────────────────
def test_verify_erc_runs_on_the_live_document_and_lists_findings():
    h = H()
    n0 = len(h.calls)
    evs = conforming(h.run_command("verify", {"what": "erc"}, timeout=240))
    assert types(evs)[:2] == ["tool_start", "tool_end"] and evs[0]["name"] == "verify" and evs[0]["title"] == "ERC", evs[:2]
    assert evs[1]["ok"] and evs[-1]["status"] == "ok"
    f = of(evs, "findings")
    assert len(f) == 1 and f[0]["document"] == "schematic" and f[0]["source"] == "erc", f
    c = f[0]["counts"]
    assert f"ERC: {c['errors']} error(s), {c['warnings']} warning(s)" in h.text_of(evs), h.text_of(evs)
    assert all(i["severity"] in ("error", "warning") and i["message"] and i["type"] for i in f[0]["items"])
    assert calls_since(h, n0) == ["host.verify"], calls_since(h, n0)      # the app checked its live document
    _STATE["erc_counts"] = c
    with_refs = next((i for i in f[0]["items"] if i.get("refs")), None)
    if with_refs is None:
        NOTES.append("verify: the project has no ERC finding with a part, selection ordering not exercised")
        return
    ref = with_refs["refs"][0]
    n0 = len(h.calls)
    evs = conforming(h.run_turn("/verify erc", timeout=240, focus={"document": "schematic", "mode": "selection", "refs": [ref]}))
    f2 = of(evs, "findings")[0]
    assert ref in f2["items"][0]["refs"] and "on your selection" in h.text_of(evs), (ref, h.text_of(evs))
    assert h.calls[n0][1].get("baseline"), "the second check passes the first one's baseline back"


def test_verify_falls_back_to_the_engine_on_a_shadow_checkout_and_the_saved_board():
    h = H()
    h.fail["host.verify"] = ("UNSUPPORTED", "this app build has no live checks")
    try:
        before = (md5(sheet(h)), md5(board(h)))
        n0 = len(h.calls)
        evs = conforming(h.run_turn("/verify all", timeout=420))
        starts, ends, f = of(evs, "tool_start"), of(evs, "tool_end"), of(evs, "findings")
        assert [s["title"] for s in starts] == ["ERC", "DRC"] and all(e["ok"] for e in ends), (starts, ends)
        assert [x["document"] for x in f] == ["schematic", "board"] and [x["source"] for x in f] == ["erc", "drc"]
        assert "unrouted" in f[1]["counts"] and "as last saved" in h.text_of(evs), h.text_of(evs)
        if "erc_counts" in _STATE:
            assert f[0]["counts"] == _STATE["erc_counts"], (f[0]["counts"], _STATE["erc_counts"])   # app and engine agree
        used = calls_since(h, n0)
        assert used.count("host.verify") == 2 and "host.apply" not in used and "host.reload" not in used, used
        assert of(evs, "tool_progress"), "the engine check reports progress from its child process"
        assert before == (md5(sheet(h)), md5(board(h))), "a check must not change the design"
    finally:
        h.fail.pop("host.verify", None)


def test_verify_can_be_stopped_while_the_engine_check_runs():
    h = H()
    h.fail["host.verify"] = ("UNSUPPORTED", "no live checks")
    try:
        turn = h.command("verify", {"what": "drc"})
        evs, dt = cancel_running(h, turn)
        assert evs[-1]["status"] == "cancelled" and dt < GRACE + 2, (dt, types(evs))
        end = of(evs, "tool_end")[0]
        assert end["ok"] is False and end["error"]["code"] == "CANCELLED", end
        assert wait_until(lambda: not children(h.p.pid), 15), "the check's process tree must be gone"
        assert h.text_of(conforming(h.run_command("help")))                 # the agent is fine
    finally:
        h.fail.pop("host.verify", None)


# ── /tidy ─────────────────────────────────────────────────────────────────
def test_tidy_changes_the_schematic_only_through_one_host_apply():
    h = H()
    f, focus, targs = sheet(h), None, {}
    n0 = len(h.calls)
    evs = conforming(h.run_turn("/tidy preview", timeout=420))                           # dry run
    if not of(evs, "tool_end")[0]["ok"] and "hierarchical" in h.text_of(evs):
        # the root sheet only holds sheet symbols: tidy the sheet the user is looking at instead (focus.sheet_path)
        sub = next((s_ for s_ in h.sheets() if s_["sheet_path"] != "/"), None)
        if sub is None:
            raise Skip("sch_tidy refuses this project: " + h.text_of(evs)[:200])
        focus = {"document": "schematic", "sheet_path": sub["sheet_path"], "mode": "selection", "refs": []}
        evs = conforming(h.run_turn("/tidy preview", timeout=420, focus=focus))
        assert of(evs, "tool_start")[0]["args"].get("sheet") == sub["sheet_path"].rsplit("/", 1)[-1], of(evs, "tool_start")[0]
        name = h.text_of(evs).split()[0]
        f = next(iter(h.live.rglob(name)), f) if name.endswith(".kicad_sch") else f
    if of(evs, "tool_end")[0]["ok"] and "/tidy force" in h.text_of(evs):
        targs = {"force": True}                          # a hand-drawn sheet: the command says how to insist
        evs = conforming(h.run_turn("/tidy force preview", timeout=420, focus=focus))
        assert of(evs, "tool_start")[0]["args"].get("force") is True and "/tidy force" not in h.text_of(evs), h.text_of(evs)
    if not of(evs, "tool_end")[0]["ok"]:
        raise Skip("sch_tidy refuses this project: " + h.text_of(evs)[:200])
    original = f.read_bytes()
    assert evs[-1]["status"] == "ok" and "host.apply" not in calls_since(h, n0) and not of(evs, "proposal"), types(evs)
    # the app refuses the patch: the user's file is untouched, i.e. host.apply is the ONLY way in
    h.busy = True
    n0 = len(h.calls)
    evs = conforming(h.run_command("tidy", dict(targs), timeout=420, focus=focus))
    h.busy = False
    assert calls_since(h, n0).count("host.apply") == 1 and not of(evs, "proposal"), calls_since(h, n0)
    assert of(evs, "tool_end")[0]["error"]["code"] == "BUSY" and "did not work" in h.text_of(evs), h.text_of(evs)
    assert f.read_bytes() == original, "a refused proposal must leave the document byte-identical"
    # for real
    n0, u0 = len(h.calls), len(h.undo_stack)
    evs = conforming(h.run_command("tidy", dict(targs, style="pro"), timeout=420, focus=focus))
    used = calls_since(h, n0)
    assert used.count("host.apply") == 1, used
    p = of(evs, "proposal")
    assert len(p) == 1 and p[0]["document"] == "schematic" and p[0]["state"] == "review", p
    assert p[0]["summary"] == "Tidy the sheet layout" and sum(p[0]["counts"].values()) > 0, p[0]
    assert p[0].get("netlist_ok") is True, f"the tidied sheet must have the same netlist: {p[0]}"
    assert "Keep or Undo" in h.text_of(evs), h.text_of(evs)
    assert f.read_bytes() != original and len(h.undo_stack) == u0 + 1, "one apply = one undo step"
    apply_params = [p_ for m, p_ in h.calls[n0:] if m == "host.apply"][0]
    assert apply_params["expect"]["netlist_hash"].startswith("sha256:"), apply_params.get("expect")
    assert h.undo() == p[0]["proposal_id"] and f.read_bytes() == original, "one undo restores the original bytes"
    NOTES.append(f"tidy of {f.name}: {p[0]['counts']} in one proposal, netlist_ok={p[0].get('netlist_ok')}")


# ── /parts and /focus ─────────────────────────────────────────────────────
def test_parts_searches_by_query_and_from_the_selected_part():
    h = H()
    ref = facts()["ref"]
    evs = conforming(h.run_command("parts", {"query": "10k 0603", "offline": True}))
    assert [e["name"] for e in of(evs, "tool_start")] == ["parts_search"], types(evs)
    assert of(evs, "tool_start")[0]["args"] == {"query": "10k 0603", "limit": 6, "live": False}
    text = h.text_of(evs)
    assert "part(s) for '10k 0603'" in text and "0603" in text and "stock" in text, text
    evs = conforming(h.run_command("parts", {"offline": True},
                                   focus={"document": "schematic", "mode": "selection", "refs": [ref]}, timeout=180))
    assert [e["name"] for e in of(evs, "tool_start")] == ["sch_read", "parts_search"], types(evs)
    assert f"{ref} is " in h.text_of(evs), h.text_of(evs)
    evs = conforming(h.run_command("parts"))
    assert types(evs) == ["text_delta", "done"] and "/parts 10k 0603" in h.text_of(evs), evs
    assert commands.parse("/parts 4k7 0402 1%") == ("parts", {"query": "4k7 0402 1%"})


def test_focus_detail_reads_pins_nets_and_areas_from_the_engine():
    h = H()
    fx = facts()
    sel = {"document": "schematic", "sheet_path": "/", "mode": "selection", "refs": [fx["ref"]], "nets": fx["nets"]}
    evs = conforming(h.run_turn("/focus", focus=sel))
    assert types(evs) == ["text_delta", "reveal", "done"], types(evs)                 # plain /focus stays instant
    evs = conforming(h.run_turn("/focus detail", focus=sel, timeout=180))
    assert [e["name"] for e in of(evs, "tool_start")] == ["sch_read"], types(evs)
    text = h.text_of(evs)
    assert f"parts {fx['ref']}" in text and all(n in text for n in fx["nets"]) and f"\n{fx['ref']}  " in text, text
    # a net alone
    evs = conforming(h.run_command("focus", {"detail": True}, timeout=180,
                                   focus={"document": "schematic", "mode": "selection", "nets": [fx["net"]]}))
    assert f"net {fx['net']}: " in h.text_of(evs) and fx["ref"] + "." in h.text_of(evs), h.text_of(evs)
    # a point on the sheet: the engine says what is there, on the live document's shadow
    n0 = len(h.calls)
    evs = conforming(h.run_command("focus", {"detail": True}, timeout=180,
                                   focus={"document": "schematic", "sheet_path": "/", "mode": "point", "point_mm": fx["at"]}))
    assert [e["name"] for e in of(evs, "tool_start")] == ["focus"] and of(evs, "tool_end")[0]["ok"], evs
    rev = of(evs, "reveal")
    assert rev and fx["ref"] in rev[-1]["refs"] and f"{fx['ref']}  " in h.text_of(evs), (rev, h.text_of(evs))
    assert "host.apply" not in calls_since(h, n0)
    # the same part on the board
    evs = conforming(h.run_command("focus", {"detail": True}, timeout=180,
                                   focus={"document": "board", "mode": "selection", "refs": [fx["ref"]]}))
    assert [e["name"] for e in of(evs, "tool_start")] == ["pcb_read"], types(evs)
    assert f"{fx['ref']}  " in h.text_of(evs) and " mm, " in h.text_of(evs) and "as last saved" in h.text_of(evs)
    evs = conforming(h.run_turn("/focus sideways", focus=sel))
    assert "does not know" in h.text_of(evs) and not of(evs, "tool_start"), evs


# ── /export ───────────────────────────────────────────────────────────────
def test_export_saves_first_then_writes_files_and_says_when_it_could_not_save():
    h = H()
    n0 = len(h.calls)
    evs = conforming(h.run_turn("/export netlist", timeout=240))
    used = h.calls[n0:]
    assert [m for m, _ in used] == ["host.ask_permission", "host.save"], used
    assert used[0][1]["kind"] == "save" and used[0][1]["detail"]["documents"] == ["schematic"], used[0]
    assert used[1][1] == {"document": "schematic"}
    assert of(evs, "tool_start")[0]["name"] == "export" and of(evs, "tool_end")[0]["ok"], evs
    outs = list((h.live / "copper-out").rglob("*.net"))
    assert len(outs) == 1 and outs[0].stat().st_size > 1000 and "exported netlist" in h.text_of(evs), (outs, h.text_of(evs))
    assert "as last saved" not in h.text_of(evs)
    # the user says no to saving: the export still runs, and says what it read
    h.permission = lambda p: {"granted": p.get("kind") != "save", "remember": False}
    try:
        n0 = len(h.calls)
        evs = conforming(h.run_command("export", {"kind": "drill"}, focus={"document": "board", "mode": "selection"}, timeout=240))
        assert calls_since(h, n0) == ["host.ask_permission"], calls_since(h, n0)
        assert of(evs, "tool_end")[0]["ok"] and "as last saved" in h.text_of(evs), h.text_of(evs)
    finally:
        h.permission = {"granted": True, "remember": False}
    evs = conforming(h.run_command("export"))
    assert types(evs) == ["text_delta", "done"] and "Kinds: fab" in h.text_of(evs)
    evs = conforming(h.run_turn("/export hologram"))
    assert "does not know 'hologram'" in h.text_of(evs) and not of(evs, "tool_start")
    assert not list(h.live.glob("*.net")), "exports go to copper-out, not next to the design"


def test_export_of_the_fab_pack_can_be_stopped():
    h = H()
    before = (md5(sheet(h)), md5(board(h)))
    turn = h.command("export", {"kind": "fab"})
    evs, dt = cancel_running(h, turn)
    assert evs[-1]["status"] == "cancelled" and dt < GRACE + 2, (dt, types(evs))
    assert of(evs, "tool_start")[0]["name"] == "fab_export" and of(evs, "tool_end")[0]["error"]["code"] == "CANCELLED"
    assert wait_until(lambda: not children(h.p.pid), 15), "the export's process tree must be gone"
    assert before == (md5(sheet(h)), md5(board(h)))


# ── board jobs ────────────────────────────────────────────────────────────
def test_route_runs_as_a_file_job_and_undo_checkpoint_restores_and_redoes():
    h = H()
    fx = facts()
    b = board(h)
    original, sch0 = b.read_bytes(), md5(sheet(h))
    n0 = len(h.calls)
    evs = conforming(h.run_turn(f"/route {fx['net']} reroute", timeout=600))
    used = h.calls[n0:]
    assert of(evs, "tool_end")[0]["ok"], (of(evs, "tool_end")[0], h.text_of(evs))
    assert [m for m, _ in used] == ["host.ask_permission", "host.save", "host.save", "host.checkpoint", "host.reload"], used
    assert used[0][1]["kind"] == "file_job" and used[0][1]["detail"]["tool"] == "pcb_route_guided", used[0]
    assert used[-1][1] == {"document": "board"}
    start, end = of(evs, "tool_start")[0], of(evs, "tool_end")[0]
    assert start["name"] == "pcb_route_guided" and start["args"] == {"ripup": "nets", "nets": [fx["net"]]}, start
    assert start["title"] == f"Re-route {fx['net']}" and end["ok"], (start, end)
    prog = of(evs, "tool_progress")
    assert len(prog) >= 2 and all(p["call"] == start["call"] for p in prog), prog
    assert "Undo it with /undo-checkpoint" in h.text_of(evs), h.text_of(evs)
    assert of(evs, "reveal")[-1] == dict(of(evs, "reveal")[-1], net=fx["net"], document="board")
    routed = b.read_bytes()
    assert routed != original and md5(sheet(h)) == sch0, "the job changed the board file and nothing else"
    cps = jobs.checkpoints(h.live)
    assert len(cps) == 1 and cps[0]["state"] == "done" and cps[0]["tool"] == "pcb_route_guided", cps
    assert (h.live / ".copper" / "jobs" / cps[0]["id"] / b.name).read_bytes() == original

    listing = h.text_of(conforming(h.run_turn("/undo-checkpoint list")))
    assert cps[0]["id"] in listing and f"before: Re-route {fx['net']}" in listing, listing
    n0 = len(h.calls)
    evs = conforming(h.run_command("undo-checkpoint", timeout=120))
    assert calls_since(h, n0) == ["host.ask_permission", "host.save", "host.reload"], calls_since(h, n0)
    assert b.read_bytes() == original, "the board is byte-identical to before the job"
    assert "as it was before" in h.text_of(evs) and "again to redo" in h.text_of(evs), h.text_of(evs)
    evs = conforming(h.run_turn("/undo-checkpoint", timeout=120))                       # again = redo
    assert b.read_bytes() == routed and "Redid" in h.text_of(evs), h.text_of(evs)
    evs = conforming(h.run_command("undo-checkpoint", {"id": "nope"}))
    assert of(evs, "tool_end")[0]["error"]["code"] == "NO_CHECKPOINT" and b.read_bytes() == routed
    evs = conforming(h.run_command("undo-checkpoint", timeout=120))                     # and back
    assert b.read_bytes() == original and md5(sheet(h)) == sch0
    NOTES.append(f"route: {len(prog)} progress events; undo / redo / undo of {fx['net']} byte-exact")


def test_a_stopped_route_leaves_the_board_exactly_as_it_was():
    h = H()
    b = board(h)
    original = b.read_bytes()
    cps0 = [c["id"] for c in jobs.checkpoints(h.live, states=None)]
    n0 = len(h.calls)
    turn = h.command("route", {"reroute": True})                    # rip up and re-route everything: the long one
    h.wait_event(turn, "tool_start", 60)
    # Stop late: after the router wrote the board, while KiCad's DRC still runs
    wait_until(lambda: any("verifying" in (e.get("message") or "") for e in h.events.get(turn, []))
               or any(e["type"] == "done" for e in h.events.get(turn, [])), 300)
    written = b.read_bytes() != original
    t0 = time.time()
    assert h.cancel(turn) == {"cancelled": True}
    evs = conforming(h.wait_done(turn, 30))
    dt = time.time() - t0
    assert evs[-1]["status"] == "cancelled" and dt < GRACE + 2, (dt, types(evs))
    end = of(evs, "tool_end")[0]
    assert end["ok"] is False and end["error"]["code"] == "CANCELLED", end
    assert wait_until(lambda: b.read_bytes() == original, 10), "the board file must be byte-identical after Stop"
    assert wait_until(lambda: not children(h.p.pid), 15), "router, worker and job process must all be gone"
    time.sleep(1.0)
    assert b.read_bytes() == original, "nothing writes the board after the job was killed"
    assert "host.reload" not in calls_since(h, n0), "no reload: the editor still shows what is on disk"
    assert [c["id"] for c in jobs.checkpoints(h.live, states=None)] == cps0, "a cancelled job leaves no checkpoint"
    assert h.text_of(conforming(h.run_command("help")))
    NOTES.append(f"cancelled a full re-route {'after' if written else 'BEFORE'} the router wrote the board: done after "
                 f"{dt:.2f} s, board byte-identical again, no process left")


def test_board_jobs_ask_first_and_a_preview_keeps_the_undo_history():
    h = H()
    b = board(h)
    before = b.read_bytes()
    h.permission = {"granted": False, "remember": False}
    try:
        n0 = len(h.calls)
        evs = conforming(h.run_turn("/pour GND", timeout=120))
        assert calls_since(h, n0) == ["host.ask_permission"], calls_since(h, n0)
        assert of(evs, "tool_end")[0]["error"]["code"] == "DENIED" and "not run" in h.text_of(evs), h.text_of(evs)
        evs = conforming(h.run_command("undo-checkpoint", timeout=60))
        assert of(evs, "tool_end")[0]["error"]["code"] in ("DENIED", "NO_CHECKPOINT"), evs
    finally:
        h.permission = {"granted": True, "remember": False}
    assert b.read_bytes() == before
    n0 = len(h.calls)
    evs = conforming(h.run_turn("/route preview", timeout=600))
    used = calls_since(h, n0)
    assert used == ["host.ask_permission", "host.save", "host.save"], used       # no checkpoint, no reload
    assert of(evs, "tool_start")[0]["args"] == {"ripup": "none", "dry_run": True} and of(evs, "tool_end")[0]["ok"], evs
    assert b.read_bytes() == before and "/undo-checkpoint" not in h.text_of(evs), h.text_of(evs)
    # /pour end to end: refilling the zones is a file job like any other
    n0 = len(h.calls)
    evs = conforming(h.run_turn("/pour refill", timeout=600))
    start, end = of(evs, "tool_start")[0], of(evs, "tool_end")[0]
    assert start["name"] == "pcb_pour" and start["args"] == {"fill_only": True} and start["title"] == "Refill the copper zones"
    assert calls_since(h, n0)[:4] == ["host.ask_permission", "host.save", "host.save", "host.checkpoint"], calls_since(h, n0)
    assert (end["ok"] and "pcb_pour" in h.text_of(evs)) or "did not work" in h.text_of(evs), h.text_of(evs)
    assert ("host.reload" in calls_since(h, n0)) == (b.read_bytes() != before), "reload exactly when the file changed"
    NOTES.append(f"pour refill: ok={end['ok']}, board changed={b.read_bytes() != before}")
    before = b.read_bytes()
    # a busy editor: the job never starts
    h.fail["host.save"] = ("BUSY", "the editor is in the middle of a move")
    try:
        evs = conforming(h.run_command("pour", {"fill_only": True}, timeout=120))
        assert of(evs, "tool_end")[0]["error"]["code"] == "BUSY" and b.read_bytes() == before, evs
    finally:
        h.fail.pop("host.save", None)


def test_route_and_pour_map_words_and_focus_to_engine_arguments():
    """The argument mapping, without running a router: the job runner is replaced in-process."""
    seen = []
    real = jobs.board_job

    def fake(turn, name, args=None, label=None):
        seen.append((name, dict(args or {}), label))
        return {"ok": True, "text": "ok", "data": {}, "images": [], "error": None, "proposal": None, "route": "board"}

    def turn(focus=None):
        out = []
        return SimpleNamespace(focus=focus, project_dir=str(TMP), server=SimpleNamespace(hello={"frame": "board"}),
                               request=SimpleNamespace(conversation=""), text=out.append, said=out,
                               reveal=lambda **k: out.append(("reveal", k)), findings=lambda *a, **k: None,
                               check_cancel=lambda: None)

    def run(line, focus=None):
        del seen[:]
        t = turn(focus)
        name, args = commands.parse(line)
        commands.run(t, name, args)
        return (seen[0] if seen else None), t.said

    jobs.board_job = fake
    try:
        sel = {"document": "board", "mode": "selection", "nets": ["/SDA", "/SCL"]}
        assert run("/route")[0] == ("pcb_route_guided", {"ripup": "none"}, "Route the board")
        assert run("/route", sel)[0] == ("pcb_route_guided", {"ripup": "none", "nets": ["/SDA", "/SCL"]}, "Route /SDA, /SCL")
        assert run("/route all reroute", sel)[0] == ("pcb_route_guided", {"ripup": "all"}, "Re-route the board")
        assert run("/route /A, /B reroute preview")[0] == ("pcb_route_guided", {"ripup": "nets", "nets": ["/A", "/B"], "dry_run": True},
                                                          "Re-route /A, /B")
        assert run('/route {"nets": ["VBUS"], "reroute": true}')[0][1] == {"ripup": "nets", "nets": ["VBUS"]}
        assert run("/route freerouting reroute")[0] == ("pcb_route", {"mode": "auto", "clear_existing": True},
                                                        "Autoroute the board (Freerouting)")
        job, said = run("/route freerouting preview")
        assert job is None and "no preview" in said[0], said
        assert run("/route", {"document": "board", "mode": "box", "nets": ["/X"], "box_mm": [0, 0, 1, 1]})[0][1] == {"ripup": "none"}
        assert run("/pour")[0] == ("pcb_pour", {}, "Pour copper on GND")
        assert run("/pour", {"document": "board", "mode": "selection", "nets": ["+3V3"]})[0] == \
            ("pcb_pour", {"net": "+3V3"}, "Pour copper on +3V3")
        assert run("/pour VBUS stitch")[0] == ("pcb_pour", {"stitch": True, "net": "VBUS"}, "Pour copper on VBUS")
        assert run("/pour GND remove")[0] == ("pcb_pour", {"remove": True, "net": "GND"}, "Remove the GND pour")
        assert run("/pour refill", sel)[0] == ("pcb_pour", {"fill_only": True}, "Refill the copper zones")
        job, said = run("/pour A B")
        assert job is None and "One net at a time" in said[0]
        _, said = run("/pour +5V")
        assert ("reveal", {"document": "board", "net": "+5V"}) in said, said
    finally:
        jobs.board_job = real


def test_job_checkpoints_snapshot_restore_and_prune():
    d = workdir("jobs")
    d.mkdir(parents=True)
    (d / "x.kicad_pcb").write_bytes(b"(kicad_pcb v1)\r\n")
    (d / "x.kicad_pro").write_text("{}", encoding="utf-8")
    (d / "x.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    job = jobs.snapshot(d, "Route everything", "pcb_route_guided", "host-cp-7")
    assert job["files"] == ["x.kicad_pcb", "x.kicad_pro"] and job["state"] == "open" and job["host_checkpoint"] == "host-cp-7"
    assert jobs.checkpoints(d) == [] and [j["id"] for j in jobs.checkpoints(d, states=None)] == [job["id"]]
    assert jobs.changed_since(d, job) == []
    (d / "x.kicad_pcb").write_bytes(b"(kicad_pcb v2)")
    (d / "x.kicad_dru").write_text("(rules)", encoding="utf-8")            # the job created a file
    (d / "x.kicad_pro").unlink()                                           # ... and lost one
    (d / "x.kicad_sch").write_text("(kicad_sch edited)", encoding="utf-8")
    assert jobs.changed_since(d, job) == ["x.kicad_dru", "x.kicad_pcb", "x.kicad_pro"]
    assert sorted(jobs.put_back(d, job)) == ["x.kicad_dru", "x.kicad_pcb", "x.kicad_pro"]
    assert (d / "x.kicad_pcb").read_bytes() == b"(kicad_pcb v1)\r\n" and not (d / "x.kicad_dru").exists()
    assert (d / "x.kicad_pro").read_text(encoding="utf-8") == "{}"
    assert (d / "x.kicad_sch").read_text(encoding="utf-8") == "(kicad_sch edited)", "the schematic is never restored"
    assert not list(d.glob(".*copper-restore")) and jobs.changed_since(d, job) == []
    for i in range(jobs.KEEP_JOBS + 3):
        jobs.snapshot(d, f"job {i}", "t")
    assert len(list((d / ".copper" / "jobs").iterdir())) == jobs.KEEP_JOBS


# ── accounts.* ────────────────────────────────────────────────────────────
def test_accounts_add_list_test_remove_with_the_secret_in_the_link_store_only():
    h = H()
    from copper_kicad.link import store
    store.reset_cache()
    st = store.get_store()
    assert st.id == "keyfile" and st.strength != "os", f"the test must not use an OS store, got {st.id}"
    base = provider()
    n0 = len(h.transcript)
    kinds = {d["kind"]: d for d in h.request("accounts.list")["drivers"]}
    assert kinds["test-key"]["needs"] == ["secret", "base_url"] and kinds["none"]["available"], kinds
    a = h.request("accounts.add", {"kind": "test-key", "label": "Key account", "base_url": base, "secret": SECRET,
                                   "default_model": "test-small"})["account"]
    assert a["kind"] == "test-key" and a["label"] == "Key account" and a["has_secret"] is True and "secret" not in a, a
    assert a["base_url"] == base and a["default_model"] == "test-small" and a["id"].startswith("test-key-"), a
    listed = h.request("accounts.list")["accounts"]
    assert listed[0]["id"] == "none" and [x for x in listed if x["id"] == a["id"]] == [a], listed
    assert st.get(f"agent.{a['id']}") == SECRET, "the secret is in the link store under agent.<id>"
    disk = (TMP / "home" / "agent" / "accounts.json").read_text(encoding="utf-8")
    assert SECRET not in disk and a["id"] in disk
    # accounts.test: one cheap request with the stored key
    r = h.request("accounts.test", {"id": a["id"]}, timeout=60)
    assert r["ok"] is True and "2 model(s)" in r["message"] and [m["id"] for m in r["models"]] == ["test-large", "test-small"], r
    assert ("/models", SECRET) in _Provider.seen
    bad = h.request("accounts.add", {"kind": "test-key", "label": "wrong key", "base_url": base, "secret": "nope"})["account"]
    r = h.request("accounts.test", {"id": bad["id"]}, timeout=60)
    assert r["ok"] is False and r["kind"] == "auth" and r["code"] == "HTTP_401", r
    dead = h.request("accounts.add", {"kind": "test-key", "base_url": "http://127.0.0.1:9", "secret": "x"})["account"]
    r = h.request("accounts.test", {"id": dead["id"]}, timeout=60)
    assert r["ok"] is False and r["kind"] == "network", r
    for method, params, code in (("accounts.test", {"id": "ghost"}, "UNKNOWN_ACCOUNT"),
                                 ("accounts.remove", {"id": "ghost"}, "UNKNOWN_ACCOUNT"),
                                 ("accounts.remove", {"id": "none"}, "UNKNOWN_ACCOUNT"),
                                 ("accounts.add", {"kind": "none"}, "UNKNOWN_DRIVER"),
                                 ("accounts.add", {"label": "x"}, "INVALID_PARAMS"),
                                 ("accounts.add", {"kind": "test-key", "secret": 7}, "INVALID_PARAMS")):
        try:
            h.request(method, params)
            raise AssertionError(f"{method} {params} should fail with {code}")
        except RpcFailure as e:
            assert e.code == code, (method, params, e.code, e.message)
    text = h.text_of(conforming(h.run_command("accounts")))
    assert "Key account  (test-key" in text and "Can be connected:" in text and SECRET not in text, text
    # remove: the record and the secret both go
    for acct in (bad, dead):
        assert h.request("accounts.remove", {"id": acct["id"]}) == {}
        assert st.get(f"agent.{acct['id']}") is None
    assert [x["id"] for x in h.request("accounts.list")["accounts"]] == ["none", a["id"]]
    from_agent = json.dumps([t["msg"] for t in h.transcript[n0:] if t["dir"] == "agent>app"])
    assert SECRET not in from_agent and SECRET not in "".join(h.stderr), "the secret never comes back or reaches the log"
    _STATE["account"] = a["id"]


# ── usage ─────────────────────────────────────────────────────────────────
def test_usage_is_recorded_per_turn_and_summed_per_project():
    h = H()
    if "account" not in _STATE:
        test_accounts_add_list_test_remove_with_the_secret_in_the_link_store_only()
    acct = _STATE["account"]
    f = h.live / ".copper" / "usage.jsonl"
    base = usage.summary(h.live)["project"]
    lines0 = len(usage.read(h.live))
    r1 = h.request("turn.start", {"conversation": None, "text": "small question", "account": acct, "mode": "chat"})
    evs = conforming(h.wait_done(r1["turn"], 60))
    assert evs[-1]["usage"] == {"in": 1200, "out": 80, "cached": 1000, "model": "test-small"}, evs[-1]
    conv = r1["conversation"]
    r2 = h.request("turn.start", {"conversation": conv, "text": "big question", "account": acct, "mode": "agent"})
    evs = conforming(h.wait_done(r2["turn"], 60))
    assert evs[-1]["usage"]["cost"] == 0.0125 and evs[-1]["usage"]["model"] == "test-large", evs[-1]
    conforming(h.run_command("help"))                                           # a command: a line with zero tokens
    recs = usage.read(h.live)[lines0:]
    assert [r["kind"] for r in recs] == ["model", "model", "command"], recs
    assert recs[0] == dict(recs[0], turn=r1["turn"], conversation=conv, status="ok", account=acct, account_kind="test-key",
                           model="test-small", mode="chat", cached=1000, tools=0) and "cost" not in recs[0], recs[0]
    assert recs[0]["in"] == 1200 and recs[0]["out"] == 80 and recs[1]["in"] == 5000 and recs[1]["cost"] == 0.0125
    assert recs[2] == dict(recs[2], command="help", status="ok") and recs[2]["in"] == recs[2]["out"] == 0, recs[2]
    assert all(isinstance(r["ms"], int) and isinstance(r["ts"], int) for r in recs)
    raw = f.read_text(encoding="utf-8")
    assert SECRET not in raw and "question" not in raw, "the ledger holds numbers, not text or secrets"
    s = h.request("usage.get", {"conversation": conv, "limit": 3})
    p = s["project"]
    assert p["model_turns"] == base["model_turns"] + 2 and p["in"] == base["in"] + 6200 and p["out"] == base["out"] + 380, p
    assert p["cached"] == base["cached"] + 5000 and abs(p["cost"] - base["cost"] - 0.0125) < 1e-9 and p["cost_complete"] is False
    assert p["command_turns"] >= base["command_turns"] + 1 and p["turns"] == p["model_turns"] + p["command_turns"]
    assert s["conversation"] == {"turns": 2, "in": 6200, "out": 380, "cached": 5000, "cost": 0.0125, "tools": 0}, s["conversation"]
    assert s["by_model"]["test-large"]["in"] == 5000 and s["by_model"]["test-small"]["turns"] >= 1, s["by_model"]
    assert s["by_account"][acct]["turns"] == 2 and s["today"]["in"] >= 6200 and s["last"]["turn"] == r2["turn"]
    assert [r["turn"] for r in s["turns"]][-3:] == [r1["turn"], r2["turn"], recs[2]["turn"]] and Path(s["file"]) == f
    assert "model turn(s)" in s["line"] and "cached" in s["line"], s["line"]
    text = h.text_of(conforming(h.run_turn("/usage")))
    assert "Project:" in text and "test-large: 1 turn(s), 5,000 in / 300 out (4,000 cached), cost 0.0125" in text, text
    assert "no cost" in text
    try:
        h.request("usage.get", {"limit": -1})
        raise AssertionError("limit -1 should be refused")
    except RpcFailure as e:
        assert e.code == "INVALID_PARAMS"
    # a second agent on the same project (the board editor's) sees the same totals
    h2 = FakeHost(None, workdir("second"), env=ENV).start()
    try:
        h2.hello(project_dir=str(h.live), frame="board")
        s2 = h2.request("usage.get", {})
        assert s2["project"]["in"] == p["in"] and s2["project"]["model_turns"] == p["model_turns"], s2["project"]
    finally:
        assert h2.close() == 0


def test_usage_counts_cancelled_and_failed_turns_and_survives_a_torn_file():
    d = workdir("usage")
    d.mkdir(parents=True)

    def turn(tid, kind="model", status="ok", u=None, **req):
        return SimpleNamespace(id=tid, kind=kind, status=status, started=time.monotonic() - 1.5, _calls=2, _usage=u,
                               command={"name": "verify", "args": {}} if kind == "command" else None,
                               request=SimpleNamespace(conversation=req.get("conversation", "c1"), mode="chat", model="m1",
                                                       text=req.get("text", "hi"),
                                                       account=req.get("account", {"id": "a1", "kind": "k"})))
    assert usage.record_turn(d, turn("t-1", u={"in": 100, "out": 10, "cached": 0, "cost": 0.5}))["ms"] >= 1500
    usage.record_turn(d, turn("t-2", status="cancelled", u={"in": 40, "out": 0, "cached": 0, "cost": 0.25}))
    usage.record_turn(d, turn("t-3", status="error"))                                   # failed before any usage
    usage.record_turn(d, turn("t-4", kind="command"))
    usage.record_turn(d, turn("t-5", text="/verify drc", account={"id": "none", "kind": "none"}))   # typed, no account
    with open(usage.path(d), "ab") as f:
        f.write(b'{"ts": 1, "turn": "t-torn", "kind": "mod')                            # a crash mid-line
    recs = usage.read(d)
    assert [r["turn"] for r in recs] == ["t-1", "t-2", "t-3", "t-4", "t-5"], recs
    assert recs[4]["kind"] == "command" and recs[4]["command"] == "verify" and "account" not in recs[4], recs[4]
    assert recs[3]["command"] == "verify" and recs[1]["status"] == "cancelled" and recs[2]["in"] == 0
    s = usage.summary(d, "c1")
    assert s["project"] == {"turns": 5, "in": 140, "out": 10, "cached": 0, "cost": 0.75, "tools": 6, "model_turns": 3,
                            "command_turns": 2, "cost_complete": True}, s["project"]
    assert s["conversation"]["turns"] == 3 and s["by_model"]["m1"]["in"] == 140 and s["last"]["turn"] == "t-3"
    assert usage.line(s).startswith("Project: 3 model turn(s), 140 in / 10 out, 0.75 cost; 2 command(s) without a model"), usage.line(s)
    assert usage.line(usage.summary(workdir("empty"))) == "No usage recorded yet."
    assert usage.record_turn(d / "x" / ("y" * 300) / "\0", turn("t-9")) is None, "a failing ledger never raises"


# ── the end ───────────────────────────────────────────────────────────────
def test_closing_the_app_mid_job_restores_the_board_before_the_agent_exits():
    h = FakeHost(source_project(), workdir("eof"), env=ENV).start()
    h.hello()
    b = board(h)
    original = b.read_bytes()
    turn = h.command("route", {"reroute": True})
    h.wait_event(turn, "tool_start", 60)
    wait_until(lambda: any("writing" in (e.get("message") or "") for e in h.events.get(turn, [])), 300)
    time.sleep(0.3)
    assert h.close(40) == 0, "stdin EOF: the agent ends the turn and exits 0"
    evs = h.events[turn]
    assert evs[-1]["type"] == "done" and evs[-1]["status"] == "error", types(evs)
    assert b.read_bytes() == original, "the agent put the board back before it exited"
    assert not list((h.live / ".copper" / "jobs").glob("*")), "no half-made checkpoint is left"
    assert not h.garbage, h.garbage


def test_a_killed_agent_takes_its_job_with_it_and_the_next_agent_rolls_the_board_back():
    from copper_kicad.host.rendezvous import pid_alive
    h = FakeHost(source_project(), workdir("kill"), env=ENV).start()
    h.hello()
    b = board(h)
    original = b.read_bytes()
    turn = h.command("route", {"reroute": True})
    h.wait_event(turn, "tool_start", 60)
    time.sleep(0.7)
    kids = children(h.p.pid)
    assert kids, "the job runs in a child process of the agent"
    h.kill()                                                  # the agent dies without a chance to clean up
    if sys.platform == "win32":
        assert wait_until(lambda: not any(pid_alive(k) for k in kids), 10), "the system ends the job with the agent"
    else:
        NOTES.append("killed agent: orphaned job processes are only ended by the system on Windows")
    evs = h.events[turn]
    assert evs[-1]["type"] == "done" and evs[-1].get("synthetic"), "the app synthesises the end of the turn"
    left = jobs.checkpoints(h.live, states=("open",))
    assert len(left) == 1 and left[0]["pid"] == h.p.pid, left
    b.write_bytes(original + b"\n; half a routing job\n")     # what a job killed mid-way may leave behind
    h2 = FakeHost(None, workdir("kill2"), env=ENV)
    h2.live = h.live
    h2.start()
    try:
        h2.hello(frame="board")
        evs = conforming(h2.run_command("undo-checkpoint", timeout=60))
        assert of(evs, "tool_end")[0]["error"]["code"] == "NO_CHECKPOINT", evs    # nothing to undo: nothing was done
        assert b.read_bytes() == original, "the orphaned checkpoint was put back"
        assert jobs.checkpoints(h.live, states=None) == []
    finally:
        assert h2.close() == 0


def test_the_source_project_was_never_written_and_the_run_was_clean():
    assert tree_sig(source_project()) == _STATE["src_sig"], "tests work on copies only"
    h = H()
    out = h.save_transcript(TMP / "transcript-commands.jsonl")
    assert not h.garbage, h.garbage
    for turn, evs in h.events.items():
        conforming(evs)
    assert h.close(20) == 0
    assert not any("Traceback" in line for line in h.stderr), [line for line in h.stderr if "Traceback" in line][:3]
    home = TMP / "home"
    for f in list(home.rglob("*")) + list((h.live / ".copper").rglob("*.jsonl")):
        if f.is_file():
            assert SECRET.encode() not in f.read_bytes(), f"{f} holds the secret in clear"
    NOTES.append(f"transcript: {out} ({len(h.transcript)} messages, {len(h.events)} turns)")


def _main() -> int:
    only = sys.argv[1:]
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
             and (not only or any(o in n for o in only))]
    failed = skipped = 0
    for n, f in tests:
        t0 = time.time()
        try:
            f()
            print(f"PASS {n} ({time.time() - t0:.1f}s)", flush=True)
        except Skip as e:
            skipped += 1
            print(f"SKIP {n}: {e}", flush=True)
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}\n{traceback.format_exc()}", flush=True)
            h = _STATE.get("host")
            if h is not None and h.stderr:
                print("agent log tail:\n" + "".join(h.stderr[-12:]), flush=True)
    h = _STATE.get("host")
    if h is not None:
        h.close(5)
    if "srv" in _STATE:
        _STATE["srv"].shutdown()
    for note in NOTES:
        print(f"NOTE {note}")
    if not failed and not os.environ.get("COPPER_TEST_KEEP") and not _BASE:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed - skipped}/{len(tests) - skipped} passed" + (f" ({skipped} skipped)" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
