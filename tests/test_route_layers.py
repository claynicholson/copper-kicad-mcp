"""Inner layers for signals (pcb_route(layer_policy='auto'), pcb_pour(region=...)) and the guided finisher's
targeted rip-up (finish_ripup).

Run:  "C:/Program Files/KiCad/10.0/bin/python.exe" tests/test_route_layers.py
The selection logic is pure Python. The pour / rip-up tests need KiCad 10 and a copy of a routed 4-layer board:
COPPER_TEST_ROUTED_BOARD (default C:/t/speed/foc-orig, only ever copied, never modified). The Freerouting end-to-end
run (several minutes) also needs Java + Freerouting already installed and COPPER_TEST_SLOW=1. Missing -> skipped.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-test-home-"))
from copper_kicad import project as projmod  # noqa: E402
from copper_kicad.mcp import catalogue  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.pcb import board, route, worker  # noqa: E402
import copper_kicad.tools.pcb_tools as T  # noqa: E402
import copper_kicad.tools.route_tools  # noqa: E402,F401
import copper_kicad.tools.flow_tools  # noqa: E402,F401

SRC_BOARD = Path(os.environ.get("COPPER_TEST_ROUTED_BOARD", r"C:\t\speed\foc-orig"))
PLANE_NET = "/VBUS_M"           # the In2.Cu power pour of the FOC board
_TMP = Path(tempfile.mkdtemp(prefix="copper-test-route-layers-"))


class Skip(Exception):
    pass


def _copy(name: str) -> projmod.Project:
    if not list(SRC_BOARD.glob("*.kicad_pcb")):
        raise Skip(f"no routed board at {SRC_BOARD}")
    dst = _TMP / name
    shutil.copytree(SRC_BOARD, dst)
    return projmod.resolve(str(dst))


def _pcbnew():
    try:
        import pcbnew
        return pcbnew
    except ImportError:
        raise Skip("pcbnew not importable (run with KiCad's Python)") from None


def _zone_bbox(pcb: Path, net: str, layer: str):
    P = _pcbnew()
    b = P.LoadBoard(str(pcb))
    for z in b.Zones():
        if not z.GetIsRuleArea() and z.GetNetname() == net and b.GetLayerName(z.GetLayer()) == layer:
            bb = z.GetBoundingBox()
            return [bb.GetLeft() / 1e6, bb.GetTop() / 1e6, bb.GetRight() / 1e6, bb.GetBottom() / 1e6]
    return None


# ── pure ──────────────────────────────────────────────────────────────────
def test_openable_layers_never_gnd():
    zones = {"In1.Cu": [{"net": "GND"}], "In2.Cu": [{"net": "/VBUS_M"}]}
    assert route._openable_layers(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], ["F.Cu", "B.Cu"], zones) == ["In2.Cu"]
    # a layer with any ground pour stays closed; so does an unnamed pour; an empty inner layer opens
    zones = {"In1.Cu": [{"net": "+5V"}, {"net": "AGND"}], "In2.Cu": [{"net": ""}]}
    assert route._openable_layers(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], ["F.Cu", "B.Cu"], zones) == []
    assert route._openable_layers(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], ["F.Cu", "B.Cu"], {}) == ["In1.Cu", "In2.Cu"]
    # already a signal layer: nothing to open
    assert route._openable_layers(["F.Cu", "In1.Cu", "B.Cu"], ["F.Cu", "In1.Cu", "B.Cu"], {}) == []
    assert route._shrink_margins(1.0) == [1.0, 3.0, 6.0]


def test_layer_policy_validated_before_anything():
    class _P:
        pcb = Path("does-not-exist.kicad_pcb")
    try:
        route.autoroute(_P(), layer_policy="everything")
        raise AssertionError("no error")
    except ToolError as e:
        assert e.code == "BAD_ARGUMENT" and "layer_policy" in e.message, e.message


def test_ripup_nets_selection():
    real = worker.run
    worker.run = lambda kind, args, **k: {"results": [{
        "by_pin": {"U1.3": ["/A", "/B", "GND", "+3V3"], "U1.4": ["/B", "/C", "/FROZEN"]},
        "nets": ["/A", "/B", "/C", "+3V3", "GND", "/FROZEN"],
        "pad_count": {"/A": 2, "/B": 3, "/C": 2, "+3V3": 40, "GND": 80, "/FROZEN": 2}}]}
    try:
        class _P:
            pcb = Path("x.kicad_pcb")
        failed = [{"net": "/SIG", "from": "U1.3", "to": "R5.1", "where": [1, 2], "blocked_by": ["/C"]},
                  {"net": "/SIG2", "from": "U1.4", "to": "pour"}]
        rip = route._ripup_nets(_P(), failed, keep_out={"/FROZEN"})
        # failed nets first, then blockers by weight (blocked_by counts double); no ground, no big rail, no frozen net
        assert rip[:2] == ["/SIG", "/SIG2"], rip
        assert rip[2] == "/C" and set(rip) == {"/SIG", "/SIG2", "/A", "/B", "/C"}, rip
        assert route._ripup_nets(_P(), failed, keep_out=set(), limit=3) == ["/SIG", "/SIG2", "/C"]
        # a failed net that is a pour / frozen net is never ripped up itself
        assert "/SIG" not in route._ripup_nets(_P(), failed, keep_out={"/SIG"})
        # v2 names a net-less blocker by its tag ('track', 'via'): never handed to the rip-up as a net
        worker.run = lambda kind, args, **k: {"results": [{
            "by_pin": {"U1.3": ["/A"]}, "nets": ["/A"], "pad_count": {"/A": 2},
            "board_nets": ["/A", "/C", "/SIG", "GND"]}]}
        failed = [{"net": "/SIG", "from": "U1.3", "to": "R5.1", "blocked_by": ["track", "via", "/C"]}]
        assert route._ripup_nets(_P(), failed, keep_out=set()) == ["/SIG", "/C", "/A"]
    finally:
        worker.run = real


def test_catalogue_signatures_current():
    st = {k: v for k, v in catalogue.stale().items() if k in ("pcb_route", "pcb_pour")}
    assert not st, st


# ── KiCad: pours limited to a region ──────────────────────────────────────
def test_pour_region_auto_rect_refs():
    pr = _copy("pour")
    d = T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], render=False).data   # whole board
    assert d["unrouted"] == 0 and d["verify"]["drc"]["errors"] == 0, d
    full = _zone_bbox(pr.pcb, PLANE_NET, "In2.Cu")
    ol = board.read(pr, what=["summary"])["outline"]["bbox"]
    assert full[2] - full[0] > 0.95 * (ol[2] - ol[0]), (full, ol)
    # 'auto' on a planes-only board (no other net's copper on In2): drop every In2 track of other nets first
    P = _pcbnew()
    b = P.LoadBoard(str(pr.pcb))
    for t in list(b.GetTracks()):
        if t.GetClass() == "PCB_TRACK" and b.GetLayerName(t.GetLayer()) == "In2.Cu" and t.GetNetname() != PLANE_NET:
            b.Delete(t)
    b.Save(str(pr.pcb))
    unc0 = route._board_stats(pr.pcb)["unconnected_by_net"].get(PLANE_NET, 0)
    d = T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], region="auto", render=False).data
    auto = _zone_bbox(pr.pcb, PLANE_NET, "In2.Cu")
    assert (auto[2] - auto[0]) * (auto[3] - auto[1]) < 0.9 * (full[2] - full[0]) * (full[3] - full[1]), (auto, full)
    assert route._board_stats(pr.pcb)["unconnected_by_net"].get(PLANE_NET, 0) <= unc0      # still joins its net
    assert d["verify"]["drc"]["errors"] == 0, d["verify"]
    # a rectangle (as a JSON string too, as some clients send it) and a hull of parts
    T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], region="[110, 105, 150, 140]", render=False)
    bb = _zone_bbox(pr.pcb, PLANE_NET, "In2.Cu")
    assert [round(v, 2) for v in bb] == [110, 105, 150, 140], bb
    T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], refs=["Q1", "Q3"], region_margin=0.5, render=False)
    bb = _zone_bbox(pr.pcb, PLANE_NET, "In2.Cu")
    assert 120 < bb[0] and bb[2] < 140 and bb[3] - bb[1] < 25, bb
    try:
        T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], region=[1, 2, 3], render=False)
        raise AssertionError("bad region accepted")
    except ToolError as e:
        assert e.code == "BAD_ARGUMENT", e.code
    # zone_region: never touches a multi-layer zone, keeps a zone it would not shrink
    r = board.run_ops(pr, [{"op": "zone_region", "net": PLANE_NET, "layer": "In2.Cu", "region": [100, 100, 184, 162],
                            "min_shrink": 0.95}], label="t")["results"][0]
    assert r["zones"] == 0 and r["skipped"], r


def test_escape_nets_reads_neighbours():
    pr = _copy("esc")
    e = worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "escape_nets", "pins": ["U1.10", "U1.20"],
                                                         "radius": 1.0}], "save": False})["results"][0]
    assert e["by_pin"].get("U1.10") and e["by_pin"].get("U1.20"), e
    P = _pcbnew()
    b = P.LoadBoard(str(pr.pcb))
    own = {pd.GetNetname() for pd in b.FindFootprintByReference("U1").Pads() if pd.GetNumber() == "10"}
    assert own and not own & set(e["by_pin"]["U1.10"]), (own, e)      # a pin's own net is not in its way
    assert set(e["pad_count"]) == set(e["nets"]) and all(isinstance(v, int) for v in e["pad_count"].values()), e


SES_ONE_VIA = """(session board1
  (base_design board1)
  (routes
    (resolution um 10)
    (parser (host_cad "KiCad's Pcbnew") (host_version "10.0.6"))
    (library_out
      (padstack "Via[0-3]_600:300_um"
        (shape (circle F.Cu 6000 0 0)) (shape (circle In1.Cu 6000 0 0))
        (shape (circle In2.Cu 6000 0 0)) (shape (circle B.Cu 6000 0 0))
        (attach off)))
    (network_out
      (net "/PH_C"
        (via "Via[0-3]_600:300_um" 1296141 -1313912)))))
"""


def test_import_ses_gives_a_fallback_via_its_padstack_drill():
    # a Power-class net (class via 0.8/0.4) that Freerouting gave the 0.6/0.3 signal via came back from KiCad's
    # importer as 0.6/0.4 (the class drill): annular 0.1 mm < 0.13 mm, one DRC error per via
    pr = _copy("ses")
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "all_tracks": True}], "save": True})
    ses = pr.dir / "one.ses"
    ses.write_text(SES_ONE_VIA, encoding="utf-8")
    r = worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "import_ses", "path": str(ses), "check": False}],
                             "save": True})["results"][0]
    assert r["via_drills_fixed"] == 1, r
    vias = [t for t in board.read(pr, what=["tracks"])["track_list"] if t["type"] == "via"]
    # (on save KiCad re-nets a via that touches nothing but a plane: the net may read GND here)
    assert len(vias) == 1, vias
    assert vias[0]["size"] == 0.6 and vias[0]["drill"] == 0.3, vias[0]


# ── KiCad: the finisher's targeted rip-up ─────────────────────────────────
def test_finisher_ripup_keeps_only_improvements():
    pr = _copy("rip")
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "nets": ["/TEMP"]}], "save": True})
    by = route._board_stats(pr.pcb)["unconnected_by_net"]
    unc0 = sum(by.values())
    assert by.get("/TEMP"), by
    pads = [n for n in board.read(pr, what=["nets"])["nets"] if n["name"] == "/TEMP"][0]["nodes"]
    failed = [{"net": "/TEMP", "from": pads[0], "to": pads[1], "reason": "test"}]
    rev, raw = projmod.revision(pr.pcb), pr.pcb.read_bytes()
    zone_nets = {z["net"] for z in board.read(pr, what=["zones"])["zones"]}
    # (an engine test: keep_out is only the pour nets here. autoroute also keeps out every net that had copper
    # before the call, see test_read_copper_nets_and_finisher_ripup_spares_routed_nets)
    # 1. a rip-up that makes things worse (it also deletes another net) is thrown away: the file is byte-identical
    real = route._ripup_guided
    calls = []

    def worse(pr_, sig, budget, while_open, rip):
        calls.append(list(rip))
        worker.run("board", {"pcb": str(pr_.pcb), "ops": [{"op": "delete", "nets": ["/CANH"]}], "save": True})
        return {}
    route._ripup_guided = worse
    try:
        entry, _ = route.finisher_ripup(pr, failed, zone_nets, ["F.Cu", "B.Cu"], unc0, set(), 30)
    finally:
        route._ripup_guided = real
    assert calls and calls[0][0] == "/TEMP" and "GND" not in calls[0], calls
    assert entry.get("discarded") == "no gain" and not entry.get("kept"), entry
    assert pr.pcb.read_bytes() == raw and projmod.revision(pr.pcb) == rev
    # 2. the real guided rip-up: kept only with fewer unrouted and no new DRC error, else the board is unchanged
    err0 = board.drc(pr, parity=False, record=False)["errors"]
    t0 = time.time()
    entry, stats = route.finisher_ripup(pr, failed, zone_nets, ["F.Cu", "In2.Cu", "B.Cu"], unc0, set(), 60)
    secs = time.time() - t0
    if entry.get("kept"):
        assert entry["unrouted"] < unc0, entry
        assert board.drc(pr, parity=False, record=False)["errors"] <= err0
        assert sum(stats["unconnected_by_net"].values()) == entry["unrouted"]
    else:
        assert pr.pcb.read_bytes() == raw, entry
    print(f"    finisher rip-up of {entry.get('nets')}: {entry} in {secs:.0f} s")


# ── review fixes: the rip-up never touches pre-existing copper; an ambiguous padstack drill is left alone ──
def test_ripup_keep_out_protects_preexisting_copper():
    ko = route._ripup_keep_out({"/IGN"}, {"/FROZ"}, {"GND"}, None, ["/A", "/B", "/NEW"], {"/A", "/B"})
    assert {"/IGN", "/FROZ", "GND", "/A", "/B"} <= ko and "/NEW" not in ko, ko
    # an ECO: every net outside `only` is kept out too, and a pre-routed net inside it as well
    ko = route._ripup_keep_out(set(), set(), set(), {"/NEW", "/B"}, ["/A", "/B", "/NEW", "/X"], {"/B"})
    assert ko == {"/A", "/X", "/B"}, ko
    # the selection: a pre-routed blocker of a failed connection is never ripped up
    real = worker.run
    worker.run = lambda kind, args, **k: {"results": [{"by_pin": {"U1.3": ["/A", "/NEW2"]}, "nets": ["/A", "/NEW2"],
                                                        "pad_count": {"/A": 2, "/NEW2": 2}}]}
    try:
        class _P:
            pcb = Path("x.kicad_pcb")
        rip = route._ripup_nets(_P(), [{"net": "/NEW", "from": "U1.3", "to": "R1.1", "blocked_by": ["/A"]}],
                                keep_out=route._ripup_keep_out(set(), set(), set(), None, [], {"/A"}))
        assert rip == ["/NEW", "/NEW2"], rip
    finally:
        worker.run = real


def test_read_copper_nets_and_finisher_ripup_spares_routed_nets():
    pr = _copy("cn")
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "nets": ["/TEMP"]}], "save": True})
    cn = set(board.read(pr, what=["summary", "copper_nets"])["copper_nets"])
    assert "/TEMP" not in cn and "/ENC_MOSI" in cn and len(cn) > 20, sorted(cn)[:10]
    # the reviewer's case: closing one /TEMP connection used to rip 9 user-routed nets. With the nets that
    # had copper before the call kept out, only /TEMP itself is a candidate
    pads = [n for n in board.read(pr, what=["nets"])["nets"] if n["name"] == "/TEMP"][0]["nodes"]
    failed = [{"net": "/TEMP", "from": pads[0], "to": pads[1], "reason": "test"}]
    zone_nets = {z["net"] for z in board.read(pr, what=["zones"])["zones"]}
    ko = route._ripup_keep_out(set(), set(), zone_nets, None, [], cn)
    rip = route._ripup_nets(pr, failed, ko)
    assert rip == ["/TEMP"], rip


SES_TWO_PADSTACKS = SES_ONE_VIA.replace(
    "        (attach off)))\n    (network_out",
    "        (attach off))\n      (padstack \"Via[0-3]_600:400_um\"\n"
    "        (shape (circle F.Cu 6000 0 0)) (shape (circle B.Cu 6000 0 0))\n"
    "        (attach off)))\n    (network_out")


def test_import_ses_ambiguous_padstack_drill_left_alone():
    # two padstacks with one diameter (600:300 and 600:400, two net classes): a 0.6/0.4 via is legitimate and the
    # importer cannot tell which padstack a via came from, so it is never cut down to 0.3
    assert SES_TWO_PADSTACKS != SES_ONE_VIA
    pr = _copy("ses2")
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "all_tracks": True}], "save": True})
    ses = pr.dir / "two.ses"
    ses.write_text(SES_TWO_PADSTACKS, encoding="utf-8")
    r = worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "import_ses", "path": str(ses), "check": False}],
                             "save": True})["results"][0]
    assert not r.get("via_drills_fixed"), r
    vias = [t for t in board.read(pr, what=["tracks"])["track_list"] if t["type"] == "via"]
    assert len(vias) == 1 and vias[0]["drill"] == 0.4, vias


# ── review fixes: layers round vs. its own pour nets; the report after a discarded round (mocked engine) ──
def test_split_nets_pure():
    zones = {"In2.Cu": [{"net": "+5V"}, {"net": ""}], "In1.Cu": [{"net": "GND"}]}
    assert route._pour_watch(["In2.Cu"], zones) == {"+5V"}
    a = {"unconnected_by_net": {"/A": 2}}
    b = {"unconnected_by_net": {"/A": 0, "+5V": 1}}
    assert route._split_nets(a, b, {"+5V"}) == ["+5V"] and route._split_nets(b, a, {"+5V"}) == []
    assert route._split_nets({}, b, {"+5V"}) == []        # no per-net counts: cannot tell


def _fake_autoroute(shrink_by_net, layers_by_net, ignore=("+5V",)):
    """autoroute(layer_policy='auto') with Freerouting, the pcbnew worker and DRC replaced by stubs: round 1 leaves
    /A open (2), the shrink of the In2 +5V plane gives ``shrink_by_net``, the layers round ``layers_by_net``."""
    d = Path(tempfile.mkdtemp(prefix="copper-fake-route-", dir=_TMP))
    (d / "b.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")

    class _P:
        pcb = d / "b.kicad_pcb"
        copper_dir = d
    zones = [{"net": "GND"}, {"net": "+5V"}]
    inner = {"In1.Cu": [{"net": "GND", "single_layer": True, "frac": 1.0}],
             "In2.Cu": [{"net": "+5V", "single_layer": True, "frac": 1.0}]}
    imports = [{"/A": 2}, layers_by_net]

    def run(kind, args, **k):
        names = [o["op"] for o in args["ops"]]
        if "export_dsn" in names:
            Path(args["ops"][-1]["path"]).write_text("(pcb)", encoding="utf-8")
            return {"results": [{"nets": ["/A", "+5V", "GND"], "copper_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                                 "planes": {"In1.Cu": "GND", "In2.Cu": "+5V"}, "inner_zones": inner}],
                    "after": {"tracks": 3, "unconnected_by_net": {"/A": 4}}}
        if "zone_region" in names:
            return {"results": [{"zones": 1, "layer": "In2.Cu", "net": "+5V", "area_mm2": 10, "polygon": []}, {}],
                    "after": {"tracks": 3, "unconnected_by_net": dict(shrink_by_net)}}
        if "import_ses" in names:
            return {"results": [{}], "after": {"tracks": 5, "unconnected_by_net": dict(imports.pop(0))}}
        raise AssertionError(names)
    fake = {(worker, "run"): run,
            (board, "drc"): lambda pr, **k: {"errors": 0, "unconnected": 4},
            (board, "read"): lambda pr, **k: {"zones": zones, "copper_nets": ["/A"], "tracks": 3},
            (projmod, "guard_writable"): lambda *a, **k: None,
            (projmod, "checkpoint"): lambda *a, **k: "cp",
            (projmod, "revision"): lambda *a, **k: "r",
            (route, "_patch_dsn"): lambda *a, **k: 0, (route, "_wiring_to_keepouts"): lambda *a, **k: 0,
            (route, "_add_keepouts"): lambda *a, **k: 0, (route, "_edge_keepouts"): lambda *a, **k: 0,
            (route, "_patch_rules"): lambda dsn, clearance=None, neck=None, power_layers=None:
                {"clearance": None, "necked": {}, "plane_layers": list(power_layers or [])},
            (route.freerouting, "find_java"): lambda: "java", (route.freerouting, "ensure_java"): lambda *a: None,
            (route.freerouting, "ensure_jar"): lambda *a: None,
            (route.freerouting, "jar_path"): lambda: d,
            (route.freerouting, "run"): lambda *a, **k: {"ses_written": True, "seconds": 1.0, "timed_out": False,
                                                         "passes_done": 1, "returncode": 0, "log_tail": []}}
    real = {key: getattr(*key) for key in fake}
    try:
        for (mod, name), fn in fake.items():
            setattr(mod, name, fn)
        return route.autoroute(_P(), ignore_nets=list(ignore), layer_policy="auto", rounds=1, finish=False,
                               fanout=False, preflight=False, timeout=600)
    finally:
        for (mod, name), fn in real.items():
            setattr(mod, name, fn)


def test_layers_round_never_splits_an_ignored_pour():
    # the shrunk +5V hull is cut in two (a +5V connection opens): every margin fails, the round never runs
    r = _fake_autoroute({"/A": 2, "+5V": 1}, {"/A": 0})
    lay = [x for x in r["rounds"] if x.get("strategy") == "open_layers"][0]
    assert "shrinking" in lay.get("discarded", "") and not lay["kept"] and not r["planes_opened"], lay
    # the layers round routes /A but splits the (ignored) +5V pour: discarded, the planes-only board stands
    r = _fake_autoroute({"/A": 2}, {"/A": 0, "+5V": 1})
    lay = [x for x in r["rounds"] if x.get("strategy") == "open_layers"][0]
    assert "split the pour of +5V" in lay.get("discarded", "") and not lay["kept"], lay
    assert not r["planes_opened"] and r["unrouted_by_net"] == {"/A": 2}, r
    # a clean round is kept
    r = _fake_autoroute({"/A": 2}, {"/A": 0})
    assert r["planes_opened"] and r["unrouted_by_net"] == {"/A": 0}, r
    assert r["signal_layers"] == ["F.Cu", "In2.Cu", "B.Cu"], r["signal_layers"]


def test_discarded_layers_round_reports_the_kept_board():
    r = _fake_autoroute({"/A": 2}, {"/A": 2})          # no gain: discarded
    lay = [x for x in r["rounds"] if x.get("strategy") == "open_layers"][0]
    assert lay.get("discarded") == "no gain" and not lay["kept"], lay
    assert r["signal_layers"] == ["F.Cu", "B.Cu"], r["signal_layers"]
    assert r["plane_layers"] == ["In1.Cu", "In2.Cu"], r["plane_layers"]


# ── review fixes: pcb_pour outlines; ECO sync of a footprint re-linked by reference ──
def _add_track(b, P, net, layer, x0, y0, x1, y1):
    t = P.PCB_TRACK(b)
    t.SetStart(P.VECTOR2I(P.FromMM(x0), P.FromMM(y0)))
    t.SetEnd(P.VECTOR2I(P.FromMM(x1), P.FromMM(y1)))
    t.SetWidth(P.FromMM(0.2))
    t.SetLayer(layer)
    t.SetNet(b.FindNet(net))
    b.Add(t)
    return t


def test_pour_auto_hull_per_layer():
    pr = _copy("hull")
    P = _pcbnew()
    net = "/TEMP"
    ol = board.read(pr, what=["summary"])["outline"]["bbox"]
    b = P.LoadBoard(str(pr.pcb))
    pads = [pd.GetPosition() for fp in b.GetFootprints() for pd in fp.Pads() if pd.GetNetname() == net]
    cx, cy = sum(p.x for p in pads) / len(pads) / 1e6, sum(p.y for p in pads) / len(pads) / 1e6
    # a B.Cu track of the net in the board corner farthest from its pads
    x = ol[0] + 3 if cx > (ol[0] + ol[2]) / 2 else ol[2] - 4
    y = ol[1] + 3 if cy > (ol[1] + ol[3]) / 2 else ol[3] - 3
    _add_track(b, P, net, P.B_Cu, x, y, x + 1, y)
    b.Save(str(pr.pcb))
    del b
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "zone", "net": net, "layers": ["F.Cu", "B.Cu"],
                                                     "region": "auto", "margin": 0.5}], "save": True})
    fb, bb = _zone_bbox(pr.pcb, net, "F.Cu"), _zone_bbox(pr.pcb, net, "B.Cu")
    assert bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3], (bb, x, y)        # the B.Cu pour reaches its B.Cu copper
    assert not (fb[0] <= x <= fb[2] and fb[1] <= y <= fb[3]), (fb, x, y)  # the F.Cu hull is its own


def test_pour_polygon_validated_before_the_old_pour_goes():
    pr = _copy("poly")
    T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], render=False)
    full = _zone_bbox(pr.pcb, PLANE_NET, "In2.Cu")
    ol = board.read(pr, what=["summary"])["outline"]["bbox"]
    for bad in ([[0, 0], [10, 0], [20, 0]],                                              # collinear
                [[ol[2] + 10, ol[3] + 10], [ol[2] + 20, ol[3] + 10], [ol[2] + 20, ol[3] + 20]]):   # off the board
        try:
            T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], region=bad, render=False)
            raise AssertionError(f"bad polygon {bad} accepted")
        except ToolError as e:
            assert e.code == "BAD_ARGUMENT", (e.code, e.message)
        assert _zone_bbox(pr.pcb, PLANE_NET, "In2.Cu") == full      # the existing plane is still there
    # a good polygon is used as given
    poly = [[ol[0] + 5, ol[1] + 5], [ol[0] + 30, ol[1] + 5], [ol[0] + 5, ol[1] + 30]]
    T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], region=poly, render=False)
    bb = _zone_bbox(pr.pcb, PLANE_NET, "In2.Cu")
    assert [round(v, 2) for v in bb] == [round(ol[0] + 5, 2), round(ol[1] + 5, 2), round(ol[0] + 30, 2),
                                         round(ol[1] + 30, 2)], bb


def _sync_args(pcb: Path, new_path: dict):
    """op_sync components / nets that reproduce the board as it is, with the paths in ``new_path`` (ref -> path)."""
    P = _pcbnew()
    b = P.LoadBoard(str(pcb))
    comps, nets = [], {}
    for fp in b.GetFootprints():
        if fp.IsBoardOnly():
            continue
        ref = fp.GetReference()
        comps.append({"ref": ref, "value": fp.GetValue(), "footprint": str(fp.GetFPID().GetUniStringLibId()),
                      "path": new_path.get(ref, fp.GetPath().AsString()), "dnp": bool(fp.IsDNP()),
                      "exclude_bom": bool(fp.IsExcludedFromBOM())})
        for pd in fp.Pads():
            if pd.GetNumber() and pd.GetNetCode() > 0:
                nets.setdefault(pd.GetNetname(), []).append([ref, pd.GetNumber()])
    return comps, [{"name": n, "nodes": v} for n, v in nets.items()]


def test_eco_sync_relink_by_reference_keeps_copper():
    pr = _copy("relink")
    P = _pcbnew()
    b = P.LoadBoard(str(pr.pcb))
    zone_nets = {z.GetNetname() for z in b.Zones()}
    fp = next(f for f in b.GetFootprints() if f.GetReference().startswith("R") and f.GetPath().AsString()
              and len(f.Pads()) == 2 and all(pd.GetNetCode() > 0 and pd.GetNetname() not in zone_nets for pd in f.Pads()))
    ref, net = fp.GetReference(), fp.Pads()[0].GetNetname()
    ol = b.GetBoardEdgesBoundingBox()
    x, y = ol.GetLeft() / 1e6 + 2, ol.GetTop() / 1e6 + 2
    stub = _add_track(b, P, net, P.F_Cu, x, y, x + 1, y)     # a deliberate stub far away, hanging from nothing
    stub_id = stub.m_Uuid.AsString()
    old_path = fp.GetPath().AsString()
    b.Save(str(pr.pcb))
    del b
    # the symbol was deleted and re-added with the same reference: a new UUID, so the footprint is re-linked by ref
    new_path = old_path.rsplit("/", 1)[0] + "/" + "0badc0de-0000-4000-8000-000000000001"
    comps, nets = _sync_args(pr.pcb, {ref: new_path})
    r = worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "sync", "components": comps, "nets": nets,
                                                         "eco": True}], "save": True})["results"][0]
    eco = r["eco"]
    assert not eco["nets_lost_pads"] and net not in eco["nets_gained_pads"], eco
    assert eco["pruned"] == 0 and eco["tracks"] == 0 and eco["vias"] == 0, eco
    b = P.LoadBoard(str(pr.pcb))
    assert any(t.m_Uuid.AsString() == stub_id for t in b.GetTracks()), "the stub was pruned"
    assert b.FindFootprintByReference(ref).GetPath().AsString() == new_path


# ── Freerouting end to end (slow) ─────────────────────────────────────────
MCU_NETS = ["/SWCLK", "/NRST", "/ENC_SCK", "/ENC_CS", "/UART_RX", "/USB_DP", "/USB_DM", "/CANH", "/CANL", "/TEMP"]


def test_layer_policy_auto_end_to_end():
    from copper_kicad.pcb import freerouting
    if os.environ.get("COPPER_TEST_SLOW") != "1":
        raise Skip("set COPPER_TEST_SLOW=1 (several minutes of Freerouting)")
    if not freerouting.status().get("ready"):
        raise Skip("Freerouting / Java not installed")
    pr = _copy("e2e")
    # the board as a planes-only flow would leave it: In2 one whole-board VBUS_M plane, no signal on In2 ...
    T.pcb_pour(project=str(pr.dir), net=PLANE_NET, layers=["In2.Cu"], render=False)
    P = _pcbnew()
    b = P.LoadBoard(str(pr.pcb))
    in2 = sorted({t.GetNetname() for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"
                  and b.GetLayerName(t.GetLayer()) == "In2.Cu" and t.GetNetname() != PLANE_NET})
    del b
    # ... and those nets plus a handful of MCU nets to route again
    rip = sorted(set(in2) | set(MCU_NETS))
    worker.run("board", {"pcb": str(pr.pcb), "ops": [{"op": "delete", "nets": rip}, {"op": "fill"}], "save": True})
    before = board.drc(pr, parity=False, record=False)
    t0 = time.time()
    d = T.pcb_route(project=str(pr.dir), layer_policy="auto", render=False, polish=False, timeout=1500).data
    secs = time.time() - t0
    after = d["verify"]["drc"]
    print(f"    {len(rip)} nets ripped, unrouted {before['unconnected']} -> {d['unrouted_after']} in {secs:.0f} s; "
          f"DRC errors {before['errors']} -> {after['errors']}")
    for r in d["rounds"]:
        print("     ", {k: v for k, v in r.items() if k != "opened"})
        for o in r.get("opened") or []:
            print("        opened", {k: v if k != "shrunk" else [{x: y for x, y in s.items() if x != "outline"} for s in v]
                                     for k, v in o.items()})
    assert after["errors"] <= before["errors"], after
    assert d["unrouted_after"] <= before["unconnected"]
    gnd = [o for o in (d.get("planes_opened") or []) if o["layer"] == "In1.Cu"]
    assert not gnd, d["planes_opened"]              # the GND plane is never opened


if __name__ == "__main__":
    fails = skips = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    only = sys.argv[1:]
    for name, fn in tests:
        if only and not any(o in name for o in only):
            continue
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
