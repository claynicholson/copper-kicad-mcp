"""Router v2 tile graph (design §4.6): capacities from exact geometry, via capacity, pad-pair cuts, calibration.

Pure tests on synthetic boards (no KiCad) and the calibration against the human-routed corpus references
(needs the benchmark corpus; the worker extracts are cached under bench/route/.work/w3, KiCad is only needed
to build that cache the first time).

Run:  python tests/test_v2_tiles.py              pure tests + calibration on 4 small references
      python tests/test_v2_tiles.py --full       ... + every corpus reference
      python tests/test_v2_tiles.py test_name    one test
Scratch files go to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import math
import os
import sys
import time
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
from copper_kicad.pcb.v2 import tiles as TL  # noqa: E402
from copper_kicad.pcb.v2.types import ConnV2, Cut, Piece, Terminal  # noqa: E402

FULL = "--full" in sys.argv or os.environ.get("COPPER_V2_TILES_FULL") == "1"


# ── synthetic boards (no KiCad) ───────────────────────────────────────────
def board(pads=(), *, size=(40.0, 30.0), origin=(0.0, 0.0), layers=("F.Cu", "B.Cu"), tracks=(), vias=(), zones=(),
          nets=None, rules=None, outline=None):
    """pads: dicts {ref, net, x, y, hw, hh, r=0, rot=0, kind='smd'|'tht'|'npth', hole=radius, clearance, num='1',
    layer=0}. nets: {name: {clearance, width, ...}} (default class: clearance 0.2, width 0.2, via 0.6 / 0.3).
    outline: polygon points (default: the size rectangle at origin)."""
    names = sorted({p["net"] for p in pads if p.get("net")} | set(nets or {}) | {t[0] for t in tracks} | {v[0] for v in vias})
    R = {"clearance": 0.1, "track_width": 0.1, "via": 0.4, "drill": 0.2, "edge_clearance": 0.3, "hole_clearance": 0.25,
         "hole_to_hole": 0.25, "annular": 0.05,
         "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.25, "dp_width": 0.2}}
    R.update(rules or {})
    x0, y0 = origin
    poly = outline or [[x0, y0], [x0 + size[0], y0], [x0 + size[0], y0 + size[1]], [x0, y0 + size[1]]]
    ex = {"layers": list(layers), "rules": R,
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.25, "dp_width": 0.2, **((nets or {}).get(n) or {})} for i, n in enumerate(names)},
          "outline": {"closed": True, "polys": [{"o": [list(p) for p in poly]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [list(t) for t in tracks], "vias": [list(v) for v in vias],
          "zones": [dict(z) for z in zones], "mask_openings": [],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    allL = list(range(len(layers)))
    for i, p in enumerate(pads):
        kind = p.get("kind", "smd")
        lays = allL if kind in ("tht", "npth") else [p.get("layer", 0)]
        a = math.radians(p.get("rot", 0.0))
        d = {"ref": p["ref"], "num": p.get("num", "1" if kind != "npth" else ""), "net": p.get("net", ""), "x": p["x"],
             "y": p["y"], "kind": kind, "layers": lays, "c": p.get("c", i + 1)}
        if kind != "npth":
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


def mk(pads=(), tile=1.0, w=0.2, cl=0.2, cal=1.0, **kw):
    b = board(pads, **kw)
    o = O.Oracle(b)
    return b, o, TL.Tiles(b, o, tile, w, cl, cal)


def near(a, b, eps=1e-6):
    return abs(a - b) <= eps


class Lcg:
    """Deterministic pseudo-random numbers (no ``random`` module: tests must not depend on a global seed)."""

    def __init__(self, seed=12345):
        self.s = seed

    def u(self, lo=0.0, hi=1.0):
        self.s = (self.s * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return lo + (hi - lo) * ((self.s >> 11) / float(1 << 53))


# ── borders ───────────────────────────────────────────────────────────────
def test_empty_border_is_the_design_formula():
    b, o, t = mk()
    assert (t.nx, t.ny, t.nl, t.t) == (40, 30, 2, 1.0) and t.G == 1200 and t.N == 2400
    for l in (0, 1):
        for side in ("E", "N"):
            assert near(t.raw_cap(l, 10, 10, side), 3 * 0.4)        # floor((T + cl) / p) x p = 3 tracks = 1.2 mm
            assert t.tracks(l, 10, 10, side) == 3 and near(t.slack(l, 10, 10, side), 0.0)
            assert near(t.cap(l, 10, 10, side), 1.2) and t.passable(l, 10, 10, side)
    assert near(t.full, 1.2)
    # a reference class whose pitch does not divide T + cl: 2 tracks (0.9 mm) and the remainder as slack
    _b, _o, t2 = mk(w=0.25)
    assert near(t2.raw_cap(0, 10, 10, "E"), 0.9) and t2.tracks(0, 10, 10, "E") == 2
    assert near(t2.slack(0, 10, 10, "E"), 0.3) and near(t2.cap(0, 10, 10, "E"), 1.2)           # = T + cl exactly
    _b, _o, t3 = mk(tile=2.0, w=0.3)
    assert (t3.nx, t3.ny) == (20, 15) and near(t3.raw_cap(1, 4, 5, "N"), 2.0) and near(t3.slack(1, 4, 5, "N"), 0.2)
    # sides: W / S are the neighbour's E / N; borders off the grid do not exist
    t.book(0, 9, 10, "E", 0.3)
    assert near(t.cap(0, 10, 10, "W"), 0.9) and near(t.cap(0, 9, 10, "E"), 0.9)
    t.book(0, 10, 9, "N", 0.5)
    assert near(t.cap(0, 10, 10, "S"), 0.7)
    assert t.cap(0, 39, 5, "E") == 0.0 and not t.passable(0, 39, 5, "E")
    assert t.cap(0, 5, 29, "N") == 0.0 and t.cap(0, -1, 5, "E") == 0.0 and t.cap(2, 5, 5, "E") == 0.0
    try:
        t.cap(0, 5, 5, "X")
        raise AssertionError("bad side accepted")
    except ValueError:
        pass
    # tiles
    assert t.tile_of(0.5, 0.5) == (0, 0) and t.tile_of(39.99, 29.99) == (39, 29)
    assert t.tile_of(-5, 100) == (0, 29) and t.tile_of(10.0, 10.0) == (10, 10)
    assert t.center(10, 10) == (10.5, 10.5) and t.rect(10, 10) == (10.0, 10.0, 11.0, 11.0)
    assert t.edge_of(((1 * 30 + 7) * 40 + 3) * 2 + 1) == (1, 3, 7, "N")


def test_pad_row_reduces_the_border_exactly():
    # T = 2: the E border of tile (4, 5) is the line x = 10, y in [10, 12]. Two pads on it, each 0.2 mm high,
    # inflated by the 0.2 mm clearance: blocked [10.0, 10.6] and [11.4, 12.0]  ->  free [10.6, 11.4] = 0.8 mm
    pads = [{"ref": "P1", "net": "A", "x": 10.0, "y": 10.3, "hw": 0.3, "hh": 0.1},
            {"ref": "P2", "net": "B", "x": 10.0, "y": 11.7, "hw": 0.3, "hh": 0.1}]
    b, o, t = mk(pads, tile=2.0)
    fr = t.free_intervals(0, 4, 5, "E")
    assert len(fr) == 1 and near(fr[0][0], 10.6) and near(fr[0][1], 11.4)
    assert t.tracks(0, 4, 5, "E") == 2 and near(t.raw_cap(0, 4, 5, "E"), 0.8)       # floor((0.8 + 0.2) / 0.4) = 2
    assert near(t.slack(0, 4, 5, "E"), 0.2) and near(t.cap(0, 4, 5, "E"), 1.0)      # = len + cl
    assert [x[2] for x in t.blocked(0, 4, 5, "E")] == ["P1.1", "P2.1"]
    # the pads are SMD on F.Cu: the same border on B.Cu is empty
    assert t.tracks(1, 4, 5, "E") == 5 and near(t.cap(1, 4, 5, "E"), 2.2) and t.blocked(1, 4, 5, "E") == []
    # a pad that asks for more clearance (local override 0.35) takes more of the border: free 0.65 mm
    pads2 = [dict(pads[0]), dict(pads[1], clearance=0.35)]
    _b, _o, t2 = mk(pads2, tile=2.0)
    fr = t2.free_intervals(0, 4, 5, "E")
    assert near(fr[0][0], 10.6) and near(fr[0][1], 11.25)
    assert t2.tracks(0, 4, 5, "E") == 2 and near(t2.slack(0, 4, 5, "E"), 0.05)
    # a wider reference class: 0.8 mm of free copper takes one 0.5 mm track (0.5 + 0.2 + 0.5 > 0.8)
    _b, _o, t3 = mk(pads, tile=2.0, w=0.5)
    assert t3.tracks(0, 4, 5, "E") == 1 and near(t3.cap(0, 4, 5, "E"), 1.0)
    # the pad's own shadow: the borders its inflated copper touches (its connections cross them for free)
    code = ((0 * 15 + 5) * 20 + 4) * 2
    assert code in t.pad_edges("P1.1") and code in t.pad_edges("P2.1") and t.pad_edges(None) == frozenset()
    assert t.pad_edges("nope") == frozenset()
    na, nb = o.net_id("A"), o.net_id("B")
    assert t.own_shadow(code, 10.3, na) and not t.own_shadow(code, 10.3, nb) and t.own_shadow(code, 11.7, nb)
    assert not t.own_shadow(code, 11.0, na) and not t.own_shadow(code, 10.3, 0)


def test_round_rotated_and_through_hole_shapes():
    # a round pad of radius 0.5 centred 0.3 mm beside the line x = 10: chord of the circle inflated to 0.7
    pads = [{"ref": "C", "net": "A", "x": 10.3, "y": 11.0, "hw": 0.5, "hh": 0.5, "r": 0.5}]
    _b, _o, t = mk(pads, tile=2.0)
    h = math.sqrt(0.7 ** 2 - 0.3 ** 2)
    fr = t.free_intervals(0, 4, 5, "E")
    assert len(fr) == 2 and near(fr[0][1], 11.0 - h) and near(fr[1][0], 11.0 + h)
    assert t.tracks(0, 4, 5, "E") == 2          # one track on each side (each piece joins the next border)
    # a through-hole pad blocks every layer (copper + hole), an NPTH blocks by its hole clearance
    pads = [{"ref": "J", "net": "A", "x": 10.0, "y": 11.0, "hw": 0.8, "hh": 0.8, "r": 0.8, "kind": "tht", "hole": 0.5},
            {"ref": "H", "x": 20.0, "y": 11.0, "kind": "npth", "hole": 0.6}]
    _b, _o, t = mk(pads, tile=2.0)
    for l in (0, 1):
        assert t.free_intervals(l, 4, 5, "E") == []                             # 0.8 + 0.2 on each side: nothing left
        assert t.tracks(l, 4, 5, "E") == 0 and t.cap(l, 4, 5, "E") == 0.0
        fr = t.free_intervals(l, 9, 5, "E")                                     # hole 0.6 + hole clearance 0.25
        assert near(fr[0][1], 11.0 - 0.85) and near(fr[1][0], 11.0 + 0.85)
        assert "H" in [x[2] for x in t.blocked(l, 9, 5, "E")]                   # named after the (unnumbered) pad
    # a pad rotated by 30 degrees: the exact span agrees with the signed distance function
    pads = [{"ref": "R", "net": "A", "x": 10.2, "y": 11.0, "hw": 0.6, "hh": 0.2, "rot": 30.0}]
    b, _o, t = mk(pads, tile=2.0)
    s = b.pads[0].shapes[0][0]
    (lo, hi), = TL.shape_spans(s, 0.2, 0, 10.0)
    for y, inside in ((lo + 1e-4, True), (lo - 1e-4, False), (hi - 1e-4, True), (hi + 1e-4, False)):
        assert (s.dist_pt(10.0, y) <= 0.2) == inside
    fr = t.free_intervals(0, 4, 5, "E")
    assert near(fr[0][1], lo) and near(fr[1][0], hi)


def test_spans_agree_with_the_distance_functions():
    """box_span / poly_spans are exact: every sampled point of an axis-parallel line is inside the span iff it
    is within ``grow`` of the shape."""
    rnd = Lcg(7)
    checked = 0
    for _ in range(120):
        ang = rnd.u(0, math.pi)
        g = (rnd.u(-1, 1), rnd.u(-1, 1), math.cos(ang), math.sin(ang), rnd.u(0, 1.2) if rnd.u() > 0.2 else 0.0,
             rnd.u(0, 0.8) if rnd.u() > 0.3 else 0.0, rnd.u(0, 0.5) if rnd.u() > 0.3 else 0.0)
        grow = rnd.u(0, 0.5) if rnd.u() > 0.2 else 0.0
        for axis in (0, 1):
            c = rnd.u(-2.5, 2.5)
            sp = TL.box_span(g, grow, axis, c)
            for i in range(-400, 401):
                tt = i * 0.01 + 0.0037
                d = geom.box_dist(g, c, tt) if axis == 0 else geom.box_dist(g, tt, c)
                if abs(d - grow) < 2e-4:
                    continue                       # too close to the boundary to call
                inside = sp is not None and sp[0] <= tt <= sp[1]
                assert inside == (d < grow), (g, grow, axis, c, tt, d, sp)
                checked += 1
    # a concave polygon (an L), with and without inflation
    L = [(0.0, 0.0), (3.0, 0.0), (3.0, 1.0), (1.0, 1.0), (1.0, 3.0), (0.0, 3.0)]
    for grow in (0.0, 0.25):
        for axis in (0, 1):
            for k in range(-10, 45):
                c = k * 0.1 + 0.013
                spans = TL.poly_spans(L, grow, axis, c)
                for i in range(-100, 450):
                    tt = i * 0.01 + 0.0037
                    d = geom.pt_poly_dist(L, c, tt) if axis == 0 else geom.pt_poly_dist(L, tt, c)
                    if abs(d - grow) < 2e-4:
                        continue
                    inside = any(a <= tt <= b_ for a, b_ in spans)
                    assert inside == (d < grow), (grow, axis, c, tt, d, spans)
                    checked += 1
    assert checked > 100_000
    # interval helpers
    assert TL._merge([(2, 3), (0, 1), (0.5, 2.5), (5, 5)]) == [(0, 3)]
    assert TL._subtract([(0, 10)], [(1, 2), (4, 5), (9, 12)]) == [(0, 1), (2, 4), (5, 9)]
    assert TL._subtract([(0, 1), (2, 3)], [(0.5, 2.5)]) == [(0, 0.5), (2.5, 3)]
    assert TL._clip([(0, 1), (2, 3), (4, 5)], 0.5, 4.2) == [(0.5, 1), (2, 3), (4, 4.2)]


def test_gap_cut_by_a_tile_corner_is_counted_once():
    """T = 1, line x = 10: the tile corner at y = 11 cuts a free gap in two. Each piece alone would be floored
    on its own (0 + 0 or 1 + 1); together they carry floor((len_a + len_b + cl) / p) tracks."""
    def pads(top, bot):                                    # blocked [9.95, top] and [bot, 12.05]
        return [{"ref": "A", "net": "A", "x": 10.0, "y": (9.95 + top) / 2, "hw": 0.3, "hh": (top - 9.95) / 2 - 0.2},
                {"ref": "B", "net": "B", "x": 10.0, "y": (bot + 12.05) / 2, "hw": 0.3, "hh": (12.05 - bot) / 2 - 0.2}]

    def tracks(top, bot):
        _b, _o, t = mk(pads(top, bot))
        fa, fb = t.free_intervals(0, 9, 10, "E"), t.free_intervals(0, 9, 11, "E")
        assert len(fa) == 1 and near(fa[0][0], top) and near(fa[0][1], 11.0), fa
        assert len(fb) == 1 and near(fb[0][0], 11.0) and near(fb[0][1], bot), fb
        return t.tracks(0, 9, 10, "E"), t.tracks(0, 9, 11, "E")

    assert sum(tracks(10.8, 11.2)) == 1          # 0.2 + 0.2: each alone floor(0.4 / 0.4) = 1, together 0.4 mm = 1
    assert tracks(10.9, 11.3) == (0, 1)          # 0.1 + 0.3: one track, in the larger piece
    assert sum(tracks(10.85, 11.15)) == 1        # 0.15 + 0.15: neither alone, one together
    # 0.5 + 0.5 = 1.0 mm: each alone floor(0.7 / 0.4) = 1, together floor(1.2 / 0.4) = 3 (3 x 0.2 + 2 x 0.2 = 1.0)
    assert tracks(10.5, 11.5) == (1, 2)


def test_outline_edge_clearance_and_tiles_off_the_board():
    b, o, t = mk()
    # first row: the outline at y = 0 and its 0.3 mm edge clearance take the low end of the border x = 10, y in [0, 1]
    fr = t.free_intervals(0, 9, 0, "E")
    assert len(fr) == 1 and near(fr[0][0], 0.3) and near(fr[0][1], 1.0)
    assert [x[2] for x in t.blocked(0, 9, 0, "E")] == ["edge"]
    assert t.tracks(0, 9, 0, "E") == 1           # 0.7 mm joined with the whole border above: floor(1.9/0.4) - 3
    assert 0.0 < t.cap(0, 9, 0, "E") < t.cap(0, 9, 5, "E")
    # an L-shaped board: the part cut away is off the board
    L = [[0, 0], [40, 0], [40, 30], [20, 30], [20, 15], [0, 15]]
    b, o, t = mk(outline=L)
    assert not t.passable(0, 5, 20, "E") and t.cap(0, 5, 20, "E") == 0.0 and t.cap(1, 5, 20, "N") == 0.0
    assert not t.passable(0, 19, 20, "N")                      # x in [19, 20] at y = 21: outside
    assert t.passable(0, 20, 20, "N") and t.tracks(0, 20, 20, "N") == 1     # x in [20.3, 21]
    assert t.passable(0, 5, 10, "E") and t.tracks(0, 5, 10, "E") == 3
    assert t.via_cap(5, 20) == 0 and t.via_cap(25, 20) == 1 and t.via_cap(5, 5) == 1
    # keep-outs: a no-track area closes borders, a no-via area closes columns; each leaves the other alone
    b, o, t = mk(zones=[keepout(9.5, 9.5, 12.5, 12.5, tracks=True, vias=False),
                        keepout(19.5, 9.5, 22.5, 12.5, tracks=False, vias=True, layers=(0,))])
    assert t.cap(0, 10, 10, "E") == 0.0 and t.cap(1, 10, 10, "N") == 0.0 and t.passable(0, 10, 10, "E")
    assert t.blocked(0, 10, 10, "E")[0][2] == "keepout"
    assert t.via_cap(10, 10) == 1
    assert near(t.cap(0, 20, 10, "E"), 1.2) and t.via_cap(20, 10) == 0 and t.via_cap(21, 11) == 0
    assert t.via_cap(25, 10) == 1


# ── vias ──────────────────────────────────────────────────────────────────
def test_via_capacity_counts_every_layer():
    b, o, t = mk()
    assert (t.via_size, t.via_drill) == (0.6, 0.3)
    assert t.via_cap(20, 20) == 1                        # 1 mm tile, 0.8 mm via pitch: one via
    _b, _o, t2 = mk(tile=2.0)
    assert t2.via_cap(10, 7) == 6                        # 4 mm2 / 0.64 mm2 = 6.25
    assert t.via_cap(-1, 0) == 0 and t.via_cap(40, 0) == 0
    assert t.via_cap(0, 0) == 1                          # a corner tile: one sample clears the edge
    # a pad on the BOTTOM layer only covers tile (20, 20): no through via there, although F.Cu is empty
    pads = [{"ref": "B1", "net": "A", "x": 20.5, "y": 20.5, "hw": 0.6, "hh": 0.6, "layer": 1}]
    b, o, t = mk(pads)
    assert near(t.cap(0, 20, 20, "E"), 1.2) and t.cap(1, 20, 20, "E") < 0.5
    assert t.via_cap(20, 20) == 0 and t.via_cap(25, 20) == 1
    # a drilled hole needs hole-to-hole distance as well
    pads = [{"ref": "H", "x": 20.5, "y": 20.5, "kind": "npth", "hole": 0.3}]
    _b, _o, t = mk(pads)
    assert t.via_cap(20, 20) == 0
    # reservations
    b, o, t = mk()
    v = t.version
    t.book_via(20, 20)
    assert t.via_cap(20, 20) == 0 and t.via_cap(21, 20) == 1 and t.version == v + 1
    t.book_via(20, 20, -1)
    assert t.via_cap(20, 20) == 1
    t.book_via(99, 99)                                   # off the grid: ignored


def test_only_static_geometry_counts():
    """Copper v2 placed itself (dynamic pieces of the oracle) is demand, not capacity; kept tracks are static."""
    b = board()
    o = O.Oracle(b)
    base = TL.Tiles(b, o, 1.0, 0.2, 0.2)
    o.add_piece(Piece(id=1, net="A", conn=0, kind="seg", layer=0, x1=5.0, y1=10.5, x2=15.0, y2=10.5, w=0.25))
    o.add_piece(Piece(id=2, net="A", conn=0, kind="via", layer=-1, x1=20.5, y1=20.5, x2=20.5, y2=20.5, w=0.6, drill=0.3))
    t = TL.Tiles(b, o, 1.0, 0.2, 0.2)
    assert t.capE == base.capE and t.capN == base.capN and bytes(t.hardE) == bytes(base.hardE)
    assert t.via_cap(20, 20) == 1 and t.blocked(0, 9, 10, "E") == []
    # the same track as copper already on the board
    b2 = board(tracks=[("A", 0, 5.0, 10.5, 15.0, 10.5, 0.25, 0, False)], nets={"A": {}})
    t2 = TL.Tiles(b2, O.Oracle(b2), 1.0, 0.2, 0.2)
    fr = t2.free_intervals(0, 9, 10, "E")                # blocked 10.5 +- (0.125 + 0.2)
    assert near(fr[0][1], 10.175) and near(fr[1][0], 10.825) and t2.tracks(0, 9, 10, "E") < 3
    assert near(t2.cap(1, 9, 10, "E"), 1.2)
    # building twice gives the same graph
    t3 = TL.Tiles(b2, O.Oracle(b2), 1.0, 0.2, 0.2)
    assert t3.capE == t2.capE and t3.capN == t2.capN and t3._free == t2._free
    assert [t3.via_cap(x, y) for y in range(t3.ny) for x in range(t3.nx)] == \
           [t2.via_cap(x, y) for y in range(t2.ny) for x in range(t2.nx)]


# ── bookings ──────────────────────────────────────────────────────────────
def test_book_scale_and_calibration_constant():
    b, o, t = mk()
    v = t.version
    t.book(0, 10, 10, "E", 0.5)
    assert near(t.cap(0, 10, 10, "E"), 0.7) and near(t.raw_cap(0, 10, 10, "E"), 1.2) and t.version == v + 1
    assert near(t.cap(1, 10, 10, "E"), 1.2) and near(t.cap(0, 10, 10, "N"), 1.2)
    t.book(0, 10, 10, "E", 5.0)
    assert t.cap(0, 10, 10, "E") == 0.0                  # never negative
    t.book(0, 10, 10, "E", -5.5)
    assert near(t.cap(0, 10, 10, "E"), 1.2)
    t.scale(0, 10, 10, "E", 0.5)
    assert near(t.cap(0, 10, 10, "E"), 0.6)
    t.scale(0, 10, 10, "E", 0.5)
    assert near(t.cap(0, 10, 10, "E"), 0.3)              # factors accumulate
    t.book(0, 99, 99, "E", 1.0)
    t.scale(0, 99, 99, "E", 0.0)                         # off the grid: ignored
    n = t.scale_rect((4.9, 4.9, 7.1, 7.1), 2.0, layers=[1])
    assert n > 0 and near(t.cap(1, 5, 5, "E"), 2.4) and near(t.cap(0, 5, 5, "E"), 1.2) and near(t.cap(1, 8, 5, "E"), 1.2)
    # a reserved area as demand: each border it crosses loses the crossed length + the reference clearance
    _b, _o, tb = mk()
    box = geom.Shape("box", (10.0, 10.5, 1.0, 0.0, 1.5, 0.25, 0.0), "soft", 0, 0.0, (0,))      # x 8.5..11.5, y 10.25..10.75
    assert tb.book_shape(0, box) == 3                    # the E borders of tiles (8..10, 10)
    assert near(tb.cap(0, 9, 10, "E"), 1.2 - 0.7) and near(tb.cap(0, 8, 10, "E"), 0.5) and near(tb.cap(0, 11, 10, "E"), 1.2)
    assert near(tb.cap(0, 9, 10, "N"), 1.2) and near(tb.cap(1, 9, 10, "E"), 1.2)
    tall = geom.Shape("box", (20.5, 10.0, 1.0, 0.0, 0.1, 1.2, 0.0), "soft", 0, 0.0, (0,))      # y 8.8..11.2 at x 20.4..20.6
    assert tb.book_shape(1, tall) == 3 and near(tb.cap(1, 20, 9, "N"), 1.2 - 0.4) and near(tb.cap(1, 20, 8, "N"), 0.8)
    assert not tb.reservations_booked and not tb.regions_scaled
    # cal multiplies every capacity
    _b, _o, tc = mk(cal=1.25)
    assert near(tc.cap(0, 10, 10, "E"), 1.5) and near(tc.raw_cap(0, 10, 10, "E"), 1.2)
    # the module constants the engine uses
    assert 0.8 <= TL.CAL <= 1.3 and isinstance(TL.CORRIDORS_ADVISORY, bool) and t.advisory == TL.CORRIDORS_ADVISORY
    tm = TL.make(b, o)
    assert tm.cal == TL.CAL and tm.t == 1.0 and (tm.ref_w, tm.ref_cl) == (0.2, 0.2)


def test_utilisation_picture():
    b, o, t = mk()
    assert t.utilisation(0) == []
    useE, useN, useV = [0.0] * t.N, [0.0] * t.N, [0] * t.G
    s = (0 * 30 + 10) * 40 + 10
    useE[s] = 0.6                                        # half of 1.2
    useN[s + 1] = 2.4                                    # twice the capacity
    t.set_usage(useE, useN, useV)
    u = t.utilisation(0)
    assert len(u) == 30 and len(u[0]) == 40
    assert u[10][10] == 50 and u[10][11] == 200 and u[11][11] == 200 and u[5][5] == 0
    assert t.utilisation(1)[10][10] == 0


# ── cuts ──────────────────────────────────────────────────────────────────
def test_pad_cut_formula_including_the_diagonal():
    pads = [{"ref": "U1", "num": "1", "net": "A", "x": 10.0, "y": 10.0, "hw": 0.15, "hh": 0.5},
            {"ref": "U1", "num": "2", "net": "B", "x": 10.8, "y": 10.0, "hw": 0.15, "hh": 0.5},     # gap 0.5
            {"ref": "U1", "num": "3", "net": "C", "x": 12.07, "y": 10.0, "hw": 0.15, "hh": 0.5},    # gap 0.97 to pad 2
            {"ref": "U2", "num": "1", "net": "D", "x": 20.0, "y": 20.0, "hw": 0.25, "hh": 0.25},
            {"ref": "U2", "num": "2", "net": "E", "x": 21.0, "y": 21.0, "hw": 0.25, "hh": 0.25},    # diagonal neighbour
            {"ref": "U3", "num": "1", "net": "F", "x": 30.0, "y": 10.0, "hw": 0.15, "hh": 0.5},
            {"ref": "U3", "num": "2", "net": "G", "x": 30.95, "y": 10.0, "hw": 0.15, "hh": 0.5, "clearance": 0.3}]
    b, o, t = mk(pads)
    c = t.pad_cut("U1.1", "U1.2", 0, 0.2, 0.2)
    assert isinstance(c, Cut) and (c.a, c.b, c.layer) == ("U1.1", "U1.2", "F.Cu")
    assert c.capacity_mm == 0.0 and c.demand_mm == 0.0 and c.nets == []        # floor((0.5 - 0.2) / 0.4) = 0
    assert near(t.pad_cut("U1.1", "U1.2", 0, 0.1, 0.2).capacity_mm, 0.3)       # floor(0.3 / 0.3) = 1 track of 0.1
    assert near(t.pad_cut("U1.2", "U1.3", 0, 0.2, 0.2).capacity_mm, 0.4)       # floor((0.97 - 0.2) / 0.4) = 1
    # asking for 0.1 does not undercut the pads' own 0.2 class clearance: floor((0.97 - 2 x 0.1 - 0.1) / 0.2) = 3
    assert near(t.pad_cut("U1.2", "U1.3", 0, 0.1, 0.1).capacity_mm, 0.6)
    # the diagonal (vertex) gap: hypot(0.5, 0.5) = 0.7071 -> floor(0.5071 / 0.4) = 1
    assert near(o.pad_gap("U2.1", "U2.2", 0), math.hypot(0.5, 0.5))
    assert near(t.pad_cut("U2.1", "U2.2", 0, 0.2, 0.2).capacity_mm, 0.4)
    assert t.pad_cut("U2.1", "U2.2", 0, 0.2, 0.35).capacity_mm == 0.0          # floor(0.357 / 0.55) = 0
    # a pad that asks for 0.3 instead of 0.2 narrows the cut: gap 0.65 -> floor(0.45 / 0.4) = 1 without, 0 with
    assert near(o.pad_gap("U3.1", "U3.2", 0), 0.65)
    assert t.pad_cut("U3.1", "U3.2", 0, 0.2, 0.2).capacity_mm == 0.0
    assert near(t.pad_cut("U3.1", "U3.2", 0, 0.15, 0.2).capacity_mm, 0.35)     # floor(0.35 / 0.35) = 1
    # no copper on the other layer
    c = t.pad_cut("U1.1", "U1.2", 1, 0.2, 0.2)
    assert c.capacity_mm == 0.0 and c.layer == "B.Cu"
    try:
        t.pad_cut("U1.1", "nope", 0, 0.2, 0.2)
        raise AssertionError("unknown pad accepted")
    except KeyError:
        pass
    # cuts around a footprint: each pair once, sorted
    cuts = t.neighbour_cuts("U1", 0, 0.2, 0.2)
    assert [(c.a, c.b) for c in cuts] == [("U1.1", "U1.2"), ("U1.1", "U1.3"), ("U1.2", "U1.3")]
    assert [round(c.capacity_mm, 3) for c in cuts] == [0.0, 1.2, 0.4]          # gaps 0.5, 1.77, 0.97
    assert t.neighbour_cuts("U1", 0, 0.2, 0.2, max_gap=0.6)[0].b == "U1.2" and len(t.neighbour_cuts("U1", 0, 0.2, 0.2, 0.6)) == 1
    assert t.neighbour_cuts("nope", 0, 0.2, 0.2) == [] and t.neighbour_cuts("U1", 1, 0.2, 0.2) == []


def test_cut_for_edge_names_the_obstacles():
    pads = [{"ref": "P1", "net": "A", "x": 10.0, "y": 10.3, "hw": 0.3, "hh": 0.1},
            {"ref": "P2", "net": "B", "x": 10.0, "y": 11.7, "hw": 0.3, "hh": 0.1}]
    b, o, t = mk(pads, tile=2.0)
    c = t.cut_for_edge(0, 4, 5, "E", [("X", 0.4), ("Y", 0.4), ("X", 0.4)])
    assert (c.a, c.b, c.layer) == ("P1.1", "P2.1", "F.Cu") and near(c.capacity_mm, 1.0) and near(c.demand_mm, 1.2)
    assert c.nets == ["X", "Y"]
    assert t.cut_for_edge(0, 5, 5, "W", []).a == "P1.1"                         # the same border by its other name
    c = t.cut_for_edge(1, 4, 5, "E", [("X", 0.4)])
    assert c.a == c.b == "tile(4,5)|E" and c.layer == "B.Cu" and near(c.capacity_mm, 2.2)
    c = t.cut_for_edge(0, 4, 0, "E", [])                                        # bounded by the board edge on one side
    assert c.a == "edge" and c.b == "tile(4,0)|E"
    c = t.cut_for_edge(0, 99, 0, "E", [("X", 0.4)])
    assert c.capacity_mm == 0.0 and near(c.demand_mm, 0.4)


# ── calibration helpers ───────────────────────────────────────────────────
def test_reference_demand_counts_crossings():
    pads = [{"ref": "P", "net": "A", "x": 9.9, "y": 20.5, "hw": 0.4, "hh": 0.3}]
    b, o, t = mk(pads, nets={"A": {}, "B": {"clearance": 0.3}})
    tracks = [["B", "F.Cu", 9.5, 10.5, 12.5, 10.5, 0.25],          # crosses the E borders of (9..11, 10)
              ["B", 0, 10.5, 9.5, 10.5, 11.5, 0.25],               # crosses the N borders of (10, 9) and (10, 10)
              ["A", "F.Cu", 9.9, 20.5, 12.5, 20.5, 0.25],          # leaves its own pad across x = 10, then x = 11, 12
              ["B", "B.Cu", 5.2, 5.2, 5.8, 5.8, 0.25],             # stays inside one tile
              ["B", "In9.Cu", 0.0, 0.0, 9.0, 9.0, 0.25]]           # unknown layer: ignored
    d = TL.reference_demand(t, tracks)
    assert set(d) == {(0, 9, 10, "E"), (0, 10, 10, "E"), (0, 11, 10, "E"), (0, 10, 9, "N"), (0, 10, 10, "N"),
                      (0, 10, 20, "E"), (0, 11, 20, "E")}
    assert all(near(v, 0.4) for v in d.values())                   # one reference pitch per crossing
    dw = TL.reference_demand(t, tracks, by="width")
    assert near(dw[(0, 9, 10, "E")], 0.25 + 0.3) and near(dw[(0, 10, 20, "E")], 0.25 + 0.2)
    raw = TL.reference_demand(t, tracks, own=False)
    assert (0, 9, 20, "E") in raw and len(raw) == len(d) + 1       # without the own-pad exemption
    # a diagonal crosses one E and one N border
    d = TL.reference_demand(t, [["B", 0, 10.6, 10.8, 11.4, 11.6, 0.2]])
    assert set(d) == {(0, 10, 10, "E"), (0, 10, 10, "N")} or set(d) == {(0, 10, 11, "E"), (0, 10, 10, "N")} \
        or set(d) == {(0, 10, 10, "E"), (0, 11, 10, "N")}
    # calibration: 3 tracks fit an empty border, the 4th is a shortfall at CAL 1.0 (1.2 mm) and fits at 1.4 (1.68)
    four = [["B", 0, 9.5, 10.2 + 0.05 * i, 10.5, 10.2 + 0.05 * i, 0.2] for i in range(4)]
    res = TL.calibration(t, four, cals=(1.0, 1.3, 1.4))
    assert res["used"] == 1 and res["by_cal"][1.0]["short"] == 1 and res["by_cal"][1.3]["short"] == 1
    assert res["by_cal"][1.4]["short"] == 0
    assert res["borders"] == 2 * t.N - sum(t.hardE) - sum(t.hardN) and res["worst"][0]["edge"] == [0, 9, 10, "E"]
    assert res["by_cal"][1.0]["ok_used"] == 0.0 and res["by_cal"][1.0]["ok_all"] > 0.999
    res = TL.calibration(t, four[:3])
    assert res["by_cal"][1.0]["short"] == 0 and res["ratio_hist"]["<=1.0"] == 1


def test_tile_size_and_reference_class():
    coarse = [{"ref": "J1", "num": str(i), "net": f"N{i}", "x": 5 + 2.54 * i, "y": 5, "hw": 0.8, "hh": 0.8, "kind": "tht",
               "hole": 0.4} for i in range(4)]
    b = board(coarse)
    assert near(TL.min_pad_pitch(b), 2.54) and TL.choose_tile_mm(b) == 1.0
    fine = [{"ref": "U1", "num": str(i), "net": f"N{i}", "x": 5 + 0.5 * i, "y": 5, "hw": 0.12, "hh": 0.4} for i in range(8)]
    b = board(fine)
    assert near(TL.min_pad_pitch(b), 0.5) and TL.choose_tile_mm(b) == 0.5
    # two-pad parts do not set the pitch (an 0402 is not a fine-pitch footprint)
    b = board([{"ref": "C1", "num": "1", "net": "A", "x": 5, "y": 5, "hw": 0.1, "hh": 0.2},
               {"ref": "C1", "num": "2", "net": "B", "x": 5.4, "y": 5, "hw": 0.1, "hh": 0.2}])
    assert TL.min_pad_pitch(b) == float("inf") and TL.choose_tile_mm(b) == 1.0
    # doubled until tiles x layers <= 60 000
    b = board(fine, size=(400.0, 300.0))
    assert TL.choose_tile_mm(b) == 2.0                    # 0.5 -> 960 000, 1.0 -> 240 000, 2.0 -> 60 000
    b = board(coarse, size=(400.0, 300.0), layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    assert TL.choose_tile_mm(b) == 4.0
    t = TL.make(b, O.Oracle(b))
    assert t.t == 4.0 and t.nx * t.ny * t.nl <= 60_000
    assert TL.make(b, O.Oracle(b), tile_mm=5.0).t == 5.0
    # the reference class: the most common (width, clearance) of the connections, else the default class
    b = board(coarse)
    assert TL.reference_class(b) == (0.2, 0.2) and TL.default_via(b) == (0.6, 0.3)
    ta, tb = Terminal(0, 0, (0,)), Terminal(1, 1, (0,))
    conns = [ConnV2(idx=i, net="N0", a=ta, b=tb, width=w, clearance=c) for i, (w, c) in
             enumerate([(0.15, 0.15), (0.15, 0.15), (0.5, 0.2), (0.25, 0.2), (0.25, 0.2)])]
    assert TL.reference_class(b, conns) == (0.15, 0.15)   # a tie goes to the finer class
    assert TL.reference_class(b, conns[2:]) == (0.25, 0.2)
    assert TL.make(b, O.Oracle(b), conns[2:]).pitch == 0.45


# ── the corpus: calibration against human routing ─────────────────────────
def _corpus(names):
    try:
        import tiles_cal
    except ImportError:
        return None, []
    have = [n for n in names if (common.CORPUS / n / "reference.json").exists()
            and ((tiles_cal.CACHE / f"ex_{n}.json").exists() or common.KICAD_PY.exists())]
    return tiles_cal, have


def test_calibration_on_corpus_references():
    """Design §4.6 calibration gate: cap × CAL >= the demand of the human routing on >= 99.5 % of the borders."""
    names = ["ecc83", "sonde_xilinx", "multichannel_mixer", "stickhub"]
    if FULL:
        names = sorted(p.name for p in common.CORPUS.iterdir() if (p / "reference.json").exists()) \
            if common.CORPUS.exists() else []
    tc, names = _corpus(names)
    if not names:
        print("    (skipped: no benchmark corpus / no cached extracts)")
        return
    short = borders = used = 0
    shortw = 0
    for n in names:
        b, o = tc.load(n)
        t0 = time.perf_counter()
        t = TL.make(b, o)
        dt = time.perf_counter() - t0
        assert dt < 3.0, (n, dt)                               # design §8: tile graph build <= 3 s
        assert t.nx * t.ny * t.nl <= 60_000
        res = TL.calibration(t, tc.reference(n), cals=(TL.CAL,))
        resw = TL.calibration(t, tc.reference(n), cals=(TL.CAL,), by="width")
        s = res["by_cal"][TL.CAL]
        short += s["short"]
        shortw += resw["by_cal"][TL.CAL]["short"]
        borders += res["borders"]
        used += res["used"]
        print(f"    {n}: T={t.t} {t.nx}x{t.ny}x{t.nl} built in {dt:.2f}s, borders {res['borders']}, used {res['used']}, "
              f"short {s['short']} (ok {s['ok_all']:.4f} of all, {s['ok_used']:.4f} of used; by width: "
              f"{resw['by_cal'][TL.CAL]['ok_used']:.4f} of used)")
        assert s["ok_all"] >= 0.99, (n, s)
    ok_all, ok_used = 1 - short / borders, 1 - short / max(1, used)
    print(f"    pooled over {len(names)} boards at CAL={TL.CAL}: {ok_all:.5f} of all borders, {ok_used:.5f} of used "
          f"borders (width-weighted demand: {1 - shortw / max(1, used):.5f} of used)")
    assert ok_all >= TL.CAL_GATE, ok_all
    assert not TL.CORRIDORS_ADVISORY
    if FULL:
        assert len(names) == 12 and ok_used >= 0.99


if __name__ == "__main__":
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t_ = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t_:.1f}s)")
            except Exception as e:  # noqa: BLE001
                fails += 1
                traceback.print_exc()
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
