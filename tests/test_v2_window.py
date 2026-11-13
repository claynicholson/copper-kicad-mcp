"""Router v2 window grid (design §4.7): pitch ladder, lattice origin, corridor mask, exact rasterisation, LRU.

Pure tests on synthetic boards (no KiCad).   Run:  python tests/test_v2_window.py [test_name ...]
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v2_w4_support as S  # noqa: E402
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import geom  # noqa: E402
from copper_kicad.pcb.guided.grid import Grid  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2 import window as W  # noqa: E402
from copper_kicad.pcb.v2.types import GUARD_MM  # noqa: E402


def row(ref, pitch, n=8, x=10.0, y=10.0, hw=0.1, hh=0.4, net=None):
    return [{"ref": ref, "num": str(i + 1), "net": net or f"{ref}n{i}", "x": round(x + i * pitch, 6), "y": y, "hw": hw,
             "hh": hh} for i in range(n)]


class Tiles:
    """Minimal stand-in for tiles.Tiles: uniform tiles of ``t`` mm with origin (x0, y0); no x0/y0 attributes."""
    def __init__(self, t=1.0, x0=0.0, y0=0.0, nx=40, ny=30):
        self.t, self.nx, self.ny, self.nl, self._o = t, nx, ny, 2, (x0, y0)

    def tile_of(self, x, y):
        return (int(math.floor((x - self._o[0]) / self.t)), int(math.floor((y - self._o[1]) / self.t)))


# ── pitch ─────────────────────────────────────────────────────────────────
def test_pitch_ladder_for_pad_pitches():
    box = (8.0, 8.0, 16.0, 12.0)
    for pitch, w, cl, want in ((0.4, 0.2, 0.2, 0.2),        # largest divisor of 0.4 that is <= (w + cl) / 2
                               (0.4, 0.1, 0.1, 0.1),
                               (0.5, 0.2, 0.2, 0.125),      # 0.25 fails (a); 0.125 = pitch / 4
                               (0.5, 0.3, 0.2, 0.25),
                               (0.65, 0.2, 0.2, 0.05),      # the only ladder value dividing 0.65
                               (1.27, 0.2, 0.2, 0.2),       # no divisor: p <= 1.27 / 4, so (a) decides
                               (1.27, 0.5, 0.2, 0.25),
                               (0.8, 0.15, 0.15, 0.1)):
        b = S.board(row("U1", pitch))
        p, ox, oy = W.choose_pitch(b, box, w, cl, 2)
        assert p == want, (pitch, w, cl, p, want)
        assert p <= (w + cl) / 2 + 1e-9
    # no fine-pitch footprint in the box: only (a) applies
    b = S.board(row("U1", 0.4, x=30.0, y=25.0))
    assert W.choose_pitch(b, box, 0.2, 0.2, 2)[0] == 0.2
    assert W.choose_pitch(b, box, 0.3, 0.3, 2)[0] == 0.25
    # a pitch no ladder value divides: p <= pitch / 4
    b = S.board(row("U1", 0.635))
    assert W.choose_pitch(b, box, 0.2, 0.2, 2)[0] == 0.15
    assert W.min_pad_pitch(b, box) == 0.635 and W.min_pad_pitch(b, (30, 20, 35, 25)) is None


def test_pitch_is_coarsened_when_the_window_is_too_big():
    b = S.board(row("U1", 0.5), size=(120.0, 120.0))
    p, _ox, _oy = W.choose_pitch(b, (0.0, 0.0, 120.0, 120.0), 0.2, 0.2, 2)
    assert p > 0.125 and W._states((0.0, 0.0, 120.0, 120.0), p, 2) <= W.MAX_STATES
    assert W._states((0.0, 0.0, 120.0, 120.0), 0.125, 2) > W.MAX_STATES
    # smaller box, same board: the fine pitch again
    assert W.choose_pitch(b, (5.0, 5.0, 25.0, 25.0), 0.2, 0.2, 2)[0] == 0.125


def test_origin_puts_pad_centres_on_cells():
    pads = row("U1", 0.5, n=10, x=10.037, y=10.213) + [{"ref": "R1", "net": "a", "x": 12.4, "y": 14.07, "hw": 0.3, "hh": 0.3}]
    b = S.board(pads)
    p, ox, oy = W.choose_pitch(b, (8.0, 8.0, 16.0, 16.0), 0.2, 0.2, 2)
    assert p == 0.125
    assert abs(ox - (10.037 % 0.125)) < 1e-6 and abs(oy - (10.213 % 0.125)) < 1e-6
    c = S.conn(0, "a", (12.4, 14.07), (30.0, 14.0), pa="R1.1")
    ctx = S.ctx_for(b, [c])
    win = W.build(ctx, c, None, (8.0, 8.0, 16.0, 16.0), width=0.2, cache=False)
    for q in pads[:10]:
        x, y, _l = win.xy(win.cell(q["x"], q["y"], 0))
        assert abs(x - q["x"]) < 1e-6 and abs(y - q["y"]) < 1e-6, (q["x"], x, q["y"], y)
    # a fine-pitch TERMINAL wins over the majority: its centre must be on the lattice
    pads2 = row("U2", 0.5, n=3, x=10.06, y=10.0, net=None) + row("R", 2.0, n=6, x=9.0, y=13.0)
    b2 = S.board(pads2)
    c2 = S.conn(0, "U2n1", (10.56, 10.0), (20.0, 20.0), pa="U2.2")
    ctx2 = S.ctx_for(b2, [c2])
    win2 = W.build(ctx2, c2, None, (8.0, 8.0, 21.0, 21.0), width=0.2, cache=False)
    x, y, _l = win2.xy(win2.cell(10.56, 10.0, 0))
    assert abs(x - 10.56) < 1e-6 and abs(y - 10.0) < 1e-6


# ── rasterisation ─────────────────────────────────────────────────────────
def _ctx(pads, **kw):
    b = S.board(pads, **kw)
    c = S.conn(0, "N", (5.0, 5.0), (35.0, 25.0), la=(0, 1), lb=(0, 1))
    return S.ctx_for(b, [c]), c


def test_free_cells_and_steps_between_them_are_oracle_legal():
    """The window is only an accelerator, but with raster_guard every step between two free neighbouring cells
    must be exactly legal: the property whose absence was v1's one_air_max failure (Appendix A1)."""
    rnd = random.Random(11)
    pads = [{"ref": "A", "net": "N", "x": 5.0, "y": 5.0, "hw": 0.3, "hh": 0.3}]
    for i in range(40):
        rot = rnd.choice((0, 0, 30, 45, 77))
        pads.append({"ref": f"P{i}", "net": f"n{i % 5}", "x": round(rnd.uniform(8, 32), 3), "y": round(rnd.uniform(6, 24), 3),
                     "hw": rnd.choice((0.15, 0.4, 0.8)), "hh": rnd.choice((0.15, 0.3)), "r": rnd.choice((0.0, 0.05, 0.15)),
                     "rot": rot})
    pads.append({"ref": "H", "x": 20.0, "y": 15.0, "kind": "npth", "hole": 0.8})
    for width, pitch in ((0.25, 0.15), (0.2, 0.2), (0.15, 0.1)):
        ctx, c = _ctx(pads)
        win = W.build(ctx, c, None, (6.0, 4.0, 34.0, 26.0), width=width, pitch=pitch, cache=False)
        g, occ = win.grid, win.cm.occ
        free = [s for s in range(g.G) if occ[s] == 0]
        assert len(free) > 2000
        bad = 0
        for s in free:
            x, y, l = win.xy(s)
            for d in (1, g.W, g.W + 1, g.W - 1):
                if occ[s + d] == 0:
                    x2, y2, _l = win.xy(s + d)
                    if not ctx.oracle.seg_legal(l, x, y, x2, y2, width, "N", 0.2):
                        bad += 1
        assert bad == 0, (width, pitch, bad)
        # and the grid is not grossly over-blocked: a blocked interior cell is within the inflation of something
        over = 0
        for s in range(g.G):
            if occ[s] == -1:
                x, y, l = win.xy(s)
                if ctx.oracle.inside(x, y) and not ctx.oracle.blockers(l, x - 0.3, y, x + 0.3, y, width, "N", clearance=0.2) \
                        and not ctx.oracle.blockers(l, x, y - 0.3, x, y + 0.3, width, "N", clearance=0.2):
                    over += 1
        assert over == 0, over


def test_inflation_uses_raster_guard_not_the_fixed_guard():
    ctx, c = _ctx([{"ref": "X", "net": "o", "x": 20.0, "y": 15.0, "hw": 0.5, "hh": 0.5}])
    p, w = 0.15, 0.25
    win = W.build(ctx, c, None, (15.0, 10.0, 25.0, 20.0), width=w, pitch=p, cache=False)
    reach = w / 2 + 0.2
    guard = O.raster_guard(p, reach)
    assert guard > 5 * GUARD_MM and abs(guard - 0.016868) < 1e-5      # 16.9 um here; 23.7 um at reach 0.225 (A1)
    assert abs(O.raster_guard(0.15, 0.225) - 0.0237) < 1e-4
    # walk along the pad's east side: blocked exactly up to edge + reach + guard
    g = win.grid
    seen = 0
    for s in range(g.G):
        x, y, _l = win.xy(s)
        if abs(y - 15.0) < 0.3 and 20.5 < x < 21.5:
            d = x - 20.5
            seen += 1
            if d < reach + guard - 0.0004:
                assert win.cm.occ[s] == -1, (x, d)
            elif d > reach + guard:
                assert win.cm.occ[s] == 0, (x, d)
    assert seen > 10


def test_own_net_copper_is_free_and_same_net_pads_block_via_centres():
    pads = [{"ref": "A", "net": "N", "x": 20.0, "y": 15.0, "hw": 0.5, "hh": 0.5},
            {"ref": "B", "net": "o", "x": 24.0, "y": 15.0, "hw": 0.5, "hh": 0.5, "layer": 1}]
    ctx, c = _ctx(pads)
    win = W.build(ctx, c, None, (15.0, 10.0, 30.0, 20.0), width=0.2, pitch=0.1, cache=False)
    assert win.layers == (0, 1) and win.lidx == {0: 0, 1: 1}
    s = win.cell(20.0, 15.0, 0)
    assert win.free(s) and win.free(win.cell(20.0, 15.0, 1))
    G = win.grid.G
    assert win.cm.vocc[s % G] == -1                       # no via in an SMD pad of the own net (v1 policy)
    assert win.cm.vocc[win.cell(22.0, 15.0, 0) % G] == 0
    # pad B is on the back only: it blocks back-layer tracks and (a through via) every via site near it
    sb = win.cell(24.0, 15.0, 1)
    assert not win.free(sb) and win.free(win.cell(24.0, 15.0, 0))
    assert win.cm.vocc[sb % G] == -1
    # every free via cell is a legal via for the oracle
    bad = n = 0
    for i in range(G):
        if win.cm.vocc[i] == 0:
            x, y, _l = win.xy(i)
            n += 1
            if not ctx.oracle.via_legal(x, y, 0.6, 0.3, "N", clearance=0.2):
                bad += 1
    assert n > 1000 and bad == 0, (n, bad)


def test_single_layer_window_and_board_edge():
    ctx, c = _ctx([{"ref": "A", "net": "N", "x": 5.0, "y": 5.0, "hw": 0.3, "hh": 0.3}], size=(20.0, 12.0))
    win = W.build(ctx, c, None, (-5.0, -5.0, 30.0, 30.0), width=0.2, layers=(1,), cache=False)
    assert win.layers == (1,) and win.box[0] >= -0.5 and win.box[2] <= 20.5        # clipped to the board
    assert all(v == -1 for v in win.cm.vocc)                                         # one layer: no vias
    x, y, l = win.xy(win.cell(10.0, 6.0, 1))
    assert l == 1 and win.free(win.cell(10.0, 6.0, 1))
    assert not win.free(win.cell(0.3, 6.0, 1))            # 0.3 from the edge: needs 0.3 + 0.1
    assert win.free(win.cell(0.6, 6.0, 1))
    assert not win.free(win.cell(-0.2, 6.0, 1))           # off the board


def test_keepout_and_soft_shapes():
    b = S.board([{"ref": "A", "net": "N", "x": 5.0, "y": 5.0, "hw": 0.3, "hh": 0.3},
                 {"ref": "T", "net": "G", "x": 30.0, "y": 20.0, "hw": 0.3, "hh": 0.3}],
                zones=[S.keepout(10.0, 10.0, 14.0, 14.0, tracks=True, vias=False),
                       S.keepout(20.0, 10.0, 24.0, 14.0, tracks=False, vias=True)])
    c = S.conn(0, "N", (5.0, 5.0), (35.0, 25.0), la=(0, 1), lb=(0, 1))
    ctx = S.ctx_for(b, [c])
    sid = ctx.oracle.add_soft("thermal", "G", 0, (17.0, 20.0, 1.0, 0.0, 1.0, 0.25, 0.0))
    win = W.build(ctx, c, None, (4.0, 4.0, 36.0, 26.0), width=0.2, pitch=0.1, cache=False)
    G = win.grid.G
    assert not win.free(win.cell(12.0, 12.0, 0)) and win.cm.vocc[win.cell(12.0, 12.0, 0) % G] == 0
    assert win.free(win.cell(22.0, 12.0, 0)) and win.cm.vocc[win.cell(22.0, 12.0, 0) % G] == -1
    s = win.cell(17.0, 20.0, 0)
    assert win.free(s) and win.cmul is not None and win.cmul[s] == W.SOFT_PRICE["thermal"]      # priced, not blocked
    assert win.cmul[win.cell(17.0, 20.0, 1)] == 1.0                                              # soft shape is on F.Cu
    assert win.cmul[win.cell(17.0, 23.0, 0)] == 1.0
    # hardened: closed
    ctx.oracle.harden(sid)
    win2 = W.build(ctx, c, None, (4.0, 4.0, 36.0, 26.0), width=0.2, pitch=0.1, cache=False)
    assert not win2.free(win2.cell(17.0, 20.0, 0))
    # pad repulsion: one cell beyond a foreign pad's halo is free but priced
    x = 30.0 + 0.3 + 0.1 + 0.2 + O.raster_guard(0.1, 0.3)
    s = win.cell(x + 0.1, 20.0, 0)
    assert win.free(s) and abs(win.cmul[s] - W.PAD_REPEL) < 1e-6 and win.cmul[win.cell(x + 0.4, 20.0, 0)] == 1.0


def test_routed_pieces_go_to_the_owner_arrays():
    ctx, c = _ctx([{"ref": "A", "net": "N", "x": 5.0, "y": 5.0, "hw": 0.3, "hh": 0.3}], nets={"o": {}, "o2": {}})
    st = ctx.state
    p1 = st.add(kind="seg", net="o", conn=-1, layer=0, x1=10.0, y1=10.0, x2=20.0, y2=10.0, w=0.2)
    p2 = st.add(kind="seg", net="o2", conn=-1, layer=0, x1=15.0, y1=5.0, x2=15.0, y2=15.0, w=0.2)
    pv = st.add(kind="via", net="o", conn=-1, layer=-1, x1=25.0, y1=10.0, x2=25.0, y2=10.0, w=0.6, drill=0.3)
    st.add(kind="seg", net="N", conn=-1, layer=0, x1=10.0, y1=20.0, x2=20.0, y2=20.0, w=0.2)
    box = (6.0, 4.0, 34.0, 26.0)
    win = W.build(ctx, c, None, box, width=0.2, pitch=0.1, soft=True, cache=False)
    G = win.grid.G
    s = win.cell(12.0, 10.0, 0)
    assert win.cm.S[s] == 0 and win.cm.occ[s] == -1 and win.owner[s] == p1.id        # static free, dynamic hard
    assert win.cm.occ[win.cell(12.0, 10.0, 1)] == 0                                  # other layer untouched
    x = win.cell(15.0, 10.0, 0)
    assert win.owner[x] in (p1.id, p2.id) and sorted(win.multi[x]) == [p1.id, p2.id]  # both pieces recorded
    v = win.cell(25.0, 10.0, 1)
    assert win.cm.occ[v] == -1 and win.owner[v] == pv.id and win.vowner[v % G] == pv.id and win.cm.vocc[v % G] == -1
    assert win.vowner[win.cell(12.0, 10.6, 0) % G] == p1.id                           # a via there would hit the track
    o = win.cell(15.0, 20.0, 0)
    assert win.free(o) and win.owner[o] == 0                                          # own net: no obstacle
    hard = W.build(ctx, c, None, box, width=0.2, pitch=0.1, soft=False, cache=False)
    assert list(hard.cm.occ) == list(win.cm.occ) and not hard.multi


# ── corridor mask ─────────────────────────────────────────────────────────
def test_corridor_mask_blocks_outside_and_keeps_terminals_open():
    b = S.board([{"ref": "A", "net": "N", "x": 5.5, "y": 5.5, "hw": 0.3, "hh": 0.3},
                 {"ref": "B", "net": "N", "x": 20.5, "y": 5.5, "hw": 0.3, "hh": 0.3}])
    c = S.conn(0, "N", (5.5, 5.5), (20.5, 5.5), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c])
    ctx.tiles = Tiles(t=1.0, x0=0.25, y0=0.5)
    assert W.tile_origin(ctx.tiles) == (0.25, 0.5, 1.0)          # derived from tile_of by bisection
    mask = frozenset((tx, 5) for tx in range(7, 19)) | frozenset((tx, 4) for tx in range(7, 19))
    assert W.tiles_box(ctx.tiles, mask) == (7.25, 4.5, 19.25, 6.5)
    win = W.build(ctx, c, mask, (3.0, 3.0, 23.0, 9.0), width=0.2, pitch=0.1, cache=False)
    g = win.grid
    n_in = n_out = 0
    for s in range(g.G):
        x, y, _l = win.xy(s)
        ix, iy = s % g.W, s // g.W
        if ix in (0, g.W - 1) or iy in (0, g.H - 1):
            assert win.cm.occ[s] == W.MASKED
            continue
        t = ctx.tiles.tile_of(x + 1e-9, y + 1e-9)
        d = min(math.hypot(x - 5.5, y - 5.5), math.hypot(x - 20.5, y - 5.5))
        if abs(d - W.TERMINAL_OPEN) < 1e-6:
            continue                                       # on the rim of the terminal opening: either way
        if t in mask or d < W.TERMINAL_OPEN:
            assert win.cm.occ[s] != W.MASKED, (x, y)
            n_in += 1
        else:
            assert win.cm.occ[s] == W.MASKED, (x, y, t)
            n_out += 1
    assert n_in > 2000 and n_out > 5000
    assert win.free(win.cell(5.5, 5.5, 0)) and win.free(win.cell(20.5, 5.5, 0))      # terminals outside the mask tiles
    assert win.col_tile is not None and win.tile_t == 1.0
    assert (win.col_tile[win.cell(10.0, 5.2, 0) % g.W], win.row_tile[win.cell(10.0, 5.2, 0) // g.W]) == (9, 4)
    try:
        ctx.tiles = None
        W.build(ctx, c, mask, (3.0, 3.0, 23.0, 9.0), width=0.2, cache=False)
        raise AssertionError("a mask without tiles must raise")
    except ValueError:
        pass
    # Tiles objects that expose x0 / y0 are read directly
    t2 = Tiles(t=0.5, x0=3.0, y0=4.0)
    t2.x0, t2.y0 = 3.0, 4.0
    assert W.tile_origin(t2) == (3.0, 4.0, 0.5)


# ── block / reset / LRU ───────────────────────────────────────────────────
def _snap(win):
    return (list(win.cm.S), list(win.cm.occ), list(win.cm.SV), list(win.cm.vocc), list(win.owner), list(win.vowner),
            None if win.cmul is None else list(win.cmul), win.pitch, win.layers, win.grid.x0, win.grid.y0,
            win.grid.W, win.grid.H)


def test_block_and_reset():
    ctx, c = _ctx([{"ref": "A", "net": "N", "x": 5.0, "y": 5.0, "hw": 0.3, "hh": 0.3}])
    win = W.build(ctx, c, None, (6.0, 4.0, 20.0, 16.0), width=0.2, cache=False)
    before = _snap(win)
    a, b_ = win.cell(10.0, 10.0, 0), win.cell(11.0, 10.0, 1)
    win.block([a, b_])
    win.block_via([a % win.grid.G])
    assert not win.free(a) and not win.free(b_) and win.cm.vocc[a % win.grid.G] == -1
    assert list(win.cm.S) == before[0]                    # the static map is never touched
    win.reset()
    assert _snap(win) == before


def test_lru_hit_equals_rebuild():
    W.clear_cache()
    pads = [{"ref": "A", "net": "N", "x": 5.0, "y": 5.0, "hw": 0.3, "hh": 0.3}] + row("U1", 0.5, n=12, x=10.0, y=12.0)
    ctx, c = _ctx(pads, nets={"o": {}})
    box = (6.0, 4.0, 30.0, 20.0)
    h0, b0 = W.STATS["hits"], W.STATS["builds"]
    w1 = W.build(ctx, c, None, box, width=0.2)
    fresh = _snap(W.build(ctx, c, None, box, width=0.2, cache=False))
    assert _snap(w1) == fresh
    w1.block([w1.cell(8.0, 8.0, 0)])
    w2 = W.build(ctx, c, None, box, width=0.2)
    assert w2 is w1 and W.STATS["hits"] == h0 + 1          # a hit ...
    assert _snap(w2) == fresh                             # ... equal to a rebuild (the block was undone)
    # any difference in what the window depends on is a miss
    assert W.build(ctx, c, None, box, width=0.15) is not w1
    assert W.build(ctx, c, None, (6.0, 4.0, 30.0, 21.0), width=0.2) is not w1
    assert W.build(ctx, c, None, box, width=0.2, soft=True) is not w1
    assert W.build(ctx, c, None, box, width=0.2, layers=(0,)) is not w1
    # copper inside the box changes the key; copper far outside does not
    far = ctx.state.add(kind="seg", net="o", conn=-1, layer=0, x1=36.0, y1=27.0, x2=38.0, y2=27.0, w=0.2)
    assert W.build(ctx, c, None, box, width=0.2) is w1
    near = ctx.state.add(kind="seg", net="o", conn=-1, layer=0, x1=8.0, y1=6.0, x2=12.0, y2=6.0, w=0.2)
    w3 = W.build(ctx, c, None, box, width=0.2)
    assert w3 is not w1 and w3.owner[w3.cell(10.0, 6.0, 0)] == near.id
    assert _snap(w3) == _snap(W.build(ctx, c, None, box, width=0.2, cache=False))
    ctx.state.remove(near.id)
    assert W.build(ctx, c, None, box, width=0.2) is w1     # back to the earlier state: the old window is valid again
    assert far.id
    # at most LRU_SIZE windows are kept
    for k in range(W.LRU_SIZE + 3):
        W.build(ctx, c, None, (6.0, 4.0, 20.0 + k, 20.0), width=0.2)
    assert len(W._CACHES[ctx.oracle]) == W.LRU_SIZE
    assert W.STATS["builds"] > b0
    W.clear_cache()


def test_same_twice():
    pads = [{"ref": "A", "net": "N", "x": 5.0, "y": 5.0, "hw": 0.3, "hh": 0.3}] + row("U1", 0.65, n=14, x=10.0, y=12.0)
    snaps = []
    for _ in range(2):
        ctx, c = _ctx(pads, nets={"o": {}})
        ctx.state.add(kind="seg", net="o", conn=-1, layer=1, x1=8.0, y1=6.0, x2=12.0, y2=9.0, w=0.2)
        snaps.append(_snap(W.build(ctx, c, None, (6.0, 4.0, 30.0, 20.0), width=0.2, soft=True, cache=False)))
    assert snaps[0] == snaps[1] and snaps[0][7] == 0.05


def test_rotated_box_spans_match_the_per_cell_test():
    rnd = random.Random(5)
    for _ in range(120):
        g = Grid((0, 0, 6, 6), rnd.choice((0.05, 0.1, 0.125)), 1)
        a = rnd.uniform(0.05, 3.0)
        b = (rnd.uniform(1, 5), rnd.uniform(1, 5), math.cos(a), math.sin(a), rnd.uniform(0.05, 1.0),
             rnd.uniform(0.05, 1.0), rnd.uniform(0, 0.2))
        grow = rnd.uniform(0.05, 0.5)
        ref = set(g.cells(g.box_spans(b, grow)))
        got = set(g.cells(W._rbox_spans(g, b, b[6] + grow)))
        for cdiff in ref ^ got:                           # only cells on the boundary itself may differ
            x, y = g.xy(cdiff)
            assert abs(geom.box_dist(b, x, y) - grow) < 1e-7


def test_build_speed_report():
    """Not a gate: prints the measured build time of a 50k-cell, 2-layer window (design §8 budget: <= 15 ms)."""
    import time
    rnd = random.Random(3)
    pads = [{"ref": "A", "net": "N", "x": 5.0, "y": 5.0, "hw": 0.3, "hh": 0.3}]
    for i in range(150):
        pads.append({"ref": f"P{i}", "net": f"n{i % 9}", "x": round(rnd.uniform(6, 34), 2), "y": round(rnd.uniform(4, 26), 2),
                     "hw": 0.4, "hh": 0.25})
    ctx, c = _ctx(pads)
    box = (6.0, 4.0, 28.0, 26.0)
    t = time.perf_counter()
    n = 5
    for _ in range(n):
        win = W.build(ctx, c, None, box, width=0.2, pitch=0.1, cache=False)
    ms = (time.perf_counter() - t) / n * 1000
    print(f"    window build: {win.grid.G} cells x {len(win.layers)} layers, {win.shapes} shapes: {ms:.1f} ms")
    assert win.grid.G > 45_000


if __name__ == "__main__":
    sys.exit(S.run_tests(dict(globals())))
