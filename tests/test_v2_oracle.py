"""Router v2 oracle (design §4.1): the single exact legality test.

Pure tests on synthetic boards (no KiCad), the one_air_max regression (design Appendix A1), and the
differential test against kicad-cli DRC (seeded at-the-rule ladder + corpus references; needs KiCad 10
and the benchmark corpus, skipped otherwise).

Run:  python tests/test_v2_oracle.py            pure tests + seeded ladder + 4 small / instructive references
      python tests/test_v2_oracle.py --full     ... + every corpus reference (about a minute)
      python tests/test_v2_oracle.py test_name  one test
Scratch files go to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench" / "route"))
import common  # noqa: E402  (bench/route/common.py)

common.sandbox()       # KICAD_CONFIG_HOME, COPPER_HOME and the temp dir -> bench/route/.work, before copper_kicad loads
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import geom, model  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2.types import TOL_MM, Blocker, Piece  # noqa: E402

FULL = "--full" in sys.argv or os.environ.get("COPPER_V2_ORACLE_FULL") == "1"
UM = 0.001


# ── synthetic boards (no KiCad) ───────────────────────────────────────────
def board(pads=(), *, size=(40.0, 30.0), origin=(0.0, 0.0), layers=("F.Cu", "B.Cu"), tracks=(), vias=(), zones=(),
          mask_openings=(), nets=None, rules=None):
    """pads: dicts {ref, net, x, y, hw, hh, r=0, rot=0, kind='smd'|'tht'|'npth', hole=radius, clearance, num='1'}.
    nets: {name: {clearance, dp_gap}} (default class: clearance 0.2, pair gap 0.25)."""
    names = sorted({p["net"] for p in pads if p.get("net")} | set(nets or {}) | {t[0] for t in tracks} | {v[0] for v in vias})
    R = {"clearance": 0.1, "track_width": 0.1, "via": 0.4, "drill": 0.2, "edge_clearance": 0.3, "hole_clearance": 0.25,
         "hole_to_hole": 0.25, "annular": 0.05,
         "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.25, "dp_width": 0.2}}
    R.update(rules or {})
    x0, y0 = origin
    ex = {"layers": list(layers), "rules": R,
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.25, "dp_width": 0.2, **((nets or {}).get(n) or {})} for i, n in enumerate(names)},
          "outline": {"closed": True, "polys": [{"o": [[x0, y0], [x0 + size[0], y0], [x0 + size[0], y0 + size[1]],
                                                       [x0, y0 + size[1]]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [list(t) for t in tracks], "vias": [list(v) for v in vias],
          "zones": [dict(z) for z in zones], "mask_openings": [dict(m) for m in mask_openings],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    allL = list(range(len(layers)))
    for i, p in enumerate(pads):
        kind = p.get("kind", "smd")
        lays = allL if kind in ("tht", "npth") else [p.get("layer", 0)]
        a = math.radians(p.get("rot", 0.0))
        d = {"ref": p["ref"], "num": p.get("num", "1" if kind != "npth" else ""), "net": p.get("net", ""), "x": p["x"],
             "y": p["y"], "kind": kind, "layers": lays, "c": i + 1}
        if kind != "npth":
            if "poly" in p:
                d["shapes"] = [[lays, "poly", [p["poly"]]]]
            else:
                d["shapes"] = [[lays, "box", [p["x"], p["y"], round(math.cos(a), 6), round(-math.sin(a), 6), p["hw"], p["hh"],
                                              p.get("r", 0.0)]]]
        if p.get("hole"):
            d["hole"] = [p["x"], p["y"], 1.0, 0.0, p["hole"], p["hole"]]
        if p.get("clearance"):
            d["clearance"] = p["clearance"]
        ex["pads"].append(d)
    return model.Board(ex)


def keepout(x0, y0, x1, y1, tracks=True, vias=True, layers=(0, 1)):
    return {"name": "ko", "layers": list(layers), "rule_area": True, "no_tracks": tracks, "no_vias": vias,
            "polys": [{"o": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}]}


PA = {"ref": "PA", "net": "A", "x": 10.0, "y": 10.0, "hw": 0.5, "hh": 0.3}      # lower edge at y = 10.3
PB = {"ref": "PB", "net": "B", "x": 30.0, "y": 25.0, "hw": 0.3, "hh": 0.3}
W = 0.2


def below(o, y_edge, gap, net="B", x=10.0, layer=0, w=W, **kw):
    """seg_ok of a horizontal track of width w whose copper edge is ``gap`` below y_edge."""
    y = y_edge + gap + w / 2
    return o.seg_ok(layer, x - 2.0, y, x + 2.0, y, w, net, **kw)


def seg(pid, net, layer, x1, y1, x2, y2, w=0.25, **kw):
    return Piece(id=pid, net=net, conn=kw.pop("conn", pid), kind="seg", layer=layer, x1=x1, y1=y1, x2=x2, y2=y2, w=w, **kw)


def via(pid, net, x, y, size=0.6, drill=0.3, **kw):
    return Piece(id=pid, net=net, conn=kw.pop("conn", pid), kind="via", layer=-1, x1=x, y1=y, x2=x, y2=y, w=size,
                 drill=drill, **kw)


# ── the rule distance ─────────────────────────────────────────────────────
def test_gap_at_the_rule_is_legal_and_2um_under_is_not():
    o = O.Oracle(board([PA, PB]))
    assert below(o, 10.3, 0.2) is None                                  # exactly at the rule
    assert below(o, 10.3, 0.2 + 2 * UM) is None
    b = below(o, 10.3, 0.2 - 2 * UM)
    assert isinstance(b, Blocker) and b.rule == "clearance" and b.tag == "PA.1" and b.net == "A" and b.static
    assert b.piece is None and b.need == 0.2 and abs(b.dist - 0.198) < 1e-9 and b.layer == 0
    assert abs(b.at[0] - 10.0) < 1e-9 and abs(b.at[1] - (10.3 + 0.198 + 0.1)) < 1e-6
    # the calibrated tolerance: KiCad waives up to 0.5 um; the oracle waives TOL_MM (0.3 um) and nothing more
    assert TOL_MM == 0.0003
    assert below(o, 10.3, 0.2 - 0.2 * UM) is None
    assert below(o, 10.3, 0.2 - 0.3 * UM) is None
    assert below(o, 10.3, 0.2 - 0.4 * UM) is not None
    strict = O.Oracle(board([PA, PB]), tol=0.0)
    assert below(strict, 10.3, 0.2) is None and below(strict, 10.3, 0.2 - 0.1 * UM) is not None
    # an overlap is a short, on the right layer only; the pad is not on B.Cu
    assert below(o, 10.3, -0.25).rule == "short"
    assert below(o, 10.3, -0.25, layer=1) is None
    # the track's own net never conflicts with its pad
    assert below(o, 10.3, -0.25, net="A") is None
    assert o.seg_legal(0, 8.0, 10.6, 12.0, 10.6, W, "B") and not o.seg_legal(0, 8.0, 10.598, 12.0, 10.598, W, "B")


def test_coordinates_are_judged_at_1nm():
    o = O.Oracle(board([PA, PB]), tol=0.0)
    y = 10.3 + 0.2 + 0.1
    assert o.seg_ok(0, 8.0, y + 4e-10, 12.0, y - 4e-10, W, "B") is None           # sub-nanometre noise is not a violation
    assert o.seg_ok(0, 8.0, y - 1e-6, 12.0, y - 1e-6, W, "B") is not None         # one nanometre is
    # a 45 degree track at the rule past a pad corner: the answer flips within 2 nm of the exact distance
    c = (10.5, 10.3)
    r = 0.2 + 0.1

    def diag(off):
        g = (r + off) * math.sqrt(2)
        return o.seg_ok(0, c[0] + g + 1.5, c[1] - 1.5, c[0] + g - 1.5, c[1] + 1.5, W, "B")
    assert diag(2e-6) is None and diag(-2e-6) is not None


def test_round_and_rotated_shapes_are_exact():
    pads = [{"ref": "C", "net": "A", "x": 10.0, "y": 10.0, "hw": 0.45, "hh": 0.45, "r": 0.45},            # circle
            {"ref": "RR", "net": "A", "x": 20.0, "y": 10.0, "hw": 0.5, "hh": 0.4, "r": 0.2},              # round-rect
            {"ref": "X", "net": "A", "x": 30.0, "y": 10.0, "hw": 0.5, "hh": 0.3, "rot": 30.0}, PB]
    o = O.Oracle(board(pads), tol=0.0)
    # circle: a vertical track beside it
    x = 10.0 + 0.45 + 0.2 + 0.1
    assert o.seg_ok(0, x, 8.0, x, 12.0, W, "B") is None and o.seg_ok(0, x - UM, 8.0, x - UM, 12.0, W, "B") is not None
    # round-rect corner: 45 degree track past the corner arc (centre (20.3, 10.2), radius 0.2)
    def diag(off):
        g = (0.2 + 0.2 + 0.1 + off) * math.sqrt(2)
        return o.seg_ok(0, 20.3 + g + 1.5, 10.2 - 1.5, 20.3 + g - 1.5, 10.2 + 1.5, W, "B")
    assert diag(UM) is None and diag(-UM) is not None
    # rotated rectangle: vertical track right of its rightmost corner
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    xr = 30.0 + 0.5 * c + 0.3 * s + 0.2 + 0.1
    assert o.seg_ok(0, xr + 1e-5, 8.0, xr + 1e-5, 12.0, W, "B") is None
    assert o.seg_ok(0, xr - UM, 8.0, xr - UM, 12.0, W, "B") is not None


# ── every rule of need() ──────────────────────────────────────────────────
def test_pad_clearance_overrides_replace_the_net_class():
    pads = [dict(PA, ref="HI", clearance=0.35), dict(PA, ref="LO", x=20.0, clearance=0.15),
            dict(PA, ref="MIN", x=30.0, clearance=0.05), dict(PA, ref="FP", y=20.0), PB]
    o = O.Oracle(board(pads, nets={"C": {"clearance": 0.3}}), overrides={"fp_clearance": {"FP": 0.3}})
    assert below(o, 10.3, 0.35) is None and below(o, 10.3, 0.35 - UM).need == 0.35            # above the class
    assert below(o, 10.3, 0.15, x=20.0) is None and below(o, 10.3, 0.15 - UM, x=20.0).need == 0.15   # below the class
    assert below(o, 10.3, 0.1, x=30.0) is None and below(o, 10.3, 0.1 - UM, x=30.0).need == 0.1      # floored at the board minimum
    assert below(o, 20.3, 0.3) is None and below(o, 20.3, 0.3 - UM).need == 0.3                      # footprint-level
    # the override also beats a WIDER class of the probing net (KiCad: an override replaces the classes)
    assert below(o, 10.3, 0.15, x=20.0, net="C") is None
    # ... unless the plan asks this track for more
    assert below(o, 10.3, 0.15, x=20.0, net="C", clearance=0.4).need == 0.4
    # without the file overrides the footprint value is unknown: the class applies
    plain = O.Oracle(board(pads))
    assert below(plain, 20.3, 0.2) is None and below(plain, 20.3, 0.2 - UM).need == 0.2


def test_net_class_clearance_is_the_larger_of_the_two():
    b = board([PA, PB], nets={"C": {"clearance": 0.3}}, tracks=[["C", 0, 5.0, 20.0, 15.0, 20.0, 0.25, 0, 0]])
    o = O.Oracle(b)
    assert below(o, 20.125, 0.3) is None                               # B (0.2) next to C (0.3): 0.3
    bl = below(o, 20.125, 0.3 - UM)
    assert bl.need == 0.3 and bl.tag == "track" and bl.net == "C" and bl.static
    assert below(o, 10.3, 0.3, net="C") is None and below(o, 10.3, 0.3 - UM, net="C").need == 0.3   # C next to pad A (0.2)


def test_coupled_differential_pair_uses_the_pair_gap():
    cls = {"clearance": 0.25, "dp_gap": 0.16}
    b = board([PA, PB], nets={"USB_P": cls, "USB_N": cls, "ETH1_N": cls, "ETH1_P": dict(cls, dp_gap=0.4)},
              tracks=[["USB_P", 0, 5.0, 20.0, 15.0, 20.0, 0.25, 0, 0], ["ETH1_P", 0, 25.0, 20.0, 35.0, 20.0, 0.25, 0, 0]])
    o = O.Oracle(b)
    assert below(o, 20.125, 0.16, net="USB_N") is None and below(o, 20.125, 0.16 - UM, net="USB_N").need == 0.16
    assert below(o, 20.125, 0.25) is None and below(o, 20.125, 0.25 - UM).need == 0.25          # net B is not the partner
    # a pair gap that is not below the clearance changes nothing
    assert below(o, 20.125, 0.25, net="ETH1_N", x=30.0) is None
    assert below(o, 20.125, 0.25 - UM, net="ETH1_N", x=30.0).need == 0.25
    # dynamic copper too
    o.add_piece(seg(1, "USB_N", 1, 5.0, 12.0, 15.0, 12.0))
    assert below(o, 12.125, 0.16, net="USB_P", layer=1) is None and below(o, 12.125, 0.158, net="USB_P", layer=1) is not None
    for a, want in (("USB_D+", "USB_D-"), ("/A/TX_P", "/A/TX_N"), ("ETH_N1", "ETH_P1"), ("D_P_2", "D_N_2"), ("P", "N"),
                    ("RESET", None), ("GPIO1", None), ("", None), ("CLK-", "CLK+"), ("DN12_", "DP12_")):
        assert O.dp_complement(a) == want, (a, O.dp_complement(a))


def test_holes_npth_and_hole_to_hole():
    pads = [{"ref": "H1", "kind": "npth", "x": 10.0, "y": 10.0, "hole": 0.5},
            {"ref": "T1", "kind": "tht", "net": "A", "x": 20.0, "y": 10.0, "hw": 0.8, "hh": 0.8, "r": 0.8, "hole": 0.4}, PB]
    o = O.Oracle(board(pads))
    # track to NPTH: hole clearance 0.25 from the hole edge, on every layer
    for lay in (0, 1):
        assert below(o, 10.5, 0.25, layer=lay) is None
        b = below(o, 10.5, 0.25 - UM, layer=lay)
        assert b.rule == "hole" and b.need == 0.25 and b.tag == "H1" and b.net is None
    # via (0.6 / 0.3) to NPTH: copper to hole 0.25 binds (0.25 + 0.3 > 0.25 + 0.15)
    assert o.via_ok(10.0, 10.5 + 0.25 + 0.3, 0.6, 0.3, "B") is None
    assert o.via_ok(10.0, 10.5 + 0.25 + 0.3 - UM, 0.6, 0.3, "B").rule == "hole"
    # same-net via next to the plated hole of its own pad: copper is free, drill to drill is not (hole to hole 0.25)
    y = 10.0 + 0.4 + 0.25 + 0.15
    assert o.via_ok(20.0, y, 0.6, 0.3, "A") is None
    b = o.via_ok(20.0, y - UM, 0.6, 0.3, "A")
    assert b.rule == "hole" and b.tag == "T1.1" and abs((b.dist + 0.3 - 0.15) - (0.25 - UM)) < 1e-6
    # a same-net track runs through its own plated hole; a foreign one keeps pad clearance (the pad is bigger than the hole)
    assert o.seg_ok(1, 18.0, 10.0, 22.0, 10.0, W, "A") is None
    assert below(o, 10.8, 0.2, x=20.0, layer=1) is None and below(o, 10.8, 0.198, x=20.0, layer=1).rule == "clearance"
    # with hole-to-hole 0.6 the drill rule binds against the NPTH: 0.5 + 0.6 + 0.15 from the centre
    o2 = O.Oracle(board(pads, rules={"hole_to_hole": 0.6}))
    assert o2.via_ok(10.0, 10.0 + 0.5 + 0.6 + 0.15, 0.6, 0.3, "B") is None
    assert o2.via_ok(10.0, 10.0 + 0.5 + 0.6 + 0.15 - UM, 0.6, 0.3, "B").rule == "hole"


def test_pad_clearance_override_applies_to_its_hole():
    """KiCad tests copper against an NPTH pad's hole with the pad's own clearance when that is the larger one
    ("hole_clearance (pad clearance 1.7 mm; actual 0.97 mm)" next to a module's mounting hole)."""
    pads = [{"ref": "H1", "kind": "npth", "x": 10.0, "y": 10.0, "hole": 0.5, "clearance": 1.0}, PB]
    o = O.Oracle(board(pads))
    for lay in (0, 1):
        assert below(o, 10.5, 1.0, layer=lay) is None
        b = below(o, 10.5, 1.0 - UM, layer=lay)
        assert b is not None and b.rule == "hole" and abs(b.need - 1.0) < 1e-9 and b.tag == "H1"
    # an override smaller than the board's hole clearance changes nothing
    pads[0] = dict(pads[0], clearance=0.1)
    o = O.Oracle(board(pads))
    assert below(o, 10.5, 0.25) is None and below(o, 10.5, 0.25 - UM).need == 0.25
    # a footprint-level override reaches the hole as well
    pads[0] = {"ref": "H1", "kind": "npth", "x": 10.0, "y": 10.0, "hole": 0.5}
    o = O.Oracle(board(pads), overrides={"fp_clearance": {"H1": 0.8}})
    assert below(o, 10.5, 0.8) is None and below(o, 10.5, 0.8 - UM).rule == "hole"


def test_via_copper_hole_and_dynamic_vias():
    o = O.Oracle(board([PA, PB]))
    assert o.via_ok(10.0, 10.3 + 0.2 + 0.3, 0.6, 0.3, "B") is None
    b = o.via_ok(10.0, 10.3 + 0.2 + 0.3 - UM, 0.6, 0.3, "B")
    assert b.rule == "clearance" and b.tag == "PA.1" and b.layer == 0
    assert o.via_ok(10.0, 10.0, 0.6, 0.3, "A") is None                        # on its own pad: legal (policy is the caller's)
    # a via with a thin annulus (0.6 / 0.56): its own drill needs 0.25 to foreign copper -> 0.23 from the via's copper
    assert o.via_ok(10.0, 10.3 + 0.23 + 0.3, 0.6, 0.56, "B") is None
    b = o.via_ok(10.0, 10.3 + 0.23 + 0.3 - UM, 0.6, 0.56, "B")
    assert b.rule == "hole" and abs(b.need - 0.23) < 1e-9
    # dynamic via of net A at (20, 20): copper on every layer, hole clearance from its drill
    o.add_piece(via(7, "A", 20.0, 20.0, 0.5, 0.45))
    for lay in (0, 1):
        y = 20.0 + 0.225 + 0.25 + 0.1                                          # hole (0.225 + 0.25) beats copper (0.25 + 0.2)
        assert o.seg_ok(lay, 18.0, y, 22.0, y, W, "B") is None
        b = o.seg_ok(lay, 18.0, y - UM, 22.0, y - UM, W, "B")
        assert b.rule == "hole" and b.piece == 7 and not b.static and b.tag == "via"
    assert o.seg_ok(0, 18.0, 20.0, 22.0, 20.0, W, "A") is None
    # via to via: copper clearance between different nets, hole to hole between the same net
    # (A's drill to B's copper binds: 0.225 + 0.25 + 0.3 > A's copper 0.25 + 0.2 + 0.3)
    assert o.via_ok(20.0, 20.0 + 0.225 + 0.25 + 0.3 - UM, 0.6, 0.3, "B").rule == "hole"
    assert o.via_ok(20.0, 20.0 + 0.225 + 0.25 + 0.3, 0.6, 0.3, "B") is None
    assert o.via_ok(20.0, 20.0 + 0.225 + 0.25 + 0.15, 0.6, 0.3, "A") is None
    assert o.via_ok(20.0, 20.0 + 0.225 + 0.25 + 0.15 - UM, 0.6, 0.3, "A").rule == "hole"


def test_board_edge_and_outside():
    o = O.Oracle(board([PA, PB]))
    x = 0.3 + 0.1
    assert o.seg_ok(0, x, 5.0, x, 25.0, W, "B") is None
    b = o.seg_ok(0, x - UM, 5.0, x - UM, 25.0, W, "B")
    assert b.rule == "edge" and b.tag == "edge" and b.need == 0.3 and b.static
    assert o.seg_ok(1, 5.0, 30.0 - 0.3 - 0.1 + UM, 25.0, 30.0 - 0.3 - 0.1 + UM, W, "B").rule == "edge"
    assert o.seg_ok(0, 35.0, 15.0, 45.0, 15.0, W, "B").rule == "edge"          # crosses the edge
    assert o.seg_ok(0, 50.0, 15.0, 55.0, 15.0, W, "B").rule == "edge"          # far outside: no edge near, still illegal
    assert o.via_ok(0.3 + 0.3, 15.0, 0.6, 0.3, "B") is None
    assert o.via_ok(0.3 + 0.3 - UM, 15.0, 0.6, 0.3, "B").rule == "edge"
    assert o.via_ok(-5.0, 15.0, 0.6, 0.3, "B").rule == "edge"
    assert not o.seg_legal(0, 50.0, 15.0, 55.0, 15.0, W, "B") and not o.via_legal(-5.0, 15.0, 0.6, 0.3, "B")
    # a cut-out in the board is an edge too
    ex = dict(board([PA, PB]).ex)
    ex["outline"] = {"closed": True, "polys": [{"o": [[0, 0], [40, 0], [40, 30], [0, 30]],
                                                "h": [[[18, 13], [22, 13], [22, 17], [18, 17]]]}]}
    o2 = O.Oracle(model.Board(ex))
    assert o2.seg_ok(0, 16.0, 17.0 + 0.4, 24.0, 17.0 + 0.4, W, "B") is None
    assert o2.seg_ok(0, 16.0, 17.0 + 0.4 - UM, 24.0, 17.0 + 0.4 - UM, W, "B").rule == "edge"
    assert o2.seg_ok(0, 19.0, 15.0, 21.0, 15.0, W, "B").rule == "edge"         # inside the cut-out


def test_keep_outs_are_strict_and_per_item_kind():
    zones = [keepout(20, 20, 24, 24), keepout(5, 20, 9, 24, vias=False), keepout(30, 5, 34, 9, tracks=False),
             keepout(12, 20, 16, 24, layers=(1,))]
    o = O.Oracle(board([PA, PB], zones=zones))
    assert below(o, 24.0, 0.0, x=22.0) is None                                 # touching the keep-out is legal
    b = below(o, 24.0, -0.1 * UM, x=22.0)                                      # 0.1 um inside is not: KiCad has no epsilon here
    assert b.rule == "keepout" and b.tag == "keepout" and b.need == 0.0 and b.net is None
    assert o.seg_ok(0, 21.0, 22.0, 23.0, 22.0, W, "B").rule == "keepout"       # fully inside
    assert o.via_ok(22.0, 24.0 + 0.3, 0.6, 0.3, "B") is None and o.via_ok(22.0, 24.0 + 0.3 - UM, 0.6, 0.3, "B").rule == "keepout"
    # no-tracks only: a via may sit there; no-vias only: a track may cross
    assert o.seg_ok(0, 6.0, 22.0, 8.0, 22.0, W, "B").rule == "keepout" and o.via_ok(7.0, 22.0, 0.6, 0.3, "B") is None
    assert o.seg_ok(0, 31.0, 7.0, 33.0, 7.0, W, "B") is None and o.via_ok(32.0, 7.0, 0.6, 0.3, "B").rule == "keepout"
    # a keep-out on B.Cu only: free on F.Cu, closed on B.Cu, and closed to through vias
    assert o.seg_ok(0, 13.0, 22.0, 15.0, 22.0, W, "B") is None and o.seg_ok(1, 13.0, 22.0, 15.0, 22.0, W, "B").rule == "keepout"
    assert o.via_ok(14.0, 22.0, 0.6, 0.3, "B").rule == "keepout"
    # keep-outs bind every net
    assert o.seg_ok(0, 21.0, 22.0, 23.0, 22.0, W, "A").rule == "keepout"


def test_mask_graphics_and_solder_mask_bridge():
    art = [{"layer": 0, "poly": [[20, 20], [24, 20], [24, 24], [20, 24]]}]
    o = O.Oracle(board([PA, PB], mask_openings=art))
    b = o.seg_ok(0, 21.0, 22.0, 23.0, 22.0, W, "B")
    assert b.rule == "mask" and b.tag == "mask"
    assert o.seg_ok(1, 21.0, 22.0, 23.0, 22.0, W, "B") is None                # the opening is on the front only
    assert o.via_ok(22.0, 22.0, 0.6, 0.3, "B").rule == "mask"
    o.mask_openings_hard = False                                               # KiCad itself allows one net under an opening
    assert o.seg_ok(0, 21.0, 22.0, 23.0, 22.0, W, "B") is None
    # solder-mask bridge: a foreign pad's mask opening (margin 0.3 + half the 0.1 web) reaches further than its clearance
    ov = {"mask_margin": 0.05, "mask_web": 0.1, "pad_mask_margin": {("PA", "1"): 0.3}}
    o = O.Oracle(board([PA, PB, dict(PA, ref="PC", x=20.0), dict(PA, ref="PD", x=10.0, y=20.0, layer=1)]), overrides=ov)
    assert below(o, 10.3, 0.35) is None
    b = below(o, 10.3, 0.35 - 0.1 * UM)                                        # no tolerance on the mask rule
    assert b.rule == "mask" and abs(b.need - 0.35) < 1e-12 and b.tag == "PA.1"
    assert below(o, 10.3, 0.35 - UM, net="A") is None                          # same net: nothing to bridge
    assert below(o, 10.3, 0.2, x=20.0) is None                                 # board margin 0.05 + 0.05 < clearance 0.2
    assert below(o, 10.3, 0.2 - UM, x=20.0).rule == "clearance"
    assert below(o, 20.3, 0.2, layer=1) is None                                # back-side pad: board margin, bottom layer is outer
    off = O.Oracle(board([PA, PB]))                                            # no overrides dict: the mask need is off
    assert below(off, 10.3, 0.2) is None


def test_poly_pads_and_static_copper():
    tri = {"ref": "TP", "net": "A", "x": 10.0, "y": 10.0, "poly": [[9.0, 9.0], [11.0, 9.0], [10.0, 11.0]]}
    b = board([tri, PB], tracks=[["A", 0, 5.0, 20.0, 15.0, 20.0, 0.25, 0, 0]], vias=[["A", 25.0, 10.0, 0.8, 0.4, 0, 1, 0, 0]])
    o = O.Oracle(b)
    assert below(o, 11.0, 0.2) is None and below(o, 11.0, 0.198).tag == "TP.1"            # the apex
    assert o.seg_ok(0, 9.5, 9.5, 10.5, 9.5, W, "B").rule == "short"                       # inside the polygon
    assert below(o, 20.125, 0.2) is None and below(o, 20.125, 0.198).tag == "track"
    bl = o.seg_ok(1, 23.0, 10.0 + 0.4 + 0.198 + 0.1, 27.0, 10.0 + 0.4 + 0.198 + 0.1, W, "B")
    assert bl.tag == "via" and bl.rule == "clearance" and bl.static
    assert o.seg_ok(1, 23.0, 10.0, 27.0, 10.0, W, "A") is None


# ── dynamic copper ────────────────────────────────────────────────────────
def test_same_net_tee_is_legal_and_foreign_crossing_is_a_short():
    o = O.Oracle(board([PA, PB]))
    o.add_piece(seg(1, "A", 0, 15.0, 15.0, 25.0, 15.0))
    assert o.seg_ok(0, 20.0, 12.0, 20.0, 15.0, W, "A") is None                 # tee onto the track
    assert o.seg_ok(0, 20.0, 12.0, 20.0, 18.0, W, "A") is None                 # crossing its own net
    b = o.seg_ok(0, 20.0, 12.0, 20.0, 18.0, W, "B")
    assert b.rule == "short" and b.piece == 1 and not b.static and b.tag == "track" and b.net == "A" and b.dist < 0
    assert o.seg_ok(1, 20.0, 12.0, 20.0, 18.0, W, "B") is None                 # other layer
    assert below(o, 15.125, 0.2, x=20.0) is None and below(o, 15.125, 0.198, x=20.0).rule == "clearance"


def test_add_remove_restores_answers_and_bumps_version():
    o = O.Oracle(board([PA, PB]))
    q0 = [(0, 18.0, 15.3, 22.0, 15.3, W, "B"), (1, 18.0, 15.3, 22.0, 15.3, W, "B"), (0, 20.0, 12.0, 20.0, 18.0, W, "A")]
    before = [o.seg_ok(*a) for a in q0]
    vbefore = o.via_ok(20.0, 15.5, 0.6, 0.3, "B")
    assert before == [None, None, None] and vbefore is None
    v0 = o.version
    p1, p2 = seg(1, "A", 0, 15.0, 15.0, 25.0, 15.0), via(2, "A", 20.0, 15.0)
    o.add_piece(p1)
    o.add_piece(p2)
    assert o.version == v0 + 2 and o.has_piece(1) and o.has_piece(2)
    assert o.seg_ok(*q0[0]).piece in (1, 2) and o.seg_ok(*q0[1]).piece == 2 and o.seg_ok(*q0[2]) is None
    assert o.via_ok(20.0, 15.5, 0.6, 0.3, "B") is not None
    # ignore: look through chosen pieces (a re-route of them)
    assert o.seg_ok(*q0[1], ignore=frozenset({2})) is None
    assert o.seg_ok(*q0[0], ignore=frozenset({2})).piece == 1 and o.seg_ok(*q0[0], ignore=frozenset({1, 2})) is None
    assert o.piece_ok(p1) is None and o.piece_ok(p2) is None                   # a piece never blocks itself
    try:
        o.add_piece(p1)
        raise AssertionError("adding a piece twice must be refused")
    except ValueError:
        pass
    o.remove_piece(p2)
    o.remove_piece(p1)
    o.remove_piece(p1)                                                         # removing twice is a no-op
    assert o.version == v0 + 4 and not o.has_piece(1)
    assert [o.seg_ok(*a) for a in q0] == before and o.via_ok(20.0, 15.5, 0.6, 0.3, "B") is None
    # many add / remove cycles leave nothing behind
    for k in range(50):
        p = seg(100 + k, "A", k % 2, 5.0 + k * 0.1, 15.0, 30.0, 15.0 + k * 0.05)
        o.add_piece(p)
        o.remove_piece(p)
    assert [o.seg_ok(*a) for a in q0] == before and not any(o._dyn.b.values())


def test_answers_do_not_depend_on_insertion_order():
    pieces = [seg(1, "A", 0, 15.0, 15.0, 25.0, 15.0), seg(2, "C", 0, 15.0, 15.6, 25.0, 15.6), via(3, "A", 20.0, 16.4),
              seg(4, "C", 0, 19.0, 14.0, 19.0, 13.0)]
    probe = (0, 20.0, 15.3, 20.0, 16.0, W, "B")

    def answers(order, churn=False):
        o = O.Oracle(board([PA, PB], nets={"C": {}}))
        for i in order:
            o.add_piece(pieces[i])
        if churn:
            for i in order[:2]:
                o.remove_piece(pieces[i])
            for i in reversed(order[:2]):
                o.add_piece(pieces[i])
        return o.seg_ok(*probe), o.blockers(*probe), o.via_ok(20.0, 15.3, 0.6, 0.3, "B"), \
            [(s.tag, s.cls) for s in o.near(0, (15, 13, 25, 17), 0.5) if s.cls != "edge" and isinstance(s.tag, int)]
    ref = answers([0, 1, 2, 3])
    assert ref[0] is not None and len(ref[1]) >= 3
    for order in ([3, 2, 1, 0], [2, 0, 3, 1], [1, 3, 0, 2]):
        assert answers(order) == ref and answers(order, churn=True) == ref
    assert [b.dist for b in ref[1]] == sorted(b.dist for b in ref[1])           # nearest first
    assert ref[3] == sorted(ref[3])                                             # dynamic shapes by piece id


def test_plan_clearance_override():
    o = O.Oracle(board([PA, PB]))
    o.set_clearance("B", 0.4)
    assert o.clearance("B") == 0.4 and o.clearance("A") == 0.2
    assert below(o, 10.3, 0.4) is None and below(o, 10.3, 0.4 - UM).need == 0.4
    o.set_clearance("B", 0.05)                                                 # never below the class
    assert o.clearance("B") == 0.2
    o.set_clearance("B", 0.4)
    o.add_piece(seg(1, "B", 0, 15.0, 15.0, 25.0, 15.0))                        # B's copper carries its 0.4 to others
    assert below(o, 15.125, 0.4, net="A", x=20.0) is None and below(o, 15.125, 0.4 - UM, net="A", x=20.0).need == 0.4


# ── soft shapes ───────────────────────────────────────────────────────────
def test_soft_shapes_never_block_until_hardened():
    o = O.Oracle(board([PA, PB]))
    v0 = o.version
    spoke = (10.0, 11.0, 0.0, 1.0, 0.7, 0.25, 0.0)                             # a 0.5 mm wide corridor below pad A
    sid = o.add_soft("thermal", "A", 0, spoke)
    site = o.add_soft("reserve", "A", -1, geom.Shape("box", geom.box_circle(20.0, 20.0, 0.5), "cu", 0, 0.0, (0,)))
    assert o.version == v0 + 2 and sid != site
    cross = (0, 8.0, 11.0, 12.0, 11.0, W, "B")
    assert o.seg_ok(*cross) is None                                            # soft: legal
    hits = o.soft_hits(*cross)
    assert len(hits) == 1 and hits[0].rule == "thermal" and hits[0].net == "A" and hits[0].tag == f"soft:{sid}"
    assert hits[0].need == 0.0 and hits[0].dist < 0 and hits[0].piece is None
    assert o.soft_hits(0, 8.0, 11.0, 12.0, 11.0, W, "A") == []                 # the owner is never charged
    assert o.soft_hits(1, 8.0, 11.0, 12.0, 11.0, W, "B") == []                 # other layer
    assert o.soft_hits(0, 8.0, 12.0, 12.0, 12.0, W, "B") == []                 # beside the corridor
    assert [b.rule for b in o.blockers(*cross)] == ["thermal"]                 # blockers() lists soft crossings too
    for lay in (0, 1):                                                         # the reserved via site is on every layer
        assert [h.rule for h in o.soft_hits(lay, 20.0, 20.0, 20.0, 20.0, 0.6, "B")] == ["reserve"]
    assert [b.rule for b in o.via_blockers(20.0, 20.0, 0.6, 0.3, "B")] == ["reserve", "reserve"]
    assert o.via_ok(20.0, 20.0, 0.6, 0.3, "B") is None
    assert [s.tag for s in o.near_soft(0, (0, 0, 40, 30), 0.0)] == [("thermal", sid), ("reserve", site)]
    assert all(s.cls != "soft" for s in o.near(0, (0, 0, 40, 30), 0.0))
    o.harden(sid)
    o.harden(sid)
    b = o.seg_ok(*cross)
    assert b.rule == "thermal" and b.net == "A" and b.need == 0.0 and b.static
    assert o.seg_ok(0, 8.0, 11.0, 12.0, 11.0, W, "A") is None                  # still open to its owner
    assert o.soft_hits(*cross) == [] and any(s.cls == "soft" for s in o.near(0, (0, 0, 40, 30), 0.0))
    assert o.need("B", 0.1, o.near(0, (9, 10.5, 11, 11.5), 0.0)[-1], 0) == 0.0
    o.remove_soft(sid)
    o.remove_soft(site)
    assert o.seg_ok(*cross) is None and o.soft_hits(0, 20.0, 20.0, 20.0, 20.0, 0.6, "B") == []
    try:
        o.add_soft("clearance", "A", 0, spoke)
        raise AssertionError("only thermal / reserve are soft rules")
    except ValueError:
        pass
    assert o.add_soft("reserve", "A", 0, [(1, 1), (2, 1), (2, 2)]) > site       # a polygon works too


# ── helpers for the other stages ──────────────────────────────────────────
def test_need_near_and_pad_gap():
    pads = [PA, PB, {"ref": "H1", "kind": "npth", "x": 20.0, "y": 10.0, "hole": 0.5},
            {"ref": "PX", "net": "C", "x": 11.5, "y": 10.0, "hw": 0.5, "hh": 0.3},                 # 0.5 mm right of PA
            {"ref": "C1", "net": "A", "x": 10.0, "y": 20.0, "hw": 0.4, "hh": 0.4, "r": 0.4},
            {"ref": "C2", "net": "B", "x": 13.0, "y": 24.0, "hw": 0.6, "hh": 0.6, "r": 0.6},
            {"ref": "RX", "net": "B", "x": 20.0, "y": 20.0, "hw": 0.5, "hh": 0.3, "rot": 45.0},
            {"ref": "RY", "net": "C", "x": 22.0, "y": 20.0, "hw": 0.5, "hh": 0.3},
            {"ref": "BK", "net": "C", "x": 30.0, "y": 10.0, "hw": 0.5, "hh": 0.3, "layer": 1}]
    b = board(pads, zones=[keepout(30, 20, 34, 24)], nets={"C": {"clearance": 0.3}})
    o = O.Oracle(b)
    pa = b.pads[[p.name for p in b.pads].index("PA.1")].shapes[0][0]
    hole = [s for s in b.shapes if s.cls == "hole"][0]
    kt = [s for s in b.shapes if s.cls == "kt"][0]
    kv = [s for s in b.shapes if s.cls == "kv"][0]
    assert o.need("B", 0.1, pa, 0) == 0.2 and o.need("C", 0.1, pa, 0) == 0.3 and o.need("A", 0.1, pa, 0) == O.NO_NEED
    assert o.need("B", 0.1, pa, 0, clearance=0.5) == 0.5
    assert o.need("B", 0.3, pa, 0, kind="via", drill=0.3) == 0.2
    assert abs(o.need("B", 0.3, pa, 0, kind="via", drill=0.56) - 0.23) < 1e-12        # the via's own drill
    assert o.need("B", 0.1, hole, 0) == 0.25
    assert o.need("B", 0.3, hole, 0, kind="via", drill=0.3) == 0.25                   # max(0.25, 0.25 + 0.15 - 0.3)
    assert abs(o.need("B", 0.2, hole, 0, kind="via", drill=0.38) - 0.25) < 1e-12
    assert o.need("B", 0.1, kt, 0) == 0.0 and o.need("B", 0.3, kt, 0, kind="via") == O.NO_NEED
    assert o.need("B", 0.3, kv, 0, kind="via") == 0.0 and o.need("B", 0.1, kv, 0) == O.NO_NEED
    edge = [s for s in o.near(0, (0, 0, 1, 1), 0.5) if s.cls == "edge"]
    assert edge and o.need("B", 0.1, edge[0], 0) == 0.3
    # near(): static + edge + dynamic (tag = piece id), bbox query, layer aware
    o.add_piece(seg(5, "B", 0, 8.0, 12.0, 12.0, 12.0))
    o.add_piece(via(4, "B", 9.0, 13.0))
    got = o.near(0, (9.0, 9.0, 11.0, 13.0), 0.5)
    tags = [s.tag if isinstance(s.tag, int) else getattr(s.tag, "name", s.tag) for s in got]
    assert "PA.1" in tags and "PX.1" in tags and tags[-3:] == [4, 4, 5] and "BK.1" not in tags
    assert [s.cls for s in got[-3:]] == ["cu", "hole", "cu"]
    assert 5 not in [s.tag for s in o.near(1, (9.0, 9.0, 11.0, 13.0), 0.5)] and 4 in [s.tag for s in o.near(1, (9.0, 9.0, 11.0, 13.0), 0.5)]
    assert o.near(0, (25.0, 14.0, 26.0, 15.0), 0.2) == []
    # pad_gap: exact edge to edge
    assert abs(o.pad_gap("PA.1", "PX.1", 0) - 0.5) < 1e-12 and o.pad_gap("PX.1", "PA.1", 0) == o.pad_gap("PA.1", "PX.1", 0)
    assert abs(o.pad_gap("C1.1", "C2.1", 0) - (5.0 - 0.4 - 0.6)) < 1e-12              # two circles, centres 3-4-5 apart
    c = math.cos(math.radians(45))
    want = 22.0 - 0.5 - (20.0 + 0.5 * c + 0.3 * c)                                    # corner of the rotated pad to RY's edge
    assert abs(o.pad_gap("RX.1", "RY.1", 0) - want) < 2e-6                            # (cos is rounded to 1e-6 in the extract)
    assert o.pad_gap("PA.1", "BK.1", 0) == float("inf") and o.pad_gap("PA.1", "BK.1", 1) == float("inf")
    assert o.pad_gap("PA.1", "PA.1", 0) <= 0
    try:
        o.pad_gap("PA.1", "NOPE.9", 0)
        raise AssertionError("unknown pad must raise")
    except KeyError:
        pass
    assert O.shape_gap(pa, geom.Shape("poly", [(10.0, 11.0), (11.0, 12.0), (9.0, 12.0)], "cu", 0, 0, (0,))) == 0.7 - 1e-16 or \
        abs(O.shape_gap(pa, geom.Shape("poly", [(10.0, 11.0), (11.0, 12.0), (9.0, 12.0)], "cu", 0, 0, (0,))) - 0.7) < 1e-12


def test_checks_are_counted_and_fast_paths_agree():
    o = O.Oracle(board([PA, PB]))
    o.add_piece(seg(1, "A", 0, 15.0, 15.0, 25.0, 15.0))
    n0 = o.checks
    cases = [(0, 8.0, 10.6, 12.0, 10.6), (0, 8.0, 10.59, 12.0, 10.59), (0, 20.0, 12.0, 20.0, 18.0), (1, 20.0, 12.0, 20.0, 18.0),
             (0, 0.39, 5.0, 0.39, 25.0), (0, 50.0, 1.0, 51.0, 1.0)]
    for c in cases:
        assert (o.seg_ok(*c, W, "B") is None) == o.seg_legal(*c, W, "B")
    for x, y in ((10.0, 10.8), (10.0, 10.7), (20.0, 15.5), (0.5, 15.0), (-1.0, 3.0), (30.0, 5.0)):
        assert (o.via_ok(x, y, 0.6, 0.3, "B") is None) == o.via_legal(x, y, 0.6, 0.3, "B")
    assert o.checks == n0 + 2 * len(cases) + 12
    assert abs(O.raster_guard(0.15, 0.225) - 0.0237) < 1e-4 and O.raster_guard(0.05, 0.3) < 0.0021


def test_local_overrides_from_file():
    d = common.WORK / "test_v2_oracle"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "ovr.kicad_pcb"
    f.write_text('''(kicad_pcb (version 20241229) (generator "pcbnew")
\t(setup
\t\t(pad_to_mask_clearance 0.051)
\t\t(solder_mask_min_width 0.1)
\t)
\t(net 1 "A (weird) name")
\t(footprint "Lib:QFN \\"x\\" (0.4mm)" (layer "F.Cu")
\t\t(property "Reference" "U6" (at 0 0))
\t\t(property "Value" "clearance 9.9 )")
\t\t(clearance 0.18)
\t\t(solder_mask_margin 0.07)
\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")
\t\t\t(clearance 0.5)
\t\t\t(solder_mask_margin -0.02)
\t\t\t(options (clearance outline) (anchor rect))
\t\t)
\t\t(pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu"))
\t\t(zone (net 0) (clearance 3.3))
\t)
\t(footprint "Lib:R" (layer "F.Cu")
\t\t(fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
\t\t(pad "A1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (solder_mask_margin 0.102))
\t)
\t(zone (net 1) (connect_pads (clearance 0.508)))
\t(segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu") (net 1))
)
''', encoding="utf-8")
    ov = O.local_overrides_from_file(f)
    assert ov == {"mask_margin": 0.051, "mask_web": 0.1, "fp_clearance": {"U6": 0.18},
                  "pad_clearance": {("U6", "1"): 0.5}, "fp_mask_margin": {"U6": 0.07},
                  "pad_mask_margin": {("U6", "1"): -0.02, ("R1", "A1"): 0.102}}, ov
    assert O.local_overrides_from_file(d / "missing.kicad_pcb")["fp_clearance"] == {}
    # the resolution order in the oracle: pad value, else footprint value, else board
    pads = [dict(PA, ref="U6", num="1"), dict(PA, ref="U6", num="2", x=20.0), dict(PA, ref="R1", num="A1", x=30.0), PB]
    o = O.Oracle(board(pads), overrides=ov)
    assert below(o, 10.3, 0.5 - UM).need == 0.5 and below(o, 10.3, 0.5) is None
    assert below(o, 10.3, 0.18 - UM, x=20.0).need == 0.18 and below(o, 10.3, 0.18, x=20.0) is None
    assert below(o, 10.3, 0.2 - UM, x=30.0).rule == "clearance"               # mask 0.102 + 0.05 < class 0.2


# ── the one_air_max self-check failure (design Appendix A1) ───────────────
def test_one_air_max_regression():
    """v1's Router._verify dropped all of GND (37 connections) at (126.51, 81.42) and all of +3V3 (38) at
    (121.41, 79.62) on one_air_max. Traced (bench/route/trace_verify.py; the numbers below are the real ones):
    both are ON-GRID 45 degree steps past the corner of a foreign pad — no snap or escape stub involved. The grid
    (0.15 mm) keeps cell CENTRES half-width + clearance + EPS (0.012) away; between two diagonal neighbours the
    track dips up to 23.7 um closer, more than EPS. One segment is a real 2.3 um violation, the other is legal
    with 1.3 um to spare and only fails v1's extra 2 um margin; either way v1 then ripped the whole net."""
    nets = {n: {"clearance": 0.1} for n in ("GND", "+3V3", "RST", "/EN")}
    pads = [{"ref": "U3", "num": "3", "net": "RST", "x": 126.01, "y": 82.085, "hw": 0.55, "hh": 0.3, "layer": 1},
            {"ref": "R6", "num": "2", "net": "/EN", "x": 121.76, "y": 80.145, "hw": 0.27, "hh": 0.285, "layer": 1},
            {"ref": "U3", "num": "2", "net": "GND", "x": 126.01, "y": 81.135, "hw": 0.55, "hh": 0.3, "layer": 1},
            {"ref": "U12", "num": "8", "net": "+3V3", "x": 121.76, "y": 79.285, "hw": 0.27, "hh": 0.2, "layer": 1}]
    b = board(pads, nets=nets, origin=(120.06, 75.12), size=(12.0, 12.0), rules={"clearance": 0.0, "edge_clearance": 0.3})
    gnd = (1, 126.21, 81.12, 126.81, 81.72)          # the segment v1 reported at its midpoint (126.51, 81.42)
    v33 = (1, 121.11, 79.92, 121.71, 79.32)          # ... and at (121.41, 79.62)
    o = O.Oracle(b)
    bl = o.seg_ok(*gnd, 0.25, "GND")
    assert bl is not None and bl.rule == "clearance" and bl.tag == "U3.3" and bl.need == 0.1
    assert abs(bl.dist - 0.09774) < 2e-5                                       # 2.26 um short: KiCad flags this one
    assert [round((gnd[1] + gnd[3]) / 2, 2), round((gnd[2] + gnd[4]) / 2, 2)] == [126.51, 81.42]
    assert o.seg_ok(*v33, 0.25, "+3V3") is None                                # gap 0.10127: legal, v1 dropped 38 connections for it
    # v1's own exact check (need + 2 um) rejects both ...
    assert not b.seg_clear(1, *gnd[1:], 0.125, b.nets["GND"].id, 0.1)
    assert not b.seg_clear(1, *v33[1:], 0.125, b.nets["+3V3"].id, 0.1)
    # ... although on v1's 0.15 mm grid all four end cells are free for the net: legal by construction, it assumed
    g = b.make_grid(0.15)
    cm = b.class_map(b.class_key(0.25, 0.1, 0.8, 0.508))
    for (lay, x1, y1, x2, y2), net in ((gnd, "GND"), (v33, "+3V3")):
        for x, y in ((x1, y1), (x2, y2)):
            ix, iy = g.cell(x, y)
            assert abs(g.mx(ix) - x) < 1e-9 and abs(g.my(iy) - y) < 1e-9       # both ends are cell centres
            assert cm.occ[lay * g.G + iy * g.W + ix] in (0, b.nets[net].id)
    # the dip of a diagonal step at this pitch and reach is larger than the grid's EPS: that is the root cause
    from copper_kicad.pcb.guided.grid import EPS
    assert O.raster_guard(0.15, 0.125 + 0.1) > EPS > O.raster_guard(0.1, 0.125 + 0.2)
    # the last diagonal step of the GND segment, (126.66, 81.57) -> (126.81, 81.72), passes U3.3's corner: both cell
    # centres clear reach + EPS (free in v1), the chord between them does not clear reach; a window rasterised with
    # raster_guard() blocks the nearer cell, so the search cannot take this step
    corner, reach = (126.56, 81.785), 0.125 + 0.1
    d1 = math.hypot(126.66 - corner[0], 81.57 - corner[1])
    d2 = math.hypot(126.81 - corner[0], 81.72 - corner[1])
    assert min(d1, d2) >= reach + EPS and geom.pt_seg_dist(*corner, 126.66, 81.57, 126.81, 81.72) < reach
    assert min(d1, d2) < reach + O.raster_guard(0.15, reach)


# ── differential test against kicad-cli DRC ───────────────────────────────
def have_kicad() -> bool:
    return common.KICAD_CLI.exists() and common.KICAD_PY.exists()


def test_kicad_seeded_at_the_rule():
    """One generated board per delta; KiCad says which probes it flags. The gate: zero false-legal at TOL_MM, and
    the measured epsilon is what the oracle's tolerance model assumes."""
    if not have_kicad():
        print("    (skipped: KiCad not found)")
        return
    import oracle_diff
    res = oracle_diff.run_seeded(tols=(TOL_MM, 0.0, 0.0004, 0.0005, 0.001), verbose=False)
    assert res["fixed_copper_flagged_at_max_delta"] == 0
    assert res["board_rules"]["clearance"] == 0.15 and res["board_rules"]["edge_clearance"] == 0.3
    eps = res["epsilon"]
    assert len(eps) >= 34
    for name, e in eps.items():
        kinds = set(e["kicad_type"])
        assert kinds, f"KiCad never flagged '{name}': the case does not test anything"
        if name in ("seg|keep-out", "seg|pad solder-mask margin 0.3 (bridge)"):
            assert (e["kicad_accepts_down_to_um"], e["kicad_flags_from_um"]) == (0.0, -0.1), (name, e)      # no epsilon
        elif name == "via|keep-out":
            assert e["kicad_flags_from_um"] == -0.6, (name, e)
        else:
            assert kinds <= {"clearance", "hole_clearance", "hole_to_hole", "copper_edge_clearance"}, (name, e)
            # 0.5 um epsilon: -0.4 um always passes, -0.6 um never (at -0.5 the 1 nm rounding of 45 degree geometry decides)
            assert e["kicad_accepts_down_to_um"] in (-0.5, -0.4) and e["kicad_flags_from_um"] in (-0.5, -0.6), (name, e)
    t = res["tols"]
    assert t[str(TOL_MM)]["false_legal"] == [], t[str(TOL_MM)]["false_legal"]
    assert t[str(TOL_MM)]["unexpected_rule"] == [] and t["0.0"]["false_legal"] == [] and t["0.0004"]["false_legal"] == []
    # everything the oracle rejects that KiCad accepts lies inside KiCad's epsilon (or is the via / keep-out case)
    for name, du in t[str(TOL_MM)]["false_illegal"]:
        assert -0.5 <= du < -0.25 or name == "via|keep-out", (name, du)
    # the design's first guess (1 um) and KiCad's own epsilon (0.5 um) are both too generous for the oracle
    assert len(t["0.001"]["false_legal"]) > 50 and len(t["0.0005"]["false_legal"]) >= 1


def test_kicad_references_agree():
    """Human routing of corpus boards: the oracle flags every item KiCad flags (zero false-legal), and whatever
    only the oracle flags has a known, strict-side reason (zero unexplained)."""
    if not have_kicad():
        print("    (skipped: KiCad not found)")
        return
    import oracle_diff
    boards = sorted(p.name for p in common.CORPUS.iterdir() if (p / "reference.json").exists()) if FULL else \
        ["ecc83", "sonde_xilinx", "multichannel_mixer", "stickhub"]
    boards = [b for b in boards if (common.CORPUS / b / "reference.json").exists()]
    if not boards:
        print("    (skipped: no benchmark corpus)")
        return
    table = oracle_diff.run_refs(boards, None, verbose=False)
    tot = {k: sum(v[k] for v in table.values()) for k in ("items", "both_legal", "both_flagged", "false_legal",
                                                         "false_illegal", "unexplained")}
    print(f"    {len(table)} boards: {tot}")
    for b, s in table.items():
        assert s["false_legal"] == 0, (b, json.dumps(s)[:400])
        assert s["unexplained"] == 0, (b, json.dumps(s)[:400])
    assert table["multichannel_mixer"]["both_flagged"] == 12                   # the reference's 12 real clearance errors
    assert table["stickhub"]["both_flagged"] >= 80
    assert tot["items"] >= 2000 and tot["false_illegal"] <= 0.01 * tot["items"]
    if FULL:
        assert len(table) == 12 and tot["items"] >= 14000 and tot["both_flagged"] >= 390


if __name__ == "__main__":
    import time
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
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
    sys.exit(1 if fails else 0)
