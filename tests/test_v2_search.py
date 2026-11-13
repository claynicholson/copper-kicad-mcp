"""Router v2 window search (design §4.7): A* against a brute-force reference, multi-target, bend state, soft
mode, limits, determinism, the probe flood.

Pure tests on synthetic boards (no KiCad).   Run:  python tests/test_v2_search.py [test_name ...]
"""
from __future__ import annotations

import heapq
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v2_w4_support as S  # noqa: E402
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import astar  # noqa: E402
from copper_kicad.pcb.v2 import search as A  # noqa: E402
from copper_kicad.pcb.v2 import window as W  # noqa: E402
from copper_kicad.pcb.v2.types import CHARGE_SEARCH, Budget  # noqa: E402

SQ2 = 2 ** 0.5
BIG = 10 ** 9


def maze(seed=1, n=26, size=(30.0, 22.0), pitch=0.2, layers=(0, 1), soft=False, state_hook=None):
    """A window over a board with random foreign pads on both layers."""
    rnd = random.Random(seed)
    pads = [{"ref": "A", "net": "N", "x": 3.0, "y": 3.0, "hw": 0.3, "hh": 0.3},
            {"ref": "B", "net": "N", "x": size[0] - 3.0, "y": size[1] - 3.0, "hw": 0.3, "hh": 0.3}]
    for i in range(n):
        pads.append({"ref": f"P{i}", "net": f"o{i % 4}", "x": round(rnd.uniform(5, size[0] - 5), 1),
                     "y": round(rnd.uniform(2, size[1] - 2), 1), "hw": rnd.choice((0.3, 0.6, 1.5)),
                     "hh": rnd.choice((0.3, 1.2)), "layer": rnd.choice((0, 0, 1))})
    b = S.board(pads, size=size, nets={"o0": {}, "o1": {}, "o2": {}, "o3": {}, "N": {}})
    c = S.conn(0, "N", (3.0, 3.0), (size[0] - 3.0, size[1] - 3.0), pa="A.1", pb="B.1")
    ctx = S.ctx_for(b, [c])
    if state_hook:
        state_hook(ctx)
    win = W.build(ctx, c, None, (0.0, 0.0, size[0], size[1]), width=0.2, pitch=pitch, layers=layers, soft=soft,
                  cache=False)
    return ctx, c, win


def reference(win, sources, targets, turn, via_cost, lis, cmul=None, hist=None, bend=True):
    """Plain Dijkstra over (state, incoming direction): the exact optimum of the cost model."""
    g = win.grid
    G, Wd = g.G, g.W
    occ, vocc = win.cm.occ, win.cm.vocc
    dist = {}
    heap = []
    for s, g0 in sources.items():
        dist[(s, 8)] = g0
        heap.append((g0, s, 8))
    heapq.heapify(heap)
    while heap:
        d, s, pd = heapq.heappop(heap)
        if d > dist[(s, pd)]:
            continue
        if s in targets:
            return d
        l = s // G
        for k, (dx, dy) in enumerate(astar.DIRS):
            n = s + dy * Wd + dx
            if occ[n] != 0:
                continue
            c = SQ2 if dx and dy else 1.0
            if cmul is not None:
                if cmul[n] < 0:
                    continue
                c *= cmul[n]
            nd = d + c + (turn[pd][k] if bend else 0.0) + (hist[n] if hist is not None else 0.0)
            key = (n, k if bend else 8)
            if nd < dist.get(key, 1e18):
                dist[key] = nd
                heapq.heappush(heap, (nd, n, key[1]))
        if via_cost is not None and vocc[s % G] == 0:
            for l2 in lis:
                n = l2 * G + s % G
                if l2 != l and occ[n] == 0:
                    nd = d + via_cost
                    if nd < dist.get((n, 8), 1e18):
                        dist[(n, 8)] = nd
                        heapq.heappush(heap, (nd, n, 8))
    return None


def cost_of(win, path, sources, turn, via_cost, cmul=None, hist=None):
    g = win.grid
    G, Wd = g.G, g.W
    total = sources[path[0]]
    pd = 8
    for a, b in zip(path, path[1:]):
        if a // G != b // G:
            assert a % G == b % G, "a layer change must stay on its cell"
            total += via_cost
            pd = 8
            continue
        dlt = b - a
        k = [dy * Wd + dx for dx, dy in astar.DIRS].index(dlt)
        c = SQ2 if astar.DIRS[k][0] and astar.DIRS[k][1] else 1.0
        if cmul is not None:
            c *= cmul[b]
        total += c + turn[pd][k] + (hist[b] if hist is not None else 0.0)
        pd = k
    return total


def ends(win, c):
    return {win.cell(c.a.x, c.a.y, 0): 0.0}, {win.cell(c.b.x, c.b.y, 0)}


def check_path(win, path, sources, targets):
    assert path[0] in sources and path[-1] in targets
    assert all(win.cm.occ[s] == 0 for s in path)
    assert len(set(path)) == len(path)


# ── optimality ────────────────────────────────────────────────────────────
def test_optimal_against_brute_force():
    turn0 = astar.turn_table(0.0)
    n = 0
    for seed in range(1, 9):
        ctx, c, win = maze(seed)
        src, tgt = ends(win, c)
        for via_cost in (None, 6.0, 25.0):
            want = reference(win, src, tgt, turn0, via_cost, (0, 1), bend=False)
            for h in (None, A.heuristic(win, tgt, None, 1.0)):
                path, info = A.search(win, win.net_id, src, tgt, h=h, turn=turn0, via_cost=via_cost, layers_ok=(0, 1),
                                      limit=BIG, budget=Budget(BIG))
                if want is None:
                    assert path is None and info["reason"] == "exhausted"
                    continue
                n += 1
                assert info["reason"] == "ok", (seed, via_cost)
                check_path(win, path, src, tgt)
                got = cost_of(win, path, src, turn0, via_cost)
                assert abs(got - want) < 1e-6, (seed, via_cost, h is None, got, want)
    assert n >= 30
    # the heuristic prunes: fewer expansions than Dijkstra, same cost
    ctx, c, win = maze(3)
    src, tgt = ends(win, c)
    _p, i0 = A.search(win, win.net_id, src, tgt, h=None, turn=turn0, via_cost=6.0, layers_ok=(0, 1), limit=BIG,
                      budget=Budget(BIG))
    _p, i1 = A.search(win, win.net_id, src, tgt, h=A.heuristic(win, tgt), turn=turn0, via_cost=6.0, layers_ok=(0, 1),
                      limit=BIG, budget=Budget(BIG))
    assert i1["expanded"] < i0["expanded"] / 2, (i1["expanded"], i0["expanded"])


def test_bend_state_is_exact_and_plain_search_is_legal():
    turn = astar.turn_table(1.5)
    worse = 0
    for seed in range(1, 7):
        ctx, c, win = maze(seed, pitch=0.25)
        src, tgt = ends(win, c)
        want = reference(win, src, tgt, turn, 8.0, (0, 1), bend=True)
        if want is None:
            continue
        for h in (None, A.heuristic(win, tgt)):
            pb, ib = A.search(win, win.net_id, src, tgt, h=h, turn=turn, via_cost=8.0, layers_ok=(0, 1),
                              bend_state=True, limit=BIG, budget=Budget(BIG))
            check_path(win, pb, src, tgt)
            assert abs(cost_of(win, pb, src, turn, 8.0) - want) < 1e-6, seed       # exact with the direction in the state
        pp, ip = A.search(win, win.net_id, src, tgt, h=A.heuristic(win, tgt), turn=turn, via_cost=8.0,
                          layers_ok=(0, 1), bend_state=False, limit=BIG, budget=Budget(BIG))
        check_path(win, pp, src, tgt)
        got = cost_of(win, pp, src, turn, 8.0)
        assert got >= want - 1e-6 and got <= want * 1.25, (seed, got, want)        # legal, near-optimal
        worse += got > want + 1e-6
    print(f"    plain search was sub-optimal (within 25 %) on {worse} of 6 mazes with bend cost 1.5")


def test_cmul_hist_and_moves_are_in_the_cost():
    turn0 = astar.turn_table(0.0)
    ctx, c, win = maze(2)
    src, tgt = ends(win, c)
    from array import array
    rnd = random.Random(4)
    N = win.grid.N
    cmul = array("f", [1.0]) * N
    hist = array("f", [0.0]) * N
    for i in range(N):
        r = rnd.random()
        cmul[i] = 1.0 if r < 0.6 else (2.5 if r < 0.9 else -1.0)
        hist[i] = 0.0 if rnd.random() < 0.8 else 0.75
    for s in list(src) + list(tgt):
        cmul[s] = 1.0
    want = reference(win, src, tgt, turn0, 6.0, (0, 1), cmul=cmul, hist=hist, bend=False)
    path, info = A.search(win, win.net_id, src, tgt, h=None, turn=turn0, via_cost=6.0, layers_ok=(0, 1), cmul=cmul,
                          hist=hist, limit=BIG, budget=Budget(BIG))
    assert want is not None and path is not None
    assert all(cmul[s] >= 0 for s in path)
    assert abs(cost_of(win, path, src, turn0, 6.0, cmul, hist) - want) < 1e-4
    # direction-biased layers: a horizontal layer makes vertical steps dearer, so a tall detour changes layer
    g = win.grid
    mv = (astar.layer_moves(g.W, g.G, 0, 1.0, 3.0, 2.0), astar.layer_moves(g.W, g.G, 1, 3.0, 1.0, 2.0))
    path2, _i = A.search(win, win.net_id, src, tgt, h=None, turn=turn0, via_cost=2.0, layers_ok=(0, 1), limit=BIG,
                         budget=Budget(BIG), moves=mv)
    assert path2 is not None and len({s // g.G for s in path2}) == 2
    # layers_ok restricts the search to one layer
    path3, _i = A.search(win, win.net_id, src, tgt, h=None, turn=turn0, via_cost=2.0, layers_ok=(0,), limit=BIG,
                         budget=Budget(BIG))
    assert path3 is None or {s // g.G for s in path3} == {0}


def test_multi_source_multi_target():
    turn0 = astar.turn_table(0.0)
    ctx, c, win = maze(5, n=6)
    far = win.cell(27.0, 19.0, 0)
    near = win.cell(8.0, 3.0, 0)
    back = win.cell(27.0, 3.0, 1)
    src = {win.cell(3.0, 3.0, 0): 0.0, win.cell(3.0, 10.0, 0): 40.0}
    for tgt in ({far}, {far, near}, {far, back}, {far: "x", near: "y"}):
        want = reference(win, src, tgt, turn0, 5.0, (0, 1), bend=False)
        path, info = A.search(win, win.net_id, src, tgt, h=A.heuristic(win, tgt), turn=turn0, via_cost=5.0,
                              layers_ok=(0, 1), limit=BIG, budget=Budget(BIG))
        check_path(win, path, src, tgt)
        assert abs(cost_of(win, path, src, turn0, 5.0) - want) < 1e-6
    path, _i = A.search(win, win.net_id, src, {far, near}, h=A.heuristic(win, {far, near}), turn=turn0, via_cost=5.0,
                        layers_ok=(0, 1), limit=BIG, budget=Budget(BIG))
    assert path[-1] == near and path[0] == win.cell(3.0, 3.0, 0)
    # a dear first source loses to a cheap second one
    src2 = {win.cell(3.0, 3.0, 0): 500.0, win.cell(3.0, 10.0, 0): 0.0}
    path, _i = A.search(win, win.net_id, src2, {far}, h=A.heuristic(win, {far}), turn=turn0, via_cost=5.0,
                        layers_ok=(0, 1), limit=BIG, budget=Budget(BIG))
    assert path[0] == win.cell(3.0, 10.0, 0)
    # a source that is a target: zero-length path
    s0 = win.cell(3.0, 3.0, 0)
    path, info = A.search(win, win.net_id, {s0: 0.0}, {s0}, h=None, turn=turn0, via_cost=5.0, layers_ok=(0, 1),
                          limit=BIG, budget=Budget(BIG))
    assert path == [s0] and info["expanded"] == 0


def test_heuristic_is_lazy_sampled_and_uses_tile_distance():
    ctx, c, win = maze(1, n=0)
    g = win.grid
    tgt = {win.cell(27.0, 19.0, 0)}
    h = A.heuristic(win, tgt, None, 1.15)
    assert len(h) == g.G and all(v < 0 for v in h[:50]) and h.samples == ((tgt.copy().pop() % g.W, tgt.copy().pop() // g.W),)
    i = win.cell(7.0, 19.0, 0)
    v = h.fill(i)
    assert abs(v - 1.15 * (20.0 / win.pitch)) < 1e-6 and h[i] == v
    i = win.cell(25.0, 17.0, 1) % g.G
    assert abs(h.fill(i) - 1.15 * 10 * SQ2) < 1e-6
    # many targets: at most 8 samples, the nearest one counts
    line = {win.cell(5.0 + k * 0.2, 19.0, 0) for k in range(100)}
    h2 = A.heuristic(win, line)
    assert len(h2.samples) == 8
    assert h2.fill(win.cell(5.0, 3.0, 0)) == 16.0 / win.pitch
    # tile distances raise the estimate where the corridor bends (needs a window built with a tiles mask)
    class T:
        t, nx, ny, nl = 2.0, 15, 11, 2

        def tile_of(self, x, y):
            return (int(x // 2.0), int(y // 2.0))
    ctx.tiles = T()
    mask = frozenset((tx, ty) for tx in range(15) for ty in range(11))
    wm = W.build(ctx, c, mask, (0.0, 0.0, 30.0, 22.0), width=0.2, pitch=0.2, cache=False)
    t2 = {wm.cell(27.0, 19.0, 0)}
    td = {(1, 1): 60.0, (13, 9): 0.0}
    h3 = A.heuristic(wm, t2, td, 1.0)
    octile = A.heuristic(wm, t2, None, 1.0).fill(wm.cell(3.0, 3.0, 0))
    assert abs(h3.fill(wm.cell(3.0, 3.0, 0)) - (60.0 - 2.0 * SQ2) / 0.2) < 1e-6 and h3.fill(wm.cell(3.0, 3.0, 0)) > octile
    assert h3.fill(wm.cell(27.0, 19.0, 0)) == 0.0
    assert h3.fill(wm.cell(9.0, 9.0, 0)) == A.heuristic(wm, t2).fill(wm.cell(9.0, 9.0, 0))     # tile not in the table


# ── soft mode ─────────────────────────────────────────────────────────────
def _walled():
    def wall(ctx):
        st = ctx.state
        ctx.w1 = st.add(kind="seg", net="o1", conn=7, layer=0, x1=15.0, y1=0.6, x2=15.0, y2=21.4, w=0.2)
        ctx.w2 = st.add(kind="seg", net="o2", conn=8, layer=1, x1=15.0, y1=0.6, x2=15.0, y2=21.4, w=0.2)
        ctx.w3 = st.add(kind="seg", net="o3", conn=9, layer=0, x1=18.0, y1=0.6, x2=18.0, y2=21.4, w=0.2)
        ctx.w4 = st.add(kind="seg", net="o3", conn=9, layer=1, x1=18.0, y1=0.6, x2=18.0, y2=21.4, w=0.2)
    return maze(1, n=0, soft=True, state_hook=wall)


def test_soft_mode_reports_crossed_pieces():
    turn0 = astar.turn_table(0.0)
    ctx, c, win = _walled()
    src, tgt = ends(win, c)
    kw = dict(h=A.heuristic(win, tgt), turn=turn0, via_cost=5.0, layers_ok=(0, 1), limit=BIG)
    path, info = A.search(win, win.net_id, src, tgt, budget=Budget(BIG), **kw)
    assert path is None and info["reason"] == "exhausted" and not info["open"] and info["crossed"] == {}
    assert info["best"] >= 0 and info["frontier"] and info["frontier"][0] == info["best"]
    # uniform price, no vias: straight through both walls on the front layer
    path, info = A.search(win, win.net_id, src, tgt, budget=Budget(BIG), soft_cost=3.0, **dict(kw, via_cost=None))
    assert path is not None and info["reason"] == "ok"
    assert set(info["crossed"]) == {ctx.w1.id, ctx.w3.id} and all(n >= 3 for n in info["crossed"].values())
    assert A.crossed(win, win.net_id, path) == info["crossed"]
    # per-piece prices: w1 and w4 are dear, so the path crosses w2 on the back and w3 on the front
    price = {ctx.w1.id: 500.0, ctx.w2.id: 1.0, ctx.w3.id: 1.0, ctx.w4.id: 500.0}
    path, info = A.search(win, win.net_id, src, tgt, budget=Budget(BIG), soft_cost=price, **kw)
    assert set(info["crossed"]) == {ctx.w2.id, ctx.w3.id}
    # a piece without a price stays hard; with a default (key None) it is priced
    path, info = A.search(win, win.net_id, src, tgt, budget=Budget(BIG), soft_cost={ctx.w1.id: 1.0, ctx.w2.id: 1.0}, **kw)
    assert path is None and info["reason"] == "exhausted"
    path, info = A.search(win, win.net_id, src, tgt, budget=Budget(BIG),
                          soft_cost={ctx.w1.id: 1.0, ctx.w2.id: 1.0, None: 2.0}, **kw)
    assert path is not None and (ctx.w3.id in info["crossed"] or ctx.w4.id in info["crossed"])
    # bend-state search agrees on what is crossed
    path, info = A.search(win, win.net_id, src, tgt, budget=Budget(BIG), soft_cost=price, bend_state=True, **kw)
    assert set(info["crossed"]) == {ctx.w2.id, ctx.w3.id}
    # static obstacles stay hard in soft mode
    ctx2, c2, win2 = maze(1, n=0, soft=True, size=(30.0, 22.0))
    src2, tgt2 = ends(win2, c2)
    win2.cm.S[win2.cell(c2.b.x, c2.b.y, 0)] = -1
    win2.cm.occ[win2.cell(c2.b.x, c2.b.y, 0)] = -1
    for d in (1, -1, win2.grid.W, -win2.grid.W, win2.grid.W + 1, win2.grid.W - 1, -win2.grid.W + 1, -win2.grid.W - 1):
        for l in (0, 1):
            s = win2.cell(c2.b.x, c2.b.y, l) + d
            win2.cm.S[s] = win2.cm.occ[s] = -1
    win2.cm.SV[win2.cell(c2.b.x, c2.b.y, 0) % win2.grid.G] = win2.cm.vocc[win2.cell(c2.b.x, c2.b.y, 0) % win2.grid.G] = -1
    path, info = A.search(win2, win2.net_id, src2, tgt2, h=None, turn=turn0, via_cost=5.0, layers_ok=(0, 1), limit=BIG,
                          budget=Budget(BIG), soft_cost=1.0)
    assert path is None


# ── limits, budget, determinism ───────────────────────────────────────────
def test_limit_and_budget_reasons():
    turn0 = astar.turn_table(0.0)
    ctx, c, win = maze(4)
    src, tgt = ends(win, c)
    kw = dict(h=A.heuristic(win, tgt), turn=turn0, via_cost=5.0, layers_ok=(0, 1))
    b = Budget(BIG)
    path, info = A.search(win, win.net_id, src, tgt, limit=BIG, budget=b, **kw)
    full = info["expanded"]
    assert path is not None and full > 200 and b.used == CHARGE_SEARCH + full
    # limit
    b = Budget(BIG)
    path, info = A.search(win, win.net_id, src, tgt, limit=100, budget=b, **kw)
    assert path is None and info["reason"] == "limit" and info["expanded"] == 100 and b.used == CHARGE_SEARCH + 100
    assert info["frontier"] and info["best"] >= 0
    # budget smaller than the limit
    b = Budget(CHARGE_SEARCH + 150)
    path, info = A.search(win, win.net_id, src, tgt, limit=BIG, budget=b, **kw)
    assert path is None and info["reason"] == "budget" and info["expanded"] == 150 and b.used == b.total
    # no budget for even starting a search
    b = Budget(CHARGE_SEARCH)
    path, info = A.search(win, win.net_id, src, tgt, limit=BIG, budget=b, **kw)
    assert path is None and info["reason"] == "budget" and info["expanded"] == 0 and b.used == 0
    # exactly enough
    b = Budget(CHARGE_SEARCH + full)
    path, info = A.search(win, win.net_id, src, tgt, limit=BIG, budget=b, **kw)
    assert path is not None and info["reason"] == "ok" and b.spent_out()
    # charges reach the parent budget
    parent = Budget(BIG)
    child = parent.sub(0.5)
    A.search(win, win.net_id, src, tgt, limit=BIG, budget=child, **kw)
    assert parent.used == child.used == CHARGE_SEARCH + full
    # exhausted inside a corridor mask says so (open)
    m = W.MASKED
    for s in range(win.grid.G):
        x, y, _l = win.xy(s)
        if y > 12.0:
            win.cm.occ[s] = win.cm.S[s] = m
            win.cm.occ[s + win.grid.G] = win.cm.S[s + win.grid.G] = m
    path, info = A.search(win, win.net_id, src, tgt, limit=BIG, budget=Budget(BIG), **kw)
    assert path is None and info["reason"] == "exhausted" and info["open"]


def test_same_twice_and_heap_determinism():
    turn = astar.turn_table(0.4)
    runs = []
    for _ in range(2):
        ctx, c, win = maze(6, n=30)
        src, tgt = ends(win, c)
        more = {win.cell(3.0, 3.0 + k, 0): 0.0 for k in range(4)}
        more.update(src)
        out = []
        for srcs in (more, dict(reversed(list(more.items())))):          # insertion order must not matter
            for bend in (False, True):
                p, i = A.search(win, win.net_id, srcs, tgt, h=A.heuristic(win, tgt, None, 1.15), turn=turn,
                                via_cost=5.0, layers_ok=(0, 1), bend_state=bend, limit=BIG, budget=Budget(BIG))
                out.append((tuple(p), i["expanded"], tuple(i["frontier"])))
        assert out[0] == out[2] and out[1] == out[3]
        runs.append(out)
    assert runs[0] == runs[1]


# ── probe ─────────────────────────────────────────────────────────────────
def test_probe_enclosed_open_and_blockers():
    def ring(ctx):
        st = ctx.state
        # terminal B fenced in by one routed track (3 sides) and a static pad row (4th side is the board edge side)
        ctx.r = [st.add(kind="seg", net="o1", conn=5, layer=0, x1=25.0, y1=17.0, x2=29.2, y2=17.0, w=0.2),
                 st.add(kind="seg", net="o1", conn=5, layer=0, x1=25.0, y1=17.0, x2=25.0, y2=21.2, w=0.2)]
    ctx, c, win = maze(1, n=0, layers=(0,), soft=True, state_hook=ring)
    src, tgt = ends(win, c)
    b = Budget(BIG)
    pr = A.probe(win, win.net_id, {t: 0.0 for t in tgt}, 2000, b)
    assert pr["enclosed"] and not pr["open"] and 100 < pr["reached"] < 2000 and b.used == pr["reached"]
    kinds = dict(pr["frontier_blockers"])
    assert set(kinds) == {"static", ctx.r[0].id, ctx.r[1].id} and kinds[ctx.r[0].id] > 10
    assert 0.0 < pr["static_share"] < 1.0                        # board edge (static) + the routed track
    assert pr["frontier"] and all(win.cm.occ[a] == 0 and win.cm.occ[n] != 0 for a, n in pr["frontier"])
    # the other terminal is in the open: the flood stops at the limit
    pr = A.probe(win, win.net_id, src, 2000, Budget(BIG))
    assert not pr["enclosed"] and pr["reached"] == 2000
    # a small window: the flood reaches the window border (MASKED) -> open, not enclosed
    small = W.build(ctx, c, None, (1.0, 1.0, 5.0, 5.0), width=0.2, pitch=0.2, layers=(0,), cache=False)
    pr = A.probe(small, small.net_id, {small.cell(3.0, 3.0, 0): 0.0}, 2000, Budget(BIG))
    assert pr["open"] and not pr["enclosed"] and pr["reached"] < 2000
    # two layers: the fence is on the front only, a via gets out
    ctx2, c2, win2 = maze(1, n=0, layers=(0, 1), state_hook=ring)
    pr = A.probe(win2, win2.net_id, {win2.cell(c2.b.x, c2.b.y, 0): 0.0}, 2000, Budget(BIG))
    assert not pr["enclosed"]
    pr = A.probe(win2, win2.net_id, {win2.cell(c2.b.x, c2.b.y, 0): 0.0}, 2000, Budget(BIG), vias=False)
    assert pr["enclosed"]
    # no sources: nothing to flood, reported as enclosed with zero reach; a spent budget floods nothing
    pr = A.probe(win, win.net_id, {}, 2000, Budget(BIG))
    assert pr["enclosed"] and pr["reached"] == 0
    pr = A.probe(win, win.net_id, src, 2000, Budget(0))
    assert pr["reached"] == 0 and not pr["enclosed"]


def test_throughput_report():
    """Not a gate: prints expansions per second of both searches on a 110 x 150 cell, 2-layer window."""
    turn = astar.turn_table(0.3 / 0.2)
    ctx, c, win = maze(7, n=40)
    src, tgt = ends(win, c)
    for bend in (False, True):
        n = 0
        t = time.perf_counter()
        for _ in range(4):
            _p, i = A.search(win, win.net_id, src, tgt, h=None, turn=turn, via_cost=25.0, layers_ok=(0, 1),
                             bend_state=bend, limit=BIG, budget=Budget(BIG))
            n += i["expanded"]
        dt = time.perf_counter() - t
        print(f"    search bend_state={bend}: {n} expansions in {dt:.2f} s = {n / dt / 1000:.0f} k/s")
    t = time.perf_counter()
    pr = A.probe(win, win.net_id, src, BIG, Budget(BIG))
    dt = time.perf_counter() - t
    print(f"    probe flood: {pr['reached']} cells in {dt:.3f} s = {pr['reached'] / dt / 1000:.0f} k/s")


if __name__ == "__main__":
    sys.exit(S.run_tests(dict(globals())))
