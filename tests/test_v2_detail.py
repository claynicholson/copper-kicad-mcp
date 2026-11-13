"""Router v2 detail stage (design §4.8): pattern routes, window search inside the corridor, bounded widening.

Pure tests on synthetic boards (no KiCad). ``--corpus`` adds one run of the stage alone on three corpus boards
(needs KiCad for the first extract; cached under bench/route/.work/w4).

Run:  python tests/test_v2_detail.py [--corpus] [test_name ...]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v2_w4_support as S  # noqa: E402
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import geom  # noqa: E402
from copper_kicad.pcb.v2 import detail as D  # noqa: E402
from copper_kicad.pcb.v2 import search as A  # noqa: E402
from copper_kicad.pcb.v2 import window as W  # noqa: E402
from copper_kicad.pcb.v2.types import (CHARGE_PATTERN, CHARGE_SEARCH, CHARGE_WINDOW, Blocker, Budget, Corridor,  # noqa: E402
                                       StageReport)

BIG = 10 ** 9


def pad(ref, net, x, y, hw=0.3, hh=0.3, **kw):
    return dict({"ref": ref, "net": net, "x": x, "y": y, "hw": hw, "hh": hh}, **kw)


class NC:
    """The guided.plan.NetCfg fields detail reads."""
    def __init__(self, L=2, **kw):
        self.layers_ok, self.layer_cost, self.layer_dir = set(range(L)), {}, {}
        self.via_cost, self.max_vias, self.avoid, self.corridor, self.keep_short = 5.0, None, [], None, False
        self.__dict__.update(kw)


class Tiles:
    def __init__(self, t=1.0, nx=60, ny=40):
        self.t, self.nx, self.ny, self.nl, self.x0, self.y0 = t, nx, ny, 2, 0.0, 0.0

    def tile_of(self, x, y):
        return (int(math.floor(x / self.t)), int(math.floor(y / self.t)))


def geometry_ok(ctx, c, pieces=None):
    """All pieces of a connection: oracle-legal, no zero length, quantised, ends on the terminals, connected."""
    ps = pieces if pieces is not None else ctx.state.pieces(conn=c.idx)
    assert ps, "no pieces"
    assert not S.all_legal(ctx, ps), S.all_legal(ctx, ps)[:2]
    for p in ps:
        for v in (p.x1, p.y1, p.x2, p.y2):
            assert abs(v * 1e6 - round(v * 1e6)) < 1e-6, ("not on the 1 nm grid", p)
        assert p.net == c.net and p.conn == c.idx and p.stage == "detail"
        if p.kind == "seg":
            assert p.length > 1e-6 and p.w > 0
    assert ctx.state.connected(c.idx)
    pts = [(p.x1, p.y1) for p in ps] + [(p.x2, p.y2) for p in ps]
    assert any(abs(x - c.a.x) < 2e-6 and abs(y - c.a.y) < 2e-6 for x, y in pts), "does not start on terminal a"


def odd(p):
    return p.kind == "seg" and not D._aligned(p.x2 - p.x1, p.y2 - p.y1)


# ── 1: pattern ────────────────────────────────────────────────────────────
def test_pattern_hit_for_clear_l():
    b = S.board([pad("A", "N", 5.0, 5.0), pad("B", "N", 15.0, 9.0)])
    c = S.conn(0, "N", (5.0, 5.0), (15.0, 9.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c])
    assert D.pattern(ctx, c) == [(5.0, 5.0, 0), (9.0, 9.0, 0), (15.0, 9.0, 0)]        # diagonal first, then straight
    bud = Budget(BIG)
    r = D.route_conn(ctx, c, bud)
    assert r["ok"] and r["how"] == "pattern" and r["fail"] is None and len(r["pieces"]) == 2
    assert ctx.state.status(0) == "routed" and r["pieces"] == ctx.state.pieces(conn=0)
    geometry_ok(ctx, c)
    assert all(p.w == 0.2 and p.target_w == 0.2 for p in r["pieces"])
    st = ctx.history["detail"]
    assert st["pattern_hits"] == 2 and st["windows"] == 0 and st["searches"] == 0      # pattern() above + route_conn
    assert CHARGE_PATTERN * 1 <= bud.used < CHARGE_PATTERN * 2 and st["pattern_tries"] == 2    # first shape, both times
    # routed already: nothing more is added
    r2 = D.route_conn(ctx, c, bud)
    assert r2["ok"] and r2["how"] == "already" and len(ctx.state.pieces()) == 2


def test_pattern_shapes_and_refusals():
    # aligned terminals: the straight line only
    assert D._pattern_shapes(0, 0, 5, 0) == [[(0, 0), (5, 0)]] and D._pattern_shapes(0, 0, 3, -3) == [[(0, 0), (3, -3)]]
    sh = D._pattern_shapes(0.0, 0.0, 10.0, 4.0)
    assert len(sh) == 4 and all(s[0] == (0.0, 0.0) and s[-1] == (10.0, 4.0) for s in sh)
    for s in sh:
        for (x1, y1), (x2, y2) in zip(s, s[1:]):
            assert D._aligned(x2 - x1, y2 - y1)
    assert D._pattern_shapes(1, 1, 1, 1) == []
    # an obstacle on the first posture: the second one wins; on both L postures: a Z
    b = S.board([pad("A", "N", 5.0, 5.0), pad("B", "N", 15.0, 9.0), pad("X", "o", 7.0, 7.0)])
    c = S.conn(0, "N", (5.0, 5.0), (15.0, 9.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c])
    assert D.pattern(ctx, c) == [(5.0, 5.0, 0), (11.0, 5.0, 0), (15.0, 9.0, 0)]
    b = S.board([pad("A", "N", 5.0, 5.0), pad("B", "N", 15.0, 9.0), pad("X", "o", 7.0, 7.0), pad("Y", "o", 13.0, 7.0)])
    ctx = S.ctx_for(b, [c])
    assert D.pattern(ctx, c) == [(5.0, 5.0, 0), (8.0, 5.0, 0), (12.0, 9.0, 0), (15.0, 9.0, 0)]
    # everything direct is blocked: None
    b = S.board([pad("A", "N", 5.0, 5.0), pad("B", "N", 15.0, 9.0), pad("X", "o", 10.0, 7.0, hw=0.3, hh=6.0)])
    ctx = S.ctx_for(b, [c])
    assert D.pattern(ctx, c) is None
    # terminals that share no layer: None; the back layer is used when that is the shared one
    b = S.board([pad("A", "N", 5.0, 5.0), pad("B", "N", 15.0, 9.0, layer=1)])
    c2 = S.conn(0, "N", (5.0, 5.0), (15.0, 9.0), lb=(1,))
    assert D.pattern(S.ctx_for(b, [c2]), c2) is None
    c3 = S.conn(0, "N", (5.0, 5.0), (15.0, 9.0), la=(0, 1), lb=(1,))
    assert D.pattern(S.ctx_for(b, [c3]), c3)[0] == (5.0, 5.0, 1)
    # a thermal-spoke corridor of another net on the line: no pattern (the search prices it instead)
    b = S.board([pad("A", "N", 5.0, 5.0), pad("B", "N", 15.0, 5.0), pad("G", "g", 25.0, 20.0)])
    c4 = S.conn(0, "N", (5.0, 5.0), (15.0, 5.0))
    ctx = S.ctx_for(b, [c4])
    assert D.pattern(ctx, c4) == [(5.0, 5.0, 0), (15.0, 5.0, 0)]
    ctx.oracle.add_soft("thermal", "g", 0, (10.0, 5.0, 1.0, 0.0, 0.2, 1.0, 0.0))
    assert D.pattern(ctx, c4) is None
    # a plan region over the terminals' box, an avoid rectangle: no pattern
    class Reg:
        rect, forbid, cost, layers = (8.0, 0.0, 9.0, 20.0), True, 1.0, None

        def applies(self, net):
            return True
    ctx = S.ctx_for(b, [c4], regions=[Reg()])
    assert D.pattern(ctx, c4) is None
    ctx = S.ctx_for(b, [c4], nets={"N": NC(avoid=[(0, 0, 1, 1)])})
    assert D.pattern(ctx, c4) is None


def test_pattern_stays_in_the_corridor():
    b = S.board([pad("A", "N", 5.5, 5.5), pad("B", "N", 15.5, 9.5)])
    c = S.conn(0, "N", (5.5, 5.5), (15.5, 9.5))
    ctx = S.ctx_for(b, [c])
    ctx.tiles = Tiles()
    # corridor along the "straight first" L: x from 5 to 11 on row 5, then the diagonal
    tiles = {(tx, 5) for tx in range(4, 13)} | {(tx, ty) for tx in range(10, 17) for ty in range(5, 11)}
    ctx.corridors = {0: Corridor(conn=0, tiles=frozenset(tiles), layers=(0,))}
    assert D.pattern(ctx, c) == [(5.5, 5.5, 0), (11.5, 5.5, 0), (15.5, 9.5, 0)]       # not the diagonal-first posture
    ctx.corridors = {0: Corridor(conn=0, tiles=frozenset({(5, 5), (15, 9)}), layers=(0,))}
    assert D.pattern(ctx, c) is None


# ── 3: window search, geometry ────────────────────────────────────────────
def _fence(n=9, x=12.0, y0=2.0, dy=2.0, net="o"):
    return [pad(f"F{i}", net, x, y0 + i * dy, hw=0.3, hh=0.7) for i in range(n)]


def test_window_route_is_legal_including_off_lattice_pad_joins():
    """Pads at arbitrary 1 um positions, a fence of foreign pads between them: every emitted piece is legal for the
    oracle, the ends are exactly the pad centres, and almost everything is axis-aligned or at 45 degrees."""
    import random
    rnd = random.Random(9)
    n_odd = n_seg = 0
    for k in range(12):
        ax, ay = round(4.0 + rnd.uniform(0, 1), 3), round(6.0 + rnd.uniform(0, 8), 3)
        bx, by = round(20.0 + rnd.uniform(0, 1), 3), round(6.0 + rnd.uniform(0, 8), 3)
        pads = [pad("A", "N", ax, ay), pad("B", "N", bx, by)] + _fence()
        b = S.board(pads)
        c = S.conn(0, "N", (ax, ay), (bx, by), pa="A.1", pb="B.1")
        ctx = S.ctx_for(b, [c])
        r = D.route_conn(ctx, c, Budget(BIG))
        assert r["ok"] and r["how"] == "window", (k, r["fail"])
        geometry_ok(ctx, c)
        ps = ctx.state.pieces(conn=0)
        assert (ps[0].x1, ps[0].y1) == (ax, ay) and (ps[-1].x2, ps[-1].y2) == (bx, by)      # in path order, exact ends
        for p, nx in zip(ps, ps[1:]):
            assert abs(p.x2 - nx.x1) < 1e-9 and abs(p.y2 - nx.y1) < 1e-9                     # a chain
        n_odd += sum(odd(p) for p in ps)
        n_seg += len(ps)
        assert sum(p.length for p in ps) < 1.6 * c.est + 3.0
    assert n_odd <= 0.1 * n_seg, (n_odd, n_seg)
    print(f"    {n_seg} segments, {n_odd} at an odd angle")


def test_join_candidates():
    pts = [(1.0, 1.0), (3.0, 1.0), (5.0, 3.0)]
    # on the cell: no join
    assert D._join_cands(("pt", 1.0, 1.0), pts, 0.1) == [([(1.0, 1.0), (3.0, 1.0), (5.0, 3.0)], 0, "on")]
    # 30 um off sideways: slide the first run through the anchor (no stub), then ray hits, doglegs, the stub
    cands = D._join_cands(("pt", 1.0, 1.03), pts, 0.1)
    labels = [c[2] for c in cands]
    assert labels[0] == "slide" and labels[-1] == "stub" and "ray" in labels
    assert cands[0][0] == [(1.0, 1.03), (3.03, 1.03), (5.0, 3.0)]
    assert [c[2] for c in D._join_cands(("pt", 0.98, 1.03), pts, 0.1)].count("dogleg") == 2
    for poly, k, _label in cands:
        assert poly[0] == (1.0, 1.03) and poly[-1] == (5.0, 3.0) and 1 <= k <= 2
        if _label != "stub":
            for (x1, y1), (x2, y2) in zip(poly, poly[1:3]):
                assert D._aligned(x2 - x1, y2 - y1), (_label, poly)
    # tee onto a same-net segment: extension of the last run first, the projection last
    cands = D._join_cands(("seg", 0.0, 0.94, 10.0, 0.94), [(2.0, 1.0), (2.5, 1.5)], 0.1)
    assert cands[0][2] == "tee_ext" and cands[0][0][0] == (1.94, 0.94) and cands[-1][2] == "tee_proj"
    assert cands[-1][0][0] == (2.0, 0.94)
    assert D._join_cands(("seg", 0.0, 1.0, 10.0, 1.0), [(2.0, 1.0), (2.5, 1.5)], 0.1)[0][2] == "on"


def test_failing_segment_blocks_its_cells_and_researches():
    pads = [pad("A", "N", 4.0, 10.0), pad("B", "N", 20.0, 10.0)] + _fence()
    b = S.board(pads)
    c = S.conn(0, "N", (4.0, 10.0), (20.0, 10.0), pa="A.1", pb="B.1")
    # reference run
    ctx0 = S.ctx_for(b, [c])
    assert D.route_conn(ctx0, c, Budget(BIG))["ok"]
    ref = [(p.layer, p.x1, p.y1, p.x2, p.y2) for p in ctx0.state.pieces()]
    longest = max(ctx0.state.pieces(), key=lambda p: p.length)
    # inject a fault: the oracle rejects that one segment (as if the grid had let an illegal step through)
    ctx = S.ctx_for(b, [c])
    real_legal, real_ok = ctx.oracle.seg_legal, ctx.oracle.seg_ok
    key = (longest.layer, longest.x1, longest.y1, longest.x2, longest.y2)
    hits = []

    def seg_legal(layer, x1, y1, x2, y2, w, net, clearance=None, ignore=frozenset()):
        if (layer, x1, y1, x2, y2) == key:
            hits.append(1)
            return False
        return real_legal(layer, x1, y1, x2, y2, w, net, clearance, ignore)

    def seg_ok(layer, x1, y1, x2, y2, w, net, clearance=None, ignore=frozenset()):
        if (layer, x1, y1, x2, y2) == key:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            return Blocker(rule="clearance", net="o", tag="F4.1", static=True, piece=None, dist=0.19, need=0.2,
                           at=(mx, my), layer=layer)
        return real_ok(layer, x1, y1, x2, y2, w, net, clearance, ignore)
    ctx.oracle.seg_legal, ctx.oracle.seg_ok = seg_legal, seg_ok
    r = D.route_conn(ctx, c, Budget(BIG))
    st = ctx.history["detail"]
    assert r["ok"] and hits and st["emit_rejects"] >= 1 and st["searches"] == st["emit_rejects"] + 1
    assert st["windows"] == 1                                    # same window, cells blocked, searched again
    got = [(p.layer, p.x1, p.y1, p.x2, p.y2) for p in ctx.state.pieces()]
    assert got != ref and key not in got
    ctx.oracle.seg_legal, ctx.oracle.seg_ok = real_legal, real_ok
    geometry_ok(ctx, c)
    # a segment that is rejected every time: the connection fails alone, after at most 3 re-searches per window
    ctx = S.ctx_for(b, [c])
    real_legal = ctx.oracle.seg_legal
    ctx.oracle.seg_legal = lambda layer, x1, y1, x2, y2, w, net, clearance=None, ignore=frozenset(): \
        False if x1 > 6.0 and x2 > 6.0 else real_legal(layer, x1, y1, x2, y2, w, net, clearance, ignore)
    r = D.route_conn(ctx, c, Budget(BIG))
    st = ctx.history["detail"]
    assert not r["ok"] and r["fail"]["code"] == "rejected" and "exact clearance check" in r["fail"]["reason"]
    assert st["searches"] <= (D.RESEARCH + 1) * st["windows"] and st["emit_rejects"] >= D.RESEARCH + 1
    assert ctx.state.pieces() == [] and ctx.state.status(0) != "routed"


def test_emit_returns_the_blocker():
    pads = [pad("A", "N", 4.0, 10.0), pad("B", "N", 20.0, 10.0), pad("X", "o", 12.0, 10.0)]
    b = S.board(pads)
    c = S.conn(0, "N", (4.0, 10.0), (20.0, 10.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c])
    win = W.build(ctx, c, None, (2.0, 6.0, 22.0, 14.0), width=0.2, pitch=0.1, layers=(0,), cache=False)
    g = win.grid
    s0, s1 = win.cell(4.0, 10.0, 0), win.cell(20.0, 10.0, 0)
    path = list(range(s0, s1 + 1))                                # straight through the foreign pad
    win.ends = ({s0: ("pt", 4.0, 10.0)}, {s1: ("pt", 20.0, 10.0)})
    out = D.emit(ctx, c, win, path, 0.2)
    assert isinstance(out, Blocker) and out.tag == "X.1" and out.rule == "short" and win.last_reject[0] == "seg"
    # a legal path: fields ready for RouteState.add, in path order
    path = [s0 + k * (g.W + 1) for k in range(11)] + [s0 + 10 * (g.W + 1) + k for k in range(1, 141)] \
        + [s0 + 10 * (g.W + 1) + 140 - k * (g.W - 1) for k in range(1, 11)]
    assert path[-1] == s1
    out = D.emit(ctx, c, win, path, 0.2)
    assert [(d["x1"], d["y1"], d["x2"], d["y2"]) for d in out] == [(4.0, 10.0, 5.0, 11.0), (5.0, 11.0, 19.0, 11.0),
                                                                  (19.0, 11.0, 20.0, 10.0)]
    assert all(d["kind"] == "seg" and d["layer"] == 0 and d["w"] == 0.2 and d["conn"] == 0 for d in out)


def test_bounded_wide_cap_and_limits_are_honoured():
    # a wall across the whole board with one gap far from both terminals
    pads = [pad("A", "N", 30.0, 8.0), pad("B", "N", 30.0, 16.0)]
    pads += [pad("W1", "o", 17.15, 12.0, hw=16.65, hh=0.3), pad("W2", "o", 55.15, 12.0, hw=20.35, hh=0.3)]
    # ... a 1 mm gap at x = 33.8 .. 34.8: outside the corridor boxes (+- 3.5 mm), inside the wide box (+- 5 mm)
    b = S.board(pads, size=(76.0, 24.0))
    c = S.conn(0, "N", (30.0, 8.0), (30.0, 16.0), pa="A.1", pb="B.1", la=(0,), lb=(0,))
    calls = []
    real = A.search

    def spy(win, net_id, sources, targets, **kw):
        p, i = real(win, net_id, sources, targets, **kw)
        calls.append((kw["limit"], i["expanded"], i["reason"], win.box))
        return p, i
    D.srch.search = spy
    try:
        for effort, expect_ok in ((1.0, True), (0.02, False)):
            calls.clear()
            ctx = S.ctx_for(b, [c], effort=effort, nets={"N": NC(layers_ok={0})})
            r = D.route_conn(ctx, c, Budget(BIG))
            assert r["ok"] == expect_ok, (effort, r["fail"])
            assert all(exp <= lim for lim, exp, _r, _b in calls)
            tried = (r["fail"] or {}).get("tried") or []
            wide = [cl for cl in calls if cl[3][0] < 30.0 - 4.9]                # the wide box: +- (25 % + 5 mm)
            assert wide and all(lim == int(D.WIDE_CAP * effort) for lim, _e, _r, _b in wide)
            narrow = [cl for cl in calls if cl not in wide]
            p = 0.2
            assert narrow and all(lim == int(min(D.SEARCH_MAX, D.SEARCH_BASE + D.SEARCH_PER_STEP * c.est / p) * effort)
                                  for lim, _e, _r, _b in narrow)
            if not expect_ok:
                assert r["fail"]["code"] == "limit" and "effort" in r["fail"]["reason"]
                assert tried == ["pattern", "corridor@0.2", "corridor+2@0.2", "wide@0.2"]
                assert wide[-1][2] == "limit" and wide[-1][1] == int(D.WIDE_CAP * effort)
                assert not ctx.state.pieces()
            else:
                assert r["step"] == "wide@0.2"
                geometry_ok(ctx, c)
    finally:
        D.srch.search = real
    # there is no box larger than the wide one: the board is 76 mm wide, the wide window is not
    assert all(bx[2] - bx[0] <= 2 * D.WIDE_PAD + 0.1 for _l, _e, _r, bx in calls)
    # a gap outside the wide box is never found, whatever the effort: v2 has no full-board search
    pads[2:] = [pad("W1", "o", 39.0, 12.0, hw=36.5, hh=0.3)]                # gap only at x < 2.5
    b = S.board(pads, size=(76.0, 24.0))
    ctx = S.ctx_for(b, [c], effort=5.0, nets={"N": NC(layers_ok={0})})
    r = D.route_conn(ctx, c, Budget(BIG))
    assert not r["ok"] and r["fail"]["code"] == "exhausted" and r["fail"]["tried"][-1] == "wide@0.2"


def test_neck_result_carries_target_w():
    # a 0.75 mm slot between two foreign pads: 0.5 mm + 2 x 0.2 does not fit, 0.2 mm does
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0),
            pad("U", "o", 12.0, 5.0, hw=0.5, hh=4.625), pad("D", "o", 12.0, 15.0, hw=0.5, hh=4.625)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1", width=0.5, neck=0.2)
    ctx = S.ctx_for(b, [c], nets={"N": NC(layers_ok={0})})
    r = D.route_conn(ctx, c, Budget(BIG))
    assert r["ok"] and r["how"] == "window" and r["width"] == 0.2 and r["step"] == "corridor@0.2"
    ps = ctx.state.pieces(conn=0)
    assert ps and all(p.w == 0.2 and p.target_w == 0.5 for p in ps)
    geometry_ok(ctx, c)
    assert ctx.history["detail"]["necked"] == 1
    assert ctx.state.score().necked_mm > 13.0
    # the full width was tried first, and without a neck width the connection fails
    c2 = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1", width=0.5)
    ctx2 = S.ctx_for(b, [c2], nets={"N": NC(layers_ok={0})})
    r2 = D.route_conn(ctx2, c2, Budget(BIG))
    assert not r2["ok"] and all(t.endswith("@0.5") for t in r2["fail"]["tried"][1:])
    # full width where it fits: target_w == w
    b3 = S.board(pads[:2] + [pad("X", "o", 12.0, 10.0)], size=(24.0, 20.0))
    ctx3 = S.ctx_for(b3, [c])
    r3 = D.route_conn(ctx3, c, Budget(BIG))
    assert r3["ok"] and all(p.w == 0.5 and p.target_w == 0.5 for p in r3["pieces"])


def test_vias_and_max_vias():
    # terminals on different layers: exactly one via
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0, layer=1)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1", la=(0,), lb=(1,))
    ctx = S.ctx_for(b, [c])
    r = D.route_conn(ctx, c, Budget(BIG))
    vias = [p for p in r["pieces"] if p.kind == "via"]
    assert r["ok"] and len(vias) == 1 and vias[0].w == 0.6 and vias[0].drill == 0.3 and vias[0].layer == -1
    geometry_ok(ctx, c)
    assert {p.layer for p in r["pieces"] if p.kind == "seg"} == {0, 1}
    assert not (abs(vias[0].x1 - 5.0) < 0.3 and abs(vias[0].y1 - 10.0) < 0.3)        # not in the SMD pad
    # max_vias = 0: no layer change allowed, so it cannot be done
    ctx = S.ctx_for(b, [c], nets={"N": NC(max_vias=0)})
    r = D.route_conn(ctx, c, Budget(BIG))
    assert not r["ok"] and not ctx.state.pieces()
    # same-layer terminals, a full-height wall on the front: two vias; with max_vias = 1 it fails as max_vias
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0), pad("X", "o", 12.0, 10.0, hw=0.3, hh=9.6)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c])
    r = D.route_conn(ctx, c, Budget(BIG))
    assert r["ok"] and sum(p.kind == "via" for p in r["pieces"]) == 2
    geometry_ok(ctx, c)
    ctx = S.ctx_for(b, [c], nets={"N": NC(max_vias=1)})
    r = D.route_conn(ctx, c, Budget(BIG))
    assert not r["ok"] and r["fail"]["code"] == "max_vias" and "max_vias=1" in r["fail"]["reason"]
    ctx = S.ctx_for(b, [c], nets={"N": NC(max_vias=1)})
    assert D.route_conn(ctx, c, Budget(BIG), extra_vias=1)["ok"]                      # rescue: one via above the limit


def test_via_limit_and_the_via_frugal_reroute():
    from copper_kicad.pcb.v2 import polish as PO
    # a wall on the front with a way round it: at a low via cost the search hops over it with two vias
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0), pad("X", "o", 12.0, 10.0, hw=0.3, hh=5.0)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c], nets={"N": NC(via_cost=1.0)})
    r = D.route_conn(ctx, c, Budget(BIG))
    assert r["ok"] and sum(p.kind == "via" for p in r["pieces"]) == 2
    # via_limit: fewer vias or no result; via_scale prices them
    ctx2 = S.ctx_for(b, [c], nets={"N": NC(via_cost=1.0)})
    assert not D.route_conn(ctx2, c, Budget(BIG), via_limit=1, via_scale=4.0)["ok"]     # the ladder's windows end at the wall
    r2 = D.route_conn(ctx2, c, Budget(BIG), via_limit=1, via_scale=4.0, lateral=8.0)
    assert r2["ok"] and not any(p.kind == "via" for p in r2["pieces"])
    geometry_ok(ctx2, c)
    # polish.revia does the same on the routed state: the connection is re-laid on one layer, still connected
    rep = PO.revia(ctx, Budget(BIG))
    assert rep.data["vias_removed"] == 2 and rep.data["tried"] == 1, rep.data
    assert not any(p.kind == "via" for p in ctx.state.pieces()) and ctx.state.status(0) == "routed"
    geometry_ok(ctx, c)
    assert PO.revia(ctx, Budget(BIG)).data["tried"] == 0            # nothing left to do
    # a wall with no way round: the attempt changes nothing
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0), pad("X", "o", 12.0, 10.0, hw=0.3, hh=9.6)]
    b = S.board(pads, size=(24.0, 20.0))
    ctx = S.ctx_for(b, [c])
    assert D.route_conn(ctx, c, Budget(BIG))["ok"]
    before = [(p.kind, p.layer, p.x1, p.y1, p.x2, p.y2) for p in ctx.state.pieces()]
    rep = PO.revia(ctx, Budget(BIG))
    assert rep.data["vias_removed"] == 0 and rep.data["tried"] == 1
    assert [(p.kind, p.layer, p.x1, p.y1, p.x2, p.y2) for p in ctx.state.pieces()] == before
    assert ctx.state.status(0) == "routed" and ctx.state.connected(0)


def test_run_stops_after_max_failed():
    # three connections that cannot be done (a full wall, no vias allowed): the stage gives up after the first
    pads = [pad("X", "o", 12.0, 15.0, hw=0.3, hh=14.6)]
    cs = []
    for i in range(3):
        y = 6.0 + 8.0 * i
        pads += [pad(f"A{i}", f"N{i}", 5.0, y), pad(f"B{i}", f"N{i}", 19.0, y)]
        cs.append(S.conn(i, f"N{i}", (5.0, y), (19.0, y), pa=f"A{i}.1", pb=f"B{i}.1"))
    b = S.board(pads, size=(24.0, 30.0))
    nets = {f"N{i}": NC(max_vias=0) for i in range(3)}
    rep = D.run(S.ctx_for(b, cs, nets=nets), Budget(BIG))
    assert rep.failed == 3 and rep.data["hard_failed"] == 3 and not rep.data.get("aborted")
    rep = D.run(S.ctx_for(b, cs, nets=nets), Budget(BIG), max_failed=0)
    assert rep.failed == 1 and rep.data.get("aborted") is True


def test_tee_onto_the_routed_tree():
    pads = [pad("A", "N", 4.0, 6.0), pad("B", "N", 24.0, 6.0), pad("C", "N", 14.03, 14.0), pad("X", "o", 14.0, 10.0, hw=1.5)]
    b = S.board(pads, size=(28.0, 20.0))
    c0 = S.conn(0, "N", (4.0, 6.0), (24.0, 6.0), pa="A.1", pb="B.1", ca=1, cb=2)
    c1 = S.conn(1, "N", (14.03, 14.0), (4.0, 6.0), pa="C.1", pb="A.1", ca=3, cb=1)
    ctx = S.ctx_for(b, [c0, c1])
    assert D.route_conn(ctx, c0, Budget(BIG))["how"] == "pattern"
    trunk = ctx.state.pieces(conn=0)[0]
    r = D.route_conn(ctx, c1, Budget(BIG))
    assert r["ok"] and r["how"] == "pattern" and len(r["pieces"]) == 1           # one 45 degree piece onto the trunk
    geometry_ok(ctx, c1)
    end = r["pieces"][-1]
    assert (end.x2, end.y2) == (6.03, 6.0) and abs(end.length - 8.0 * 2 ** 0.5) < 1e-6          # pad A is 12.8 away
    # the same through the window search (an avoid rectangle elsewhere switches patterns off)
    ctx = S.ctx_for(b, [c0, c1], nets={"N": NC(avoid=[(0.0, 18.0, 1.0, 19.0)])})
    assert D.route_conn(ctx, c0, Budget(BIG))["how"] == "window"
    r = D.route_conn(ctx, c1, Budget(BIG))
    assert r["ok"] and r["how"] == "window"
    geometry_ok(ctx, c1)
    end = r["pieces"][-1]
    assert min(geom.pt_seg_dist(end.x2, end.y2, t.x1, t.y1, t.x2, t.y2) for t in ctx.state.pieces(conn=0)) < 1e-6
    assert 5.0 < end.x2 < 23.0 and abs(end.y2 - 6.0) < 1e-9                                       # a tee, not pad A
    assert sum(p.length for p in r["pieces"]) < 12.0
    ctx = S.ctx_for(b, [c0, c1])
    D.route_conn(ctx, c0, Budget(BIG))
    trunk = ctx.state.pieces(conn=0)[0]
    # tree_target = False: the same connection goes to the pad itself
    c1b = S.conn(1, "N", (14.03, 14.0), (4.0, 6.0), pa="C.1", pb="A.1", ca=3, cb=1, tree=False)
    ctx = S.ctx_for(b, [c0, c1b])
    D.route_conn(ctx, c0, Budget(BIG))
    r = D.route_conn(ctx, c1b, Budget(BIG))
    assert r["ok"] and (r["pieces"][-1].x2, r["pieces"][-1].y2) == (4.0, 6.0)
    # copper on terminal a's own side is not a target: an escape stub that touches a is ignored
    ctx = S.ctx_for(b, [c0, c1])
    D.route_conn(ctx, c0, Budget(BIG))
    stub = ctx.state.add(kind="seg", net="N", conn=-1, layer=0, x1=14.03, y1=14.0, x2=14.03, y2=15.0, w=0.2,
                         stage="fanout", protected=True)
    a_side, b_side, _cl = D._sides(ctx, c1)
    assert a_side == [stub] and [p.id for p in b_side] == [trunk.id]
    r = D.route_conn(ctx, c1, Budget(BIG))
    assert r["ok"] and r["pieces"] and stub not in r["pieces"]


def test_boxed_in_fails_fast():
    # terminal B inside a closed ring of foreign pads (static), one layer
    ring = [pad("R1", "o", 20.0, 8.6, hw=1.7, hh=0.3), pad("R2", "o", 20.0, 11.4, hw=1.7, hh=0.3),
            pad("R3", "o", 18.6, 10.0, hw=0.3, hh=1.1), pad("R4", "o", 21.4, 10.0, hw=0.3, hh=1.1)]
    b = S.board([pad("A", "N", 5.0, 10.0), pad("B", "N", 20.0, 10.0)] + ring, size=(26.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (20.0, 10.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c], nets={"N": NC(layers_ok={0})})
    bud = Budget(BIG)
    r = D.route_conn(ctx, c, bud)
    f = r["fail"]
    assert not r["ok"] and f["code"] == "enclosed" and f["probe"] == {"enclosed": "b"}
    assert "pad B.1 is boxed in" in f["reason"] and f["static_share"] == 1.0
    assert f["tried"] == ["pattern", "corridor@0.2"]                 # no widening: the flood never touched the mask
    st = ctx.history["detail"]
    assert st["searches"] == 0 and st["windows"] == 1 and st["probes"] == 1         # the A* was never paid for
    assert bud.used < CHARGE_WINDOW + 2 * D.PROBE_LIMIT + 5 * CHARGE_PATTERN + 2000
    assert {x["tag"] for x in f["blockers"]} <= {"R1.1", "R2.1", "R3.1", "R4.1"} and f["blockers"]
    assert all(x["static"] and x["net"] == "o" for x in f["blockers"])
    assert f["blocked_by"] == [] and f["frontier"] and len(f["frontier"][0]) == 2      # nothing routed is in the way
    # the START walled in: the search runs dry at once, reported as terminal a
    c2 = S.conn(0, "N", (20.0, 10.0), (5.0, 10.0), pa="B.1", pb="A.1")
    ctx = S.ctx_for(b, [c2], nets={"N": NC(layers_ok={0})})
    r = D.route_conn(ctx, c2, Budget(BIG))
    assert r["fail"]["code"] == "enclosed" and r["fail"]["probe"] == {"enclosed": "a"} and "B.1" in r["fail"]["reason"]
    assert ctx.history["detail"]["expansions"] < D.PROBE_LIMIT
    # walled in by ROUTED copper: the share says rip-up can help, and names the connection
    b2 = S.board([pad("A", "N", 5.0, 10.0), pad("B", "N", 20.0, 10.0), pad("O", "o", 2.0, 2.0)], size=(26.0, 20.0))
    ctx = S.ctx_for(b2, [c], nets={"N": NC(layers_ok={0})})
    for x1, y1, x2, y2 in ((18.8, 8.8, 21.2, 8.8), (21.2, 8.8, 21.2, 11.2), (21.2, 11.2, 18.8, 11.2), (18.8, 11.2, 18.8, 8.8)):
        ctx.state.add(kind="seg", net="o", conn=7, layer=0, x1=x1, y1=y1, x2=x2, y2=y2, w=0.2)
    r = D.route_conn(ctx, c, Budget(BIG))
    f = r["fail"]
    assert f["code"] == "enclosed" and f["static_share"] == 0.0
    assert f["blockers"][0] == {"net": "o", "tag": "track", "static": False, "conn": 7, "count": f["blockers"][0]["count"]}
    assert f["blocked_by"] == ["o"] and "boxed in" in f["reason"]
    # two layers: a via gets in, so it is routed
    ctx = S.ctx_for(b2, [c])
    for x1, y1, x2, y2 in ((18.0, 8.0, 22.0, 8.0), (22.0, 8.0, 22.0, 12.0), (22.0, 12.0, 18.0, 12.0), (18.0, 12.0, 18.0, 8.0)):
        ctx.state.add(kind="seg", net="o", conn=7, layer=0, x1=x1, y1=y1, x2=x2, y2=y2, w=0.2)
    c3 = S.conn(0, "N", (5.0, 10.0), (20.0, 10.0), pa="A.1", pb="B.1", la=(0,), lb=(0,))
    ctx.conns[:] = [c3]
    ctx.state = S.make_state(ctx.oracle, [c3]) if not ctx.state.pieces() else ctx.state
    r = D.route_conn(ctx, c3, Budget(BIG))
    assert r["ok"] and sum(p.kind == "via" for p in r["pieces"]) == 2


def test_failure_codes_outside_and_layers():
    b = S.board([pad("A", "N", 5.0, 10.0), pad("B", "N", 50.0, 10.0)], size=(26.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (50.0, 10.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c])
    r = D.route_conn(ctx, c, Budget(BIG))
    assert not r["ok"] and r["fail"]["code"] == "outside" and "outside the board outline" in r["fail"]["reason"]
    assert "B.1" in r["fail"]["reason"]
    b = S.board([pad("A", "N", 5.0, 10.0), pad("B", "N", 20.0, 10.0)], size=(26.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (20.0, 10.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c], nets={"N": NC(layers_ok={1})})
    r = D.route_conn(ctx, c, Budget(BIG))
    assert r["fail"]["code"] == "layer_pad" and "not on any layer" in r["fail"]["reason"]
    # an end without layers is a pour connection: same code (repair skips it), its own wording
    cp = S.conn(0, "N", (5.0, 10.0), (20.0, 10.0), pa="A.1", lb=())
    r2 = D.route_conn(S.ctx_for(b, [cp]), cp, Budget(BIG))
    assert r2["fail"]["code"] == "layer_pad" and "pour" in r2["fail"]["reason"]
    for f in (r["fail"],):
        assert set(f) >= {"reason", "code", "where", "blocked_by", "blockers", "static_share", "tried", "probe",
                          "frontier", "width"}


def test_soft_plan_names_the_crossed_connections():
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0), pad("O", "o", 12.0, 1.0), pad("P", "o", 12.0, 19.0)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1")
    co = S.conn(5, "o", (12.0, 1.0), (12.0, 19.0), pa="O.1", pb="P.1")
    ctx = S.ctx_for(b, [c, co], nets={"N": NC(layers_ok={0}), "o": NC(layers_ok={0})})
    assert D.route_conn(ctx, co, Budget(BIG))["how"] == "pattern"          # a wall of routed copper between A and B
    before = ctx.state.pieces()
    r = D.route_conn(ctx, c, Budget(BIG))
    assert not r["ok"] and r["fail"]["static_share"] < 1.0 and r["fail"]["blockers"][0]["conn"] == 5
    assert r["fail"]["blocked_by"] == ["o"] and r["fail"]["reason"] == "blocked by routed nets: o"
    # soft plan: nothing is added, the victim is named
    r = D.route_conn(ctx, c, Budget(BIG), widen=2, soft_cost=4.0)
    assert not r["ok"] and r.get("plan") and r["crossed"] == {5: 1} and r["fail"]["code"] == "plan"
    assert list(r["crossed_pieces"]) == [before[0].id] and ctx.state.pieces() == before
    assert ctx.state.status(0) != "routed"
    # a price table without that connection: it stays hard
    r = D.route_conn(ctx, c, Budget(BIG), soft_cost={99: 1.0})
    assert not r["ok"] and not r.get("plan")
    r = D.route_conn(ctx, c, Budget(BIG), soft_cost=lambda piece: 2.0 if piece.conn == 5 else None)
    assert r.get("plan") and r["crossed"] == {5: 1}
    # rip the victim, route, re-route the victim (what repair does): both end up routed on one layer? no: reported
    ctx.state.rip_conn(5)
    assert D.route_conn(ctx, c, Budget(BIG))["ok"]
    # soft mode with nothing in the way routes at once
    b2 = S.board(pads[:2] + [pad("X", "q", 12.0, 10.0)], size=(24.0, 20.0))
    ctx = S.ctx_for(b2, [c])
    r = D.route_conn(ctx, c, Budget(BIG), soft_cost=4.0)
    assert r["ok"] and r["how"] == "window" and not r.get("plan") and r["crossed"] == {}


def test_corridor_mask_and_widening():
    pads = [pad("A", "N", 5.5, 10.5), pad("B", "N", 25.5, 10.5), pad("X", "o", 15.5, 10.5, hw=0.3, hh=1.2)]
    b = S.board(pads, size=(32.0, 22.0))
    c = S.conn(0, "N", (5.5, 10.5), (25.5, 10.5), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c], nets={"N": NC(layers_ok={0})})
    ctx.tiles = Tiles(nx=32, ny=22)
    # a 5-tile-high corridor: the detour round X fits inside
    tiles = frozenset((tx, ty) for tx in range(4, 27) for ty in range(8, 13))
    ctx.corridors = {0: Corridor(conn=0, tiles=tiles, layers=(0,))}
    r = D.route_conn(ctx, c, Budget(BIG))
    assert r["ok"] and r["step"] == "corridor@0.2"
    geometry_ok(ctx, c)
    for p in r["pieces"]:
        for x, y in ((p.x1, p.y1), (p.x2, p.y2)):
            assert ctx.tiles.tile_of(x, y) in tiles, (x, y)
    # a 1-tile-high corridor is closed by X: two more rings open it
    ctx = S.ctx_for(b, [c], nets={"N": NC(layers_ok={0})})
    ctx.tiles = Tiles(nx=32, ny=22)
    ctx.corridors = {0: Corridor(conn=0, tiles=frozenset((tx, 10) for tx in range(4, 27)), layers=(0,))}
    r = D.route_conn(ctx, c, Budget(BIG))
    assert r["ok"] and r["step"] == "corridor+2@0.2"
    geometry_ok(ctx, c)
    assert max(abs(p.y1 - 10.5) for p in r["pieces"]) <= 2.5 + 1e-9
    # widen=2 starts there (what repair asks for)
    ctx = S.ctx_for(b, [c], nets={"N": NC(layers_ok={0})})
    ctx.tiles = Tiles(nx=32, ny=22)
    ctx.corridors = {0: Corridor(conn=0, tiles=frozenset((tx, 10) for tx in range(4, 27)), layers=(0,))}
    r = D.route_conn(ctx, c, Budget(BIG), widen=2)
    assert r["ok"] and r["step"] == "corridor@0.2" and ctx.history["detail"]["windows"] == 1
    # the corridor's layers narrow the plan's
    ctx = S.ctx_for(b, [c])
    ctx.tiles = Tiles(nx=32, ny=22)
    ctx.corridors = {0: Corridor(conn=0, tiles=tiles, layers=(0,))}
    assert W.allowed_layers(ctx, c) == (0,)
    ctx.corridors = {0: Corridor(conn=0, tiles=tiles, layers=(1,))}        # would cut the terminals off: ignored
    assert W.allowed_layers(ctx, c) == (0, 1)


def test_planned_layers_bind_only_the_unwidened_corridor():
    # a wall on the front layer; the global stage planned the connection on the front only
    pads = [pad("A", "N", 5.5, 10.5), pad("B", "N", 25.5, 10.5), pad("X", "o", 15.5, 10.5, hw=0.3, hh=10.0)]
    b = S.board(pads, size=(32.0, 22.0))
    c = S.conn(0, "N", (5.5, 10.5), (25.5, 10.5), pa="A.1", pb="B.1")
    tiles = frozenset((tx, ty) for tx in range(4, 27) for ty in range(8, 13))
    seen = []
    real = W.build

    def spy(*a, **kw):
        w = real(*a, **kw)
        seen.append(w.layers)
        return w
    D.wnd.build = spy
    try:
        ctx = S.ctx_for(b, [c])
        ctx.tiles = Tiles(nx=32, ny=22)
        ctx.corridors = {0: Corridor(conn=0, tiles=tiles, layers=(0,))}
        r = D.route_conn(ctx, c, Budget(BIG))
    finally:
        D.wnd.build = real
    assert r["ok"] and r["step"] == "corridor+2@0.2" and seen == [(0,), (0, 1)]
    assert sum(p.kind == "via" for p in r["pieces"]) == 2
    geometry_ok(ctx, c)
    assert W.allowed_layers(ctx, c) == (0,) and W.allowed_layers(ctx, c, corridor=False) == (0, 1)


def test_route_span_for_polish():
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0), pad("X", "o", 12.0, 10.0, hw=0.3, hh=2.0)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c])
    bud = Budget(BIG)
    pts = D.route_span(ctx, c, (8.013, 10.0), (16.0, 10.27), 1, bud)            # the back layer is empty: direct
    assert pts[0] == (8.013, 10.0) and pts[-1] == (16.0, 10.27) and len(pts) <= 4
    pts = D.route_span(ctx, c, (8.013, 10.0), (16.0, 10.27), 0, bud)            # the front has pad X in the way
    assert pts[0] == (8.013, 10.0) and pts[-1] == (16.0, 10.27) and len(pts) >= 4
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        assert ctx.oracle.seg_legal(0, x1, y1, x2, y2, 0.2, "N", 0.2)
    assert max(abs(y - 10.0) for _x, y in pts) > 2.0 and not ctx.state.pieces() and bud.used > CHARGE_WINDOW + CHARGE_SEARCH
    assert D.route_span(ctx, c, (8.0, 10.0), (8.0, 10.0), 0, bud) == [(8.0, 10.0)]
    assert D.route_span(ctx, c, (8.0, 10.0), (16.0, 10.0), 0, Budget(100)) is None          # no budget
    # walled off on that layer: None
    b2 = S.board(pads[:2] + [pad("X", "o", 12.0, 10.0, hw=0.3, hh=9.6)], size=(24.0, 20.0))
    ctx2 = S.ctx_for(b2, [c])
    assert D.route_span(ctx2, c, (8.0, 10.0), (16.0, 10.0), 0, Budget(BIG)) is None
    assert D.route_span(ctx2, c, (8.0, 10.0), (16.0, 10.0), 0, Budget(BIG), width=0.15) is None
    assert D.route_span(ctx2, c, (8.0, 10.0), (16.0, 10.0), 1, Budget(BIG)) == [(8.0, 10.0), (16.0, 10.0)]


def test_pitch_scale_halves_the_window_pitch():
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0), pad("X", "o", 12.0, 10.0)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1")
    seen = []
    real = W.build

    def spy(*a, **kw):
        w = real(*a, **kw)
        seen.append(w.pitch)
        return w
    D.wnd.build = spy
    try:
        ctx = S.ctx_for(b, [c])
        assert D.route_conn(ctx, c, Budget(BIG), pitch_scale=0.5)["ok"]
        ctx2 = S.ctx_for(b, [c])
        assert D.route_conn(ctx2, c, Budget(BIG))["ok"]
    finally:
        D.wnd.build = real
    assert seen == [0.1, 0.2]
    geometry_ok(ctx, c)
    assert "pattern" not in ctx.history["detail"] or ctx.history["detail"]["pattern_tries"] == 0     # rescue skips patterns


# ── the stage ─────────────────────────────────────────────────────────────
def _bus_board():
    pads, conns = [], []
    for i in range(8):
        y = 4.0 + i * 1.27
        pads += [pad("J1", f"n{i}", 5.0, y, num=str(i + 1)), pad("J2", f"n{i}", 30.0, 24.0 - y + 0.013 * i, num=str(i + 1))]
        conns.append(S.conn(i, f"n{i}", (5.0, y), (30.0, round(24.0 - y + 0.013 * i, 6)), pa=f"J1.{i + 1}", pb=f"J2.{i + 1}",
                            rank=(0, 0, i)))
    pads += [pad("M", "m", 17.0, 10.0, hw=0.8, hh=3.0), pad("K", "k", 12.0, 16.0, hw=0.4, hh=0.4, kind="tht", hole=0.2)]
    return S.board(pads, size=(36.0, 24.0)), conns


def test_run_report_statuses_and_same_twice():
    outs = []
    for _ in range(2):
        W.clear_cache()
        b, conns = _bus_board()
        ctx = S.ctx_for(b, conns)
        seen = []
        ctx.progress = lambda f, msg: seen.append((round(f, 3), msg))
        bud = Budget(BIG)
        rep = D.run(ctx, bud)
        assert isinstance(rep, StageReport) and rep.name == "detail" and rep.routed + rep.failed == 8
        assert rep.work == bud.used > 0 and rep.notes and rep.data["how"]["window"] + rep.data["how"]["pattern"] == rep.routed
        assert rep.routed >= 6, rep.notes
        assert len(seen) == 8 and seen[-1][0] == 1.0
        for c in conns:
            st = ctx.state.status(c.idx)
            assert st in ("routed", "failed")
            if st == "routed":
                geometry_ok(ctx, c)
            else:
                assert not ctx.state.pieces(conn=c.idx)
        assert not S.all_legal(ctx)
        assert rep.score.opens == rep.failed and rep.score.vias == sum(p.kind == "via" for p in ctx.state.pieces())
        for k in ("windows", "searches", "expansions", "median_expansions", "build_share", "window_cache_hits", "fail_codes",
                  "joins", "emit_rejects"):
            assert k in rep.data, k
        outs.append(([(p.id, p.net, p.conn, p.kind, p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.drill, p.target_w)
                      for p in ctx.state.pieces()], rep.routed, rep.failed, rep.work,
                     {k: v for k, v in rep.data.items() if not k.endswith("_s") and k != "build_share"}))
    assert outs[0] == outs[1]                                   # same board, same budget: identical copper and work
    # only=: just those connections; a second run leaves routed ones alone
    b, conns = _bus_board()
    ctx = S.ctx_for(b, conns)
    rep = D.run(ctx, Budget(BIG), only=[2, 5])
    assert rep.routed + rep.failed == 2 and {p.conn for p in ctx.state.pieces()} <= {2, 5}
    n = len(ctx.state.pieces())
    rep = D.run(ctx, Budget(BIG), only=[2, 5])
    assert rep.routed + rep.failed == rep.failed and len(ctx.state.pieces()) == n


def test_order_puts_overflowed_corridors_first_within_a_plan_rank():
    b, conns = _bus_board()
    ctx = S.ctx_for(b, conns)
    conns[6].rank = (-1, 0, 6)                                   # plan rank beats everything
    ctx.corridors = {3: Corridor(conn=3, tiles=frozenset(), overflow=2.0), 1: Corridor(conn=1, tiles=frozenset(), overflow=0.5)}
    assert [c.idx for c in D.order(ctx)] == [6, 3, 1, 0, 2, 4, 5, 7]
    assert [c.idx for c in D.order(ctx, only=[7, 1, 3])] == [3, 1, 7]


def test_budget_is_respected_and_reported():
    b, conns = _bus_board()
    full = S.ctx_for(b, conns)
    need = Budget(BIG)
    D.run(full, need)
    for total in (0, 150, CHARGE_PATTERN * 3, need.used // 3):
        W.clear_cache()
        ctx = S.ctx_for(b, conns)
        bud = Budget(total)
        rep = D.run(ctx, bud)
        assert rep.routed + rep.failed == 8 and rep.work == bud.used
        over = bud.used - bud.total
        assert over <= CHARGE_PATTERN * 6 + 64, (total, over)     # never far past the budget (one pattern batch)
        assert not S.all_legal(ctx)
        failed = [c for c in conns if ctx.state.status(c.idx) == "failed"]
        assert len(failed) == rep.failed
        if total < need.used // 3 + 1:
            assert rep.failed >= 1 and any("budget" in n for n in rep.notes)
            codes = {ctx.state.fail(c.idx)["code"] for c in failed}
            assert "budget" in codes and rep.data["fail_codes"].get("budget")
            assert {e["conn"] for e in rep.data["failed"]} == {c.idx for c in failed}
    assert rep.routed <= full.state.score().opens * 0 + 8


def test_plan_regions_are_obeyed():
    class Reg:
        idx, rect, forbid, cost, layers, nets, exc, name = 0, (10.0, 0.0, 14.0, 13.0), True, 1.0, None, None, None, "r"

        def applies(self, net):
            return net == "N"
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c], regions=[Reg()], nets={"N": NC(layers_ok={0})})
    r = D.route_conn(ctx, c, Budget(BIG))
    assert r["ok"] and r["how"] == "window"
    geometry_ok(ctx, c)
    for p in r["pieces"]:
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            x, y = p.x1 + (p.x2 - p.x1) * t, p.y1 + (p.y2 - p.y1) * t
            assert not (10.0 < x < 14.0 and y < 13.0 - 1e-9), (x, y)
    # avoid rectangles of the net itself
    ctx = S.ctx_for(b, [c], nets={"N": NC(layers_ok={0}, avoid=[(10.0, 7.0, 14.0, 20.0)])})
    r = D.route_conn(ctx, c, Budget(BIG))
    assert r["ok"] and min(min(p.y1, p.y2) for p in r["pieces"]) < 7.0


def test_history_points_steer_the_search():
    pads = [pad("A", "N", 5.0, 10.0), pad("B", "N", 19.0, 10.0), pad("X", "o", 12.0, 10.0, hw=0.3, hh=0.3)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1")
    side = []
    for hist in (None, [(12.0, 11.0, 1.2, 3.0)], [(12.0, 9.0, 1.2, 3.0)]):
        ctx = S.ctx_for(b, [c], nets={"N": NC(layers_ok={0})})
        if hist:
            ctx.history["points"] = hist
        r = D.route_conn(ctx, c, Budget(BIG))
        assert r["ok"]
        ys = [p.y1 for p in r["pieces"]] + [p.y2 for p in r["pieces"]]
        side.append(1 if max(ys) > 10.0 + 1e-9 else -1)
    assert side[1] == -1 and side[2] == 1                       # pushed to the side without the history cost


# ── bridge mode (pours pass: any copper of one side to any copper of the other) ──
def test_bridge_joins_two_sides_from_any_of_their_copper():
    """Pad A's side also owns pad A2, which is the only one with a free way to pad B: a normal route from A is
    walled in, the bridge starts from A2. No pattern, legal pieces, and the two sides end up connected."""
    from copper_kicad.pcb.v2.types import Terminal
    wall = [pad("X", "o", 7.0, 10.0, hw=0.3, hh=9.6), pad("Y", "o", 3.0, 10.0, hw=0.3, hh=9.6)]
    pads = [pad("A", "N", 5.0, 10.0), pad("A2", "N", 10.0, 10.0), pad("B", "N", 19.0, 10.0)] + wall
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (5.0, 10.0), (19.0, 10.0), pa="A.1", pb="B.1", via=(0.0, 0.0))      # no vias: one layer
    ctx = S.ctx_for(b, [c])
    plain = D.route_conn(ctx, c, Budget(BIG))
    assert not plain["ok"] and not ctx.state.pieces(), plain.get("fail")
    spec = {"src": {"pads": [Terminal(5.0, 10.0, (0,), pad="A.1"), Terminal(10.0, 10.0, (0,), pad="A2.1")]},
            "tgt": {"pads": [Terminal(19.0, 10.0, (0,), pad="B.1")]}, "box": (10.0, 10.0, 19.0, 10.0), "all": None}
    # the state's connectivity knows nothing of "A2 is on A's side" here, so the bridge is judged by its copper:
    # ask the search directly (one window), then check the emitted pieces
    r = D._route_window(ctx, c, Budget(BIG), mask=None, box=(8.0, 8.0, 21.0, 12.0), width=0.2, limit=40_000,
                        pitch_scale=1.0, extra_vias=0, soft_cost=None, lays=(0,), strict=False, bridge=spec)
    assert r.get("reason") in (None, "not_connected") or r["ok"], r.get("reason")
    # sources came from A2 (A itself is outside the window / walled in), the target is B
    win = r["win"]
    src, _sanc, tgt, _v = D._ends(ctx, c, win, 0.2, (0,), False, False, spec)
    assert src and tgt
    xs = sorted({round(win.xy(s)[0], 1) for s in src})
    assert all(x > 8.0 for x in xs), xs


def test_island_cells_keep_a_margin_and_stitch_needs_both_layers():
    """Cells of a pour island are bridge ends only well inside its outline; a stitch candidate is a lattice point
    that is source copper on one layer and target copper on another."""
    pads = [pad("A", "N", 3.0, 3.0), pad("B", "N", 21.0, 17.0)]
    b = S.board(pads, size=(24.0, 20.0))
    c = S.conn(0, "N", (3.0, 3.0), (21.0, 17.0), pa="A.1", pb="B.1", lb=(0, 1), la=(0, 1))
    ctx = S.ctx_for(b, [c])
    win = W.build(ctx, c, None, (8.0, 6.0, 16.0, 14.0), width=0.2, layers=(0, 1))
    isl_a = [(0, [(10.0, 8.0), (13.0, 8.0), (13.0, 11.0), (10.0, 11.0)])]
    isl_b = [(1, [(12.0, 10.0), (15.0, 10.0), (15.0, 13.0), (12.0, 13.0)])]
    a, t = {}, {}
    D._island_cells(win, (0, 1), False, isl_a, a)
    D._island_cells(win, (0, 1), False, isl_b, t)
    assert a and t
    for s, (g0, anc) in a.items():
        x, y, lay = win.xy(s)
        assert lay == 0 and g0 == 0.0 and anc == ("pt", x, y)
        assert 10.0 + D.BRIDGE_MARGIN - 1e-6 <= x <= 13.0 - D.BRIDGE_MARGIN + 1e-6, x
        assert 8.0 + D.BRIDGE_MARGIN - 1e-6 <= y <= 11.0 - D.BRIDGE_MARGIN + 1e-6, y
    G = win.grid.G
    both = {s % G for s in a} & {s % G for s in t}
    assert both                                               # the overlap 12..13 x 10..11
    for i in both:
        x, y = win.grid.xy(i)
        assert 12.0 < x < 13.0 and 10.0 < y < 11.0
    # a bridge between the two islands is one via in the overlap (the state has no clusters here, so the stitch
    # is rejected as not connected and nothing stays in the state)
    sources = {s: 0.0 for s in a}
    sanc = {s: v[1] for s, v in a.items()}
    tgt = {s: v[1] for s, v in t.items()}
    got = D._stitch(ctx, c, win, sources, sanc, tgt, Budget(BIG))
    assert got is None and not ctx.state.pieces()
    assert ctx.oracle.via_legal(12.5, 10.5, 0.6, 0.3, "N")


# ── corpus (optional) ─────────────────────────────────────────────────────
def test_corpus_boards_route_legally():
    """--corpus only: the stage alone (box corridors, no fan-out / repair) on three corpus boards. Every emitted
    piece is oracle-legal, the run is the same twice, and nothing regresses below what was measured for W4."""
    if "--corpus" not in sys.argv:
        print("    skipped (run with --corpus)")
        return
    sys.path.insert(0, str(S.ROOT / "bench" / "route"))
    import w4_detail_bench as B
    floor = {"ecc83": 100.0, "pic_programmer": 90.0, "multichannel_mixer": 98.0}
    for name, want in floor.items():
        a = B.run_board(name)
        b2 = B.run_board(name)
        print(f"    {name}: {a['routed']}/{a['conns']} ({a['pct']} %), {a['wall_s']} s, {a['exp_per_s']} exp/s, "
              f"build share {a['build_share']}")
        assert a["oracle_illegal"] == 0 and a["pct"] >= want, a
        for k in ("routed", "failed", "work", "segs", "vias", "expansions", "windows", "fail_codes"):
            assert a[k] == b2[k], (name, k, a[k], b2[k])


if __name__ == "__main__":
    sys.exit(S.run_tests(dict(globals())))
