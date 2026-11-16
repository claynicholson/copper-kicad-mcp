"""Routing speed-ups: ECO-aware sync + re-route (pcb_sync(eco=true), pcb_route(nets=[...]), eco_rip), the
skipped Freerouting 'continue' round with per-round logs, and the routability preflight.

Run:  "C:/Program Files/KiCad/10.0/bin/python.exe" tests/test_route_speed.py
The preflight tests are pure Python. The ECO / route tests need KiCad 10 and a copy of a routed board: set
COPPER_TEST_ROUTED_BOARD to a project folder (default C:/t/speed/foc-orig, only ever copied, never modified);
the Freerouting e2e also needs Java + Freerouting already installed (it never downloads). Missing -> skipped.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-test-home-"))
from copper_kicad import project as projmod  # noqa: E402
from copper_kicad.mcp import catalogue  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.pcb import board, preflight, route, worker  # noqa: E402
from copper_kicad.pcb.guided import model  # noqa: E402
import copper_kicad.tools.pcb_tools as T  # noqa: E402
import copper_kicad.tools.route_tools  # noqa: E402,F401
import copper_kicad.tools.flow_tools  # noqa: E402,F401

SRC_BOARD = Path(os.environ.get("COPPER_TEST_ROUTED_BOARD", r"C:\t\speed\foc-orig"))
ECO_NETS = ["/ENC_MOSI", "/PWM_AH", "/CANL", "/TEMP"]
_TMP = Path(tempfile.mkdtemp(prefix="copper-test-route-speed-"))


class Skip(Exception):
    pass


def _copy(name: str) -> projmod.Project:
    if not (SRC_BOARD / "foc-g4.kicad_pcb").exists() and not list(SRC_BOARD.glob("*.kicad_pcb")):
        raise Skip(f"no routed board at {SRC_BOARD}")
    dst = _TMP / name
    shutil.copytree(SRC_BOARD, dst)
    return projmod.resolve(str(dst))


def _segments_by_net(pcb: Path) -> dict:
    out = {}
    txt = pcb.read_text(encoding="utf-8")
    for m in re.finditer(r"\((segment|via)\b(.*?)\n\t\)", txt, re.S):
        nm = re.search(r'\(net "([^"]*)"\)', m.group(2))
        se = re.search(r"\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)", m.group(2))
        if se and abs(float(se[1]) - float(se[3])) + abs(float(se[2]) - float(se[4])) < 0.005:
            continue   # a zero-length sliver (the import clean-up drops these, as it always has)
        k = (nm.group(1) if nm else "", m.group(1))
        out[k] = out.get(k, 0) + 1
    return out


def _dups_by_net(pcb: Path) -> dict:
    """{(net, kind): n} exact duplicate segments / vias already on the board (the SES import drops these)."""
    import pcbnew
    b = pcbnew.LoadBoard(str(pcb))
    seen, out = set(), {}
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            k, kind = (p.x, p.y, t.GetNetCode(), "v"), "via"
        else:
            s_, e_ = t.GetStart(), t.GetEnd()
            if abs(s_.x - e_.x) + abs(s_.y - e_.y) < 5000:
                continue
            k, kind = (tuple(sorted([(s_.x, s_.y), (e_.x, e_.y)])), t.GetLayer(), t.GetNetCode()), "segment"
        if k in seen:
            out[(t.GetNetname(), kind)] = out.get((t.GetNetname(), kind), 0) + 1
        seen.add(k)
    return out


# ── preflight (pure) ──────────────────────────────────────────────────────
def _synth_board(walls):
    from test_guided_route import synth
    ex = synth([("U1", "N1", 5, 5), ("U2", "N1", 15, 5), ("U3", "N2", 15, 10), ("U4", "N2", 10, 12)], walls=walls)
    return model.Board(json.loads(json.dumps(ex)))


RING = [[4.2, 4.2, 5.8, 4.5], [4.2, 5.5, 5.8, 5.8], [4.2, 4.2, 4.5, 5.8], [5.5, 4.2, 5.8, 5.8]]


def test_preflight_walled_pad_is_proved():
    r = preflight.check_board(_synth_board(RING))
    assert not r["ok"] and r["walled_total"] == 1, r
    w = r["walled"][0]
    assert w["pad"] == "U1.1" and w["net"] == "N1" and w["at"] == [5, 5], w
    assert r["suggested_moves"] and "U1" in r["suggested_moves"][0]["try"], r["suggested_moves"]
    assert r["seconds"] < 5


def test_preflight_gap_in_wall_passes():
    r = preflight.check_board(_synth_board(RING[:3]))        # one side open
    assert r["ok"] and r["walled_total"] == 0, r
    r = preflight.check_board(_synth_board(RING), ignore=["N1"])   # a net left for a pour is not checked
    assert r["ok"], r
    r = preflight.check_board(_synth_board(RING), nets=["N2"])     # nets=[...] limits the check
    assert r["ok"] and r["nets_checked"] == 1, r


def test_catalogue_signatures_current():
    st = {k: v for k, v in catalogue.stale().items() if k in ("pcb_route", "pcb_sync")}
    assert not st, st


# ── KiCad: ECO on a copy of a routed board ────────────────────────────────
def test_preflight_no_false_fail_on_routed_board():
    pr = _copy("pf")
    r = preflight.check(pr)                       # fully routed: nothing to check
    assert r["ok"], r
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "nets": ECO_NETS}], "save": True})
    t0 = time.time()
    r = preflight.check(pr)
    assert r["ok"] and r["checked_pads"] > 0, r
    assert time.time() - t0 < 15, time.time() - t0
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "all_tracks": True}], "save": True})
    r = preflight.check(pr)                       # its final placement is routable: no pad may be called walled in
    assert r["ok"], r["walled"]


def test_sync_eco_unchanged_schematic_rips_nothing():
    pr = _copy("eco0")
    before = _segments_by_net(pr.pcb)
    d = T.pcb_sync(project=str(pr.dir), eco=True, render=False).data
    e = d["eco"]
    assert e["tracks_removed"] == 0 and e["vias_removed"] == 0 and not e["nets_lost_pads"], e
    assert _segments_by_net(pr.pcb) == before
    assert e["next"] == "nothing to re-route", e


def test_sync_eco_moved_pin_rips_only_that_branch():
    pr = _copy("eco1")
    nl = board.netlist(pr)
    net = next(n for n in nl["nets"] if n["name"] == "/TEMP")
    moved = net["nodes"][-1]
    # schematic change: one pin of /TEMP goes to a new net (same as re-wiring it in the schematic)
    net["nodes"] = [x for x in net["nodes"] if x != moved]
    nl["nets"].append({"name": "/TEMP_ECO", "nodes": [moved], "pins": []})
    before = _segments_by_net(pr.pcb)
    res = board.run_ops(pr, [{"op": "sync", "components": nl["components"], "nets": nl["nets"],
                              "libs": board.fp_libs(pr), "update_footprints": False, "eco": True}], label="test-eco")
    e = res["results"][0]["eco"]
    assert e["nets_lost_pads"] == {"/TEMP": 1}, e
    assert "/TEMP_ECO" in e["nets_gained_pads"], e
    assert set(e["deleted"]) == {"/TEMP"}, e["deleted"]
    assert e["tracks"] > 0, e
    after = _segments_by_net(pr.pcb)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert {k[0] for k in changed} == {"/TEMP"}, changed          # nothing else was touched
    assert after.get(("/TEMP", "segment"), 0) < before[("/TEMP", "segment")]
    by = route._board_stats(pr.pcb)["unconnected_by_net"]
    assert set(k for k, v in by.items() if v) <= {"/TEMP", "/TEMP_ECO"}, by


def test_eco_rip_refs_and_pcb_edit_warning():
    pr = _copy("eco2")
    fps = board.read(pr, what=["footprints"])["footprints"]
    # a DRC-clean board: no part collides with anything, nothing is ripped (only a stub that already ended in
    # nothing on one of those parts' nets may be pruned: this board has one, at J5.B5)
    r = board.run_ops(pr, [{"op": "eco_rip", "refs": [f["ref"] for f in fps]}], label="test-eco-rip")["results"][0]
    assert r["tracks"] + r["vias"] == r["pruned"] <= 2, r
    # move a 2-pad resistor (not locked) so its pads sit on a long horizontal F.Cu track of another net
    txt = pr.pcb.read_text(encoding="utf-8")
    seg = None
    for m in re.finditer(r"\(segment\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)\s*"
                         r"\(width ([\d.]+)\)\s*\(layer \"F.Cu\"\)\s*\(net \"([^\"]+)\"\)", txt):
        x1, y1, x2, y2 = map(float, m.groups()[:4])
        if abs(y1 - y2) < 1e-6 and abs(x2 - x1) > 4 and m.group(6) != "GND":
            seg = (x1, y1, x2, y2, m.group(6))
            break
    assert seg, "no long horizontal F.Cu track found"
    res = next(f for f in fps if re.fullmatch(r"R\d+", f["ref"]) and not f.get("locked") and f.get("side", "top") == "top")
    board.run_ops(pr, [{"op": "place", "items": [{"ref": res["ref"], "x": (seg[0] + seg[2]) / 2, "y": seg[1], "rot": 0}]}],
                  label="test-move")
    r = board.run_ops(pr, [{"op": "eco_rip", "refs": [res["ref"]]}], label="test-eco-rip")["results"][0]
    assert seg[4] in r["deleted"], (seg, r)
    assert r["tracks"] > 0, r
    d = T.pcb_edit(project=str(pr.dir), ops=[{"op": "delete", "all_tracks": True}]).data
    codes = [f["code"] for f in d["findings"]]
    assert codes[0] == "ALL_TRACKS_DELETED", codes
    assert "eco=true" in d["findings"][0]["fix"]
    d = T.pcb_edit(project=str(pr.dir), ops=[{"op": "delete", "nets": ["/TEMP"]}]).data   # nothing left: no warning
    assert "ALL_TRACKS_DELETED" not in [f["code"] for f in d["findings"]]


def test_preflight_failure_stops_before_writing():
    pr = _copy("pff")
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "nets": ["/TEMP"]}], "save": True})
    rev = projmod.revision(pr.pcb)
    real = route._preflight
    route._preflight = lambda *a, **k: {"ok": False, "walled_total": 1, "seconds": 0.1, "complete": True,
                                        "walled": [{"pad": "U1.30", "net": "/TEMP", "at": [1, 2]}],
                                        "hot_spots": [], "over_capacity": [],
                                        "suggested_moves": [{"why": "x", "try": "pcb_place(moves=[...])"}]}
    try:
        try:
            route.autoroute(pr, nets=["/TEMP"])
            raise AssertionError("no preflight failure")
        except ToolError as e:
            assert e.code == "ROUTE_PREFLIGHT_FAILED", e.code
            assert e.data["preflight"]["walled"][0]["pad"] == "U1.30"
            assert "force=true" in e.fix
        assert projmod.revision(pr.pcb) == rev
    finally:
        route._preflight = real


# ── review fixes ──────────────────────────────────────────────────────────
def test_board_build_routes_past_a_walled_pad():
    from copper_kicad.tools.flow_tools import route_past_preflight
    calls = []

    def call(**kw):
        calls.append(kw)
        if not kw.get("force"):
            raise ToolError("routing preflight: 1 pad(s) are walled in", code="ROUTE_PREFLIGHT_FAILED",
                            fix="pcb_place(moves=[...]); pcb_route(force=true) routes anyway",
                            data={"preflight": {"walled": [{"pad": "U1.30", "net": "/TEMP", "at": [1, 2]}]}})
        return {"unrouted_before": 9, "unrouted_after": 1}
    notes, pads = [], []
    r = route_past_preflight(call, 100, lambda: 80, notes, pads)
    assert r["unrouted_after"] == 1 and calls == [{"timeout": 100}, {"timeout": 80, "force": True}], calls
    assert pads == ["U1.30"] and notes and "walled" in notes[0], (pads, notes)
    # any other error still propagates, without a retry
    calls.clear()

    def bad(**kw):
        calls.append(kw)
        raise ToolError("x", code="ROUTE_FAILED")
    try:
        route_past_preflight(bad, 100, lambda: 80, [], [])
        raise AssertionError("no error")
    except ToolError as e:
        assert e.code == "ROUTE_FAILED" and len(calls) == 1


def test_nets_all_ignored_is_refused():
    pr = _copy("ign")
    rev = projmod.revision(pr.pcb)
    try:
        route.autoroute(pr, nets=["/TEMP"], ignore_nets=["/TEMP"])
        raise AssertionError("no error")
    except ToolError as e:
        assert e.code == "BAD_ARGUMENT", e.code
    assert projmod.revision(pr.pcb) == rev


class _Stop(Exception):
    pass


def test_clear_existing_with_nets_deletes_only_those_nets():
    """pcb_route(nets=[X], clear_existing=true) used to delete every net's routing (delete all_tracks)."""
    from copper_kicad.pcb import freerouting
    pr = _copy("clr")
    raw = pr.pcb.read_bytes()
    seen = []
    real_run = route.worker.run
    saved = {k: getattr(freerouting, k) for k in ("find_java", "ensure_java", "ensure_jar", "jar_path")}

    def spy(kind, args, **kw):
        ops = args.get("ops") or []
        if any(o.get("op") == "export_dsn" for o in ops):
            seen.append(ops)
            raise _Stop()            # stop before anything is written: only the planned ops matter here
        return real_run(kind, args, **kw)
    route.worker.run = spy
    freerouting.find_java = lambda: Path("java")
    freerouting.ensure_java = lambda progress=None: Path("java")
    freerouting.ensure_jar = lambda progress=None: Path("fr.jar")
    freerouting.jar_path = lambda: Path(__file__)
    try:
        for nets, want in ((["/TEMP"], {"op": "delete", "nets": ["/TEMP"]}), (None, {"op": "delete", "all_tracks": True})):
            seen.clear()
            try:
                route.autoroute(pr, nets=nets, clear_existing=True, preflight=False)
            except _Stop:
                pass
            assert seen, "export never reached"
            dels = [o for o in seen[0] if o.get("op") == "delete"]
            assert dels == [want], (nets, dels)
    finally:
        route.worker.run = real_run
        for k, v in saved.items():
            setattr(freerouting, k, v)
    assert pr.pcb.read_bytes() == raw      # the untouched board was put back


def test_eco_route_nothing_to_route_summary_names_the_nets():
    pr = _copy("ntr")
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "nets": ["/TEMP"]}], "save": True})
    res = T.pcb_route(project=str(pr.dir), nets=["/ENC_MOSI"], render=False)
    assert res.data.get("nothing_to_route"), res.data
    assert "0 unrouted connections" not in res.summary and "named nets are complete" in res.summary, res.summary


def _lock_net(pcb: Path, net: str) -> int:
    import pcbnew
    b = pcbnew.LoadBoard(str(pcb))
    n = 0
    for t in b.GetTracks():
        if t.GetNetname() == net:
            t.SetLocked(True)
            n += 1
    pcbnew.SaveBoard(str(pcb), b)
    return n


def test_eco_rip_keeps_locked_copper():
    pr = _copy("lock")
    n = _lock_net(pr.pcb, "/ENC_MOSI")
    assert n > 0
    before = _segments_by_net(pr.pcb)
    r = board.run_ops(pr, [{"op": "eco_rip", "nets": ["/ENC_MOSI"]}], label="test-eco-rip")["results"][0]
    assert r.get("locked_kept") == n and not r["deleted"].get("/ENC_MOSI"), r
    after = _segments_by_net(pr.pcb)
    assert {k: v for k, v in after.items() if k[0] == "/ENC_MOSI"} == \
        {k: v for k, v in before.items() if k[0] == "/ENC_MOSI"}


def test_eco_prune_only_where_copper_was_removed():
    # this board has one stub that ends in nothing (at J5.B5, see test_eco_rip_refs_and_pcb_edit_warning).
    # Pruning that net when nothing near the stub was ripped up must leave the stub alone.
    pr = _copy("prune")
    r = board.run_ops(pr, [{"op": "eco_rip", "refs": ["J5"]}], label="test-eco-rip")["results"][0]
    stub_nets = sorted(r["deleted"])
    if not stub_nets:
        raise Skip("no dangling stub at J5 on this board")
    pr2 = _copy("prune2")
    before = _segments_by_net(pr2.pcb)
    r2 = board.run_ops(pr2, [{"op": "eco_rip", "prune_nets": stub_nets}], label="test-eco-rip")["results"][0]
    assert r2["pruned"] == 0 and not r2["deleted"], r2
    assert _segments_by_net(pr2.pcb) == before


def test_eco_route_only_named_nets_and_round_skip():
    from copper_kicad.pcb import freerouting
    if not freerouting.status().get("ready"):
        raise Skip("Freerouting / Java not installed")
    pr = _copy("route")
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "nets": ECO_NETS + ["/CANH"]}], "save": True})
    before = _segments_by_net(pr.pcb)
    dups = _dups_by_net(pr.pcb)
    t0 = time.time()
    d = T.pcb_route(project=str(pr.dir), nets=ECO_NETS, render=False, polish=False).data
    secs = time.time() - t0
    assert d["nets_routed"] == sorted(ECO_NETS) and d["nets_frozen"] >= 1, d   # /CANH stays open + frozen
    by = d.get("unrouted_nets") or {}
    assert not any(by.get(n) for n in ECO_NETS), by
    assert by.get("/CANH"), by
    after = _segments_by_net(pr.pcb)
    # no other net's copper is removed: each keeps exactly its segments / vias, less only its OWN exact duplicates
    # (dropped by the import). Only pour nets may gain a little (the judge re-joins a pour pad a new track cut off
    # from its pour with a stub + via): at most 2 vias per ECO net, and no more stubs than vias
    pour_nets = {z.get("net") for z in board.read(pr, what=["zones"]).get("zones", [])}
    gained = {}
    for (net, kind), n in before.items():
        if net in ECO_NETS:
            continue
        got, dn = after.get((net, kind), 0), dups.get((net, kind), 0)
        if net in pour_nets and got > n:
            gained[(net, kind)] = got - n
            continue
        assert n - dn <= got <= n, (net, kind, n, got, dn)
    for (net, kind), got in after.items():     # no new copper on a net that had none of that kind
        if net not in ECO_NETS and (net, kind) not in before:
            assert net in pour_nets, (net, kind, got)
            gained[(net, kind)] = got
    # what a pour net gains is stub + via pairs (one per re-joined pad; 5 pairs on GND for these 4 nets)
    g_via = sum(v for (n_, k), v in gained.items() if k == "via")
    g_seg = sum(v for (n_, k), v in gained.items() if k == "segment")
    assert g_via <= 2 * len(ECO_NETS) and g_seg <= g_via, gained
    r1 = d["rounds"][0]
    assert r1["log"] == "freerouting-round1.log" and (pr.copper_dir / "freerouting-round1.log").exists()
    assert (pr.copper_dir / "freerouting.log").exists()
    if r1["unrouted"] and r1["unrouted"] < route.CONTINUE_MIN:
        assert any(r.get("strategy") == "continue" and "skipped" in r for r in d["rounds"]), d["rounds"]
    assert d["preflight"]["ok"] is True, d["preflight"]
    print(f"    eco route of {len(ECO_NETS)} nets: {secs:.0f} s, rounds {d['rounds']}")


if __name__ == "__main__":
    fails = skips = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f} s)")
        except Skip as e:
            skips += 1
            print(f"SKIP {name}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {type(e).__name__}: {str(e)[:400]}")
    print(f"{len(tests) - fails - skips} passed, {fails} failed, {skips} skipped")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
