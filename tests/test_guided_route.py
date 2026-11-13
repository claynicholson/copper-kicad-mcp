"""Guided autorouter tests: exact geometry, occupancy maps, the rip-up router on synthetic
boards (pure Python, no KiCad), plan validation, and the two MCP tools end-to-end on a real
board built with pcbnew and judged by kicad-cli DRC.

Run:  python -m pytest tests/test_guided_route.py      or      python tests/test_guided_route.py
The end-to-end tests need KiCad 9/10 (kicad-cli + KiCad's Python); the pure tests run anywhere.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Copper's state (active project, recent list) must never land in the real ~/.copper: project.STATE_DIR is
# fixed when copper_kicad is first imported, so this has to come before that import.
__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad import project as projmod  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp import registry  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
import copper_kicad.tools.route_tools  # noqa: E402,F401 — registers the tools
from copper_kicad.pcb import worker  # noqa: E402
from copper_kicad.pcb.guided import geom, model, plan as planmod, router  # noqa: E402
from copper_kicad.pcb.guided.grid import BLOCK  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="copper-test-guided-"))


# ── synthetic boards (no KiCad) ───────────────────────────────────────────
def synth(pads, size=(20.0, 15.0), layers=("F.Cu", "B.Cu"), walls=(), tracks=()):
    """pads: [(ref, net, x, y)] or [(ref, net, x, y, 'tht')] — 0.8 mm square SMD pads on F.Cu (or THT, all layers).
    walls: [[x0, y0, x1, y1]] no-track/no-via rule areas on every layer."""
    nets = sorted({p[1] for p in pads if p[1]})
    ex = {
        "layers": list(layers),
        "rules": {"clearance": 0.15, "track_width": 0.15, "via": 0.6, "drill": 0.3, "edge_clearance": 0.3,
                  "hole_clearance": 0.25, "hole_to_hole": 0.25, "annular": 0.1,
                  "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.2, "dp_width": 0.2}},
        "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                     "dp_gap": 0.2, "dp_width": 0.2} for i, n in enumerate(nets)},
        "outline": {"closed": True, "polys": [{"o": [[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]]}]},
        "pads": [], "footprints": [], "graphics": [], "tracks": list(tracks), "vias": [], "zones": [],
        "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0},
    }
    for i, p in enumerate(pads):
        ref, net, x, y = p[:4]
        tht = len(p) > 4 and p[4] == "tht"
        lays = list(range(len(layers))) if tht else [0]
        d = {"ref": ref, "num": "1", "net": net, "x": x, "y": y, "kind": "tht" if tht else "smd", "layers": lays,
             "shapes": [[lays, "box", [x, y, 1, 0, 0.4, 0.4, 0.1]]], "c": i + 1}
        if tht:
            d["hole"] = [x, y, 1, 0, 0.2, 0.2]
        ex["pads"].append(d)
    for w in walls:
        x0, y0, x1, y1 = w
        ex["zones"].append({"name": "wall", "layers": list(range(len(layers))), "rule_area": True, "no_tracks": True,
                            "no_vias": True, "polys": [{"o": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}]})
    return ex


def run_synth(ex, plan=None, mode="signals"):
    b = model.Board(json.loads(json.dumps(ex)))
    cfg = planmod.resolve(b, plan)
    b.make_grid(cfg.step)
    res = router.Router(b, cfg, mode=mode).run()
    return b, cfg, res


def assert_clean(b, res):
    """Independent pairwise check: copper of different nets keeps its clearance; nothing enters a keep-out."""
    tr = res["tracks"]
    lay = {n: i for i, n in enumerate(b.layers)}
    for i, a in enumerate(tr):
        for c in tr[i + 1:]:
            if a[0] == c[0] or a[1] != c[1]:
                continue
            d = geom.seg_seg_dist(a[2], a[3], a[4], a[5], c[2], c[3], c[4], c[5])
            assert d >= a[6] / 2 + c[6] / 2 + 0.2 - 1e-3, ("track/track clearance", a, c, d)
        nid = b.nets[a[0]].id
        for s in b.shapes:
            if lay[a[1]] not in s.layers:
                continue
            d = s.dist_seg(a[2], a[3], a[4], a[5])
            if s.cls == "cu" and s.net != nid:
                assert d >= a[6] / 2 + 0.2 - 1e-3, ("track/pad clearance", a, s.g, d)
            if s.cls == "kt":
                assert d >= a[6] / 2 - 1e-3, ("track in keep-out", a, d)
    for v in res["vias"]:
        nid = b.nets[v[0]].id
        for a in tr:
            if a[0] != v[0]:
                d = geom.pt_seg_dist(v[1], v[2], a[2], a[3], a[4], a[5])
                assert d >= v[3] / 2 + a[6] / 2 + 0.2 - 1e-3, ("via/track clearance", v, a, d)
        for s in b.shapes:
            if s.cls == "cu" and s.net != nid:
                assert s.dist_pt(v[1], v[2]) >= v[3] / 2 + 0.2 - 1e-3, ("via/pad clearance", v, s.g)


def connected(b, res, net):
    """Union-find over pads + new tracks/vias of a net: number of separate groups."""
    pts = [(p.x, p.y) for p in b.nets[net].pads]
    segs = [t for t in res["tracks"] if t[0] == net]
    nodes = list(pts)
    for t in segs:
        nodes += [(t[2], t[3]), (t[4], t[5])]
    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for k in range(len(segs)):
        parent[find(len(pts) + 2 * k)] = find(len(pts) + 2 * k + 1)
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if math.hypot(nodes[i][0] - nodes[j][0], nodes[i][1] - nodes[j][1]) < 0.45:   # inside a 0.8 mm pad / same point
                parent[find(i)] = find(j)
    # a track end landing on the middle of another track of the net (T junction)
    for i in range(len(pts), len(nodes)):
        for k, t in enumerate(segs):
            if geom.pt_seg_dist(nodes[i][0], nodes[i][1], t[2], t[3], t[4], t[5]) < 1e-3:
                parent[find(i)] = find(len(pts) + 2 * k)
    return len({find(i) for i in range(len(pts))})


# ── pure unit tests ───────────────────────────────────────────────────────
def test_geometry_distances():
    box = geom.box_from_pad([10, 10, 1, 0, 1.0, 0.5, 0.2])          # 2 x 1 round-rect at (10, 10)
    assert abs(geom.box_dist(box, 12, 10) - 1.0) < 1e-9
    assert abs(geom.box_dist(box, 10, 11) - 0.5) < 1e-9
    assert geom.box_dist(box, 10, 10) < 0
    # corner: inner rect corner (10.8, 10.3), radius 0.2
    assert abs(geom.box_dist(box, 11.8, 11.3) - (math.hypot(1.0, 1.0) - 0.2)) < 1e-9
    rot = geom.box_from_pad([0, 0, 0, -1, 1.0, 0.5, 0.0])           # rotated 90°: long axis along y
    assert abs(geom.box_dist(rot, 0, 2) - 1.0) < 1e-9 and abs(geom.box_dist(rot, 1.5, 0) - 1.0) < 1e-9
    assert abs(geom.seg_box_dist(12, 8, 12, 12, box) - 1.0) < 1e-9
    assert geom.seg_box_dist(9, 9, 11, 11, box) <= 0                # crosses the pad
    seg = geom.box_from_seg(0, 0, 4, 0, 0.2)
    assert abs(geom.seg_box_dist(0, 1, 4, 1, seg) - 0.9) < 1e-9
    assert geom.seg_seg_dist(0, 0, 2, 2, 0, 2, 2, 0) == 0.0
    sq = [(0, 0), (2, 0), (2, 2), (0, 2)]
    assert geom.pt_in_poly(sq, 1, 1) and not geom.pt_in_poly(sq, 3, 1)
    assert abs(geom.seg_poly_dist(3, 0, 3, 2, sq) - 1.0) < 1e-9 and abs(geom.poly_area(sq) - 4) < 1e-9


def test_occupancy_marks_restore_exactly():
    ex = synth([("A1", "A", 3, 3), ("A2", "A", 15, 3), ("B1", "B", 3, 9), ("B2", "B", 15, 9)])
    b = model.Board(ex)
    b.make_grid(0.1)
    cm = b.class_map(b.class_key(0.2, 0.2, 0.6, 0.3))
    g = b.grid
    # a cell next to A's pad belongs to A only; far away is free; outside the outline is blocked
    ix, iy = g.cell(3.6, 3.0)
    assert cm.occ[iy * g.W + ix] == b.nets["A"].id
    ix, iy = g.cell(9, 6)
    assert cm.occ[iy * g.W + ix] == 0 and cm.occ[0] == -1
    before, vbefore = bytes(cm.occ), bytes(cm.vocc)
    c1 = g.cells(g.capsule_spans(5, 6, 12, 6, 0.5))
    c2 = g.cells(g.capsule_spans(8, 4, 8, 8, 0.5))
    cm.mark(False, c1, 1)
    cm.mark(False, c2, 2)
    cm.mark(False, c2, 2)                       # the same net twice
    cm.mark(True, c1, BLOCK)
    ix, iy = g.cell(8, 6)
    assert cm.occ[iy * g.W + ix] == -1          # two nets overlap -> blocked for everyone
    ix, iy = g.cell(8, 4.5)
    assert cm.occ[iy * g.W + ix] == 2
    assert cm.vocc[g.cell(6, 6)[1] * g.W + g.cell(6, 6)[0]] == -1
    cm.unmark(False, c2, 2)
    assert cm.occ[iy * g.W + ix] == 2           # still covered once
    cm.unmark(False, c2, 2)
    cm.unmark(False, c1, 1)
    cm.unmark(True, c1, BLOCK)
    assert bytes(cm.occ) == before and bytes(cm.vocc) == vbefore


def test_router_crossing_nets_clean_and_deterministic():
    pads = [("A1", "A", 3, 7.5), ("A2", "A", 17, 7.5), ("B1", "B", 10, 2), ("B2", "B", 10, 13),
            ("C1", "C", 4, 3), ("C2", "C", 16, 12), ("C3", "C", 16, 3)]
    b, cfg, res = run_synth(synth(pads))
    assert all(c.state == "routed" for c in res["conns"]), [(c.net, c.reason) for c in res["conns"]]
    assert len(res["conns"]) == 4
    assert_clean(b, res)
    for n in ("A", "B", "C"):
        assert connected(b, res, n) == 1, n
    # track ends sit exactly on pad centres
    ends = {(t[2], t[3]) for t in res["tracks"]} | {(t[4], t[5]) for t in res["tracks"]}
    assert (3, 7.5) in ends and (17, 7.5) in ends and (10, 2) in ends
    # 45° style only: every segment is horizontal, vertical or diagonal (pad-centre stubs are < 0.1 mm)
    for t in res["tracks"]:
        dx, dy = abs(t[4] - t[2]), abs(t[5] - t[3])
        assert min(dx, dy) < 1e-6 or abs(dx - dy) < 1e-6 or math.hypot(dx, dy) < 0.15, t
    _b2, _c2, res2 = run_synth(synth(pads))
    assert res2["tracks"] == res["tracks"] and res2["vias"] == res["vias"]


def test_router_single_layer_ripup():
    # a wall with one gap; net A (routed first) runs straight across the gap's mouth, so B must rip it up
    walls = [[10, 0.2, 10.6, 6.9], [10, 8.1, 10.6, 14.8]]
    pads = [("A1", "A", 9.3, 5.5), ("A2", "A", 9.3, 9.5), ("B1", "B", 4, 7.5), ("B2", "B", 16, 7.5)]
    plan = {"order": ["A", "B"], "global": {"layers": ["F.Cu"]}}
    b, cfg, res = run_synth(synth(pads, walls=walls), plan)
    assert all(c.state == "routed" for c in res["conns"]), [(c.net, c.reason, c.blocked_by) for c in res["conns"]]
    assert res["stats"]["ripups"] >= 1 and not res["vias"]
    assert {t[1] for t in res["tracks"]} == {"F.Cu"}
    assert_clean(b, res)
    # without rip-up the second net is reported as blocked by the first — never silently dropped
    plan["global"]["ripup_rounds"] = 0
    b, cfg, res = run_synth(synth(pads, walls=walls), plan)
    bad = [c for c in res["conns"] if c.state == "failed"]
    assert len(bad) == 1 and bad[0].net == "B" and bad[0].blocked_by == ["A"] and "blocked by routed nets: A" in bad[0].reason


def test_plan_fields_steer_the_route():
    pads = [("A1", "A", 3, 7.5), ("A2", "A", 17, 7.5), ("B1", "B", 3, 3, "tht"), ("B2", "B", 17, 3, "tht")]
    ex = synth(pads)
    # avoid: A's direct line is forbidden -> the route goes round, never inside the rectangle
    rect = [8, 5, 12, 10]
    b, cfg, res = run_synth(ex, {"nets": {"A": {"avoid": [rect], "max_vias": 0}}})
    assert all(c.state == "routed" for c in res["conns"])
    for t in res["tracks"]:
        if t[0] == "A":
            for k in range(11):
                x, y = t[2] + (t[4] - t[2]) * k / 10, t[3] + (t[5] - t[3]) * k / 10
                assert not (rect[0] + 0.05 < x < rect[2] - 0.05 and rect[1] + 0.05 < y < rect[3] - 0.05), t
    la = sum(math.hypot(t[4] - t[2], t[5] - t[3]) for t in res["tracks"] if t[0] == "A")
    assert la > 14.5 and not res["vias"]
    # corridor pulls the route through a far away waypoint
    b, cfg, res = run_synth(ex, {"nets": {"A": {"corridor": [[3, 7.5], [10, 13], [17, 7.5]], "corridor_width": 1.5}}})
    ys = [max(t[3], t[5]) for t in res["tracks"] if t[0] == "A"]
    assert max(ys) > 11, ys                    # the straight line is y = 7.5
    # layers: a THT net told to stay on B.Cu does; width is honoured
    b, cfg, res = run_synth(ex, {"nets": {"B": {"layers": ["B.Cu"], "width": 0.5}}})
    tb = [t for t in res["tracks"] if t[0] == "B"]
    assert tb and {t[1] for t in tb} == {"B.Cu"} and {t[6] for t in tb if math.hypot(t[4] - t[2], t[5] - t[3]) > 0.2} == {0.5}
    # max_length shorter than the distance: reported, nothing written for that net
    b, cfg, res = run_synth(ex, {"nets": {"A": {"max_length": 5}}})
    bad = [c for c in res["conns"] if c.state == "failed"]
    assert len(bad) == 1 and "max_length" in bad[0].reason and not [t for t in res["tracks"] if t[0] == "A"]
    # skip
    b, cfg, res = run_synth(ex, {"nets": {"A": {"skip": True}}})
    assert [s["net"] for s in res["skipped"]] == ["A"] and {t[0] for t in res["tracks"]} == {"B"}
    # layer directions: with F.Cu strongly vertical a horizontal run prefers B.Cu
    b, cfg, res = run_synth(ex, {"global": {"layer_directions": {"F.Cu": "v", "B.Cu": "h"}, "direction_bias": 3,
                                            "via_cost": 1}, "nets": {"B": {"skip": True}}})
    lb = sum(math.hypot(t[4] - t[2], t[5] - t[3]) for t in res["tracks"] if t[1] == "B.Cu")
    assert lb > 10 and len(res["vias"]) == 2


def test_forbidden_regions_and_impossible_plans_are_reported():
    pads = [("A1", "A", 3, 7.5), ("A2", "A", 17, 7.5), ("B1", "B", 3, 3), ("B2", "B", 17, 3)]
    ex = synth(pads)
    # forbid everything: nothing routed, every failure names the cause, a warning is raised up front
    b, cfg, res = run_synth(ex, {"regions": [{"rect": [-5, -5, 30, 30], "forbid": True, "name": "all"}]})
    assert not res["tracks"] and all(c.state == "failed" and "forbidden region" in c.reason for c in res["conns"])
    assert any("forbids an area that contains pads" in w for w in cfg.warnings)
    # ... except B
    b, cfg, res = run_synth(ex, {"regions": [{"rect": [-5, -5, 30, 30], "forbid": True, "except": ["B"]}]})
    assert {t[0] for t in res["tracks"]} == {"B"}
    # strict corridor that leads nowhere near the target
    b, cfg, res = run_synth(ex, {"nets": {"A": {"corridor": [[3, 13], [6, 13]], "corridor_width": 1, "corridor_strict": True}}})
    bad = [c for c in res["conns"] if c.net == "A"]
    assert bad[0].state == "failed" and "strict corridor" in bad[0].reason
    # the same corridor, not strict: routed anyway
    b, cfg, res = run_synth(ex, {"nets": {"A": {"corridor": [[3, 13], [6, 13]], "corridor_width": 1}}})
    assert all(c.state == "routed" for c in res["conns"])
    # a cost region moves the route out of the rectangle without forbidding it
    b, cfg, res = run_synth(ex, {"regions": [{"rect": [8, 6, 12, 9], "cost": 50}], "nets": {"A": {"max_vias": 0}}})
    inside = [t for t in res["tracks"] if t[0] == "A" and 8.2 < (t[2] + t[4]) / 2 < 11.8 and 6.2 < (t[3] + t[5]) / 2 < 8.8]
    assert not inside and all(c.state == "routed" for c in res["conns"])


def test_plan_validation_errors_name_the_fix():
    b = model.Board(synth([("A1", "/SIG_A", 3, 3), ("A2", "/SIG_A", 9, 3), ("B1", "VBUS", 3, 9), ("B2", "VBUS", 9, 9)]))

    def bad(plan, *needles):
        try:
            planmod.resolve(b, plan)
        except ToolError as e:
            assert e.code == "BAD_PLAN", e.code
            text = e.message + " " + (e.fix or "")
            for n in needles:
                assert n in text, (n, text)
            return
        raise AssertionError(f"plan accepted: {plan}")
    bad({"nets": {"/SIG_X": {}}}, "not on the board", "/SIG_A")
    bad({"order": ["NOPE*"]}, "matches no net", "VBUS")
    bad({"globl": {}}, "unknown field", "global")
    bad({"nets": {"VBUS": {"widht": 1}}}, "unknown field", "width")
    bad({"global": {"layers": ["In1.Cu"]}}, "not a copper layer", "F.Cu")
    bad({"global": {"via_cost": "cheap"}}, "must be a number")
    bad({"global": {"grid": 0.001}}, "out of range")
    bad({"global": {"style": "curvy"}}, "'45' or '90'")
    bad({"regions": [{"rect": [0, 0, 1]}]}, "[x0, y0, x1, y1]")
    bad({"regions": [{"rect": [0, 0, 1, 1]}]}, "cost", "forbid")
    bad({"diff_pairs": [{"p": "VBUS"}]}, "'p' and 'n'")
    bad({"nets": {"VBUS": {"width": 0.01}}}, "minimum track width")
    bad({"nets": {"VBUS": {"corridor": [[1, 2, 3]]}}}, "[x, y]")
    bad({"nets": {"VBUS": {"direction": {"F.Cu": "diagonal"}}}}, "'h', 'v' or 'any'")
    # accepted forms: short name for a sheet-local net, globs, exact beats glob, aliases
    cfg = planmod.resolve(b, {"nets": {"*": {"width": 0.3}, "SIG_A": {"width": 0.25}},
                              "global": {"layer_directions": {"top": "h", "bottom": "vertical"}}})
    assert cfg.nets["/SIG_A"].width == 0.25 and cfg.nets["VBUS"].width == 0.3
    assert cfg.nets["VBUS"].layer_dir == {0: "h", 1: "v"}
    # clearance below the net class is raised, with a warning — never silently accepted
    cfg = planmod.resolve(b, {"nets": {"VBUS": {"clearance": 0.05}}})
    assert cfg.nets["VBUS"].clearance == 0.2 and any("below its net class clearance" in w for w in cfg.warnings)


def test_classification_and_default_plan():
    pads = []
    for i, n in enumerate(["GND"] * 7 + ["+3V3"] * 3 + ["/USB_DP", "/USB_DP", "/USB_DN", "/USB_DN", "/XTAL1", "/XTAL1",
                                                        "/SPI_SCK", "/SPI_SCK", "/LED", "/LED"]):
        pads.append((f"P{i}", n, 2 + (i % 6) * 3, 2 + (i // 6) * 3))
    b = model.Board(synth(pads))
    f = planmod.classify(b)
    assert f["GND"]["kind"] == "ground" and f["+3V3"]["kind"] == "power" and f["/LED"]["kind"] == "signal"
    assert f["/USB_DP"]["kind"] == "diff" and f["/USB_DP"]["pair"] == "/USB_DN" and f["/USB_DN"]["role"] == "n"
    assert "crystal" in f["/XTAL1"]["tags"] and f["/SPI_SCK"]["kind"] == "clock"
    assert f["GND"]["unrouted"] == 6 and f["GND"]["pads"] == 7
    p = planmod.default_plan(b, f)
    assert p["nets"]["GND"] == {"skip": True}                       # no pour yet: leave it for one
    assert p["nets"]["/XTAL1"] == {"keep_short": True, "max_vias": 0}
    assert p["diff_pairs"] == [{"p": "/USB_DP", "n": "/USB_DN", "gap": 0.2, "width": 0.2}]
    cfg = planmod.resolve(b, None)
    assert cfg.nets["GND"].skip and "pcb_pour" in cfg.nets["GND"].skip_reason
    assert cfg.nets["/USB_DN"].dp_partner == "/USB_DP" and cfg.nets["/XTAL1"].priority > cfg.nets["/LED"].priority
    # an explicit entry overrides the heuristic
    assert not planmod.resolve(b, {"nets": {"GND": {"skip": False}}}).nets["GND"].skip
    # the default plan is itself a valid plan (a model edits it and sends it back)
    planmod.resolve(b, p)


def test_dru_minimums_parser():
    f = _TMP / "x.kicad_dru"
    f.write_text('(version 1)\n# (rule "c" (constraint clearance (min 9mm)))\n'
                 '(rule "edge" (constraint edge_clearance (min 0.4mm)))\n'
                 '(rule "h2h" (constraint hole_to_hole (min 20mil)) (condition "A.Net != B.Net"))\n'
                 '(rule "npth" (constraint hole_size (min 0.9mm)) (condition "A.Pad_Type == \'NPTH, mechanical\'"))\n'
                 '(rule "via" (condition "A.Type == \'via\'") (constraint hole_size (min 0.35mm)))\n'
                 '(rule "silk" (layer "?.Silkscreen") (constraint clearance (min 5mm)))\n', encoding="utf-8")
    d = model.dru_minimums(f)
    assert d == {"edge_clearance": 0.4, "hole_to_hole": 0.508, "hole_size": 0.35}, d
    assert model.dru_minimums(_TMP / "missing.kicad_dru") == {}


# ── end-to-end on a real board (needs KiCad) ──────────────────────────────
BUILD = r'''
import json, sys, os
import pcbnew as P
out, libs = sys.argv[1], sys.argv[2]
mm = lambda v: int(round(v * 1e6))
b = P.CreateEmptyBoard()
nets = {}
def net(n):
    if n not in nets:
        nets[n] = P.NETINFO_ITEM(b, n); b.Add(nets[n])
    return nets[n]
def fp(ref, lib, name, x, y, rot, pins):
    f = P.FootprintLoad(os.path.join(libs, lib + ".pretty"), name)
    f.SetReference(ref); f.SetPosition(P.VECTOR2I(mm(x), mm(y))); f.SetOrientationDegrees(rot)
    b.Add(f)
    for pad in f.Pads():
        n = pins.get(pad.GetNumber())
        if n: pad.SetNet(net(n))
    return f
fp("U1", "Package_SO", "SOIC-14_3.9x8.7mm_P1.27mm", 121, 115, 0,
   {"1": "/D0", "2": "/D1", "3": "/D2", "4": "/D3", "5": "/SCK", "6": "/MOSI", "7": "GND",
    "8": "/MISO", "9": "/LED", "10": "/BTN", "11": "/USB_DP", "12": "/USB_DN", "13": "/RST", "14": "+3V3"})
fp("J1", "Connector_PinHeader_2.54mm", "PinHeader_1x08_P2.54mm_Vertical", 104, 106, 0,
   {"1": "+3V3", "2": "GND", "3": "/D0", "4": "/D1", "5": "/D2", "6": "/D3", "7": "/RST", "8": "/BTN"})
fp("J2", "Connector_PinHeader_2.54mm", "PinHeader_1x06_P2.54mm_Vertical", 137, 108, 0,
   {"1": "/SCK", "2": "/MOSI", "3": "/MISO", "4": "/USB_DP", "5": "/USB_DN", "6": "GND"})
fp("C1", "Capacitor_SMD", "C_0603_1608Metric", 121, 107, 0, {"1": "+3V3", "2": "GND"})
fp("C2", "Capacitor_SMD", "C_0603_1608Metric", 112, 124, 90, {"1": "+3V3", "2": "GND"})
fp("R1", "Resistor_SMD", "R_0603_1608Metric", 128, 121, 0, {"1": "/LED", "2": "/LED_A"})
fp("D1", "LED_SMD", "LED_0603_1608Metric", 132, 124, 0, {"2": "/LED_A", "1": "GND"})
fp("R2", "Resistor_SMD", "R_0603_1608Metric", 113, 108, 90, {"1": "+3V3", "2": "/RST"})
fp("R3", "Resistor_SMD", "R_0603_1608Metric", 127, 110, 45, {"1": "+3V3", "2": "/BTN"})
def seg(x1, y1, x2, y2, layer=P.Edge_Cuts):
    s = P.PCB_SHAPE(b); s.SetShape(P.SHAPE_T_SEGMENT); s.SetStart(P.VECTOR2I(mm(x1), mm(y1)))
    s.SetEnd(P.VECTOR2I(mm(x2), mm(y2))); s.SetLayer(layer); s.SetWidth(mm(0.1)); b.Add(s)
for a in ((100, 100, 142, 100), (142, 100, 142, 130), (142, 130, 100, 130), (100, 130, 100, 100)):
    seg(*a)
def zone(netname, layer, pts, name, keepout=False):
    z = P.ZONE(b)
    if keepout:
        z.SetIsRuleArea(True); z.SetDoNotAllowTracks(True); z.SetDoNotAllowVias(True); z.SetDoNotAllowZoneFills(False)
        z.SetDoNotAllowPads(False); z.SetDoNotAllowFootprints(False)
        z.SetLayerSet(P.LSET.AllCuMask(b.GetCopperLayerCount()))
    else:
        z.SetNet(net(netname)); z.SetLayer(layer)
    o = z.Outline(); o.NewOutline()
    for x, y in pts: o.Append(mm(x), mm(y))
    z.SetZoneName(name); b.Add(z)
if "pour" in sys.argv[3:]:
    zone("GND", P.B_Cu, [(100.5, 100.5), (141.5, 100.5), (141.5, 129.5), (100.5, 129.5)], "GND_BCu")
zone("", None, [(116, 101), (126, 101), (126, 104.5), (116, 104.5)], "copper-antenna-test", keepout=True)
if "four" in sys.argv[3:]:
    b.SetCopperLayerCount(4)
    zone("GND", b.GetLayerID("In1.Cu"), [(100.5, 100.5), (141.5, 100.5), (141.5, 129.5), (100.5, 129.5)], "GND_In1")
if "locked" in sys.argv[3:]:
    t = P.PCB_TRACK(b); t.SetStart(P.VECTOR2I(mm(104), mm(111.08))); t.SetEnd(P.VECTOR2I(mm(109), mm(111.08)))
    t.SetWidth(mm(0.3)); t.SetLayer(P.F_Cu); t.SetNet(net("/D0")); t.SetLocked(True); b.Add(t)
if "teardrop" in sys.argv[3:]:
    # an (unlocked) track with a teardrop zone, as KiCad's Edit > Edit Teardrops leaves them in the file
    t = P.PCB_TRACK(b); t.SetStart(P.VECTOR2I(mm(104), mm(111.08))); t.SetEnd(P.VECTOR2I(mm(109), mm(111.08)))
    t.SetWidth(mm(0.3)); t.SetLayer(P.F_Cu); t.SetNet(net("/D0")); b.Add(t)
    z = P.ZONE(b); z.SetNet(net("/D0")); z.SetLayer(P.F_Cu)
    o = z.Outline(); o.NewOutline()
    for x, y in ((105.0, 110.7), (106.4, 110.93), (106.4, 111.23), (105.0, 111.46)): o.Append(mm(x), mm(y))
    z.SetZoneName("$teardrop_padvia$"); z.SetLocalClearance(0)
    z.SetPadConnection(P.ZONE_CONNECTION_FULL); z.SetIslandRemovalMode(P.ISLAND_REMOVAL_MODE_NEVER)
    b.Add(z)
if len(b.Zones()):
    P.ZONE_FILLER(b).Fill(b.Zones())
P.SaveBoard(out, b)
if "teardrop" in sys.argv[3:]:
    # the teardrop type enum is not scriptable: mark the zone the way KiCad writes it, and check it reads back
    s = open(out, encoding="utf-8").read()
    k = '(name "$teardrop_padvia$")'
    assert s.count(k) == 1
    open(out, "w", encoding="utf-8", newline="\n").write(s.replace(k, k + "\n\t\t(attr (teardrop (type padvia)))"))
    assert [z.IsTeardropArea() for z in P.LoadBoard(out).Zones()].count(True) == 1
if "font" in sys.argv[3:]:
    # text in a font that is not installed: pcbnew reports the substitution through wxLog (a modal dialog)
    s = open(out, encoding="utf-8").read().rstrip()
    assert s.endswith(")")
    s = s[:-1] + """  (gr_text "copper" (at 120 127 0) (layer "F.SilkS")
    (effects (font (face "No Such Font 4711") (size 1 1) (thickness 0.15)))
  )
)
"""
    open(out, "w", encoding="utf-8", newline="\n").write(s)
print("built")
'''


def have_kicad() -> bool:
    try:
        return bool(env.kicad().cli) and bool(env.kicad_python()) and bool(env.kicad().share)
    except Exception:  # noqa: BLE001
        return False


def build(name, *flags) -> str:
    d = _TMP / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    script = _TMP / "build_board.py"
    script.write_text(BUILD, encoding="utf-8")
    r = subprocess.run([str(worker.python()), str(script), str(d / "demo.kicad_pcb"),
                        str(env.kicad().share / "footprints"), *flags], capture_output=True, text=True)
    assert "built" in r.stdout, r.stderr[-800:] + r.stdout[-300:]
    (d / "demo.kicad_pro").write_text("{}", encoding="utf-8")
    return str(d)


def call(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    if r["isError"]:
        raise AssertionError(f"{name} failed: {r['content'][0]['text'][:900]}")
    return r.get("structuredContent") or {}, r


def call_err(name, **args):
    r = registry.call_tool(name, args, lambda f, m: None)
    assert r["isError"], f"{name} was expected to fail"
    return r["structuredContent"]


def kicad_drc(pcb: Path) -> dict:
    """KiCad's verdict the way a user gets it: zones refilled by KiCad itself."""
    out = Path(tempfile.mkdtemp()) / "drc.json"
    env.run(["pcb", "drc", "--format", "json", "--severity-all", "--refill-zones", "-o", str(out), str(pcb)])
    d = json.loads(out.read_text(encoding="utf-8"))
    return {"errors": [v for v in d["violations"] if v["severity"] == "error"], "unconnected": len(d["unconnected_items"])}


def copper(pcb: Path):
    s = Path(pcb).read_text(encoding="utf-8")
    segs = sorted(re.findall(r'\(segment\s+\(start ([-\d. ]+)\)\s+\(end ([-\d. ]+)\)\s+\(width ([\d.]+)\)\s+\(layer "([^"]+)"\)', s))
    vias = sorted(re.findall(r'\(via\s+\(at ([-\d. ]+)\)\s+\(size ([\d.]+)\)', s))
    return segs, vias


def images(r):
    return [c for c in r["content"] if c["type"] == "image"]


def test_e2e_plan_then_route_no_pour():
    if not have_kicad():
        return
    d = build("nopour")
    pcb = Path(d) / "demo.kicad_pcb"
    rev = projmod.revision(pcb)
    p, r = call("pcb_route_plan", project=d)
    assert projmod.revision(pcb) == rev                               # read-only
    assert p["board"]["size_mm"] == [42.0, 30.0] and [l["layer"] for l in p["board"]["layers"]] == ["F.Cu", "B.Cu"]
    rows = {row[0]: row for row in p["nets"]["rows"]}
    assert rows["GND"][1] == "ground" and rows["+3V3"][1] == "power" and rows["/USB_DP"][1] == "diff"
    assert rows["/SCK"][1] == "clock" and rows["/D0"][3] == 1 and rows["+3V3"][3] == 5
    assert p["routing"]["unrouted_connections"] == sum(row[3] for row in p["nets"]["rows"])
    assert p["keepouts"][0]["name"] == "copper-antenna-test" and p["keepouts"][0]["rect"] == [116.0, 101.0, 126.0, 104.5]
    assert p["congestion"]["hot_spots"] and len(p["congestion"]["map"]) == p["congestion"]["rows"]
    assert "regions" in p["plan_schema"] and p["suggested_plan"]["global"]["grid"] == 0.1
    assert len(images(r)) == 1 and len(json.dumps(p)) < 9000          # compact
    # the suggested plan goes straight back in; GND (6 pads, no pour) is routed because the plan says so
    plan = p["suggested_plan"]
    plan.setdefault("nets", {})["GND"] = {"skip": False}
    plan["order"] = ["/USB_D?", "*"]
    o, r = call("pcb_route_guided", project=d, plan=plan)
    assert o["percent"] == 100.0 and o["routed"] == o["total"] == p["routing"]["unrouted_connections"], o
    assert o["drc"]["errors"] == 0 and o["drc"]["unconnected"] == 0 and not o["failed"] and o["written"]
    k = kicad_drc(pcb)
    assert not k["errors"] and k["unconnected"] == 0, k
    assert o["checkpoint"] and o["plan_used"]["order"] == ["/USB_D?", "*"] and len(images(r)) == 1
    assert o["diff_pairs"][0]["p"] == "/USB_DP" and "sequential" in o["diff_pairs"][0]["coupling"]
    assert set(o["nets"]) >= {"/D0", "GND", "+3V3"} and o["nets"]["/D0"][0] > 5
    # the power class has no special width here; nothing entered the antenna keep-out
    for a, b_, _w, _l in copper(pcb)[0]:
        for xy in (a, b_):
            x, y = (float(v) for v in xy.split())
            assert not (116 < x < 126 and 101 < y < 104.5), xy
    # again: nothing left to do, board untouched
    rev = projmod.revision(pcb)
    o, _ = call("pcb_route_guided", project=d)
    assert o["nothing_to_route"] and not o["written"] and projmod.revision(pcb) == rev


def test_e2e_default_heuristics_leave_ground_for_the_pour():
    if not have_kicad():
        return
    d = build("defaults")
    o, _ = call("pcb_route_guided", project=d, render=False)           # no plan at all
    assert [s["net"] for s in o["skipped"]] == ["GND"] and o["skipped"][0]["connections"] == 5
    assert o["percent"] == 100.0 and o["drc"]["errors"] == 0 and o["drc"]["unconnected"] == 5
    assert any(f["code"] == "LEFT_FOR_POUR" and "pcb_pour(net='GND')" in f["fix"] for f in o["findings"])


def test_e2e_pour_board_is_deterministic_and_drc_clean():
    if not have_kicad():
        return
    d1, d2 = build("pour1", "pour"), build("pour2", "pour")
    plan = {"global": {"layer_directions": {"F.Cu": "h", "B.Cu": "v"}}, "nets": {"+3V3": {"width": 0.4}}}
    o1, _ = call("pcb_route_guided", project=d1, plan=plan, render=False)
    o2, _ = call("pcb_route_guided", project=d2, plan=plan, render=False)
    assert o1["percent"] == 100.0 and o1["drc"]["errors"] == 0 and o1["drc"]["unconnected"] == 0, o1
    assert copper(Path(d1) / "demo.kicad_pcb") == copper(Path(d2) / "demo.kicad_pcb")       # same plan + board = same copper
    assert o1["nets"] == o2["nets"] and o1["added"] == o2["added"]
    assert any(p["pass"] == "pours" for p in o1["passes"])                                  # SMD GND pads got vias to the pour
    assert o1["nets"]["GND"][1] >= 1
    k = kicad_drc(Path(d1) / "demo.kicad_pcb")
    assert not k["errors"] and k["unconnected"] == 0, k
    # ripup='nets' re-routes just those nets and leaves every other track byte-identical
    before = copper(Path(d1) / "demo.kicad_pcb")[0]
    o, _ = call("pcb_route_guided", project=d1, ripup="nets", nets=["/SCK", "MOSI"], render=False,
                plan={"nets": {"/SCK": {"width": 0.3}}})
    assert o["total"] == 2 and o["percent"] == 100.0 and o["drc"]["errors"] == 0 and set(o["nets"]) == {"/SCK", "/MOSI"}
    after = copper(Path(d1) / "demo.kicad_pcb")[0]
    assert len(set(before) & set(after)) >= len(before) - o["ripped_up"]
    assert o["previous"]["restore"].startswith("checkpoint(action='restore'")


def test_e2e_dry_run_compare_and_bad_plans_never_touch_the_board():
    if not have_kicad():
        return
    d = build("dry", "pour")
    pcb = Path(d) / "demo.kicad_pcb"
    raw = pcb.read_bytes()
    o, r = call("pcb_route_guided", project=d, dry_run=True)
    assert o["dry_run"] and not o["written"] and o["percent"] == 100.0 and o["drc"]["errors"] == 0
    assert pcb.read_bytes() == raw and len(images(r)) == 1 and o["checkpoint"] is None
    e = call_err("pcb_route_guided", project=d, plan={"nets": {"/NOPE": {"width": 1}}})
    assert e["error"] == "BAD_PLAN" and "/SCK" in e["fix"] and pcb.read_bytes() == raw
    e = call_err("pcb_route_guided", project=d, plan={"global": {"turbo": True}})
    assert e["error"] == "BAD_PLAN" and "valid fields" in e["message"]
    e = call_err("pcb_route_guided", project=d, ripup="nets")
    assert e["error"] == "BAD_ARGUMENT"
    e = call_err("pcb_route_guided", project=d, nets=["/NOPE"])
    assert e["error"] == "BAD_PLAN"
    # forbid everything: honest zero, clear reasons, nothing written
    o, _ = call("pcb_route_guided", project=d, render=False,
                plan={"regions": [{"rect": [90, 90, 150, 140], "forbid": True, "name": "everything"}]})
    assert o["routed"] == 0 and not o["written"] and pcb.read_bytes() == raw
    assert o["failed"] and all("forbidden region" in f["reason"] for f in o["failed"])
    assert any(f["code"] == "UNROUTED" for f in o["findings"]) and any("everything" in f["message"] for f in o["findings"])
    # route for real, then compare a fresh guided run against it: scores for both, board untouched
    call("pcb_route_guided", project=d, render=False, plan={"global": {"via_cost": 0.5, "bend_cost": 0}})
    raw = pcb.read_bytes()
    o, _ = call("pcb_route_guided", project=d, compare=True, render=False)
    c = o["compare"]
    assert c["better"] in ("guided", "existing", "tie") and c["existing"]["unrouted"] == 0 and c["guided"]["drc_errors"] == 0
    assert c["existing"]["tracks"] > 0 and "ripup='all'" in c["adopt"] and pcb.read_bytes() == raw


def test_e2e_four_layers_locked_track_and_open_board_guard():
    if not have_kicad():
        return
    d = build("four", "pour", "four", "locked")
    pcb = Path(d) / "demo.kicad_pcb"
    p, _ = call("pcb_route_plan", project=d, heatmap=False, nets=["/D*"])
    lays = {l["layer"]: l for l in p["board"]["layers"]}
    assert list(lays) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"] and lays["In1.Cu"].get("plane") and lays["B.Cu"].get("plane")
    assert p["suggested_plan"]["global"]["layers"] == ["F.Cu", "In2.Cu", "B.Cu"]             # the inner plane stays intact
    assert {row[0] for row in p["nets"]["rows"]} == {"/D0", "/D1", "/D2", "/D3"}
    # the board is "open in KiCad": refused, untouched
    lock = Path(d) / "~demo.kicad_pcb.lck"
    lock.write_text("{}", encoding="utf-8")
    raw = pcb.read_bytes()
    e = call_err("pcb_route_guided", project=d)
    assert e["error"] == "FILE_OPEN_IN_KICAD" and pcb.read_bytes() == raw
    lock.unlink()
    o, _ = call("pcb_route_guided", project=d, ripup="all", render=False)
    assert o["percent"] == 100.0 and o["drc"]["errors"] == 0 and o["drc"]["unconnected"] == 0, o
    segs, _v = copper(pcb)
    assert not [s for s in segs if s[3] == "In1.Cu"]                                         # no signal on the plane layer
    txt = pcb.read_text(encoding="utf-8")
    assert re.search(r"\(start 104 111\.08\)\s+\(end 109 111\.08\)\s+\(width 0\.3\)\s+\(locked yes\)", txt)   # survived ripup='all'
    k = kicad_drc(pcb)
    assert not k["errors"] and k["unconnected"] == 0, k


def _footprint_blocks(txt: str) -> list:
    """[(start, end, reference)] of every top-level (footprint ...) of a board file, in file order."""
    out, depth, i, n, start = [], 0, 0, len(txt), None
    while i < n:
        ch = txt[i]
        if ch == '"':
            i += 1
            while i < n and txt[i] != '"':
                i += 2 if txt[i] == "\\" else 1
        elif ch == "(":
            depth += 1
            if depth == 2 and txt.startswith("(footprint ", i):
                start = i
        elif ch == ")":
            depth -= 1
            if depth == 1 and start is not None:
                ref = re.search(r'\(property "Reference" "([^"]+)"', txt[start:i + 1])
                out.append((start, i + 1, ref.group(1) if ref else ""))
                start = None
        i += 1
    return out


def reorder_footprints(pcb: Path, order) -> None:
    """Rewrite the board with its footprints in ``order`` (references). Nothing else changes: the same parts, pads,
    nets, positions and UUIDs — only where each footprint stands in the file, which is what KiCad's save order
    (by UUID, so different on every build of the same board) varies."""
    txt = pcb.read_text(encoding="utf-8")
    blocks = _footprint_blocks(txt)
    by_ref = {ref: txt[a:b] for a, b, ref in blocks}
    assert sorted(by_ref) == sorted(order) and len(by_ref) == len(blocks), (sorted(by_ref), order)
    out, pos = [], 0
    for (a, b, _ref), ref in zip(blocks, order):
        out += [txt[pos:a], by_ref[ref]]
        pos = b
    out.append(txt[pos:])
    pcb.write_text("".join(out), encoding="utf-8", newline=chr(10))
    assert [r for _a, _b, r in _footprint_blocks(pcb.read_text(encoding="utf-8"))] == list(order)


def test_e2e_existing_copper_routes_the_same_whatever_the_footprint_order():
    """KiCad saves footprints in UUID order, so the same board comes in a different file order on every build, and
    with it KiCad numbers its connectivity clusters differently. The locked /D0 track (it hangs on J1.3) must
    belong to J1.3's cluster in every Board the run builds from one extract. It did not: the second Board looked
    the track's raw cluster id up in pad ids the first one had renumbered in place, and for some orders the track
    then counted as U1.1's copper — the router joined J1.3 to "U1's" track with a useless loop, left U1.1 open
    and reported 23/24 with failed [] (found by the independent verifier in 3 of 27 runs of the test above).
    The first order below is the verifier's failing one, the second one that happened to pass."""
    if not have_kicad():
        return
    orders = ("R1 C1 J2 C2 D1 J1 R2 R3 U1", "R2 C2 D1 J2 R1 U1 R3 C1 J1", "U1 R3 R2 R1 J2 J1 D1 C2 C1")
    seen = []
    for i, order in enumerate(orders):
        d = build(f"ord{i}", "pour", "four", "locked")
        pcb = Path(d) / "demo.kicad_pcb"
        reorder_footprints(pcb, order.split())
        # one extract, several Boards: the existing track is J1.3's copper in each of them
        from copper_kicad.pcb.guided import session
        b1, ex, _before, _rip = session.load_board(projmod.resolve(d), ripup="all")
        for b in (b1, model.Board(ex), model.Board(ex)):
            got = {tuple(sorted(q.name for q in c["pads"])): len(c["tracks"]) for c in b.clusters["/D0"].values()}
            assert got == {("J1.3",): 1, ("U1.1",): 0}, (order, got)
        o, _ = call("pcb_route_guided", project=d, ripup="all", render=False)
        assert o["percent"] == 100.0 and o["routed"] == o["total"] and not o["failed"], (order, o["routed"], o["failed"])
        assert o["drc"]["errors"] == 0 and o["drc"]["unconnected"] == 0, (order, o["drc"])
        k = kicad_drc(pcb)
        assert not k["errors"] and k["unconnected"] == 0, (order, k)
        txt = pcb.read_text(encoding="utf-8")
        assert re.search(r"\(start 104 111\.08\)\s+\(end 109 111\.08\)\s+\(width 0\.3\)\s+\(locked yes\)", txt), order
        segs, vias = copper(pcb)
        seen.append((sorted(segs), sorted(vias)))
    assert seen[0] == seen[1] == seen[2], "the same board in another file order must route to the same copper"


# ── regression tests (found by adversarial testing) ───────────────────────
def test_one_extract_builds_the_same_board_every_time():
    """``model.Board(ex)`` renumbers KiCad's connectivity clusters by pad order. It must not write the new numbers
    back into the extract: session, engine and judge each build a Board from the same extract, and the tracks /
    vias of the later ones were then looked up by RAW id in RENUMBERED pad ids — existing copper lost its cluster,
    or (raw id of one cluster == new id of another) joined an unrelated pad's cluster."""
    ex = synth([("A", "N1", 2, 2), ("B", "N1", 12, 2), ("C", "N2", 2, 8), ("D", "N2", 12, 8)],
               tracks=[["N1", 0, 2.0, 2.0, 6.0, 2.0, 0.2, 2, 0],      # hangs on A (raw cluster 2)
                       ["N2", 0, 12.0, 8.0, 8.0, 8.0, 0.2, 9, 0]])    # hangs on D (raw cluster 9)
    for p, c in zip(ex["pads"], (2, 1, 5, 9)):                         # A's raw id is the number B is renumbered to
        p["c"] = c
    ex["vias"] = [["N2", 8.0, 8.0, 0.6, 0.3, 0, 1, 9, 0]]
    snapshot = json.dumps(ex, sort_keys=True)

    def owners(b):
        return {net: {tuple(q.name for q in c["pads"]): (len(c["tracks"]), len(c["vias"])) for c in cl.values()}
                for net, cl in b.clusters.items()}
    want = {"N1": {("A.1",): (1, 0), ("B.1",): (0, 0)}, "N2": {("C.1",): (0, 0), ("D.1",): (1, 1)}}
    for _ in range(3):
        b = model.Board(ex)
        assert owners(b) == want, owners(b)
        assert json.dumps(ex, sort_keys=True) == snapshot, "Board() altered the extract it was built from"
    again = model.Board(b.ex)                                          # a Board's own (renumbered) extract: stable too
    assert owners(again) == want, owners(again)


def test_budget_is_work_not_wall_clock():
    """The time budget is counted in search work, so where the router stops does not depend on machine speed."""
    pads = []
    for i in range(8):
        pads += [(f"A{i}", f"N{i}", 2.0, 2.0 + 1.4 * i), (f"B{i}", f"N{i}", 18.0, 13.0 - 1.4 * i)]
    ex = synth(pads)

    def run(work):
        b = model.Board(json.loads(json.dumps(ex)))
        cfg = planmod.resolve(b, None)
        b.make_grid(cfg.step)
        res = router.Router(b, cfg, work=work).run()
        return res, [(c.net, c.state, c.reason) for c in res["conns"]]
    full, st_full = run(None)
    assert all(s == "routed" for _n, s, _r in st_full), st_full
    half, st_a = run(full["work"] * 0.4)
    again, st_b = run(full["work"] * 0.4)
    assert st_a == st_b and half["tracks"] == again["tracks"]                  # same stop point, same copper
    out = [n for n, s, r in st_a if s == "failed"]
    assert out and len(out) < 8 and all("time budget" in r for _n, s, r in st_a if s == "failed"), st_a
    assert not half["clock_hit"] and half["work"] <= full["work"]
    assert router.WORK_PER_SECOND > 0 and "work" in planmod.GLOBAL_FIELDS["time_budget_s"]


def test_plan_validation_regressions():
    b = model.Board(synth([("A", "+BATT", 2, 2), ("B", "+BATT", 8, 2), ("C", "/USB.VBUS", 2, 6), ("D", "/USB.VBUS", 8, 6),
                           ("E", "Net-(U5-XC1)", 2, 9), ("F", "Net-(U5-XC1)", 8, 9), ("G", "/SIG", 2, 12), ("H", "/SIG", 8, 12)]))
    f = planmod.classify(b)
    assert f["+BATT"]["kind"] == "power" and f["/USB.VBUS"]["kind"] == "power", f
    assert "crystal" in f["Net-(U5-XC1)"]["tags"] and f["/SIG"]["kind"] == "signal"
    for plan, needle in (({"nets": {"/SIG": {"corridor": [[1, True]]}}}, "corridor must be a list"),
                         ({"global": {"via_cost": float("nan")}}, "must be a number"),
                         ({"global": {"grid": float("inf")}}, "must be a number"),
                         ({"regions": [{"rect": [0, 0, 1e9, 1], "forbid": True}]}, "rect must be")):
        try:
            planmod.resolve(b, plan)
        except ToolError as e:
            assert e.code == "BAD_PLAN" and needle in e.message, (plan, e.message)
        else:
            raise AssertionError(f"plan accepted: {plan}")
    try:
        planmod._Names(b).match("/NOPE", "argument nets")
    except ToolError as e:
        assert e.message.startswith("argument nets:") and "plan." not in e.message, e.message   # not a plan field
    else:
        raise AssertionError("unknown net accepted")


def test_new_error_accounting_and_pad_rect():
    from copper_kicad.pcb.guided import session

    def v(t, d, items, sev="error"):
        return {"type": t, "description": d, "severity": sev,
                "items": [{"description": i, "pos": {"x": 1.0, "y": 2.0}} for i in items]}
    base = [v("courtyards_overlap", "Courtyards overlap", ["Footprint A", "Footprint B"]),
            v("clearance", "Clearance violation", ["Pad 1 of U1", "Pad 2 of U1"]),
            v("silk_overlap", "x", ["a"], "warning")]
    now = base + [v("clearance", "Clearance violation", ["Pad 2 of U1", "Pad 1 of U1"]),          # a second copy = new
                  v("starved_thermal", "Thermal relief incomplete", ["Zone GND", "Pad 3 of J1"]),
                  v("silk_overlap", "y", ["b"], "warning")]
    new = session._new_errors(now, base)
    assert [n["type"] for n in new] == ["clearance", "starved_thermal"] and new[1]["at"] == [1.0, 2.0], new
    assert session._new_errors(base, base) == []
    ex = {"pads": [{"x": 10.0, "y": 10.0, "shapes": [[[0], "box", [10.0, 10.0, 0, -1, 0.5, 1.2, 0.2]]]},      # rotated 90°
                   {"x": 20.0, "y": 10.0, "shapes": [[[0], "box", [20.0, 10.0, 1, 0, 0.4, 0.3, 0.0]]]}]}
    assert session._pad_rect(ex, 20.0, 10.0, "GND") == (18.7, 8.7, 21.3, 11.3, "GND")           # small pad: 1.3 mm
    assert session._pad_rect(ex, 10.0, 10.0, "GND") == (8.05, 8.7, 11.95, 11.3, "GND")          # long pad: edge + 0.75


def test_e2e_destructive_combinations_never_leave_a_ripped_up_board():
    if not have_kicad():
        return
    d = build("rip", "pour")
    pcb = Path(d) / "demo.kicad_pcb"
    o, _ = call("pcb_route_guided", project=d, render=False)
    assert o["percent"] == 100.0 and o["written"]
    raw = pcb.read_bytes()
    # rip up everything but re-route one net: refused
    e = call_err("pcb_route_guided", project=d, ripup="all", nets=["/SCK"])
    assert e["error"] == "BAD_ARGUMENT" and "ripup='nets'" in e["fix"] and pcb.read_bytes() == raw
    e = call_err("pcb_route_guided", project=d, compare=True, nets=["/SCK"])
    assert e["error"] == "BAD_ARGUMENT" and pcb.read_bytes() == raw
    # rip up everything with a plan that can route nothing: the rip-up is undone, and the report says so
    o, r = call("pcb_route_guided", project=d, ripup="all", render=False,
                plan={"regions": [{"rect": [90, 90, 150, 140], "forbid": True, "name": "everything"}]})
    assert pcb.read_bytes() == raw, "a rip-up that routed nothing must leave the board untouched"
    assert o["routed"] == 0 and o["total"] > 0 and not o["written"] and o["ripped_up"] == 0 and "previous" not in o
    assert "rip-up was undone" in o["not_written"] and "board is unchanged" in r["content"][0]["text"]
    assert o["failed"]
    k = kicad_drc(pcb)
    assert not k["errors"] and k["unconnected"] == 0, k
    # a fully routed board with nothing selected says how to re-route instead of a bare "nothing to route"
    o, r = call("pcb_route_guided", project=d, render=False)
    assert o["nothing_to_route"] and "ripup='all'" in r["content"][0]["text"] and pcb.read_bytes() == raw


# ── the safety contracts, explicitly under BOTH engines ────────────────────
ENGINES = ("v2", "guided")
RESULT_CAP = 6000         # bytes of a default (compact) result on this board; tests/test_tokens.py has the same cap
FORBID = [107.0, 109.0, 111.0, 122.5]          # a strip between J1 and U1: /D0../D3, /RST, /BTN must go round it
AVOID_SCK = [126.0, 104.0, 133.0, 119.0]       # between U1 and J2: /SCK alone must stay out


def _eng(engine, plan=None):
    plan = json.loads(json.dumps(plan or {}))
    plan.setdefault("global", {})["engine"] = engine
    return plan


def _copper_by_net(pcb: Path):
    """[(net, layer | None, x1, y1, x2, y2)] of every track and via on the board (KiCad's own reading of the file)."""
    ex = worker.run("guided_board", {"pcb": str(pcb), "save": False, "ops": [{"op": "extract"}]}, timeout=120)["results"][0]
    out = [(t[0], t[1], t[2], t[3], t[4], t[5]) for t in ex["tracks"]]
    out += [(v[0], None, v[1], v[2], v[1], v[2]) for v in ex["vias"]]
    return out


def _enters(rect, x1, y1, x2, y2, slack=0.1) -> bool:
    """Does the centre line go deeper than ``slack`` (one grid step: v1 blocks whole cells) into the rectangle?"""
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    hw, hh = (rect[2] - rect[0]) / 2 - slack, (rect[3] - rect[1]) / 2 - slack
    return geom.seg_rect_dist(x1 - cx, y1 - cy, x2 - cx, y2 - cy, hw, hh) <= 0


def test_the_default_engine_is_v2_and_guided_stays_selectable():
    from copper_kicad.pcb.v2 import planx
    assert planx.DEFAULT_ENGINE == "v2"
    b = model.Board(synth([("A", "N1", 2, 2), ("B", "N1", 12, 2)]))
    assert planmod.resolve(b, None).engine == "v2" and planmod.resolve(b, {}).engine == "v2"
    assert planmod.resolve(b, {"global": {"engine": "guided"}}).engine == "guided"
    try:
        planmod.resolve(b, {"global": {"engine": "v3"}})
        raise AssertionError("an unknown engine must be rejected")
    except ToolError as e:
        assert e.code == "BAD_PLAN" and "guided" in str(e) + str(e.fix) and "v2" in str(e) + str(e.fix)


def test_e2e_safety_contracts_hold_under_both_engines():
    """What a caller relies on whatever engine routes: forbid / avoid are never routed through, ground without a
    pour is left for pcb_pour and listed in skipped, dry_run / compare / a bad plan never write, a run that routes
    nothing leaves the file byte-identical (rip-up undone, ripped_up 0), the default result is compact and the
    default route ends DRC-clean."""
    if not have_kicad():
        return
    for engine in ENGINES:
        tag = engine
        # 4. ground with 6 pads and no pour: left for the pour, listed with its connection count
        d = build(f"sc-gnd-{tag}")
        o, _ = call("pcb_route_guided", project=d, render=False, plan=_eng(engine))
        assert (o.get("engine") or "guided") == engine, (engine, o.get("engine"))
        assert [(s_["net"], s_["connections"]) for s_ in o["skipped"]] == [("GND", 5)], (engine, o["skipped"])
        assert "pour" in o["skipped"][0]["reason"], (engine, o["skipped"])
        assert o["percent"] == 100.0 and o["drc"]["errors"] == 0 and o["drc"]["unconnected"] == 5, (engine, o["drc"])
        assert any(f["code"] == "LEFT_FOR_POUR" and "pcb_pour(net='GND')" in f["fix"] for f in o["findings"]), engine
        assert not any(n == "GND" for n, *_ in _copper_by_net(Path(d) / "demo.kicad_pcb")), f"{engine} routed GND as tracks"
        # 6. the default result is compact (detail=true is the way to the rest)
        assert len(json.dumps(o, ensure_ascii=False).encode()) <= RESULT_CAP, (engine, len(json.dumps(o)))
        if engine == "v2":
            assert "stages" not in o and "judge" not in o and "detail=true" in o["more"], sorted(o)

        # 2. dry_run, compare and bad plans never write
        d = build(f"sc-dry-{tag}", "pour")
        pcb = Path(d) / "demo.kicad_pcb"
        raw = pcb.read_bytes()
        o, _ = call("pcb_route_guided", project=d, dry_run=True, render=False, plan=_eng(engine))
        assert o["dry_run"] and not o["written"] and o["checkpoint"] is None and pcb.read_bytes() == raw, engine
        assert o["percent"] == 100.0 and o["drc"]["errors"] == 0, (engine, o["percent"], o["drc"])
        for bad in ({"nets": {"/NOPE": {"width": 1}}}, {"global": {"turbo": True}}, {"regions": [{"rect": [1, 2, 3]}]}):
            e = call_err("pcb_route_guided", project=d, ripup="all", plan=_eng(engine, bad))
            assert e["error"] == "BAD_PLAN" and pcb.read_bytes() == raw, (engine, bad, e["error"])
        # 3. forbid everything: an honest zero, nothing written
        everything = {"regions": [{"rect": [90, 90, 150, 140], "forbid": True, "name": "everything"}]}
        o, r = call("pcb_route_guided", project=d, render=False, plan=_eng(engine, everything))
        assert o["routed"] == 0 and o["total"] > 0 and not o["written"] and pcb.read_bytes() == raw, engine
        assert o["ripped_up"] == 0 and o["added"] == {"tracks": 0, "vias": 0, "length_mm": 0}, (engine, o["added"])
        assert o["failed"] and all("forbidden region" in f["reason"] for f in o["failed"]), (engine, o["failed"][:2])

        # 1. forbid / avoid are never routed through, and the route round them is DRC-clean (5.)
        plan = {"regions": [{"rect": FORBID, "forbid": True, "name": "strip"}], "nets": {"/SCK": {"avoid": [AVOID_SCK]}}}
        o, _ = call("pcb_route_guided", project=d, render=False, plan=_eng(engine, plan))
        assert o["written"] and o["routed"] > 0, (engine, o["routed"], o["failed"])
        assert o["drc"]["errors"] == 0, (engine, o["drc"])
        cu = _copper_by_net(pcb)
        assert cu
        for net, _lay, x1, y1, x2, y2 in cu:
            assert not _enters(FORBID, x1, y1, x2, y2), f"{engine}: {net} routed through the forbidden strip at {(x1, y1, x2, y2)}"
            if net == "/SCK":
                assert not _enters(AVOID_SCK, x1, y1, x2, y2), f"{engine}: /SCK entered its avoid rectangle at {(x1, y1, x2, y2)}"
        assert any(net == "/SCK" for net, *_ in cu) or any(f["net"] == "/SCK" for f in o["failed"]), engine
        k = kicad_drc(pcb)
        assert not k["errors"], (engine, k["errors"][:2])
        # the same rules hold for a re-route of the routed board
        o, _ = call("pcb_route_guided", project=d, render=False, ripup="nets", nets=["/D?", "/SCK"], plan=_eng(engine, plan))
        for net, _lay, x1, y1, x2, y2 in _copper_by_net(pcb):
            assert not _enters(FORBID, x1, y1, x2, y2), f"{engine}: {net} re-routed through the forbidden strip"
            assert net != "/SCK" or not _enters(AVOID_SCK, x1, y1, x2, y2), f"{engine}: /SCK re-routed through its avoid"
        # 3. a rip-up that then routes nothing is undone: byte-identical, ripped_up 0, nothing to restore
        raw = pcb.read_bytes()
        for kw in ({"ripup": "all"}, {"ripup": "nets", "nets": ["/D0", "/SCK"]}):
            o, r = call("pcb_route_guided", project=d, render=False, plan=_eng(engine, everything), **kw)
            assert pcb.read_bytes() == raw, f"{engine} {kw}: a rip-up that routed nothing must leave the board untouched"
            assert o["routed"] == 0 and o["total"] > 0 and not o["written"] and o["ripped_up"] == 0, (engine, kw, o["ripped_up"])
            assert "previous" not in o and "rip-up was undone" in o["not_written"], (engine, kw)
            assert "board is unchanged" in r["content"][0]["text"], (engine, kw)
        # 2. compare scores a re-route on a scratch copy: board untouched
        o, _ = call("pcb_route_guided", project=d, compare=True, render=False, plan=_eng(engine))
        assert o["compare"]["better"] in ("guided", "existing", "tie") and not o["written"] and pcb.read_bytes() == raw, engine
        assert not list(Path(d).glob("*.copper-tmp")), engine


def test_e2e_ripup_removes_teardrops_and_matches_kicads_refill():
    if not have_kicad():
        return
    d = build("tear", "pour", "teardrop")
    pcb = Path(d) / "demo.kicad_pcb"
    assert "teardrop" in pcb.read_text(encoding="utf-8")
    p, _ = call("pcb_route_plan", project=d, heatmap=False, ripup="all")
    o, _ = call("pcb_route_guided", project=d, ripup="all", render=False)
    assert o["percent"] == 100.0 and o["total"] == p["routing"]["unrouted_connections"], (o["total"], p["routing"])
    assert "$teardrop" not in pcb.read_text(encoding="utf-8"), "stale teardrop zones must go with their tracks"
    k = kicad_drc(pcb)                         # KiCad refills the zones itself: no stray teardrop island, no errors
    assert not k["errors"] and k["unconnected"] == 0 and o["drc"]["unconnected"] == 0, k
    # ripup='nets' only takes the teardrops of nets that lose all their tracks
    d = build("tear2", "pour", "teardrop")
    pcb = Path(d) / "demo.kicad_pcb"
    res = worker.run("guided_board", {"pcb": str(pcb), "save": False,
                                      "ops": [{"op": "ripup", "mode": "nets", "nets": ["/D1"]},
                                              {"op": "ripup", "mode": "nets", "nets": ["/D0"]}]}, timeout=120)["results"]
    assert res[0]["teardrops_removed"] == 0 and res[1] == {"removed": 1, "kept_locked": 0, "teardrops_removed": 1}, res


def test_e2e_worker_never_blocks_on_a_wx_dialog():
    """A board with text in a missing font made pcbnew pop up a modal 'font not found' dialog: the headless
    worker hung until its 10 minute timeout. The worker must load such a board in seconds."""
    if not have_kicad():
        return
    d = build("font", "font")
    pcb = Path(d) / "demo.kicad_pcb"
    assert "No Such Font 4711" in pcb.read_text(encoding="utf-8")
    import time as _t
    t = _t.time()
    res = worker.run("guided_board", {"pcb": str(pcb), "save": False, "ops": [{"op": "stats"}]}, timeout=90)["results"]
    assert res[0]["unconnected"] > 0 and _t.time() - t < 60
    p, _ = call("pcb_route_plan", project=d, heatmap=False)
    assert p["routing"]["unrouted_connections"] > 0


if __name__ == "__main__":
    import time
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
