"""Router v2, S1 power (design §4.4): dog-bone via sites, thermal spokes, decoupling stubs, local-only necking.

Pure tests on synthetic boards (no KiCad) + benchmark boards from cached extracts (see test_v2_classify.py).

Run:  python tests/test_v2_power.py            every test
      python tests/test_v2_power.py test_name  one test
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v2_classify as T  # noqa: E402  (sandbox + synthetic board builder + corpus extracts)

import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import plan  # noqa: E402
from copper_kicad.pcb.v2 import classify as C  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2 import pieces  # noqa: E402
from copper_kicad.pcb.v2 import power as PW  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, Ctx  # noqa: E402

P, board, pour, X = T.P, T.board, T.pour, T.X
VIA = (0.6, 0.3)
CL = 0.2


def plane_plan(b):
    """Signals stay off the layers that carry a pour: those are planes (what the default plan does by itself for
    the inner plane layers of a multilayer board)."""
    free = [name for i, name in enumerate(b.layers) if not any(i in n.pour_layers for n in b.nets.values())]
    return {"global": {"layers": free}} if free and len(free) < b.L else None


def make_ctx(b, plan_="planes", x=None, tiles=None, overrides=None):
    """``plan_``: a plan dict, None (the default plan) or 'planes' (``plane_plan``: the synthetic boards' pours
    are planes, so S1 places real dog-bones; with None a pour on a 2-layer board shares its layer with signals)."""
    cfg = plan.resolve(b, plane_plan(b) if plan_ == "planes" else plan_)
    orc = O.Oracle(b, overrides=overrides)
    ctx = Ctx(board=b, cfg=cfg, x=x if x is not None else X(), oracle=orc, tiles=tiles)
    ctx.state = pieces.RouteState(orc, ctx.conns)
    C.run(ctx, Budget(10 ** 6))
    return ctx


def orc_ok(ctx, p):
    return ctx.oracle.piece_ok(p) is None


def pad_of(b, name):
    return next(p for p in b.pads if p.name == name)


def keepout(x0, y0, x1, y1, tracks=False, vias=True, layers=(0, 1)):
    return {"name": "ko", "layers": list(layers), "rule_area": True, "no_tracks": tracks, "no_vias": vias,
            "polys": [{"o": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}]}


def octilinear(x1, y1, x2, y2):
    dx, dy = abs(x2 - x1), abs(y2 - y1)
    return dx < 1e-9 or dy < 1e-9 or abs(dx - dy) < 1e-9


def simple_board(extra=(), zones=None, **kw):
    """One SMD ground pad (C1.2 at 10, 10 on F.Cu) over a B.Cu ground pour, a through-hole ground pin in the pour."""
    pads = [P("C1", 2, "GND", 10, 10, hw=0.5, hh=0.3), P("C1", 1, "/S", 12, 10, hw=0.5, hh=0.3), P("R1", 1, "/S", 40, 30),
            P("J1", 1, "GND", 50, 30, kind="tht", hw=0.8, hh=0.8, r=0.8)] + list(extra)
    return board(pads, zones=[pour("GND", 1, (0, 0, 60, 40))] if zones is None else zones, **kw)


# ── candidates ────────────────────────────────────────────────────────────
def test_dogbone_candidates_are_canonical():
    b = simple_board()
    pad = pad_of(b, "C1.2")
    cs = PW.dogbone_candidates(pad, VIA, CL)
    assert len(cs) == 24 and len({v for _s, v in cs}) == 24 and all(s == v for s, v in cs)
    assert PW.dogbone_candidates(pad, VIA, CL) == cs                                   # same twice
    # shortest ring first; +x, +y, -x, -y, then the four diagonals; pad edge + via radius + clearance
    d = (0.8 + math.sqrt(0.64 - 0.18)) / 2                                             # 0.5 mm from the pad's corner
    want = [(11.0, 10.0), (10.0, 10.8), (9.0, 10.0), (10.0, 9.2)]
    assert [v for _s, v in cs[:4]] == want
    for (vx, vy), (sx, sy) in zip([v for _s, v in cs[4:8]], ((1, 1), (-1, 1), (-1, -1), (1, -1))):
        assert abs(vx - (10 + sx * d)) < 2e-6 and abs(vy - (10 + sy * d)) < 2e-6
    assert [v for _s, v in cs[8:12]] == [(11.3, 10.0), (10.0, 11.1), (8.7, 10.0), (10.0, 8.9)]
    assert [v for _s, v in cs[16:20]] == [(11.8, 10.0), (10.0, 11.6), (8.2, 10.0), (10.0, 8.4)]
    for _s, (vx, vy) in cs:
        assert octilinear(10.0, 10.0, vx, vy)                                          # exactly axial or 45 degrees
        assert vx == round(vx, 6) and vy == round(vy, 6)                               # 1 nm
        assert PW._layer_of(pad) == 0
        # the via copper keeps the clearance to its own pad (no via in pad), with less than 2 nm to spare on ring 0
        gap = min(s.dist_pt(vx, vy) for s in pad.shapes[0]) - VIA[0] / 2
        assert gap >= CL - 1e-9, (vx, vy, gap)
    assert all(abs(min(s.dist_pt(*v) for s in pad.shapes[0]) - 0.3 - CL) < 2e-6 for _s, v in cs[:8])
    # a round pad: the same distance in every direction; a rotated pad: its own axes
    b2 = board([P("A", 1, "GND", 10, 10, hw=0.5, hh=0.5, r=0.5), P("B", 1, "GND", 20, 10, hw=0.5, hh=0.3, rot=90),
                P("E", 1, "GND", 30, 10, hw=0.5, hh=0.3, r=0.15)])
    for _s, (vx, vy) in PW.dogbone_candidates(pad_of(b2, "A.1"), VIA, CL)[:8]:
        assert abs(math.hypot(vx - 10, vy - 10) - 1.0) < 2e-6
    assert [v for _s, v in PW.dogbone_candidates(pad_of(b2, "B.1"), VIA, CL)[:2]] == [(20.8, 10.0), (20.0, 11.0)]
    pe = pad_of(b2, "E.1")                                                             # rounded corners: exact edge
    for _s, v in PW.dogbone_candidates(pe, VIA, CL)[:8]:
        assert abs(pe.shapes[0][0].dist_pt(*v) - 0.5) < 2e-6


def _lane_board():
    """U1: a column of 3 wide pads at x = 10.5 (S1, GND, S2; pitch 0.65) and one at x = 6.5: the outward side
    of the first column is +x, where the exit lanes of S1 and S2 are."""
    pads = []
    for i, n in enumerate(("/S1", "GND", "/S2")):
        pads.append(P("U1", i + 1, n, 10.5, 9.35 + 0.65 * i, hw=0.6, hh=0.2))
    for i, n in enumerate(("/S3", "/S4", "/S5")):
        pads.append(P("U1", i + 4, n, 6.5, 9.35 + 0.65 * i, hw=0.6, hh=0.2))
    for i in range(5):
        pads.append(P(f"R{i + 1}", 1, f"/S{i + 1}", 40 + 3 * i, 30))
    pads.append(P("J1", 1, "GND", 50, 20, kind="tht", hw=0.8, hh=0.8, r=0.8))
    return board(pads, zones=[pour("GND", 1, (0, 0, 60, 40))])


def test_escape_lane_penalty_picks_the_non_blocking_site():
    b = _lane_board()
    pad = pad_of(b, "U1.2")
    reach = VIA[0] / 2 + CL
    east, west = (11.6, 10.0), (9.4, 10.0)
    assert [v for _s, v in PW.dogbone_candidates(pad, VIA, CL)[:3:2]] == [east, west]
    assert PW.lane_penalty(b, pad, *east, reach) == PW.LANE_PENALTY               # in the exit lanes of S1 / S2
    assert PW.lane_penalty(b, pad, *west, reach) == 0.0                           # under the part: nobody escapes there
    assert PW.lane_penalty(b, pad_of(b, "U1.1"), 12.0, 9.0, reach) == 0.0         # a pad's own lane is free for it
    assert PW.lane_penalty(b, pad, 10.5, 9.675, reach) >= PW.CUT_PENALTY          # between two neighbouring pads
    ctx = make_ctx(b)
    assert PW.congestion(ctx, *east) == PW.congestion(ctx, *west)                 # the penalty alone decides here
    orc = ctx.oracle
    for v in (east, west):                                                        # both sites are legal ...
        assert orc.via_ok(v[0], v[1], VIA[0], VIA[1], "GND") is None
        assert orc.seg_ok(0, 10.5, 10.0, v[0], v[1], 0.2, "GND") is None
    got = PW.place_dogbone(ctx, pad)
    assert got is not None and (got[-1].x1, got[-1].y1) == west                   # ... the non-blocking one wins
    # without the penalty the first candidate in canonical order (east) would have been taken
    keep = (PW.LANE_PENALTY, PW.CUT_PENALTY)
    try:
        PW.LANE_PENALTY = PW.CUT_PENALTY = 0.0
        ctx2 = make_ctx(_lane_board())
        got2 = PW.place_dogbone(ctx2, pad_of(ctx2.board, "U1.2"))
        assert (got2[-1].x1, got2[-1].y1) == east
    finally:
        PW.LANE_PENALTY, PW.CUT_PENALTY = keep


def test_dogbone_pieces():
    b = simple_board()
    ctx = make_ctx(b)
    pad = pad_of(b, "C1.2")
    assert PW.needs_dogbone(b, pad) and not PW.pour_reaches(b, pad)
    assert not PW.needs_dogbone(b, pad_of(b, "J1.1")) and PW.pour_reaches(b, pad_of(b, "J1.1"))   # through-hole, in the pour
    assert not PW.needs_dogbone(b, pad_of(b, "C1.1"))                                             # not a poured net
    v0 = ctx.oracle.version
    got = PW.place_dogbone(ctx, pad)
    assert [p.kind for p in got] == ["seg", "via"] and ctx.oracle.version > v0
    seg, via = got
    assert (seg.x1, seg.y1, seg.x2, seg.y2) == (10.0, 10.0, via.x1, via.y1) and seg.layer == 0 and seg.w == 0.2
    assert (via.w, via.drill, via.layer) == (0.6, 0.3, -1)
    for p in got:
        assert p.net == "GND" and p.conn == -1 and p.stage == "power" and p.protected
        assert ctx.oracle.piece_ok(p) is None and ctx.oracle.has_piece(p.id)
    assert (via.x1, via.y1) == (10.0, 9.2)        # a shortest stub in the emptier 1 mm cell; +x (legal too) is in
    #                                               the gap to the other pad of C1
    assert ctx.state.pieces() == got
    rec = ctx.history["power"]["dogbones"]["C1.2"]
    assert {k: rec[k] for k in ("site", "pieces", "net", "size")} == {
        "site": (10.0, 9.2), "pieces": [p.id for p in got], "net": "GND", "size": 0.6}
    # the via's own way into the pour is kept open too: 4 soft corridors around it on the pour layer
    sp = ctx.history["power"]["spokes"]
    assert len(rec["spokes"]) == 4 and sorted(sp[s] for s in rec["spokes"]) == [("C1.2", 1, k) for k in (4, 5, 6, 7)]
    hits = ctx.oracle.soft_hits(1, 10.5, 8.0, 10.5, 10.0, 0.2, "/S")              # a B.Cu track passing 0.5 mm east of it
    assert [h.rule for h in hits] == ["thermal"] and hits[0].net == "GND"
    assert ctx.oracle.soft_hits(0, 10.5, 8.0, 10.5, 9.0, 0.2, "/S") == []         # not on the layer without the pour
    assert PW.pour_penalty(b, "GND", 10.0, 9.2, 0.3) == 0.0
    assert PW.lane_penalty(b, pad, 11.0, 10.0, 0.5) == PW.CUT_PENALTY and PW.lane_penalty(b, pad, 10.0, 9.2, 0.5) == 0.0
    assert PW.congestion(ctx, 10.0, 9.2) < PW.congestion(ctx, 10.0, 10.8)
    assert ctx.oracle.via_ok(11.0, 10.0, 0.6, 0.3, "GND", ignore=frozenset(p.id for p in got)) is None


def test_no_legal_site_is_reported():
    # vias are forbidden everywhere a dog-bone could land
    b = simple_board(zones=[pour("GND", 1, (0, 0, 60, 40)), keepout(6, 6, 14, 14)])
    ctx = make_ctx(b)
    pad = pad_of(b, "C1.2")
    assert PW.place_dogbone(ctx, pad) is None and ctx.state.pieces() == []
    rep = PW.run(ctx, Budget(10 ** 6))
    assert rep.data["power_unreserved"] == ["C1.2"] and rep.data["dogbones"] == {}
    assert rep.data["unreserved_why"] == {"C1.2": "no legal via site"}
    assert ctx.history["power"]["unreserved"] == ["C1.2"] and any("C1.2" in n for n in rep.notes)
    assert not [p for p in ctx.state.pieces() if p.protected]
    # the pour is nowhere near the pad: said so, differently
    b2 = simple_board(zones=[pour("GND", 1, (30, 20, 60, 40))])
    ctx2 = make_ctx(b2)
    rep2 = PW.run(ctx2, Budget(10 ** 6))
    assert rep2.data["unreserved_why"] == {"C1.2": "pour out of reach"}
    # walled in by foreign copper on the pad's layer: no stub can leave
    wall = [P(f"W{i}", 1, "/S", 10 + dx, 10 + dy, hw=0.45, hh=0.45) for i, (dx, dy) in enumerate(
        [(-1.2, -1.0), (0, -1.0), (1.2, -1.0), (-1.2, 1.0), (0, 1.0), (1.2, 1.0), (-1.3, 0)])]
    wall.append(P("W9", 1, "/S", 11.15, 10, hw=0.25, hh=0.45))
    b3 = simple_board(extra=wall)
    ctx3 = make_ctx(b3)
    rep3 = PW.run(ctx3, Budget(10 ** 6))
    assert rep3.data["power_unreserved"] == ["C1.2"] and rep3.data["unreserved_why"]["C1.2"] == "no legal via site"


def test_pour_reach_and_boundary():
    isl = [(1, 1), (25, 1), (25, 25), (1, 25)]
    pads = [P("A", 1, "GND", 10, 10, c=1), P("B", 1, "GND", 40, 10, c=2), P("D", 1, "GND", 10, 30, layer=1, c=3),
            P("E", 1, "GND", 50, 30, c=4), P("E", 2, "GND", 51.5, 30, c=4)]
    b = board(pads, zones=[pour("GND", 0, (0, 0, 60, 40), islands=[isl])])
    assert b.clusters["GND"][1]["islands"] and not b.clusters["GND"][2]["islands"]
    assert PW.pour_reaches(b, pad_of(b, "A.1")) and not PW.needs_dogbone(b, pad_of(b, "A.1"))    # owns a fill island
    assert not PW.pour_reaches(b, pad_of(b, "B.1")) and PW.needs_dogbone(b, pad_of(b, "B.1"))    # filled, but not here
    assert not PW.pour_reaches(b, pad_of(b, "D.1")) and PW.needs_dogbone(b, pad_of(b, "D.1"))    # other layer
    assert PW.pour_penalty(b, "GND", 10, 10, 0.3) == 0.0                                         # well inside the island
    assert PW.pour_penalty(b, "GND", 24.8, 10, 0.3) == PW.ISLAND_PENALTY                         # on its edge
    assert PW.pour_penalty(b, "GND", 40, 10, 0.3) == PW.ISLAND_PENALTY                           # in the outline, no fill there
    assert PW.pour_penalty(b, "GND", 59.9, 10, 0.3) == PW.ISLAND_PENALTY                         # on the outline
    assert PW.pour_penalty(b, "/nope", 10, 10, 0.3) is None
    b2 = board(pads, zones=[pour("GND", 0, (0, 0, 30, 40))])                                     # unfilled: the outline decides
    assert PW.pour_reaches(b2, pad_of(b2, "A.1")) and not PW.pour_reaches(b2, pad_of(b2, "B.1"))
    assert PW.pour_penalty(b2, "GND", 40, 10, 0.3) is None and PW.pour_penalty(b2, "GND", 10, 10, 0.3) == 0.0
    # one dog-bone per unreached cluster, not per pad (E.1 + E.2 are one cluster)
    ctx = make_ctx(board(pads, zones=[pour("GND", 1, (0, 0, 60, 40))]))
    rep = PW.run(ctx, Budget(10 ** 6))
    assert sorted(rep.data["dogbones"]) == ["A.1", "B.1", "E.1"] and rep.data["power_unreserved"] == []
    # the big-fill index agrees with the plain polygon test
    import random
    from copper_kicad.pcb.guided import geom
    rnd = random.Random(3)
    star = [(30 + (12 if i % 2 else 5) * math.cos(i * math.pi / 9), 20 + (12 if i % 2 else 5) * math.sin(i * math.pi / 9))
            for i in range(18)]
    rings = PW._Rings([star])
    for _ in range(400):
        x, y = rnd.uniform(15, 45), rnd.uniform(5, 35)
        assert rings.contains(x, y) == geom.pt_in_poly(star, x, y)
        d = abs(geom.pt_poly_dist(star, x, y))
        if d < 0.5:
            assert abs(rings.edge_dist(x, y, 0.5) - d) < 1e-9
        else:
            assert rings.edge_dist(x, y, 0.5) >= 0.5


def _edge_connector():
    """J2 on the board edge (x = 58.4): ground fingers on F.Cu, signal fingers on B.Cu between them, a ground
    pour on B.Cu, and the net class's 1.65 mm via: a via for every ground finger would fence the signals in."""
    pads = []
    for i in range(4):
        pads.append(P("J2", 6 + i, "GND", 56.6, 10 + 2.77 * i, hw=1.74, hh=0.92))
    for i in range(3):
        pads.append(P("J2", 1 + i, f"/S{i}", 56.6, 11.385 + 2.77 * i, hw=1.74, hh=0.92, layer=1))
        pads.append(P(f"R{i}", 1, f"/S{i}", 20, 10 + 3 * i))
    pads.append(P("J1", 1, "GND", 10, 30, kind="tht", hw=0.8, hh=0.8, r=0.8))
    nets = {"GND": {"width": 0.635, "clearance": 0.3, "via": 1.651, "drill": 0.635}}
    nets.update({f"/S{i}": {"width": 0.635, "clearance": 0.3} for i in range(3)})
    return board(pads, zones=[pour("GND", 1, (0, 0, 60, 40))], nets=nets, rules={"edge_clearance": 0.2})


def test_reservations_do_not_wall_in_a_neighbour():
    b = _edge_connector()
    ctx = make_ctx(b)
    rep = PW.run(ctx, Budget(10 ** 6))
    got, why = rep.data["dogbones"], rep.data["unreserved_why"]
    # the two end fingers put their via beyond the row; the inner ones can only go west, and a via on both would
    # leave 1.12 mm between them: less than a 0.635 mm track and two 0.3 mm clearances for the signal finger
    # in between. The second one gets a stub to its neighbour's via instead (on F.Cu, where only ground lives).
    assert sorted(got) == ["J2.6", "J2.7", "J2.8", "J2.9"] and rep.data["power_unreserved"] == [] and why == {}
    assert rep.data["shared"] == ["J2.8"] and ctx.history["power"]["dogbones"]["J2.8"]["shares"] == "J2.7"
    assert len([p for p in ctx.state.pieces() if p.kind == "via"]) == 3
    shared = [ctx.state.piece(i) for i in got["J2.8"]]
    assert all(p.kind == "seg" and p.layer == 0 and p.protected and orc_ok(ctx, p) for p in shared)
    assert (shared[0].x1, shared[0].y1) == (56.6, 15.54)
    assert (shared[-1].x2, shared[-1].y2) == tuple(ctx.history["power"]["dogbones"]["J2.7"]["site"])
    assert PW.replace_dogbone(ctx, "J2.7") is None                 # a shared via is not moved from under its users
    # without a via to share it waits for the pours pass, and says why
    keep = PW.SHARE_REACH
    try:
        PW.SHARE_REACH = 0.0
        ctx0 = make_ctx(_edge_connector())
        rep0 = PW.run(ctx0, Budget(10 ** 6))
    finally:
        PW.SHARE_REACH = keep
    assert sorted(rep0.data["dogbones"]) == ["J2.6", "J2.7", "J2.9"] and rep0.data["power_unreserved"] == ["J2.8"]
    assert rep0.data["unreserved_why"] == {"J2.8": "would wall in J2.2"} and any("wall in" in n for n in rep0.notes)
    site7 = tuple(ctx.history["power"]["dogbones"]["J2.7"]["site"])
    assert site7[1] == 12.77 and site7[0] < 56.6 - 1.74
    pad8 = pad_of(b, "J2.8")
    legal = [v for _s, v in PW.dogbone_candidates(pad8, (1.651, 0.635), 0.3)
             if ctx.oracle.via_ok(v[0], v[1], 1.651, 0.635, "GND") is None]
    assert legal and all(v[1] == 15.54 for v in legal)          # it had legal sites: it was the wall, not the rules
    orc = ctx.oracle
    for i in range(3):                      # each signal finger still leaves westwards on its own layer
        y = 11.385 + 2.77 * i
        ys = [y + d for d in (0.0, 0.2, -0.2, 0.4, -0.4, 0.6, -0.6)]
        assert any(orc.seg_ok(1, 56.6, y, 54.0, y2, 0.635, f"/S{i}") is None
                   and orc.seg_ok(1, 54.0, y2, 50.0, y2, 0.635, f"/S{i}") is None for y2 in ys), i
    # the check itself: one via beside a channel leaves room, the second one closes it
    assert PW.lane_blocked(b, pad8, site7[0], 15.54, 0.8255, [(site7[0], site7[1], 0.8255)]) == "J2.2"
    assert PW.lane_blocked(b, pad8, site7[0], 15.54, 0.8255, []) is None
    far = [(site7[0], 30.0, 0.8255), (site7[0], 2.0, 0.8255)]            # vias elsewhere change nothing
    assert PW.lane_blocked(b, pad8, site7[0], 15.54, 0.8255, far) is None
    assert PW.lane_blocked(b, pad8, site7[0], 15.54, 0.8255, far + [(site7[0], site7[1], 0.8255)]) == "J2.2"
    assert PW.lane_blocked(b, pad8, site7[0], 15.54, 0.3, [(site7[0], site7[1], 0.3)]) is None      # small vias fit
    # a site the plan reserves is used even so (the caller's decision)
    ctx2 = make_ctx(_edge_connector(), x=X(reserve=[{"at": (site7[0], 15.54), "net": "GND"}]))
    rep2 = PW.run(ctx2, Budget(10 ** 6))
    assert rep2.data["power_unreserved"] == [] and rep2.data["shared"] == []
    assert tuple(ctx2.history["power"]["dogbones"]["J2.8"]["site"]) == (site7[0], 15.54)       # ... by the pad in front of it
    assert tuple(ctx2.history["power"]["dogbones"]["J2.7"]["site"]) == site7


def test_pour_on_a_signal_layer_is_reserved_softly():
    # the default plan routes signals on both layers of a 2-layer board: the B.Cu pour is no plane
    b = simple_board()
    ctx = make_ctx(b, None)
    assert PW.signal_layers(ctx) == frozenset({0, 1}) and not PW.on_plane(ctx, "GND", 10.0, 9.2)
    rep = PW.run(ctx, Budget(10 ** 6))
    assert rep.data["dogbones"] == {} and rep.data["power_unreserved"] == [] and ctx.state.pieces() == []
    assert rep.data["soft_reserved"] == {"C1.2": [10.0, 9.2]} and any("softly" in n for n in rep.notes)
    orc = ctx.oracle
    hits = orc.soft_hits(1, 9.0, 9.2, 11.0, 9.2, 0.2, "/S")                 # other nets see the reserved site, at a price
    assert [h.rule for h in hits] == ["reserve"] and hits[0].net == "GND"
    assert orc.seg_ok(1, 9.0, 9.2, 11.0, 9.2, 0.2, "/S") is None and orc.via_ok(10.0, 9.2, 0.6, 0.3, "/S") is None
    assert orc.soft_hits(1, 9.0, 9.2, 11.0, 9.2, 0.2, "GND") == []
    before = orc.version
    assert PW.run(ctx, Budget(10 ** 6)).data["soft_reserved"] == {"C1.2": [10.0, 9.2]} and orc.version == before
    # the pours pass asks for the dog-bone: now it is copper, on the kept site, and the reservation is gone
    pad = pad_of(b, "C1.2")
    got = PW.place_dogbone(ctx, pad)
    assert [p.kind for p in got] == ["seg", "via"] and (got[-1].x1, got[-1].y1) == (10.0, 9.2)
    assert ctx.history["power"]["soft"] == {} and "C1.2" in ctx.history["power"]["dogbones"]
    assert [h.rule for h in orc.soft_hits(1, 10.0, 8.5, 10.0, 9.0, 0.2, "/S")] == ["thermal"]     # its via corridor now
    # a site that signals took in the meantime is simply not used: the next best one is
    ctx2 = make_ctx(simple_board(), None)
    PW.run(ctx2, Budget(10 ** 6))
    ctx2.state.add(net="/S", conn=-1, kind="seg", layer=1, x1=9.0, y1=9.2, x2=11.0, y2=9.2, w=0.2)
    got2 = PW.place_dogbone(ctx2, pad_of(ctx2.board, "C1.2"))
    assert got2 and (got2[-1].x1, got2[-1].y1) != (10.0, 9.2) and all(orc_ok(ctx2, p) for p in got2)
    assert ctx2.history["power"]["soft"] == {}
    # a hard placement that fails keeps the soft reservation; a rolled-back plan site becomes a reservation again
    zones = [pour("GND", 1, (0, 0, 60, 40)), keepout(6, 6, 13, 14)]
    ctx4 = make_ctx(simple_board(zones=zones), None, x=X(reserve=[{"at": (13.5, 11.0), "net": "GND"}]))
    rep4 = PW.run(ctx4, Budget(10 ** 6))
    assert rep4.data["soft_reserved"] == {"C1.2": [13.5, 11.0]} and rep4.data["dogbones"] == {}
    pad4 = pad_of(ctx4.board, "C1.2")
    assert PW.place_dogbone(ctx4, pad4, exclude=frozenset({(13.5, 11.0)})) is None
    assert list(ctx4.history["power"]["soft"]) == ["C1.2"]
    tok = ctx4.state.begin()
    assert PW.place_dogbone(ctx4, pad4) and ctx4.history["power"]["reserve"] == {}
    ctx4.state.rollback(tok)                                       # e.g. a repair attempt that did not pay
    assert ctx4.state.pieces() == []
    PW._gc(ctx4)
    assert list(ctx4.history["power"]["reserve"]) == [(13.5, 11.0)] and ctx4.history["power"]["dogbones"] == {}
    assert [k.rule for k in ctx4.oracle.soft_hits(0, 13.0, 11.0, 14.0, 11.0, 0.2, "/S")] == ["reserve"]
    # with the pour layer closed to signals the same board gets its copper in S1
    ctx3 = make_ctx(simple_board())
    assert PW.signal_layers(ctx3) == frozenset({0}) and PW.on_plane(ctx3, "GND", 10.0, 9.2)
    rep3 = PW.run(ctx3, Budget(10 ** 6))
    assert list(rep3.data["dogbones"]) == ["C1.2"] and rep3.data["soft_reserved"] == {}


# ── thermal spokes ────────────────────────────────────────────────────────
def test_thermal_spokes_for_th_and_smd_pads():
    pads = [P("J1", 1, "GND", 20, 20, kind="tht", hw=0.8, hh=0.8, r=0.8),            # round through-hole pad
            P("C1", 2, "GND", 10, 10, hw=0.5, hh=0.3),                              # SMD on F.Cu
            P("C2", 2, "GND", 30, 10, hw=0.5, hh=0.3, layer=1),                     # SMD on B.Cu: no pour there
            P("C3", 2, "GND", 55, 35, hw=0.5, hh=0.3),                              # outside the pour outline
            P("R1", 1, "/S", 12, 10), P("R2", 1, "/S", 40, 30)]
    b = board(pads, zones=[pour("GND", 0, (0, 0, 50, 30))])
    th = PW.thermal_spokes(b, pad_of(b, "J1.1"))
    assert len(th) == 4 and all(s.cls == "soft" and s.kind == "box" and s.layers == (0,) for s in th)
    b2 = board(pads, zones=[pour("GND", 0, (0, 0, 50, 30)), pour("GND", 1, (0, 0, 50, 30))])
    th2 = PW.thermal_spokes(b2, pad_of(b2, "J1.1"))
    assert len(th2) == 8 and sorted(s.layers[0] for s in th2) == [0] * 4 + [1] * 4          # every pour layer of the pad
    s2 = 1 / math.sqrt(2)
    for k, s in enumerate(th):                                                              # round pad: the diagonals
        cx, cy, ux, uy, hl, hw, r = s.g
        assert abs(abs(ux) - s2) < 1e-9 and abs(abs(uy) - s2) < 1e-9 and r == 0.0
        assert abs(hw - PW.SPOKE_WIDTH / 2) < 1e-12 and abs(2 * hl - (PW.SPOKE_GAP + PW.SPOKE_REACH)) < 1e-12
        assert abs(math.hypot(cx - 20, cy - 20) - (0.8 + hl)) < 1e-9                        # starts at the pad edge
        assert s.tag == ("thermal", "J1.1", k) and s.net == b.nets["GND"].id
    sm = PW.thermal_spokes(b, pad_of(b, "C1.2"))
    assert len(sm) == 4
    assert [(round(s.g[2], 6), round(s.g[3], 6)) for s in sm] == [(1, 0), (0, 1), (-1, 0), (0, -1)]   # the pad's axes
    assert abs(sm[0].g[0] - (10 + 0.5 + 0.4)) < 1e-9 and abs(sm[1].g[1] - (10 + 0.3 + 0.4)) < 1e-9
    assert PW.thermal_spokes(b, pad_of(b, "C2.2")) == []          # the pour is not on its layer
    assert PW.thermal_spokes(b, pad_of(b, "C3.2")) == []          # outside the zone
    assert PW.thermal_spokes(b, pad_of(b, "R1.1")) == []          # not a poured net
    # the stage registers them as soft shapes: priced, never illegal, invisible to their own net
    ctx = make_ctx(b)
    rep = PW.run(ctx, Budget(10 ** 6))
    assert rep.data["spokes"] == 8
    spokes = ctx.history["power"]["spokes"]
    assert sorted(v for v in spokes.values() if v[2] < 4) == sorted(
        [("C1.2", 0, k) for k in range(4)] + [("J1.1", 0, k) for k in range(4)])
    # (C2.2 on B.Cu got a dog-bone into the F.Cu pour: its via has corridors of its own, numbered 4..7)
    assert sorted(v for v in spokes.values() if v[2] >= 4) == [("C2.2", 0, k) for k in (4, 5, 6, 7)]
    orc = ctx.oracle
    hits = orc.soft_hits(0, 8.0, 10.6, 12.0, 10.6, 0.2, "/S")     # a foreign track across the +y spoke of C1.2
    assert [h.rule for h in hits] == ["thermal"] and hits[0].net == "GND" and hits[0].tag.startswith("soft:")
    assert spokes[int(hits[0].tag.split(":")[1])] == ("C1.2", 0, 1)
    assert orc.soft_hits(0, 8.0, 10.6, 12.0, 10.6, 0.2, "GND") == []
    assert orc.seg_ok(0, 8.0, 10.75, 9.0, 10.75, 0.2, "/S") is None                        # soft, not hard
    assert len(ctx.history["power"]["book"]["spokes"]) == 12


def test_ground_left_for_the_pour_keeps_its_future_thermal_spokes_free():
    """A ground net with 6+ pads and no pour is skipped ("left for pcb_pour"). The pour comes after the routing:
    a track laid through a pad's spoke corridor now is a starved thermal relief then. The corridors a default
    pour needs are reserved (soft) on every copper layer of the pad, exactly like those of an existing pour."""
    pads = [P(f"C{i}", 2, "GND", 8 + 6 * i, 10, hw=0.5, hh=0.3) for i in range(5)]
    pads += [P("J1", 1, "GND", 20, 20, kind="tht", hw=0.8, hh=0.8, r=0.8), P("R1", 1, "/S", 12, 14), P("R2", 1, "/S", 40, 30)]
    b = board(pads, zones=[])
    cfg = plan.resolve(b, None)
    assert cfg.nets["GND"].skip and PW.left_for_pour(cfg, "GND") and not PW.left_for_pour(cfg, "/S")
    assert PW.thermal_spokes(b, pad_of(b, "C0.2")) == []                    # no pour, nobody asked: as before
    sm = PW.thermal_spokes(b, pad_of(b, "C0.2"), future=True)
    assert len(sm) == 4 and all(s.cls == "soft" and s.layers == (0,) for s in sm)
    assert abs(sm[0].g[5] - PW.SPOKE_WIDTH / 2) < 1e-12 and abs(2 * sm[0].g[4] - (PW.SPOKE_GAP + PW.SPOKE_REACH)) < 1e-12
    th = PW.thermal_spokes(b, pad_of(b, "J1.1"), future=True)
    assert len(th) == 8 and sorted(s.layers[0] for s in th) == [0] * 4 + [1] * 4
    ctx = make_ctx(b, None)
    rep = PW.run(ctx, Budget(10 ** 6))
    assert rep.data["spokes"] == 5 * 4 + 8, rep.data["spokes"]
    hits = ctx.oracle.soft_hits(0, 6.0, 10.6, 10.0, 10.6, 0.2, "/S")        # a foreign track across the +y spoke of C0.2
    assert [h.rule for h in hits] == ["thermal"] and hits[0].net == "GND"
    assert ctx.oracle.seg_ok(0, 6.0, 10.75, 7.0, 10.75, 0.2, "/S") is None  # soft: priced, never illegal
    # the plan routes the ground as tracks (skip: false) or names another reason: no reservation
    for p_ in ({"nets": {"GND": {"skip": False}}}, {"nets": {"GND": {"skip": True}}}):
        cfg2 = plan.resolve(b, p_)
        assert not PW.left_for_pour(cfg2, "GND"), p_
        ctx2 = make_ctx(b, p_)
        assert PW.run(ctx2, Budget(10 ** 6)).data["spokes"] == 0, p_


def test_book_into_tiles():
    class FakeTiles:
        def __init__(self):
            self.calls = []

        def tile_of(self, x, y):
            return int(math.floor(x)), int(math.floor(y))

        def via_cap(self, tx, ty):
            return 3

        def book(self, layer, tx, ty, side, mm):
            self.calls.append(("edge", layer, tx, ty, side, mm))

        def book_via(self, tx, ty, n=1):
            self.calls.append(("via", tx, ty, n))

    b = simple_board(zones=[pour("GND", 0, (0, 0, 60, 40)), pour("GND", 1, (0, 0, 60, 40))],
                     extra=[P("C9", 2, "GND", 30.2, 10.5, hw=0.5, hh=0.3, layer=1)])
    b.clusters   # (C1.2 sits in the F.Cu pour: spokes only; nothing needs a dog-bone on this board)
    t = FakeTiles()
    ctx = make_ctx(b, tiles=t)
    rep = PW.run(ctx, Budget(10 ** 6))
    assert rep.data["spokes"] == 4 + 8 + 4 and rep.data["dogbones"] == {}
    edges = [c for c in t.calls if c[0] == "edge"]
    # C1.2 at (10, 10): its -x spoke runs from x = 9.5 to 8.7 and crosses the border between tiles 8 and 9
    assert ("edge", 0, 8, 10, "E", 0.5) in edges
    # ... its -y spoke from y = 9.7 to 8.9 the border between rows 8 and 9; +x (10.5 .. 11.3) and +y (10.3 .. 11.1)
    # leave tile (10, 10) through its own E and N borders
    assert ("edge", 0, 10, 8, "N", 0.5) in edges
    assert ("edge", 0, 10, 10, "E", 0.5) in edges and ("edge", 0, 10, 10, "N", 0.5) in edges
    assert len([c for c in edges if c[1] == 0 and c[2] in (8, 10) and c[3] in (8, 10)]) == 4
    n0 = len(t.calls)
    assert ctx.history["power"]["book"]["booked"] and PW.book(ctx) == 0 and len(t.calls) == n0        # booked once
    assert PW.book(ctx, force=True) == n0 and len(t.calls) == 2 * n0
    # a dog-bone books its via column
    t2 = FakeTiles()
    ctx2 = make_ctx(simple_board(), tiles=t2)
    PW.run(ctx2, Budget(10 ** 6))
    assert ("via", 10, 9, 1) in t2.calls
    # no tiles yet: the bookings wait in ctx.history until power.book(ctx, tiles) is called
    ctx3 = make_ctx(simple_board())
    PW.run(ctx3, Budget(10 ** 6))
    t3 = FakeTiles()
    assert not ctx3.history["power"]["book"].get("booked") and PW.book(ctx3, t3) == len(t3.calls) > 0
    assert ("via", 10, 9, 1) in t3.calls


# ── local-only necking ────────────────────────────────────────────────────
def _gap_board(gap):
    """Two foreign pads above and below y = 10 around x = 20, ``gap`` apart; net /N runs between them."""
    h = 0.3
    return board([P("A", 1, "/A", 20, 10 - gap / 2 - h, hw=1.0, hh=h), P("B", 1, "/B", 20, 10 + gap / 2 + h, hw=1.0, hh=h),
                  P("N", 1, "/N", 5, 10), P("N", 2, "/N", 35, 10), P("A", 2, "/A", 50, 5), P("B", 2, "/B", 50, 35)])


def test_neck_split_narrows_only_where_it_has_to():
    orc = O.Oracle(_gap_board(0.6))
    assert orc.seg_ok(0, 10, 10, 30, 10, 0.4, "/N") is not None and orc.seg_ok(0, 10, 10, 30, 10, 0.2, "/N") is None
    spans = PW.neck_split(orc, 0, 10, 10, 30, 10, 0.4, 0.2, "/N")
    assert [s[4] for s in spans] == [0.4, 0.2, 0.4]                               # wide - narrow - wide
    assert spans[0][:2] == (10.0, 10.0) and spans[-1][2:4] == (30.0, 10.0)
    for a, c in zip(spans, spans[1:]):
        assert a[2:4] == c[0:2]                                                   # contiguous
    for x1, y1, x2, y2, w in spans:
        assert orc.seg_ok(0, x1, y1, x2, y2, w, "/N") is None                     # every span is legal as returned
    n = spans[1]
    assert n[0] <= 19.0 - 0.26 and n[2] >= 21.0 + 0.26                            # it covers the 2 mm between the pads
    assert PW.necked_length(spans, 0.4) == n[2] - n[0] <= 2.0 + 2 * 0.8 + 0.3     # ... plus at most one leaf span per side
    assert PW.necked_length(spans, 0.4) / 20.0 < 0.2                              # never the whole length
    assert PW.neck_split(orc, 0, 10, 10, 30, 10, 0.4, 0.2, "/N") == spans         # same twice
    # a finer leaf size narrows less
    fine = PW.neck_split(orc, 0, 10, 10, 30, 10, 0.4, 0.2, "/N", min_span=0.2)
    assert [s[4] for s in fine] == [0.4, 0.2, 0.4] and PW.necked_length(fine, 0.4) < PW.necked_length(spans, 0.4)
    # the narrow span gets the widest width that fits, not the minimum
    orc7 = O.Oracle(_gap_board(0.7))
    s7 = PW.neck_split(orc7, 0, 10, 10, 30, 10, 0.4, 0.2, "/N")
    assert [s[4] for s in s7] == [0.4, 0.3, 0.4]
    assert orc7.seg_ok(0, s7[1][0], 10, s7[1][2], 10, 0.31, "/N") is not None
    # legal at full width: untouched; illegal even at the neck width: None; no neck allowed: None
    assert PW.neck_split(orc, 0, 10, 12, 30, 12, 0.4, 0.2, "/N") == [(10.0, 12.0, 30.0, 12.0, 0.4)]
    assert PW.neck_split(O.Oracle(_gap_board(0.5)), 0, 10, 10, 30, 10, 0.4, 0.2, "/N") is None
    assert PW.neck_split(orc, 0, 10, 10, 30, 10, 0.4, 0.4, "/N") is None
    # a 45 degree segment stays exactly on its line
    b = board([P("A", 1, "/A", 20.45, 19.55, hw=0.2, hh=0.2), P("N", 1, "/N", 5, 5), P("N", 2, "/N", 35, 35), P("A", 2, "/A", 50, 5)])
    orcd = O.Oracle(b)
    sd = PW.neck_split(orcd, 0, 10, 10, 30, 30, 0.4, 0.2, "/N")
    assert sd is not None and len(sd) == 3 and sd[1][4] < 0.4
    for x1, y1, x2, y2, w in sd:
        assert abs((x2 - x1) - (y2 - y1)) < 1e-9 and orcd.seg_ok(0, x1, y1, x2, y2, w, "/N") is None
    assert PW.necked_length(sd, 0.4) < 0.15 * math.hypot(20, 20)


def test_pattern_shapes():
    for a, b_, n in (((0, 0), (10, 4), 4), ((0, 0), (-3, 9), 4), ((0, 0), (5, 5), 1), ((2, 2), (9, 2), 1), ((1, 1), (1, -6), 1),
                     ((0, 0), (0, 0), 0), ((0, 0), (4, -4), 1)):
        shapes = PW.pattern_shapes(*a, *b_)
        assert len(shapes) == n, (a, b_, len(shapes))
        for pts in shapes:
            assert pts[0] == (float(a[0]), float(a[1])) and pts[-1] == (float(b_[0]), float(b_[1]))
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                assert octilinear(x1, y1, x2, y2) and (x1, y1) != (x2, y2)
            L = sum(math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(pts, pts[1:]))
            assert abs(L - C._octile(b_[0] - a[0], b_[1] - a[1])) < 1e-6          # all of them are shortest
    s = PW.pattern_shapes(0, 0, 10, 4)
    assert [len(p) for p in s] == [3, 3, 4, 4] and s[0][1] == (4.0, 4.0) and s[1][1] == (6.0, 0.0)
    assert s[2][1:3] == [(3.0, 0.0), (7.0, 4.0)] and s[3][1:3] == [(2.0, 2.0), (8.0, 2.0)]


def _decap_board(extra=()):
    pads = []
    for i in range(4):
        pads.append(P("U1", i + 1, ["+3V3", "/A", "/B", "GND"][i], 20, 10 + i * 1.27, hw=0.6, hh=0.25))
    pads += [P("C1", 1, "+3V3", 17.5, 10, hw=0.4, hh=0.45), P("C1", 2, "GND", 16, 10, hw=0.4, hh=0.45),
             P("J1", 1, "+3V3", 5, 30, kind="tht", hw=0.8, hh=0.8, r=0.8), P("J1", 2, "GND", 5, 33, kind="tht", hw=0.8, hh=0.8, r=0.8),
             P("R1", 1, "/A", 40, 10), P("R2", 1, "/B", 40, 14)] + list(extra)
    return board(pads, zones=[pour("GND", 1, (0, 0, 60, 40))])


def test_decoupling_stub_is_routed_first_at_power_width():
    ctx = make_ctx(_decap_board())
    (idx,) = ctx.history["classify"]["decaps"]
    c = ctx.conns[idx]
    assert (c.a.pad, c.b.pad, c.width, c.neck) == ("C1.1", "U1.1", 0.4, 0.2)
    bud = Budget(10 ** 6)
    rep = PW.run(ctx, bud)
    assert rep.name == "power" and rep.routed == 1 and rep.failed == 0 and rep.data["decaps"] == {"routed": [idx], "failed": []}
    assert ctx.state.status(idx) == "routed" and ctx.state.connected(idx)
    ps = ctx.state.pieces(conn=idx)
    assert len(ps) == 1 and (ps[0].x1, ps[0].y1, ps[0].x2, ps[0].y2) == (17.5, 10.0, 20.0, 10.0)
    assert ps[0].w == 0.4 and ps[0].target_w == 0.4 and ps[0].stage == "power" and not ps[0].protected and ps[0].layer == 0
    assert all(ctx.oracle.piece_ok(p) is None for p in ctx.state.pieces())
    assert bud.used == rep.work >= 200                                            # one pattern attempt + oracle checks
    # the two ground pads got their dog-bones around the stub
    assert sorted(rep.data["dogbones"]) == ["C1.2", "U1.4"] and rep.data["power_unreserved"] == []
    assert rep.score == ctx.state.score()
    # the other +3V3 connection is left to the detail stage
    assert [ctx.state.status(k.idx) for k in ctx.conns if k.net == "+3V3" and k.idx != idx] == ["todo"]
    # running the stage again changes nothing
    before = ctx.state.pieces()
    rep2 = PW.run(ctx, Budget(10 ** 6))
    assert ctx.state.pieces() == before and rep2.routed == 0 and rep2.data["dogbones"] == rep.data["dogbones"]


def test_decoupling_stub_is_necked_locally_not_whole():
    # a foreign pad 0.15 mm from where the 0.4 mm stub would run: only the span beside it is narrowed
    ctx = make_ctx(_decap_board(extra=[P("TP1", 1, "/A", 18.75, 9.55, hw=0.1, hh=0.1)]))
    (idx,) = ctx.history["classify"]["decaps"]
    rep = PW.run(ctx, Budget(10 ** 6))
    assert rep.routed == 1 and ctx.state.connected(idx)
    ps = ctx.state.pieces(conn=idx)
    ws = [p.w for p in ps]
    assert min(ws) == 0.3 and max(ws) == 0.4 and len(ps) >= 2                     # 0.3 is the widest that fits there
    assert all(p.target_w == 0.4 for p in ps)                                     # polish knows what to restore
    assert all(ctx.oracle.piece_ok(p) is None for p in ps)
    necked = sum(p.length for p in ps if p.w < 0.4)
    assert 0 < necked < 2.5 and abs(sum(p.length for p in ps) - 2.5) < 1e-9
    assert abs(ctx.state.score().necked_mm - necked) < 1e-6
    # no room even at the neck width: the connection is left for the detail stage, nothing is added
    ctx2 = make_ctx(_decap_board(extra=[P("TP1", 1, "/A", 18.75, 9.72, hw=0.1, hh=0.1), P("TP2", 1, "/B", 18.75, 10.28, hw=0.1, hh=0.1)]))
    (idx2,) = ctx2.history["classify"]["decaps"]
    rep2 = PW.run(ctx2, Budget(10 ** 6))
    assert rep2.failed == 1 and rep2.data["decaps"]["failed"] == [idx2] and ctx2.state.pieces(conn=idx2) == []
    assert ctx2.state.status(idx2) == "todo"


def test_fine_pitch_stub_is_necked_next_to_its_neighbour():
    # two poured nets on neighbouring 0.5 mm-pitch pads, class width 0.4: side by side the stubs cannot both be 0.4
    pads = []
    nets = ["/S0", "GND", "VDD", "/S1", "/S2"]
    for i, n in enumerate(nets):
        pads.append(P("U1", i + 1, n, 20 + 0.5 * i, 10, hw=0.14, hh=0.7))
    for i in range(5):
        pads.append(P("U1", 6 + i, f"/T{i}", 20 + 0.5 * i, 17, hw=0.14, hh=0.7))
        pads.append(P(f"R{i}", 1, f"/T{i}", 40 + 2 * i, 30))
    pads += [P("R7", 1, "/S0", 40, 5), P("R8", 1, "/S1", 43, 5), P("R9", 1, "/S2", 46, 5),
             P("J1", 1, "GND", 5, 30, kind="tht", hw=0.8, hh=0.8, r=0.8), P("J1", 2, "VDD", 5, 33, kind="tht", hw=0.8, hh=0.8, r=0.8)]
    b = board(pads, zones=[pour("GND", 1, (0, 0, 60, 40)), pour("VDD", 1, (0, 0, 60, 40))],
              nets={"GND": {"width": 0.4, "clearance": 0.15, "via": 0.5, "drill": 0.25},
                    "VDD": {"width": 0.4, "clearance": 0.15, "via": 0.5, "drill": 0.25}},
              rules={"hole_clearance": 0.1, "hole_to_hole": 0.2})
    ctx = make_ctx(b)
    t = ctx.history["classify"]["nets"]
    assert (t["GND"]["width"], t["GND"]["neck"]) == (0.4, 0.2)
    rep = PW.run(ctx, Budget(10 ** 6))
    assert sorted(rep.data["dogbones"]) == ["U1.2", "U1.3"] and rep.data["power_unreserved"] == []
    assert all(ctx.oracle.piece_ok(p) is None for p in ctx.state.pieces())
    segs = [p for p in ctx.state.pieces() if p.kind == "seg"]
    assert all(p.target_w == 0.4 and 0.2 <= p.w <= 0.4 for p in segs)
    assert any(p.w < 0.4 for p in segs)                                           # narrowed where the neighbour is ...
    assert ctx.state.score().necked_mm > 0


def test_plane_connection_closed_by_its_dogbone():
    # a filled pour: the through-hole pin owns the fill island, the SMD pad on the other side does not
    isl = [(0.5, 0.5), (59.5, 0.5), (59.5, 39.5), (0.5, 39.5)]
    pads = [P("C1", 2, "GND", 10, 10, hw=0.5, hh=0.3), P("C1", 1, "/S", 12, 10, hw=0.5, hh=0.3), P("R1", 1, "/S", 40, 30),
            P("J1", 1, "GND", 50, 30, kind="tht", hw=0.8, hh=0.8, r=0.8)]
    b = board(pads, zones=[pour("GND", 1, (0, 0, 60, 40), islands=[isl])])
    ctx = make_ctx(b)
    (c,) = [k for k in ctx.conns if k.role == "plane"]
    assert c.a.pad == "C1.2" and c.b.pad is None and ctx.state.status(c.idx) == "todo"
    assert not ctx.state.connected(c.idx)
    rep = PW.run(ctx, Budget(10 ** 6))
    assert list(rep.data["dogbones"]) == ["C1.2"] and rep.data["served_conns"] == [c.idx]
    assert rep.data["closed_conns"] == [c.idx] and rep.routed == 1                 # the via stands in the fill island
    assert ctx.state.status(c.idx) == "routed" and ctx.state.connected(c.idx)
    assert rep.score.opens == len(ctx.conns) - 1
    # an unfilled pour: the state cannot see the connection, the search / pours pass still owns it
    ctx2 = make_ctx(simple_board())
    (c2,) = [k for k in ctx2.conns if k.role == "plane"]
    rep2 = PW.run(ctx2, Budget(10 ** 6))
    assert rep2.data["closed_conns"] == [] and ctx2.state.status(c2.idx) == "todo"


# ── plan reserve sites, re-placement ──────────────────────────────────────
def test_plan_reserve_site():
    # no via may be placed within reach of the pad, except at the site the plan reserved outside the keep-out
    zones = [pour("GND", 1, (0, 0, 60, 40)), keepout(6, 6, 13, 14)]
    x = X(reserve=[{"at": (13.5, 11.0), "net": "GND"}, {"at": (30.0, 30.0), "net": "/S"}])
    b = simple_board(zones=zones, extra=[P("C1", 3, "", 12, 10)][:0])
    ctx = make_ctx(b, x=x)
    rep = PW.run(ctx, Budget(10 ** 6))
    assert rep.data["power_unreserved"] == [] and list(rep.data["dogbones"]) == ["C1.2"]
    ps = [ctx.state.piece(i) for i in rep.data["dogbones"]["C1.2"]]
    via = ps[-1]
    assert via.kind == "via" and (via.x1, via.y1) == (13.5, 11.0)
    segs = ps[:-1]
    assert len(segs) == 2 and (segs[0].x1, segs[0].y1) == (10.0, 10.0) and (segs[-1].x2, segs[-1].y2) == (13.5, 11.0)
    assert all(octilinear(p.x1, p.y1, p.x2, p.y2) and ctx.oracle.piece_ok(p) is None for p in ps)
    # the used site is real copper now; the unused one stays a soft reservation that other nets see
    h = ctx.history["power"]
    assert list(h["reserve"]) == [(30.0, 30.0)] and rep.data["reserve"] == 1
    hits = ctx.oracle.soft_hits(0, 29.0, 30.0, 31.0, 30.0, 0.2, "GND")
    assert [k.rule for k in hits] == ["reserve"] and hits[0].net == "/S"
    assert ctx.oracle.soft_hits(0, 29.0, 30.0, 31.0, 30.0, 0.2, "/S") == []
    assert ctx.oracle.soft_hits(0, 13.0, 11.0, 14.0, 11.0, 0.2, "/S") == []        # (the hard via answers there instead)
    assert ctx.oracle.seg_ok(0, 13.0, 11.0, 14.0, 11.0, 0.2, "/S") is not None
    # raw plan form ('for' instead of 'net') is read as well
    ctx2 = make_ctx(simple_board(zones=zones), x=X(reserve=[{"at": [13.5, 11.0], "for": "GND"}]))
    assert PW.run(ctx2, Budget(10 ** 6)).data["power_unreserved"] == []


def test_replace_dogbone_moves_or_leaves_it():
    b = simple_board()
    ctx = make_ctx(b)
    pad = pad_of(b, "C1.2")
    first = PW.place_dogbone(ctx, pad)
    site1 = (first[-1].x1, first[-1].y1)
    second = PW.replace_dogbone(ctx, "C1.2")
    site2 = (second[-1].x1, second[-1].y1)
    assert site2 != site1 and ctx.state.pieces() == second                       # moved inside one transaction
    assert not any(ctx.oracle.has_piece(p.id) for p in first)
    assert all(ctx.oracle.piece_ok(p) is None and p.protected for p in second)
    rec = ctx.history["power"]["dogbones"]["C1.2"]
    assert rec["site"] == site2 and rec["tried"] == [site1]
    third = PW.replace_dogbone(ctx, "C1.2")
    assert (third[-1].x1, third[-1].y1) not in (site1, site2)                    # never back to a site already given up
    # exclude: the caller's list of sites not to use
    ctx2 = make_ctx(simple_board())
    got = PW.place_dogbone(ctx2, pad_of(ctx2.board, "C1.2"), exclude=frozenset({site1}))
    assert (got[-1].x1, got[-1].y1) == site2
    # nowhere else to go: None, and the old dog-bone is still there (rolled back)
    zones = [pour("GND", 1, (0, 0, 60, 40)), keepout(6, 6, 13, 14)]
    ctx3 = make_ctx(simple_board(zones=zones), x=X(reserve=[{"at": (13.5, 11.0), "net": "GND"}]))
    PW.run(ctx3, Budget(10 ** 6))
    before = ctx3.state.pieces()
    v = ctx3.oracle.version
    assert len(before) == 3 and PW.replace_dogbone(ctx3, "C1.2") is None
    assert ctx3.state.pieces() == before and all(ctx3.oracle.has_piece(p.id) for p in before)
    assert ctx3.history["power"]["dogbones"]["C1.2"]["site"] == (13.5, 11.0) and ctx3.oracle.version >= v
    assert PW.replace_dogbone(ctx3, "NOPE.1") is None


# ── the stage as a whole ──────────────────────────────────────────────────
def test_budget_is_charged_and_respected():
    ctx = make_ctx(_decap_board())
    bud = Budget(0)
    rep = PW.run(ctx, bud)                                                         # nothing to spend: nothing is tried
    assert rep.routed == 0 and rep.failed == 1 and rep.data["dogbones"] == {}
    assert sorted(rep.data["power_unreserved"]) == ["C1.2", "U1.4"]
    assert set(rep.data["unreserved_why"].values()) == {"budget"} and ctx.state.pieces() == []
    assert any("budget" in n for n in rep.notes)
    ctx2 = make_ctx(_decap_board())
    parent = Budget(10 ** 6)
    bud2 = parent.sub(0.03)
    rep2 = PW.run(ctx2, bud2)
    assert 0 < rep2.work == bud2.used == parent.used and len(rep2.data["dogbones"]) == 2


def test_stage_is_deterministic():
    def once():
        ctx = make_ctx(_lane_board())
        rep = PW.run(ctx, Budget(10 ** 6))
        return [(p.id, p.net, p.conn, p.kind, p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.drill, p.protected)
                for p in ctx.state.pieces()], rep.data, rep.notes
    assert once() == once()


def test_corpus_boards():
    done = 0
    for name in ("coldfire", "stickhub", "cm5_minima", "one_air_max", "tinytapeout", "synth_qfp_fanout", "multichannel_mixer"):
        b = T.corpus_board(name)
        if b is None:
            continue
        ov = O.local_overrides_from_file(T.common.CORPUS / name / f"{name}.kicad_pcb")

        def once():
            bb = T.corpus_board(name)
            ctx = make_ctx(bb, None, overrides=ov)
            c0 = ctx.oracle.checks
            t0 = time.perf_counter()
            bud = Budget(int(ctx.cfg.time_budget * 200_000 * 0.03))
            rep = PW.run(ctx, bud)
            return ctx, rep, time.perf_counter() - t0, ctx.oracle.checks - c0, bud
        ctx, rep, dt, checks, bud = once()
        ctx2, rep2, _dt, _c, _b = once()
        ps = ctx.state.pieces()
        assert ps == ctx2.state.pieces() and rep.data == rep2.data, name                    # same twice
        for p in ps:
            assert ctx.oracle.piece_ok(p) is None, (name, p)                                # every piece is legal
            assert p.stage == "power" and (p.kind == "via" or T.common is not None)
        need = [p for p in ctx.board.pads if PW.needs_dogbone(ctx.board, p)]
        clusters = {(p.net, p.cluster) for p in need}
        # (dog-bones for pads the pour reaches today — fine-pitch rows above a plane — are extra: risk_dogbones)
        risk = rep.data.get("risk_dogbones") or []
        assert all(k in rep.data["dogbones"] or k in rep.data["soft_reserved"] for k in risk), name
        assert len(rep.data["dogbones"]) + len(rep.data["power_unreserved"]) + len(rep.data["soft_reserved"])             - len(risk) == len(clusters), name
        sig = PW.signal_layers(ctx)
        for key, site in rep.data["soft_reserved"].items():                               # soft only off the planes
            assert not PW.on_plane(ctx, ctx.history["power"]["soft"][key]["net"], *site)
            assert ctx.oracle.soft_hits(0, site[0], site[1], site[0], site[1], 0.2, "__none__")
        for pad_name, ids in rep.data["dogbones"].items():
            got = [ctx.state.piece(i) for i in ids]
            rec = ctx.history["power"]["dogbones"][pad_name]
            via = got[-1] if not rec.get("shares") else ctx.state.piece(ctx.history["power"]["dogbones"][rec["shares"]]["pieces"][-1])
            assert via.kind == "via" and all(p.protected and p.conn == -1 for p in got)
            assert (got[-1].x2, got[-1].y2) == (via.x1, via.y1) == tuple(rec["site"])
            assert PW.pour_penalty(ctx.board, via.net, via.x1, via.y1, via.w / 2) is not None   # the via is in the pour
            assert all(octilinear(p.x1, p.y1, p.x2, p.y2) for p in got)
            assert all(p.w <= p.target_w + 1e-9 for p in got)
        for idx in rep.data["decaps"]["routed"]:
            assert ctx.state.status(idx) == "routed" and ctx.state.connected(idx)
            c = ctx.conns[idx]
            for p in ctx.state.pieces(conn=idx):
                assert c.neck - 1e-9 <= p.w <= c.width + 1e-9 and p.target_w == c.width
        assert not bud.spent_out() and rep.work == bud.used
        why = {}
        for v in rep.data["unreserved_why"].values():
            why[v] = why.get(v, 0) + 1
        segs = [p for p in ps if p.kind == "seg"]
        necked = sum(p.length for p in segs if p.w < p.target_w - 1e-9)
        total = sum(p.length for p in segs) or 1.0
        print(f"    {name:20s} {dt:5.2f} s  {len(rep.data['dogbones']):3d} dog-bones of {len(clusters):3d}  "
              f"soft {len(rep.data['soft_reserved'])}  shared {len(rep.data['shared'])}  unreserved {why or '{}'}  decaps {len(rep.data['decaps']['routed'])}/"
              f"{len(rep.data['decaps']['routed']) + len(rep.data['decaps']['failed'])}  spokes {rep.data['spokes']}  "
              f"{checks} oracle checks  work {rep.work}  necked {necked:.1f}/{total:.1f} mm")
        done += 1
    if not done:
        return "skipped: no corpus extract available"


def test_coldfire_fine_pitch_pour_pads_are_reserved():
    b = T.corpus_board("coldfire")
    if b is None:
        return "skipped: no corpus extract available"
    ctx = make_ctx(b, None, overrides=O.local_overrides_from_file(T.common.CORPUS / "coldfire" / "coldfire.kicad_pcb"))
    rep = PW.run(ctx, Budget(10 ** 6))
    got = rep.data["dogbones"]
    u102 = [p.name for p in b.pads if p.ref == "U102" and PW.needs_dogbone(b, p)]
    assert len(u102) >= 8 and sum(1 for n in u102 if n in got) >= len(u102) - 4     # 0.5 mm pitch, class width 0.4
    assert len(got) >= 150 and len(rep.data["power_unreserved"]) <= 10
    why = set(rep.data["unreserved_why"].values())
    assert all(w == "no legal via site" or w.startswith("would wall in ") for w in why)
    plane = [c for c in ctx.conns if c.role == "plane"]
    assert len(rep.data["served_conns"]) >= len(plane) - 10


# ── runner ────────────────────────────────────────────────────────────────
def main(argv):
    if getattr(pieces, "STUB", False):
        print("skipped: v2.pieces (RouteState) is still a stub")
        return 0
    names = [a for a in argv if not a.startswith("-")]
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f) and (not names or n in names)]
    failed = 0
    for n, f in tests:
        t0 = time.perf_counter()
        try:
            r = f()
            print(f"ok   {n}  ({time.perf_counter() - t0:.2f} s){'  ' + r if isinstance(r, str) else ''}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
