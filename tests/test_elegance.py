"""Elegance tests: the score itself, auto-size, scored placement candidates, connector flush / overhang,
decoupling-cap proximity, silkscreen tidy, track polish and their safety rules.

Run:  python tests/test_elegance.py [test_name ...]      (KiCad's python; needs KiCad 9/10)
Set COPPER_TEST_NO_ROUTE=1 to skip the test that runs Freerouting.
"""
from __future__ import annotations

import copy
import json
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Copper's state (active project, recent list) must never land in the real ~/.copper: project.STATE_DIR is
# fixed when copper_kicad is first imported, so this has to come before that import.
__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad import project as projmod  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
import copper_kicad.tools.pcb_tools  # noqa: E402,F401 — registers the tools
import copper_kicad.tools.verify_tools  # noqa: E402,F401
from copper_kicad.pcb import board, elegant, place, setup_ext  # noqa: E402
from copper_kicad.review import elegance, silkcheck  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="copper-test-elegance-"))


def call(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    if r["isError"]:
        raise AssertionError(f"{name} failed: {r['content'][0]['text'][:900]}")
    return r.get("structuredContent") or {}, r


def call_err(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    assert r["isError"], f"{name} was expected to fail"
    return r["structuredContent"]


def _project(name):
    import test_pcb as T
    d = _TMP / name
    if d.exists():
        shutil.rmtree(d)
    T.build(d)
    call("pcb_sync", project=str(d), render=False)
    return str(d)


# ── synthetic snapshots (pure Python) ─────────────────────────────────────
def _fp(ref, x, y, w=2.0, h=1.0, rot=0, pads=None, footprint="Resistor_SMD:R_0603", **kw):
    pl = pads if pads is not None else [{"num": "1", "net": f"N{ref}a", "lx": -w / 4, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"},
                                        {"num": "2", "net": f"N{ref}b", "lx": w / 4, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"}]
    d = {"ref": ref, "value": "x", "footprint": footprint, "x": x, "y": y, "rot": rot, "side": "top",
         "lbbox": [-w / 2, -h / 2, w / 2, h / 2], "pad_list": pl}
    d.update(kw)
    return d


def _outline(w=60.0, h=30.0, x0=100.0, y0=100.0):
    return {"bbox": [x0, y0, x0 + w, y0 + h], "width": w, "height": h, "area": w * h, "closed": True,
            "polygon": [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]]}


def _usb(ref="J1", x=0.0, y=0.0, rot=0):
    """Side-entry connector: body 9 x 7.4 (fab), courtyard 0.25 larger, pads at the back (local +y), two NPTH pegs."""
    pads = [{"num": str(i + 1), "net": f"U{i}", "lx": -2.75 + 0.5 * i, "ly": 3.2, "w": 0.3, "h": 1.2, "type": "smd"} for i in range(12)]
    pads += [{"num": "S1", "net": "GND", "lx": -4.3, "ly": 2.6, "w": 1.0, "h": 2.0, "type": "tht"},
             {"num": "S2", "net": "GND", "lx": 4.3, "ly": 2.6, "w": 1.0, "h": 2.0, "type": "tht"},
             {"num": "", "net": "", "lx": -2.9, "ly": 1.6, "w": 0.65, "h": 0.65, "type": "npth"},
             {"num": "", "net": "", "lx": 2.9, "ly": 1.6, "w": 0.65, "h": 0.65, "type": "npth"}]
    return _fp(ref, x, y, w=10.0, h=8.4, rot=rot, pads=pads, footprint="Connector_USB:USB_C_Receptacle", lfab=[-4.5, -3.7, 4.5, 3.7])


def test_score_names_the_visible_defects():
    ol = _outline()
    # every part in the left two thirds; a side-entry connector 0.6 mm inside the left edge
    fps = [_fp(f"R{i}", 106 + 4 * (i % 8), 106 + 5 * (i // 8)) for i in range(24)]
    usb = _usb(x=100.6 + 3.7, y=118, rot=270)      # rot 270: local -y (the mouth) points left
    fps.append(usb)
    parts = [place.Part(d) for d in fps]
    M = elegance.placement_metrics(parts, ol)
    assert M["emptiest_third"] < 0.3, M["emptiest_third"]
    c = M["connectors"][0]
    assert c["kind"] == "side_entry" and c["edge"] == "left" and abs(c["body_gap"] - 0.6) < 0.01 and not c["ok"], c
    # the same connector flush with the edge is fine; a pin header end-on to an edge is not
    usb2 = dict(usb, x=100.0 + 3.7)
    hdr = _fp("J2", 130, 100.9 + 12.7, w=2.5, h=25.4, pads=[{"num": str(i), "net": f"H{i}", "lx": 0, "ly": -11.43 + 2.54 * i, "w": 1.7, "h": 1.7, "type": "tht"} for i in range(10)],
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x10")
    M2 = elegance.placement_metrics([place.Part(d) for d in fps[:-1] + [usb2, hdr]], ol)
    by = {c["ref"]: c for c in M2["connectors"]}
    assert by["J1"]["ok"] and by["J1"]["body_gap"] == 0.0, by["J1"]
    assert by["J2"]["kind"] == "vertical" and not by["J2"]["ok"] and by["J2"]["edge"] in ("left", "right"), by["J2"]
    # two overlapping references are both bad; so is one on a pad, under another part, off the board, tiny, or tilted
    extra = {"refs": [{"ref": "R1", "visible": True, "side": "top", "box": [10, 10, 12, 11], "h": 1.0, "angle": 0},
                      {"ref": "R2", "visible": True, "side": "top", "box": [11.5, 10.2, 13.5, 11.2], "h": 1.0, "angle": 0},
                      {"ref": "R3", "visible": True, "side": "top", "box": [20, 10, 22, 11], "h": 1.0, "angle": 0},
                      {"ref": "R4", "visible": True, "side": "top", "box": [30.5, 10.2, 31.5, 10.8], "h": 0.6, "angle": 0},
                      {"ref": "R5", "visible": True, "side": "top", "box": [40, 10, 42, 11], "h": 1.0, "angle": 45},
                      {"ref": "R6", "visible": True, "side": "top", "box": [59, 10, 61, 11], "h": 1.0, "angle": 90},
                      {"ref": "R7", "visible": True, "side": "top", "box": [50, 20, 52, 21], "h": 1.0, "angle": 0},
                      {"ref": "R8", "visible": True, "side": "top", "box": [5, 20, 7, 21], "h": 1.0, "angle": 90},
                      {"ref": "R9", "visible": False, "side": "top", "h": 1.0, "angle": 0},
                      {"ref": "H1", "visible": False, "side": "top", "h": 1.0, "angle": 0, "board_only": True}],
             "courts": {"U1": {"box": [29, 9, 33, 12], "side": "top"}, "R4": {"box": [28, 13, 29, 14], "side": "top"}},
             "pads": [[20.5, 10.2, 21.0, 10.8, 1, "R3"], [5.5, 20.2, 6.0, 20.8, 2, "R8"]],
             "silk": [["s", "U9", "top", 49, 20.5, 53, 20.5, 0.12], ["c", "U8", "top", 6, 20.5, 8, 0.12], ["s", "R8", "top", 5, 20.5, 7, 20.5, 0.1]],
             "vias": [], "edge": {"bbox": [0, 0, 60, 30], "polygon": [[0, 0], [60, 0], [60, 30], [0, 30]], "holes": []}}
    ev = silkcheck.evaluate(extra)
    assert ev["visible"] == 8 and ev["hidden"] == 1, ev
    assert ev["bad"] == {"R1": ["text"], "R2": ["text"], "R3": ["pad"], "R4": ["under_part", "small"], "R5": ["angle"],
                         "R6": ["edge"], "R7": ["silk"]}, ev["bad"]      # R8: bottom pad, own silk, inside a big circle: fine


def test_gates_ungated_hidden_silk_and_provenance_rules():
    ol = _outline(40, 30)
    fps = [_fp(f"R{i}", 104 + 4.5 * (i % 8), 104 + 5 * (i // 8)) for i in range(32)]
    parts = [place.Part(d) for d in fps]
    M = elegance.placement_metrics(parts, ol)
    M.update({"tracks": 0, "silk_refs": 32, "silk_hidden": 0, "silk_bad": {f"R{i}": ["text"] for i in range(6)},
              "courtyard_overlaps": 0, "src": "auto"})
    s0 = elegance.score(M, {"errors": 0, "unconnected": 0})
    assert not s0["gates"] and s0["score"] == s0["ungated"]
    assert s0["parts"]["economy"] is None and s0["parts"]["geometry"] is None, "an unrouted board is scored on placement only"
    # gates come from the DRC numbers verify uses, and never hide the trend
    s1 = elegance.score(M, {"errors": 3, "unconnected": 5})
    assert s1["gates"] == ["unrouted:5", "drc_errors:3"] and s1["score"] == min(49.0, s0["ungated"]) and s1["ungated"] == s0["ungated"], s1
    good = dict(M, emptiest_third=0.7, imbalance=0.02, utilisation=0.45, silk_bad={})
    g0, g1 = elegance.score(good, {"errors": 0, "unconnected": 0}), elegance.score(good, {"errors": 1, "unconnected": 0})
    assert g0["score"] > 90 and g1["score"] == 49.0 and g1["ungated"] == g0["score"] and g1["gates"] == ["drc_errors:1"], (g0, g1)
    assert elegance.score(M, None)["gates"] == [], "no DRC given: no DRC gate is invented"
    assert elegance.score(dict(M, courtyard_overlaps=2), None)["gates"] == ["courtyard_overlap:2"]
    # with a DRC result the bounding-box estimate is not a gate: a real overlap is a DRC error (KiCad demo boards
    # with rotated / two-sided parts were capped at 49 while DRC and verify said "clean")
    assert elegance.score(dict(M, courtyard_overlaps=2), {"errors": 0, "unconnected": 0})["gates"] == []
    # hiding every reference must not raise the score (B13)
    hidden = elegance.score(dict(M, silk_refs=0, silk_hidden=32, silk_bad={}), {"errors": 0, "unconnected": 0})
    assert hidden["ungated"] <= s0["ungated"] + 1e-9 and hidden["parts"]["silk"] == 0.0, (hidden["ungated"], s0["ungated"])
    none = elegance.score(dict(M, silk_refs=0, silk_hidden=0, silk_bad={}), {"errors": 0, "unconnected": 0})
    assert none["parts"]["silk"] == 0.5
    # the same parts on a much larger outline: a tool-sized board is told to resize, a file / user outline never is
    big = _outline(90, 48)
    Mb = elegance.placement_metrics([place.Part(d) for d in fps], big)
    Mb.update({"tracks": 0, "silk_refs": 32, "silk_hidden": 0, "silk_bad": {}, "courtyard_overlaps": 0})
    for src, expect in (("auto", True), ("file", False), ("user", False), (None, False)):
        sc = elegance.score(dict(Mb, src=src), {"errors": 0, "unconnected": 0})
        hint = any("auto_size" in (w["fix"] or "") for w in sc["worst"])
        assert hint == expect, (src, [w["fix"] for w in sc["worst"]])
        assert any(w["what"] == "emptiest_third" for w in sc["worst"]), "spread still counts on a fixed outline"
    # on a routed board no fix may propose an auto placement of everything without clear_tracks
    Mr = dict(Mb, src="auto", tracks=10, vias=0, bends={"straight": 0, "135": 1, "90": 6, "acute": 0, "other": 0},
              off45_len_frac=0.0, detour=1.3, vias_per_net=0.2, pad_entry_centred=1.0, planes=[], hs_plane_crossings=0)
    for w in elegance.score(Mr, {"errors": 0, "unconnected": 0})["worst"]:
        f = w["fix"] or ""
        assert "auto_size=true" not in f and ("pcb_place(auto=true)" not in f or "clear_tracks" in f), f
    b = elegance.brief(s1, 3)
    assert set(b) == {"score", "ungated", "gates", "worst"} and len(json.dumps(elegance.brief(s1, 0))) <= 120


def test_bitmap_keep_in_respects_arcs_and_cutouts():
    # a 40 x 30 board with a 10 mm corner cut off diagonally and a 10 x 6 cutout
    poly = [[0, 0], [30, 0], [40, 10], [40, 30], [0, 30]]
    hole = [[15, 12], [25, 12], [25, 18], [15, 18]]
    bm = elegant.Bitmap(poly, [hole], [0, 0, 40, 30], 0.5)
    assert bm.free((2, 2, 8, 6)) and bm.free((27, 20, 39, 29))
    assert not bm.free((34, 1, 39, 5)), "the cut-off corner"
    assert not bm.free((14, 11, 18, 14)) and not bm.free((16, 13, 24, 17)), "on / inside the cutout"
    assert not bm.free((0.1, 5, 3, 8)) and bm.free((0.5, 5, 3, 8)), "edge clearance"
    assert not bm.free((24.6, 14, 28, 16)) and bm.free((25.75, 14, 28, 16)), "clearance around the cutout"
    assert not bm.free((-1, 5, 3, 8)) and not bm.free((38, 20, 41, 25)), "outside the map"
    # the placer uses it: nothing lands on the cutout or the cut corner
    ol = {"bbox": [0, 0, 40, 30], "polygon": poly, "holes": [hole], "area": 1200 - 50 - 60, "closed": True}
    fps = [_fp(f"R{i}", -10, 5 + i, pads=[{"num": "1", "net": "A", "lx": -0.5, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"},
                                           {"num": "2", "net": f"B{i % 3}", "lx": 0.5, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"}]) for i in range(40)]
    res = elegant.auto_place({"footprints": fps, "outline": ol}, candidates=1)
    assert not res["unplaced"], res["unplaced"]
    free0 = elegant.Bitmap(poly, [hole], [0, 0, 40, 30], 0.0)
    by = {d["ref"]: place.Part(d) for d in fps}
    for m in res["moves"]:
        r = by[m["ref"]].rect(m["x"], m["y"], m["rot"])
        assert free0.free(r), (m, r)


def test_connector_flush_overhang_and_clamp():
    ol = _outline(40, 30)
    r1 = _fp("R1", 0, 0, pads=[{"num": "1", "net": "U0", "lx": -0.5, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"},
                               {"num": "2", "net": "X", "lx": 0.5, "ly": 0, "w": 0.6, "h": 0.6, "type": "smd"}])

    def run(**kw):
        parts = elegant._parts_of({"footprints": [_usb(x=-20, y=-20), copy.deepcopy(r1)]})
        pl = elegant.ElegantPlacer(parts, ol, **kw)
        pl.run()
        j = next(p for p in parts if p.ref == "J1")
        return j, pl
    j, pl = run()
    fab = elegant._box_of(j, j.d["lfab"])
    assert j.placed and getattr(j, "edge", None) == "left" and abs(fab[0] - 100.0) < 1e-6, (j.x, j.y, j.rot, fab)
    assert pl.notes["flush"] == ["J1"] and not pl.notes["clamped"]
    st = elegance.connector_state(j, ol["bbox"])
    assert st["ok"] and st["body_gap"] == 0.0, st
    # the courtyard is allowed past the edge, but only on that side
    assert j.rect()[0] < 100.0 and j.rect()[1] > 100.4
    # a modest overhang is honoured; pads / pegs keep their clearance
    j, pl = run(overhang={"J1": 1.0})
    fab = elegant._box_of(j, j.d["lfab"])
    assert abs(fab[0] - 99.0) < 1e-6 and not pl.notes["clamped"], (fab, pl.notes)
    # 5 mm is too much: the NPTH pegs (hole clearance) and shell pads (copper clearance) stop it, and it says so
    j, pl = run(overhang={"J1": 5.0}, copper_edge=0.3, hole_edge=0.3)
    fab = elegant._box_of(j, j.d["lfab"])
    assert "J1" in pl.notes["clamped"] and 0 < pl.notes["clamped"]["J1"] < 5.0, pl.notes
    for pd in j.pads:
        px, _ = j.pad_pos(pd)
        half = (pd["h"] if int(j.rot) % 180 == 90 else pd["w"]) / 2
        assert px - half >= 100.0 + 0.3 - 1e-6, (pd, px)
    assert 99.0 - 5.0 < fab[0] < 100.0
    # flush=False (and vertical headers) keep the old behaviour: courtyard inside the edge clearance
    j, pl = run(flush=False)
    assert j.rect()[0] >= 100.5 - 1e-6


def test_decap_rule_evicts_a_neighbour_or_reports():
    ol = _outline(30, 24)
    u = _fp("U1", 115, 112, w=6, h=6, footprint="Package_SO:SOIC-8",
            pads=[{"num": str(i + 1), "net": n, "lx": -2.7 if i < 4 else 2.7, "ly": -1.9 + 1.27 * (i % 4), "w": 1.5, "h": 0.6, "type": "smd",
                   "fn": f} for i, (n, f) in enumerate([("+3V3", "VDD"), ("A", "IO1"), ("B", "IO2"), ("GND", "VSS"), ("C", "IO3"), ("D", "IO4"),
                                                         ("E", "IO5"), ("F", "IO6")])], locked=True)
    cap = _fp("C1", 128, 120, w=3.0, h=1.5, footprint="Capacitor_SMD:C_0603", value="100nF",
              pads=[{"num": "1", "net": "+3V3", "lx": -0.8, "ly": 0, "w": 0.9, "h": 0.9, "type": "smd"},
                    {"num": "2", "net": "GND", "lx": 0.8, "ly": 0, "w": 0.9, "h": 0.9, "type": "smd"}])
    # a resistor already sits on the only good spot next to pin 1
    blk = _fp("R9", 109.5, 110.0, w=3.0, h=1.5, pads=[{"num": "1", "net": "A", "lx": -0.8, "ly": 0, "w": 0.9, "h": 0.9, "type": "smd"},
                                                    {"num": "2", "net": "Q", "lx": 0.8, "ly": 0, "w": 0.9, "h": 0.9, "type": "smd"}])
    parts = elegant._parts_of({"footprints": [u, cap, blk]})
    pl = elegant.ElegantPlacer(parts, ol)
    pl.run()
    c = next(p for p in parts if p.ref == "C1")
    assert c.role == "decap" and c.anchor[0].ref == "U1"
    assert pl._decap_dist(c) <= elegant.DECAP_MAX and not pl.notes["decap_far"], (pl._decap_dist(c), pl.notes)
    # start far away with every near spot taken by fixed parts: left where the search put it, and reported
    wall = [_fp(f"X{i}", x, y, w=3.4, h=3.4, pads=[], locked=True) for i, (x, y) in enumerate(
        [(108.2 + 3.4 * a, 106.4 + 3.4 * b) for a in range(-1, 5) for b in range(-1, 5) if not (1 <= a <= 2 and 1 <= b <= 2)])]
    parts = elegant._parts_of({"footprints": [u, copy.deepcopy(cap)] + wall})
    pl = elegant.ElegantPlacer(parts, ol)
    pl.run()
    c = next(p for p in parts if p.ref == "C1")
    if pl._decap_dist(c) > elegant.DECAP_MAX:
        assert pl.notes["decap_far"] and pl.notes["decap_far"][0][0] == "C1", pl.notes


def test_auto_size_is_analytic_bounded_and_can_fail():
    fps = [_fp(f"R{i}", -30 - i, 0, w=3.0, h=1.6, pads=[{"num": "1", "net": "A" if i % 2 else "B", "lx": -0.8, "ly": 0, "w": 0.9, "h": 0.9, "type": "smd"},
                                                        {"num": "2", "net": f"N{i // 2}", "lx": 0.8, "ly": 0, "w": 0.9, "h": 0.9, "type": "smd"}]) for i in range(30)]
    snap = {"footprints": fps, "outline": None}
    t = time.time()
    r = elegant.auto_size(snap)
    dt = time.time() - t
    area = sum(3.0 * 1.6 for _ in fps)
    assert "width" in r and dt < 28, (r, dt)        # the search has a 20 s budget, checked before each placement
    assert 0.33 <= area / (r["width"] * r["height"]) <= 0.56, (r, area / (r["width"] * r["height"]))
    assert r["width"] == int(r["width"]) and r["height"] == int(r["height"]) and r["width"] >= r["height"]
    assert 1 <= len(r["tried"]) <= 8 and all("fits" in x for x in r["tried"])
    # one side fixed by the caller: the other is found
    r2 = elegant.auto_size(snap, width=30.0)
    assert r2["width"] == 30.0 and r2["height"] >= area / 30.0
    # no time: nothing is guessed
    r3 = elegant.auto_size(snap, budget_s=0.0)
    assert r3.get("failed") and "width" not in r3
    # corner holes to be added are budgeted for, and a long connector gets an edge it fits on
    hdr = _fp("J1", -50, -50, w=26.4, h=3.5, pads=[{"num": str(i), "net": f"H{i}", "lx": -11.43 + 2.54 * i, "ly": 0, "w": 1.7, "h": 1.7, "type": "tht"} for i in range(10)],
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x10")
    r4 = elegant.auto_size({"footprints": fps[:10] + [hdr], "outline": None}, new_holes=3.2)
    assert r4["width"] >= 26.4 + 2 * (3.5 + 3.2) and r4["height"] >= 2 * 3.5 + 4, r4


def test_candidates_are_scored_and_the_best_is_kept():
    d = _project("cands")
    call("pcb_setup", project=d, width=44, height=34, holes="M3", render=False)
    pr = projmod.resolve(d)
    snap = board.read(pr, what=["geom"])
    r1 = elegant.auto_place(snap, candidates=1)
    assert len(r1["tried"]) == 1 and r1["variant"] == "baseline"
    r6 = elegant.auto_place(snap, candidates=6)
    assert len(r6["tried"]) >= 2 and not r6["unplaced"], r6["tried"]
    best = max(t["score"] - 25 * t.get("unplaced", 0) for t in r6["tried"])
    chosen = next(t for t in r6["tried"] if t["variant"] == r6["variant"])
    assert chosen["score"] >= best - 2.0, (chosen, r6["tried"])           # the wirelength tie-break may cost < 2 points
    assert r6["placement_score"] >= r1["placement_score"] - 1e-6
    again = elegant.auto_place(snap, candidates=6)
    assert again["moves"] == r6["moves"] and again["tried"] == r6["tried"], "placement must be deterministic"
    assert elegant.auto_place(snap, candidates=6, refs=["R1"])["tried"].__len__() == 1, "a partial placement has no variants"


def test_auto_size_place_score_and_safety_through_the_tools():
    d = _project("flow")
    pr = projmod.resolve(d)
    nets = lambda: [(n["name"], n["nodes"]) for n in call("pcb_read", project=d, what=["nets"])[0]["nets"]]
    nets0 = nets()
    t = time.time()
    data, _ = call("pcb_setup", project=d, auto_size=True, holes="M3", render=False)
    t_size = time.time() - t
    az = data["auto_size"]
    assert 0.3 <= az["utilisation"] <= 0.6 and az["seconds"] <= 8, az
    assert any("board size chosen for the parts" in c for c in data["changed"]) and any("4 mounting holes" in c for c in data["changed"])
    assert 'src=auto' in pr.pcb.read_text(encoding="utf-8")
    data, _ = call("pcb_place", project=d, auto=True, render=False)
    assert not [f for f in data["findings"] if f["code"] in ("DID_NOT_FIT", "COURTYARD_OVERLAP")], data["findings"]
    assert data["verify"]["courtyard_overlaps"] == 0 and data["verify"]["drc"]["errors"] == 0
    el = data["elegance"]
    assert el["score"] <= 49.0 and el["ungated"] >= 70 and any(g.startswith("unrouted:") for g in el["gates"]), el
    assert len(json.dumps(el)) <= 300, len(json.dumps(el))
    assert "placed=auto" in pr.pcb.read_text(encoding="utf-8")
    e = elegance.elegance(pr, metrics=True)
    M = e["metrics"]
    assert M["src"] == "auto" and M["placed_auto"] and M["outside"] == 0 and M["emptiest_third"] >= 0.4, (M["emptiest_third"], M["thirds_x"], M["thirds_y"])
    assert all(c["ok"] for c in M["connectors"]), M["connectors"]
    far = [x for x in M["decap_mm"] if x[2] > 4.0]
    assert not far or any(f["code"] == "DECAP_FAR" for f in data["findings"]), far
    assert M["silk_bad"] == {} and M["silk_hidden"] <= 0.15 * (M["silk_refs"] + M["silk_hidden"]), (M["silk_bad"], M["silk_hidden"])
    # determinism of the measurement (B3)
    assert json.dumps(elegance.measure(pr), sort_keys=True) == json.dumps(elegance.measure(pr), sort_keys=True)
    # verify and pcb_read carry it
    v, _ = call("verify", project=d, what=["drc"])
    assert v["elegance"]["score"] <= 49.0 and v["elegance"]["ungated"] == e["ungated"] and len(json.dumps(v["elegance"])) <= 900, v["elegance"]
    r, _ = call("pcb_read", project=d, what=["elegance"])
    assert r["elegance"]["metrics"]["utilisation"] == M["utilisation"] and "footprints" not in r
    # an explicit move makes the layout "not all Copper's own" again
    fp = next(f for f in call("pcb_read", project=d, what=["footprints"], refs=["R1"])[0]["footprints"])
    call("pcb_place", project=d, moves=[{"ref": "R1", "x": fp["x"], "y": fp["y"], "rot": (fp["rot"] + 180) % 360}], render=False)
    assert "placed=auto" not in pr.pcb.read_text(encoding="utf-8")
    assert nets() == nets0, "sizing and placing must not change the netlist"
    # a user-given size is recorded as such and is never judged for utilisation
    call("pcb_setup", project=d, width=70, height=50, render=False)
    e2 = elegance.elegance(pr, metrics=True)
    assert e2["metrics"]["src"] == "user" and not any("auto_size" in (w["fix"] or "") for w in e2["worst"])


def test_place_sizes_a_board_without_outline_and_refuses_when_unsafe():
    d = _project("implicit")
    pr = projmod.resolve(d)
    e = call_err("pcb_place", project=d, auto=True, dry_run=True)
    assert e["error"] == "NO_OUTLINE", e
    data, _ = call("pcb_place", project=d, auto=True, render=False)
    assert "sized" in data["changed"][0] and data["auto_size"]["width"] > 10, data["changed"][:1]
    snap = board.read(pr, what=["summary"])
    assert snap["outline"]["closed"] and snap["outline"]["src"] == "auto"
    assert not [f for f in data["findings"] if f["code"] == "DID_NOT_FIT"]
    # locked parts: auto_size is refused with nothing written
    call("pcb_place", project=d, moves=[{"ref": "U1", "lock": True}], render=False)
    before = pr.pcb.read_bytes()
    e = call_err("pcb_setup", project=d, auto_size=True)
    assert e["error"] == "BAD_ARGUMENT" and "locked" in e["message"], e
    assert pr.pcb.read_bytes() == before


def test_silk_tidy_fixes_bad_references_and_leaves_good_and_locked_ones():
    d = _project("silk")
    pr = projmod.resolve(d)
    call("pcb_setup", project=d, width=50, height=40, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    snap = board.read(pr, what=["footprints"])
    by = {f["ref"]: f for f in snap["footprints"]}
    # put R1's and R2's references on top of U1 (bad), leave everything else
    import pcbnew as P
    b = P.LoadBoard(str(pr.pcb))
    pos = {}
    for fp in b.GetFootprints():
        if fp.GetReference() in ("R1", "R2"):
            fp.Reference().SetPosition(P.VECTOR2I(int(by["U1"]["x"] * 1e6), int(by["U1"]["y"] * 1e6)))
        if fp.GetReference() == "C1":
            fp.Reference().SetLocked(True)
        t = fp.Reference()
        pos[fp.GetReference()] = (t.GetPosition().x, t.GetPosition().y, t.GetTextAngleDegrees())
    P.SaveBoard(str(pr.pcb), b)
    M = elegance.measure(pr)
    assert {"R1", "R2"} <= set(M["silk_bad"]), M["silk_bad"]
    res = board.run_ops(pr, [{"op": "silk_tidy2", "hide": "never"}], label="t")["results"][0]
    assert {"R1", "R2"} <= set(res["moved"]) and not res["bad_after"] and not res["hidden"], res
    assert elegance.measure(pr)["silk_bad"] == {}
    b = P.LoadBoard(str(pr.pcb))
    moved = []
    for fp in b.GetFootprints():
        t = fp.Reference()
        now = (t.GetPosition().x, t.GetPosition().y, t.GetTextAngleDegrees())
        if now != pos[fp.GetReference()]:
            moved.append(fp.GetReference())
            c = fp.GetPosition()
            assert math.hypot(t.GetPosition().x - c.x, t.GetPosition().y - c.y) / 1e6 <= 4.0 + 8.0, "a reference stays near its part"
    assert sorted(moved) == sorted(res["moved"]), (moved, res["moved"])      # readable and locked references did not move
    assert "C1" not in moved


def _draw(d, pts, net, width=0.25, layer="F.Cu"):
    data, _ = call("pcb_edit", project=d, ops=[{"op": "add_track", "points": pts, "width": width, "layer": layer, "net": net}])
    return data


def test_polish_merges_chamfers_and_never_touches_old_or_locked_tracks():
    d = _project("polish")
    pr = projmod.resolve(d)
    call("pcb_setup", project=d, width=60, height=50, render=False)
    call("pcb_place", project=d, auto=True, render=False)
    nets = [n["name"] for n in call("pcb_read", project=d, what=["nets"])[0]["nets"]]
    net = next(n for n in nets if "LED_A" in n)
    # free area along the bottom edge: an "old" track, then new ones with collinear pieces and right angles
    _draw(d, [[105, 146], [110, 146], [110, 143]], net)                       # old: one 90 degree corner
    before = setup_ext.track_uuids(pr)
    old = {t["uuid"]: (t["start"], t["end"], t["width"], t["layer"]) for t in board.read(pr, what=["tracks"])["track_list"]}
    _draw(d, [[120, 146], [123, 146], [126, 146], [126, 142], [130, 142]], net)   # new: 1 merge, 2 corners
    d0 = board.drc(pr, parity=False, record=False)
    res = setup_ext.polish(pr, before_uuids=before, drc_before=d0, silk=False)
    assert res.get("merged", 0) >= 1 and res.get("chamfered", 0) == 2 and not res.get("reverted"), res
    tl = board.read(pr, what=["tracks"])["track_list"]
    now = {t["uuid"]: (t["start"], t["end"], t["width"], t["layer"]) for t in tl}
    for u, v in old.items():
        assert now.get(u) == v, "a track that existed before the call was changed"
    M = elegance.measure(pr)
    assert M["bends"]["90"] == 1 and M["bends"]["135"] == 4 and M["off45_len_frac"] == 0.0, M["bends"]     # only the old corner is left
    assert res["drc"]["errors"] <= d0["errors"] and res["drc"]["unconnected"] <= d0["unconnected"]
    # mode='polish' is the explicit way to tidy older tracks too; a locked one still stays
    import pcbnew as P
    b = P.LoadBoard(str(pr.pcb))
    for t in b.GetTracks():
        if t.m_Uuid.AsString() in old:
            t.SetLocked(True)
    P.SaveBoard(str(pr.pcb), b)
    data, _ = call("pcb_route", project=d, mode="polish", render=False)
    assert elegance.measure(pr)["bends"]["90"] == 1 and data["verify"]["drc"]["errors"] <= d0["errors"]
    b = P.LoadBoard(str(pr.pcb))
    for t in b.GetTracks():
        t.SetLocked(False)
    P.SaveBoard(str(pr.pcb), b)
    data, _ = call("pcb_route", project=d, mode="polish", nets=[net.rsplit("/", 1)[-1]], render=False)
    assert elegance.measure(pr)["bends"]["90"] == 0 and "chamfered: 1" in data["changed"], data["changed"]
    assert data["elegance"]["score"] is not None
    # a corner hemmed in by another net's track is left alone rather than creating a clearance error
    other = next(n for n in nets if n != net and "GND" not in n and not n.startswith("unconnected"))
    _draw(d, [[140, 146], [144, 146], [144, 142]], net)
    _draw(d, [[141.5, 144.5], [143.1, 144.5]], other, width=0.2)
    _draw(d, [[143.1, 144.5], [143.1, 143.0]], other, width=0.2)
    d1 = board.drc(pr, parity=False, record=False)
    res = setup_ext.polish(pr, all_tracks=True, nets=[net], drc_before=d1, silk=False)
    assert not res.get("reverted") and res["drc"]["errors"] <= d1["errors"], res
    # a failing polish leaves the file byte-identical (B8)
    _draw(d, [[150, 146], [153, 146], [153, 143]], net)
    saved = pr.pcb.read_bytes()
    try:
        setup_ext.polish(pr, all_tracks=True, silk=False, _fail_after=True)
        raise AssertionError("the test hook must fail the worker")
    except registry.ToolError as e:
        assert e.code == "POLISH_FORCED_FAILURE"
    assert pr.pcb.read_bytes() == saved


def test_route_polishes_only_its_own_tracks_and_reports_elegance():
    if os.environ.get("COPPER_TEST_NO_ROUTE"):
        print("  (skipped: COPPER_TEST_NO_ROUTE)")
        return
    d = _project("route")
    pr = projmod.resolve(d)
    nets = lambda: [(n["name"], n["nodes"]) for n in call("pcb_read", project=d, what=["nets"])[0]["nets"]]
    nets0 = nets()
    call("pcb_setup", project=d, auto_size=True, holes="M3", render=False)
    call("pcb_place", project=d, auto=True, render=False)
    call("pcb_route", project=d, tracks=[{"from": "R1.2", "to": "D1.2", "style": "L"}], render=False)      # hand routing with a right angle
    old = {t["uuid"]: (t.get("start"), t.get("end"), t.get("at"), t.get("width"), t.get("layer"))
           for t in board.read(pr, what=["tracks"])["track_list"]}
    assert old
    data, _ = call("pcb_route", project=d, ignore_nets=["GND"], render=False)
    assert "polish" in data and not data["polish"].get("reverted") and not data["polish"].get("skipped"), data.get("polish")
    now = {t["uuid"]: (t.get("start"), t.get("end"), t.get("at"), t.get("width"), t.get("layer"))
           for t in board.read(pr, what=["tracks"])["track_list"]}
    for u, v in old.items():
        assert now.get(u) == v, "pcb_route changed a track that existed before the call"
    assert data["verify"]["drc"]["errors"] == 0, data["verify"]
    assert "elegance" in data and len(json.dumps(data["elegance"])) <= 700, len(json.dumps(data.get("elegance")))
    pour, _ = call("pcb_pour", project=d, net="GND", stitch=True, render=False)
    assert pour["verify"]["drc"]["errors"] == 0 and pour["unrouted"] == 0, pour["verify"]
    e = elegance.elegance(pr, metrics=True)
    M = e["metrics"]
    assert not e["gates"] and e["score"] == e["ungated"] >= 75, (e["score"], e["gates"], e["parts"])
    assert M["silk_bad"] == {} and M["bends"]["acute"] <= 1 and M["off45_len_frac"] <= 0.02, (M["silk_bad"], M["bends"], M["off45_len_frac"])
    assert nets() == nets0, "routing and polishing must not change the netlist"
    print(f"  demo board: elegance {e['score']} parts {e['parts']}")


if __name__ == "__main__":
    import traceback
    fails = 0
    only = sys.argv[1:]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t:.1f}s)")
            except Exception as e:  # noqa: BLE001
                fails += 1
                traceback.print_exc()
                print(f"FAIL {name}: {e}")
    print(f"temp dir: {_TMP}")
    sys.exit(1 if fails else 0)
