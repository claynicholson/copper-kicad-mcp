"""Router v2 global stage (design §4.6): negotiated PathFinder routing on the tile graph, layer-assignment DP,
corridors and cuts.

Pure tests on synthetic boards (no KiCad) plus a smoke run on corpus boards (needs the benchmark corpus; the
worker extracts are cached under bench/route/.work/w3, KiCad is only needed to build that cache).

Run:  python tests/test_v2_globalroute.py              pure tests + 3 corpus boards
      python tests/test_v2_globalroute.py --full       ... + every corpus board
      python tests/test_v2_globalroute.py test_name    one test
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v2_tiles import Lcg, board, common, keepout, near  # noqa: E402  (also sandboxes the environment)

import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import plan as P  # noqa: E402
from copper_kicad.pcb.v2 import globalroute as GR  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2 import tiles as TL  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Corridor, Ctx, Piece, StageReport, Terminal  # noqa: E402

FULL = "--full" in sys.argv or os.environ.get("COPPER_V2_GLOBAL_FULL") == "1"
BIG = 5_000_000


# ── helpers ───────────────────────────────────────────────────────────────
class Cfg:
    """The slice of guided.plan.Config the global stage reads."""

    def __init__(self):
        self.nets, self.regions = {}, []
        self.via_cost, self.direction_bias = 5.0, 0.5

    def set(self, name, **kw):
        nc = SimpleNamespace(layers_ok=None, layer_cost={}, layer_dir={}, via_cost=self.via_cost, max_vias=None,
                             avoid=[], corridor=None)
        nc.__dict__.update(kw)
        self.nets[name] = nc
        return self

    def net(self, name):
        return self.nets[name]

    def region(self, rect, cost=1.0, forbid=False, layers=None, nets=None):
        r = P.Region()
        r.idx, r.rect, r.cost, r.forbid, r.layers, r.nets, r.exc, r.name = len(self.regions), tuple(rect), cost, forbid, \
            layers, nets, set(), "r"
        self.regions.append(r)
        return self


def conn(idx, net, a, b, la=(0,), lb=(0,), w=0.2, cl=0.2, pa=None, pb=None, ca=None, cb=None, rank=None, tree=True):
    return ConnV2(idx=idx, net=net, a=Terminal(a[0], a[1], tuple(la), pa, 2 * idx + 1 if ca is None else ca),
                  b=Terminal(b[0], b[1], tuple(lb), pb, 2 * idx + 2 if cb is None else cb),
                  est=abs(a[0] - b[0]) + abs(a[1] - b[1]), width=w, neck=w, clearance=cl, via=(0.6, 0.3),
                  rank=rank if rank is not None else (0, idx), tree_target=tree)


def ctx_for(b, conns, cfg=None, x=None, tile=1.0, w=0.2, cl=0.2):
    o = O.Oracle(b)
    c = Ctx(board=b, cfg=cfg or Cfg(), x=x, oracle=o, conns=list(conns))
    c.tiles = TL.Tiles(b, o, tile, w, cl)
    return c


def route(b, conns, cfg=None, x=None, budget=BIG, **kw):
    c = ctx_for(b, conns, cfg, x, **kw)
    rep = GR.run(c, Budget(budget))
    return c, rep


def path(c, idx):
    return c.history["global"]["path"][idx]


def crosses(p, t1, t2):
    """Does the 2-D tile path step between tiles t1 and t2?"""
    return any({p[i], p[i + 1]} == {t1, t2} for i in range(len(p) - 1))


def wall(gaps, x0=19.6, x1=20.4, layers=(0, 1), height=30.0):
    """No-track keep-outs along the line x = 20 with free gaps [(y0, y1), ...]."""
    zs = []
    y = 0.0
    for g0, g1 in sorted(gaps):
        if g0 > y:
            zs.append(keepout(x0, y, x1, g0, tracks=True, vias=False, layers=layers))
        y = g1
    if y < height:
        zs.append(keepout(x0, y, x1, height, tracks=True, vias=False, layers=layers))
    return zs


ONE = Cfg().set("N0", layers_ok=(0,)).set("N1", layers_ok=(0,))        # two nets held on F.Cu


# ── negotiation ───────────────────────────────────────────────────────────
def test_two_nets_one_track_gap_one_detours():
    """A wall with a gap for exactly one track at y 14.25..14.75 and a wide gap at y >= 26: both connections
    want the near gap in the congestion-blind iteration; negotiation sends one of them round."""
    b = board(zones=wall([(14.25, 14.75), (26.0, 30.0)]))
    conns = [conn(0, "N0", (15.5, 14.4), (25.5, 14.4)), conn(1, "N1", (15.5, 14.6), (25.5, 14.6))]
    c, rep = route(b, conns, ONE)
    t = c.tiles
    assert t.tracks(0, 19, 14, "E") == 1 and t.tracks(0, 19, 13, "E") == 0 and t.tracks(0, 19, 27, "E") >= 2
    assert isinstance(rep, StageReport) and rep.name == "global" and (rep.routed, rep.failed) == (2, 0)
    d = rep.data
    assert d["overflow_first_mm"] > 0 and d["overflow_mm"] == 0.0 and d["iterations"] >= 2 and d["cuts"] == []
    p0, p1 = path(c, 0), path(c, 1)
    assert p0[0] == (15, 14) and p0[-1] == (25, 14) and p1[0] == (15, 14) and p1[-1] == (25, 14)
    through = [crosses(p, (19, 14), (20, 14)) for p in (p0, p1)]
    assert sorted(through) == [False, True], through
    far = p1 if through[0] else p0
    assert any(crosses(far, (19, ty), (20, ty)) for ty in range(26, 30)), far
    assert all(c.corridors[i].overflow == 0.0 and c.corridors[i].layers == (0,) and c.corridors[i].layer_seq == ()
               for i in (0, 1))
    assert rep.work == d["expansions"] > 0 and rep.notes == []
    # with the second layer allowed the loser changes layer instead of walking 12 mm down and back
    b2 = board(zones=wall([(14.25, 14.75), (26.0, 30.0)], layers=(0,)))
    c2, rep2 = route(b2, conns, Cfg())
    assert rep2.data["overflow_mm"] == 0.0
    vias = sorted(len(c2.corridors[i].layer_seq) for i in (0, 1))
    assert vias == [0, 2], vias
    loser = 0 if c2.corridors[0].layer_seq else 1
    assert c2.corridors[loser].layers == (0, 1) and [(f, to) for _i, f, to in c2.corridors[loser].layer_seq] == [(0, 1), (1, 0)]


def test_overflow_is_reported_as_a_named_cut_when_impossible():
    b = board(zones=wall([(14.25, 14.75)]))                      # the one-track gap is the only way through
    conns = [conn(0, "N0", (15.5, 14.4), (25.5, 14.4)), conn(1, "N1", (15.5, 14.6), (25.5, 14.6))]
    x = SimpleNamespace(negotiation_iters=6, auto={})
    c, rep = route(b, conns, ONE, x)
    d = rep.data
    assert rep.routed == 2 and d["iterations"] == 6
    # two 0.2 mm tracks need 0.6 mm of copper; the gap has 0.5: short by 0.1 mm
    assert near(d["overflow_mm"], 0.1) and d["overflow_mm"] <= d["overflow_first_mm"]
    assert len(d["cuts"]) == 1
    cut = d["cuts"][0]
    assert cut["a"] == "keepout" and cut["b"] == "keepout" and cut["layer"] == "F.Cu"
    assert near(cut["capacity_mm"], 0.7) and near(cut["demand_mm"], 0.8) and cut["nets"] == ["N0", "N1"]
    assert c.history["global"]["cuts"][0].nets == ["N0", "N1"] and near(c.history["global"]["overflow_mm"], 0.1)
    assert near(c.corridors[0].overflow, 0.1) and near(c.corridors[1].overflow, 0.1)
    assert any("residual overflow" in n for n in rep.notes)
    # both still got a corridor through the gap (overflow is a price, never a refusal)
    assert all(crosses(path(c, i), (19, 14), (20, 14)) for i in (0, 1))
    assert c.tiles.utilisation(0)[14][19] > 100 and c.tiles.utilisation(1)[14][19] == 0
    # a connection that is walled in completely is reported and gets no corridor
    b = board(zones=[keepout(19.6, 0.0, 20.4, 30.0, tracks=True, vias=False)], outline=[[0, 0], [40, 0], [40, 30], [0, 30]])
    cfg = Cfg().region((19.0, 0.0, 21.0, 30.0), forbid=True)
    c, rep = route(b, conns[:1], cfg)
    assert rep.routed == 0 and rep.failed == 1 and 0 not in c.corridors and any("no tile path" in n for n in rep.notes)


def test_the_best_iteration_is_kept():
    b = board(zones=wall([(14.25, 14.75), (26.0, 30.0)]))
    conns = [conn(0, "N0", (15.5, 14.4), (25.5, 14.4)), conn(1, "N1", (15.5, 14.6), (25.5, 14.6))]

    class Worse(GR.GlobalState):
        """Every iteration after the first claims to be worse: the first one must come back."""
        calls = 0
        first = None

        def _score(self):
            (tot, wl), edges, cols = super()._score()
            self.calls += 1
            if self.calls == 1:
                self.first = {i: list(p) for i, p in self.paths.items()}
                return (tot, wl), edges, cols
            return (tot + 100.0 + self.calls, wl), edges, cols

    c = ctx_for(b, conns, ONE)
    st = Worse(c.tiles, conns, c.cfg, None)
    info = st.negotiate([0, 1], Budget(BIG), 5)
    assert st.calls == 5 and info["iterations"] == 5
    assert st.paths == st.first                                    # iteration 0 restored
    useE, useN = list(st.useE), list(st.useN)
    st.recount()
    assert st.useE == useE and st.useN == useN                     # and the usage is the usage of those paths
    assert info["overflow_mm"] == info["overflow_first_mm"] > 0
    # the real thing: more iterations never return a worse (overflow, wirelength)
    b = board(zones=wall([(14.25, 14.75)]))
    scores = []
    for iters in (1, 2, 4, 8):
        st = GR.GlobalState(ctx_for(b, conns, ONE).tiles, conns, ONE, None)
        info = st.negotiate([0, 1], Budget(BIG), iters)
        scores.append((info["overflow_mm"], info["wirelength_mm"]))
        tot, _e, _c = st.overflow()
        assert near(tot, info["overflow_mm"])
    assert all(scores[i + 1] <= scores[i] for i in range(len(scores) - 1)), scores


def test_same_twice():
    rnd = Lcg(99)
    pads = []
    for i in range(12):
        pads.append({"ref": f"J{i}", "net": f"P{i}", "x": 4 + 2.54 * i, "y": 12.0, "hw": 0.8, "hh": 0.8, "r": 0.8,
                     "kind": "tht", "hole": 0.4})
    b = board(pads, zones=wall([(5.0, 6.0), (20.0, 21.5)], layers=(0,)))
    conns = []
    for i in range(36):
        a = (rnd.u(1.5, 18.0), rnd.u(1.5, 28.5))
        e = (rnd.u(22.0, 38.5), rnd.u(1.5, 28.5))
        conns.append(conn(i, f"S{i % 30}", a, e, la=(i % 2,), lb=((i // 2) % 2,), w=0.2 if i % 5 else 0.5,
                          ca=1 + i, cb=100 + i, rank=(i % 3, i)))

    def snap():
        c, rep = route(b, conns)
        cors = {i: (sorted(k.tiles), k.layers, k.layer_seq, sorted(k.lateral.items()), k.overflow, k.path)
                for i, k in c.corridors.items()}
        return cors, rep.data, rep.work, rep.notes, c.history["global"]["tee"]

    one, two = snap(), snap()
    assert one == two
    assert len(one[0]) == 36 and one[1]["overflow_mm"] == 0.0


def test_budget_runs_out_cleanly():
    rnd = Lcg(5)
    b = board()
    conns = [conn(i, f"S{i}", (rnd.u(1.5, 38.5), rnd.u(1.5, 28.5)), (rnd.u(1.5, 38.5), rnd.u(1.5, 28.5)))
             for i in range(20)]
    c, rep = route(b, conns, budget=60)
    assert 0 < rep.routed < 20 and rep.routed + rep.failed == 20 and rep.data["out_of_budget"]
    assert sorted(c.corridors) == sorted(i for i in range(20) if i in c.history["global"]["path"])
    assert len(c.corridors) == rep.routed and any("ran out of budget" in n for n in rep.notes)
    assert rep.work >= 60 and rep.work < 60 + c.tiles.N                 # overshoot: at most the search in flight
    # the next call with a real budget finishes the job on the same context
    rep = GR.run(c, Budget(BIG))
    assert (rep.routed, rep.failed) == (20, 0) and len(c.corridors) == 20 and not rep.data["out_of_budget"]


# ── terminals, trees, pads ────────────────────────────────────────────────
def test_tree_target_ends_on_the_routed_tree():
    b = board()
    # one net, three clusters: 1 --- 2 along y = 5.5, then 3 joins from below
    c0 = conn(0, "N", (5.5, 5.5), (25.5, 5.5), ca=1, cb=2)
    c1 = conn(1, "N", (15.5, 12.5), (25.5, 5.5), ca=3, cb=2)
    c, rep = route(b, [c0, c1])
    assert path(c, 0) == [(x, 5) for x in range(5, 26)]
    p1 = path(c, 1)
    assert p1[0] == (15, 12) and p1[-1] == (15, 5) and len(p1) == 8          # straight up onto the tree
    assert c.history["global"]["tee"] == {1: 0}
    assert c.corridors[1].tiles == frozenset((x, y) for x in (14, 15, 16) for y in range(4, 14))
    # without tree_target it goes all the way to its own terminal b
    c, rep = route(b, [c0, conn(1, "N", (15.5, 12.5), (25.5, 5.5), ca=3, cb=2, tree=False)])
    assert path(c, 1)[-1] == (25, 5) and c.history["global"]["tee"] == {}
    # a connection never ends on a path that is on its own side of the tree: 3 -> 1 must not end on 3's other
    # connection (3 -> 2), although that path starts in its own tile
    c2 = conn(2, "N", (15.5, 12.5), (5.5, 5.5), ca=3, cb=1, rank=(1, 2))
    c, rep = route(b, [conn(1, "N", (15.5, 12.5), (25.5, 5.5), ca=3, cb=2, rank=(0, 1)), c2])
    assert path(c, 2)[-1] == (5, 5) and c.history["global"]["tee"] == {}
    # only onto a connection that precedes it in rank order: the detail stage routes in that order, so the copper
    # it is meant to join exists by then. Here the host comes later, so connection 1 goes to its own terminal.
    late = conn(0, "N", (5.5, 5.5), (25.5, 5.5), ca=1, cb=2, rank=(5, 0))
    # (the late one may in turn end on connection 1, which precedes it.)
    c, rep = route(b, [late, c1])
    assert path(c, 1)[-1] == (25, 5) and 1 not in c.history["global"]["tee"]
    assert c.history["global"]["tee"] in ({}, {0: 1}) and GR.TEE_ONLY_EARLIER is True
    # with the rule off, a re-route of connection 1 (capacity feedback) may end on the later one's path
    GR.TEE_ONLY_EARLIER = False
    try:
        c, rep = route(b, [late, conn(1, "N", (15.5, 12.5), (25.5, 5.5), ca=3, cb=2, rank=(0, 1))])
        st = c.history["global_state"]
        st.rip(1)
        st.rip(0)
        st.route(late, 0.0)
        st.route(st.conns[1], 0.0)
        assert st.end_on[1] == 0 and st.paths[1][-1] % st.G == 5 * 40 + 15
    finally:
        GR.TEE_ONLY_EARLIER = True
    # the same cluster on both ends (no tree information): plain point to point
    c, rep = route(b, [c0, conn(1, "N", (15.5, 12.5), (25.5, 5.5), ca=7, cb=7)])
    assert path(c, 1)[-1] == (25, 5)


def test_own_pad_shadow_is_free():
    """A through-hole pad's inflated copper covers all four borders of its tile (capacity 0 for everyone else);
    the connection that leaves that pad crosses them without demand."""
    pads = [{"ref": "J1", "net": "N0", "x": 10.5, "y": 10.5, "hw": 0.8, "hh": 0.8, "r": 0.8, "kind": "tht", "hole": 0.4}]
    b = board(pads, nets={"N0": {}, "N1": {}})
    c, rep = route(b, [conn(0, "N0", (10.5, 10.5), (20.5, 10.5), la=(0, 1), pa="J1.1")])
    assert c.tiles.cap(0, 10, 10, "E") == 0.0 and c.tiles.cap(1, 10, 10, "N") == 0.0
    assert path(c, 0) == [(x, 10) for x in range(10, 21)] and rep.data["overflow_mm"] == 0.0
    assert c.corridors[0].overflow == 0.0 and rep.data["cuts"] == []
    # a foreign connection through that tile is a wall crossing: it walks round the pad instead
    c, rep = route(b, [conn(1, "N1", (5.5, 10.5), (15.5, 10.5))], Cfg().set("N1", layers_ok=(0,)))
    assert (10, 10) not in path(c, 1) and rep.data["overflow_mm"] == 0.0 and len(path(c, 1)) == 13
    # terminals on different layers of one tile: a single-tile path with one via
    c, rep = route(board(), [conn(0, "N0", (10.2, 10.2), (10.8, 10.8), la=(0,), lb=(1,))])
    assert path(c, 0) == [(10, 10)] and c.corridors[0].layer_seq == ((0, 0, 1),) and c.corridors[0].layers == (0, 1)
    assert c.corridors[0].tiles == frozenset((x, y) for x in (9, 10, 11) for y in (9, 10, 11))


def test_wide_track_alone_does_not_overflow():
    """A 0.6 mm power track through empty 1 mm tiles of a 0.2 / 0.2 reference class: demand 0.8 mm against
    3 reference tracks (1.2 mm) fits; against a 0.4 / 0.3 class (1 track = 0.7 mm + 0.6 slack) it fits too."""
    b = board()
    for w, cl in ((0.2, 0.2), (0.4, 0.3)):
        c, rep = route(b, [conn(0, "PWR", (5.5, 5.5), (30.5, 20.5), w=0.6, cl=0.3)], w=w, cl=cl)
        assert rep.data["overflow_first_mm"] == 0.0 and rep.data["iterations"] == 1, (w, cl, rep.data)
    # two of them through one border of the coarse class do not fit (1.8 > 1.3) and spread out
    c, rep = route(b, [conn(0, "P1", (5.5, 10.5), (30.5, 10.5), w=0.6, cl=0.3),
                       conn(1, "P2", (5.5, 10.6), (30.5, 10.6), w=0.6, cl=0.3)],
                   Cfg().set("P1", layers_ok=(0,)).set("P2", layers_ok=(0,)), w=0.4, cl=0.3)
    assert rep.data["overflow_first_mm"] > 0 and rep.data["overflow_mm"] == 0.0


def test_crossing_nets_do_not_share_a_layer():
    """Two nets whose tile paths cross need two layers (or a way round): border capacity alone cannot see it."""
    b = board()
    hz = conn(0, "H", (5.5, 10.5), (25.5, 10.5))
    vt = conn(1, "V", (15.5, 2.5), (15.5, 20.5))
    c, rep = route(b, [hz, vt])
    d = rep.data
    assert d["overflow_first_mm"] > 0 and d["overflow_mm"] == 0.0 and d["crossings_left"] == 0 and d["cuts"] == []
    assert d["vias"] == 2                                              # one of them dives under the other
    diver = 0 if c.corridors[0].layer_seq else 1
    cor = c.corridors[diver]
    assert cor.layers == (0, 1) and [(f, to) for _i, f, to in cor.layer_seq] == [(0, 1), (1, 0)]
    assert c.corridors[1 - diver].layer_seq == () and c.corridors[1 - diver].layers == (0,)
    st = c.history["global_state"]
    assert all(not (st.hT[n] > 1e-6 and st.vT[n] > 1e-6) for n in range(st.N))
    # held on one layer, the horizontal one walks round the end of the vertical one (no via, longer path)
    one = Cfg().set("H", layers_ok=(0,)).set("V", layers_ok=(0,))
    c, rep = route(b, [hz, vt], one)
    assert rep.data["overflow_mm"] == 0.0 and rep.data["vias"] == 0
    assert len(path(c, 0)) + len(path(c, 1)) > 21 + 19
    # ... and when the vertical one runs from edge to edge there is no way round: the crossing is reported
    full = conn(1, "V", (15.5, 0.5), (15.5, 29.5))
    c, rep = route(b, [hz, full], one, SimpleNamespace(negotiation_iters=5))
    d = rep.data
    assert near(d["overflow_mm"], 0.4) and d["crossings_left"] == 1 and len(d["cuts"]) == 1
    cut = d["cuts"][0]
    assert cut["a"] == cut["b"] and cut["a"].endswith("|cross") and cut["layer"] == "F.Cu" and cut["nets"] == ["H", "V"]
    assert near(cut["capacity_mm"], 0.4) and near(cut["demand_mm"], 0.8)
    assert near(c.corridors[0].overflow, 0.4) and near(c.corridors[1].overflow, 0.4)
    assert any("same-layer crossing" in n for n in rep.notes)
    # two connections that merely turn in the same tile, or run side by side, do not cross
    side = [conn(0, "A", (5.5, 10.3), (25.5, 10.3)), conn(1, "B", (5.5, 10.7), (25.5, 10.7))]
    assert route(b, side, Cfg().set("A", layers_ok=(0,)).set("B", layers_ok=(0,)))[1].data["overflow_first_mm"] == 0.0
    ell = [conn(0, "A", (5.5, 10.5), (15.5, 20.5)), conn(1, "B", (25.5, 10.5), (15.5, 2.5))]
    assert route(b, ell, Cfg().set("A", layers_ok=(0,)).set("B", layers_ok=(0,)))[1].data["overflow_mm"] == 0.0


def test_exact_crossing_test_on_shared_runs():
    """GlobalState.crossings(): two paths that share a run of tiles must cross iff one leaves the other on
    different sides at the two ends of the run."""
    b = board()
    t = TL.Tiles(b, O.Oracle(b), 1.0, 0.2, 0.2)

    def state(*paths):
        conns = [conn(i, net, (p[0][1] + 0.5, p[0][2] + 0.5), (p[-1][1] + 0.5, p[-1][2] + 0.5)) for i, (net, p) in enumerate(paths)]
        st = GR.GlobalState(t, conns, Cfg(), None)
        for i, (_net, p) in enumerate(paths):
            st._set(i, [l * st.G + ty * st.nx + tx for l, tx, ty in p], -1)
        return st

    def node(l, tx, ty):
        return l * t.G + ty * t.nx + tx

    H = [(0, x, 10) for x in range(5, 26)]
    V = [(0, 15, y) for y in range(2, 21)]
    # straight through one tile on both axes
    st = state(("H", H), ("V", V))
    (o, run, ip, iq), = st.crossings()
    assert near(o, 0.4) and run == (node(0, 15, 10),) and (ip, iq) == (0, 1)
    tot, edges, cols = st.overflow()
    assert near(tot, 0.4) and edges == [] and cols == [] and st.xruns == [(o, run, 0, 1)]
    # the two-tile jog: H turns into V's column, runs along it for one tile and leaves on the far side
    Hj = [(0, x, 10) for x in range(5, 15)] + [(0, x, 9) for x in range(14, 26)]
    Vj = [(0, 15, y) for y in range(2, 9)] + [(0, 14, y) for y in range(8, 12)] + [(0, 15, y) for y in range(11, 21)]
    (o, run, ip, iq), = state(("H", Hj), ("V", Vj)).crossings()
    assert set(run) == {node(0, 14, 10), node(0, 14, 9)}
    # no straight-through pair anywhere in that configuration: only the run test sees it
    stj = state(("H", Hj), ("V", Vj))
    assert all(not (stj.hT[n] > 1e-6 and stj.vT[n] > 1e-6) for n in range(stj.N))
    # a shared run with both leaving on the same side is no crossing: Q joins P from the south, P then turns
    # north while Q carries straight on (P is the inner track of the bend)
    P = [(0, x, 10) for x in range(5, 11)] + [(0, 10, y) for y in range(9, 4, -1)]
    Qs = [(0, 7, 12), (0, 7, 11)] + [(0, x, 10) for x in range(7, 14)]
    assert state(("P", P), ("Q", Qs)).crossings() == []
    # ... but joining from the north it is on the side P turns to: one crossing, whichever path is listed first
    Qn = [(0, 7, 8), (0, 7, 9)] + [(0, x, 10) for x in range(7, 14)]
    assert len(state(("P", P), ("Q", Qn)).crossings()) == 1 and len(state(("Q", Qn), ("P", P)).crossings()) == 1
    assert len(state(("P", P), ("Q", Qn[::-1])).crossings()) == 1          # the direction of travel does not matter
    assert state(("P", P), ("Q", Qs[::-1])).crossings() == []
    # a run that holds a terminal of either path proves nothing
    assert state(("P", P), ("Q", Qn[3:])).crossings() == []                  # Q starts inside the run
    assert state(("H", H), ("V", V[:9])).crossings() == []                   # V ends in the crossing tile
    # other layer, same net, or a layer change in the shared tile: nothing
    V1 = [(1, 15, y) for y in range(2, 21)]
    assert state(("H", H), ("V", V1)).crossings() == []
    assert state(("N", H), ("N", V)).crossings() == []
    dive = [(0, x, 10) for x in range(5, 15)] + [(1, x, 10) for x in range(14, 17)] + [(0, x, 10) for x in range(16, 26)]
    assert state(("H", dive), ("V", V)).crossings() == []
    # three paths: every crossing pair is counted
    V2 = [(0, 18, y) for y in range(2, 21)]
    st3 = state(("H", H), ("V", V), ("W", V2))
    assert sorted((ip, iq) for _o, _r, ip, iq in st3.crossings()) == [(0, 1), (0, 2)]
    assert [(ip, iq) for _o, _r, ip, iq in st3.crossings(2)] == [(0, 2)] and len(st3.crossings(0)) == 2
    assert near(st3._cross_of(1), 0.4) and near(st3._cross_of(0), 0.8)


# ── layers and vias ───────────────────────────────────────────────────────
def test_layer_dp_minimises_vias_under_max_vias():
    row = [(x, 10) for x in range(5, 16)]
    b = board()
    t = TL.Tiles(b, O.Oracle(b), 1.0, 0.2, 0.2)
    same = conn(0, "N0", (5.5, 10.5), (15.5, 10.5))
    assert GR.assign_layers(t, same, row, Cfg(), None) == ((0,), ())
    cross = conn(0, "N0", (5.5, 10.5), (15.5, 10.5), la=(0,), lb=(1,))
    lays, seq = GR.assign_layers(t, cross, row, Cfg(), None)
    assert lays == (0, 1) and len(seq) == 1 and seq[0][1:] == (0, 1) and 0 <= seq[0][0] < len(row)
    assert GR.assign_layers(t, cross, row, Cfg().set("N0", max_vias=0), None) == ((), ())      # impossible
    # the net is held on F.Cu but its far pad is on B.Cu: the via sits in the pad's own tile
    assert GR.assign_layers(t, cross, row, Cfg().set("N0", layers_ok=(0,)), None) == ((0, 1), ((len(row) - 1, 0, 1),))
    # a through-hole end is on every layer: no via needed
    assert GR.assign_layers(t, conn(0, "N0", (5.5, 10.5), (15.5, 10.5), la=(0,), lb=(0, 1)), row, Cfg(), None) == ((0,), ())
    # F.Cu is closed over two borders in the middle of the path (x 9.6 .. 11.4): two walls cost more than two vias
    b = board(zones=[keepout(9.6, 5.0, 11.4, 15.0, tracks=True, vias=False, layers=(0,))])
    t = TL.Tiles(b, O.Oracle(b), 1.0, 0.2, 0.2)
    assert t.cap(0, 9, 10, "E") == 0.0 and t.cap(0, 10, 10, "E") == 0.0 and near(t.cap(1, 9, 10, "E"), 1.2)
    lays, seq = GR.assign_layers(t, same, row, Cfg(), None)
    assert lays == (0, 1) and [(f, to) for _i, f, to in seq] == [(0, 1), (1, 0)]
    assert row[seq[0][0]][0] <= 9 and row[seq[1][0]][0] >= 11           # down before the keep-out, up after it
    # max_vias is hard: with one via it cannot come back up, so it stays on F.Cu; with none likewise
    for mv in (0, 1):
        assert GR.assign_layers(t, same, row, Cfg().set("N0", max_vias=mv), None) == ((0,), ())
    assert len(GR.assign_layers(t, same, row, Cfg().set("N0", max_vias=2), None)[1]) == 2
    # via_budget is soft: a via above the budget costs 3x, which makes the walls the cheaper way here
    x0 = SimpleNamespace(nets={"N0": {"via_budget": 0}})
    assert GR.assign_layers(t, same, row, Cfg(), x0) == ((0,), ())
    x2 = SimpleNamespace(nets={"N0": SimpleNamespace(via_budget=2)})            # hints may be objects as well
    assert len(GR.assign_layers(t, same, row, Cfg(), x2)[1]) == 2
    # end_layers overrides the b side (a connection that ends on another one's path)
    lays, seq = GR.assign_layers(TL.Tiles(board(), O.Oracle(board()), 1.0, 0.2, 0.2), same, row, Cfg(), None, end_layers=(1,))
    assert lays == (0, 1) and len(seq) == 1
    # the whole stage: the corridor carries the plan, the path keeps its tiles
    c, rep = route(b, [same])
    cor = c.corridors[0]
    assert cor.layers == (0, 1) and len(cor.layer_seq) == 2 and rep.data["vias"] == 2 and rep.data["overflow_mm"] == 0.0
    assert all(cor.path[i][0] <= 9 for i, f, to in cor.layer_seq if (f, to) == (0, 1))
    assert c.history["global"]["layer"][0][0] == 0 or cor.layer_seq[0][0] == 0
    c, rep = route(b, [same], Cfg().set("N0", max_vias=0))
    assert c.corridors[0].layer_seq == () and c.corridors[0].layers == (0,)


def test_no_vias_regions_and_via_capacity():
    b = board()
    cn = conn(0, "N0", (10.5, 10.5), (20.5, 10.5), la=(0,), lb=(1,))

    def via_tile(x):
        c, rep = route(b, [cn], None, x)
        (i, f, to), = c.corridors[0].layer_seq
        assert (f, to) == (0, 1)
        return c.corridors[0].path[i]

    assert 10 <= via_tile(None)[0] <= 20
    assert via_tile(SimpleNamespace(regions=[{"rect": [0, 0, 18.0, 30], "no_vias": True}]))[0] >= 18
    assert via_tile(SimpleNamespace(regions=[{"rect": [12.0, 0, 40, 30], "no_vias": True}]))[0] <= 11
    # no via anywhere: the connection cannot be made and is reported
    c, rep = route(b, [cn], None, SimpleNamespace(regions=[{"rect": [0, 0, 40, 30], "no_vias": True}]))
    assert rep.routed == 0 and rep.failed == 1 and c.corridors == {}
    # a column without a via site is avoided (pads on the bottom layer under the start tile)
    pads = [{"ref": "B1", "net": "Q", "x": 10.5, "y": 10.5, "hw": 0.9, "hh": 0.9, "layer": 1}]
    b2 = board(pads, nets={"Q": {}, "N0": {}})
    c, rep = route(b2, [conn(0, "N0", (10.5, 10.5), (20.5, 10.5), la=(0,), lb=(1,))])
    (i, f, to), = c.corridors[0].layer_seq
    assert c.tiles.via_cap(10, 10) == 0 and c.corridors[0].path[i][0] >= 12 and rep.data["overflow_mm"] == 0.0
    # via columns have capacity: three nets that all want to change layer in one 1 mm tile spread out
    conns = [conn(i, f"V{i}", (10.2 + 0.2 * i, 10.5), (10.3 + 0.2 * i, 10.6), la=(0,), lb=(1,)) for i in range(3)]
    c, rep = route(b, conns)
    assert rep.data["overflow_mm"] == 0.0 and rep.data["overflow_first_mm"] > 0
    cols = sorted(c.corridors[i].path[c.corridors[i].layer_seq[0][0]] for i in range(3))
    assert len(set(cols)) == 3, cols
    # region.capacity_scale reaches the tiles once
    x = SimpleNamespace(regions=[{"rect": [0, 0, 40, 30], "capacity_scale": 0.5}])
    c, rep = route(b, [conn(0, "N0", (5.5, 5.5), (9.5, 5.5))], None, x)
    assert near(c.tiles.cap(0, 10, 10, "E"), 0.6) and rep.data["regions_scaled"] > 0
    rep = GR.run(c, Budget(BIG))
    assert near(c.tiles.cap(0, 10, 10, "E"), 0.6) and rep.data["regions_scaled"] == 0


def test_layer_directions():
    b = board()
    few = [conn(i, f"S{i}", (2.5, 2.5 + i * 0.6), (37.5, 2.5 + i * 0.6)) for i in range(39)]
    assert GR.layer_directions(b, few) == {}
    hor = few + [conn(39, "S39", (2.5, 27.5), (37.5, 27.5))]
    assert GR.layer_directions(b, hor) == {0: "h", 1: "v"}
    ver = [conn(i, f"S{i}", (2.5 + i * 0.9, 2.5), (2.5 + i * 0.9, 27.5)) for i in range(40)]
    assert GR.layer_directions(b, ver) == {0: "v", 1: "h"}
    assert GR.layer_directions(b, hor, layers=[1]) == {}
    b4 = board(layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    assert GR.layer_directions(b4, hor, layers=[0, 3]) == {0: "h", 3: "v"}
    assert GR.layer_directions(b4, hor) == {0: "h", 1: "v", 2: "h", 3: "v"}
    # the dominant axis goes to the layer with the larger share of its capacity along it: F.Cu is cut by walls
    # that close its E borders, so horizontal runs belong on B.Cu
    bw = board(zones=[keepout(x - 0.2, 1.0, x + 0.2, 29.0, tracks=True, vias=False, layers=(0,)) for x in (10, 20, 30)])
    tw = TL.Tiles(bw, O.Oracle(bw), 1.0, 0.2, 0.2)
    assert GR.layer_directions(bw, hor, tw) == {1: "h", 0: "v"}
    # the stage: decided when the plan gives none, reported in auto{} by layer name, obeyed as a soft cost
    x = SimpleNamespace(auto={})
    c, rep = route(b, hor, None, x)
    assert rep.data["layer_dirs"] == {"F.Cu": "h", "B.Cu": "v"} and x.auto["layer_dirs"] == rep.data["layer_dirs"]
    cfg = Cfg()
    for i in range(40):
        cfg.set(f"S{i}", layer_dir={0: "v", 1: "h"})
    c, rep = route(b, hor, cfg, SimpleNamespace(auto={}))
    assert rep.data["layer_dirs"] == {}
    # a long vertical run between through-hole ends prefers the layer whose direction is vertical
    tall = [conn(0, "T", (20.5, 2.5), (20.5, 27.5), la=(0, 1), lb=(0, 1))]
    st = GR.GlobalState(TL.Tiles(b, O.Oracle(b), 1.0, 0.2, 0.2), tall, Cfg(), None, {0: "h", 1: "v"})
    st.negotiate([0], Budget(BIG))
    assert {n // st.G for n in st.paths[0]} == {1}
    st = GR.GlobalState(TL.Tiles(b, O.Oracle(b), 1.0, 0.2, 0.2), tall, Cfg(), None, {0: "v", 1: "h"})
    st.negotiate([0], Budget(BIG))
    assert {n // st.G for n in st.paths[0]} == {0}


# ── corridors ─────────────────────────────────────────────────────────────
def test_corridor_shape_lateral_ranks_and_history():
    b = board()
    conns = [conn(0, "N0", (5.5, 10.7), (15.5, 10.7)), conn(1, "N1", (5.5, 10.3), (15.5, 10.3))]
    c, rep = route(b, conns, ONE)
    for i in (0, 1):
        cor = c.corridors[i]
        assert isinstance(cor, Corridor) and cor.conn == i
        assert cor.tiles == frozenset((x, y) for x in range(4, 17) for y in (9, 10, 11))      # the path + 1 ring
        assert cor.path == tuple((x, 10) for x in range(5, 16)) and path(c, i) == list(cor.path)
        assert cor.layers == (0,) and cor.layer_seq == () and cor.overflow == 0.0
        assert c.history["global"]["layer"][i] == [0] * 11
        assert sorted(cor.lateral) == [(0, x, 10, "E") for x in range(5, 15)]
    # ranked along the border (y): N1 at 10.3 before N0 at 10.7, on every border they share
    assert set(c.corridors[1].lateral.values()) == {0} and set(c.corridors[0].lateral.values()) == {1}
    assert c.history["global"]["crossings"][(0, 7, 10, "E")] == 2
    assert c.history["global"]["tile_mm"] == 1.0 and c.history["global"]["advisory"] is False
    assert isinstance(c.history["global_state"], GR.GlobalState)
    # a path at the board edge: the ring is clipped to the grid
    c, rep = route(b, [conn(0, "N0", (0.5, 0.5), (0.5, 3.5))])
    assert c.corridors[0].tiles == frozenset((x, y) for x in (0, 1) for y in range(0, 5))
    # pathfinder alone: 2-D tile paths from terminal a
    t = TL.Tiles(b, O.Oracle(b), 1.0, 0.2, 0.2)
    out = GR.pathfinder(t, conns, ONE, None, Budget(BIG))
    assert out == {0: [(x, 10) for x in range(5, 16)], 1: [(x, 10) for x in range(5, 16)]}
    st = GR.GlobalState(t, conns, ONE, None)
    bud = Budget(BIG)
    assert GR.pathfinder(t, conns, ONE, None, bud, state=st) == out and st.info["overflow_mm"] == 0.0
    assert bud.used == st.expansions > 0


def test_only_reroutes_after_capacity_feedback():
    b = board()
    conns = [conn(0, "N0", (5.5, 10.5), (15.5, 10.5)), conn(1, "N1", (5.5, 20.5), (15.5, 20.5))]
    c, rep = route(b, conns, ONE)
    st = c.history["global_state"]
    before = {i: (c.corridors[i].tiles, c.corridors[i].path) for i in (0, 1)}
    assert crosses(path(c, 0), (9, 10), (10, 10))
    # repair found that nothing gets through that border after all
    c.tiles.book(0, 9, 10, "E", 5.0)
    rep = GR.run(c, Budget(BIG), only=[0])
    assert c.history["global_state"] is st and (rep.routed, rep.failed) == (1, 0)
    assert not crosses(path(c, 0), (9, 10), (10, 10)) and len(path(c, 0)) == 13          # one tile round
    assert c.corridors[0].path != before[0][1] and rep.data["overflow_mm"] == 0.0
    assert (c.corridors[1].tiles, c.corridors[1].path) == before[1]                      # the other one is untouched
    # scaling a border down works the same way
    c.tiles.scale(0, 9, 20, "E", 0.0)
    rep = GR.run(c, Budget(BIG), only=[1])
    assert not crosses(path(c, 1), (9, 20), (10, 20))
    # a full run starts from scratch but keeps the bookings
    rep = GR.run(c, Budget(BIG))
    assert c.history["global_state"] is not st and not crosses(path(c, 0), (9, 10), (10, 10))


def test_reservations_and_routed_connections_from_the_state():
    b = board()
    prot = Piece(id=1, net="GND", conn=-1, kind="seg", layer=0, x1=9.5, y1=10.2, x2=10.5, y2=10.2, w=0.4, stage="power",
                 protected=True)
    via = Piece(id=2, net="GND", conn=-1, kind="via", layer=-1, x1=20.5, y1=20.5, x2=20.5, y2=20.5, w=0.6, drill=0.3,
                stage="power", protected=True)
    plain = Piece(id=3, net="X", conn=5, kind="seg", layer=0, x1=9.5, y1=15.2, x2=10.5, y2=15.2, w=0.4)

    class State:
        def pieces(self, conn=None, net=None):
            return [via, plain, prot]

        def status(self, idx):
            return "routed" if idx == 1 else "todo"

    conns = [conn(0, "N0", (5.5, 5.5), (15.5, 5.5)), conn(1, "N1", (5.5, 25.5), (15.5, 25.5))]
    c = ctx_for(b, conns)
    c.state = State()
    rep = GR.run(c, Budget(BIG))
    assert rep.data["booked"] == 2
    assert near(c.tiles.cap(0, 9, 10, "E"), 1.2 - 0.6)                  # 0.4 wide + 0.2 clearance booked
    assert near(c.tiles.cap(0, 9, 15, "E"), 1.2) and c.tiles.via_cap(20, 20) == 0
    assert sorted(c.corridors) == [0] and rep.routed == 1               # connection 1 is already routed
    rep = GR.run(c, Budget(BIG))
    assert rep.data["booked"] == 0 and near(c.tiles.cap(0, 9, 10, "E"), 0.6)       # booked once
    # soft shapes of the oracle (thermal-spoke corridors, reserved via sites) are booked as well
    c = ctx_for(b, conns)
    c.oracle.add_soft("thermal", "GND", 0, (10.0, 10.5, 1.0, 0.0, 0.7, 0.25, 0.0))      # x 9.3..10.7, y 10.25..10.75
    c.oracle.add_soft("reserve", "GND", -1, (20.5, 20.5, 1.0, 0.0, 0.0, 0.0, 0.3))
    rep = GR.run(c, Budget(BIG))
    assert rep.data["booked"] == 2 and c.tiles.reservations_booked
    assert near(c.tiles.cap(0, 9, 10, "E"), 1.2 - 0.7)                  # the spoke's 0.5 mm + clearance
    assert near(c.tiles.cap(1, 9, 10, "E"), 1.2) and c.tiles.via_cap(20, 20) == 0
    # a stage that booked its reservations itself says so, and nothing is booked twice
    c = ctx_for(b, conns)
    c.state = State()
    c.tiles.reservations_booked = True
    rep = GR.run(c, Budget(BIG))
    assert rep.data["booked"] == 0 and near(c.tiles.cap(0, 9, 10, "E"), 1.2)
    # a pad-to-pour connection (open terminal b: no pad, no cluster, no layers) gets no tile path
    op = ConnV2(idx=7, net="GND", a=Terminal(5.5, 15.5, (0,), "U1.3", 9), b=Terminal(20.5, 15.5, ()), width=0.2,
                clearance=0.2, role="plane", rank=(9, 7))
    c = ctx_for(b, conns + [op])
    rep = GR.run(c, Budget(BIG))
    assert sorted(c.corridors) == [0, 1] and (rep.routed, rep.failed) == (2, 0) and rep.data["open_skipped"] == 1
    assert any("pad-to-pour" in n for n in rep.notes)
    # without tiles the stage builds them (tile size from the plan when given)
    c = Ctx(board=b, cfg=Cfg(), x=SimpleNamespace(tile_mm=2.0), oracle=O.Oracle(b), conns=conns)
    rep = GR.run(c, Budget(BIG))
    assert c.tiles is not None and c.tiles.t == 2.0 and rep.data["tile_mm"] == 2.0 and rep.routed == 2


# ── plan hints ────────────────────────────────────────────────────────────
def test_plan_costs_regions_avoid_corridor_layers():
    b = board()
    straight = [(x, 10) for x in range(5, 26)]
    cn = conn(0, "N0", (5.5, 10.5), (25.5, 10.5))
    assert path(route(b, [cn])[0], 0) == straight
    # a forbid region across the way: no tile of it is entered
    c, _ = route(b, [cn], Cfg().region((14.0, 8.0, 16.0, 13.0), forbid=True))
    assert not any(14 <= x <= 15 and 8 <= y <= 12 for x, y in path(c, 0)) and len(path(c, 0)) > 21
    # ... unless it does not apply to this net, or only to the other layer
    c, _ = route(b, [cn], Cfg().region((14.0, 8.0, 16.0, 13.0), forbid=True, nets={"OTHER"}))
    assert path(c, 0) == straight
    c, _ = route(b, [cn], Cfg().region((14.0, 8.0, 16.0, 13.0), forbid=True, layers=(1,)))
    assert path(c, 0) == straight
    # an expensive region is crossed only when the way round is longer
    c, _ = route(b, [cn], Cfg().region((14.0, 9.0, 16.0, 12.0), cost=10.0))
    assert not any(14 <= x <= 15 and 9 <= y <= 11 for x, y in path(c, 0))
    c, _ = route(b, [cn], Cfg().region((14.0, 0.0, 16.0, 30.0), cost=10.0))
    assert len(path(c, 0)) == 21
    # net avoid rectangles
    c, _ = route(b, [cn], Cfg().set("N0", avoid=[(14.0, 8.0, 16.0, 13.0)]))
    assert not any(14 <= x <= 15 and 8 <= y <= 12 for x, y in path(c, 0))
    # a guide corridor (v1 plan field): the route follows the polyline
    guide = {"points": [(5.5, 10.5), (15.5, 20.5), (25.5, 10.5)], "width": 2.0, "strict": False, "outside_cost": 3.0}
    c, _ = route(b, [cn], Cfg().set("N0", corridor=guide))
    assert max(y for _x, y in path(c, 0)) >= 16 and len(path(c, 0)) >= 33       # (it may cut the apex by a tile)
    # layers: the net may only run on B.Cu although both pads are on F.Cu: down at the start, up at the end
    c, rep = route(b, [cn], Cfg().set("N0", layers_ok=(1,)))
    cor = c.corridors[0]
    assert [(f, to) for _i, f, to in cor.layer_seq] == [(0, 1), (1, 0)] and cor.layer_seq[0][0] == 0
    assert cor.layer_seq[1][0] == len(cor.path) - 1
    # layer_cost pushes a through-hole connection to the cheap layer
    th = conn(0, "N0", (5.5, 10.5), (25.5, 10.5), la=(0, 1), lb=(0, 1))
    assert route(b, [th], Cfg().set("N0", layer_cost={0: 3.0}))[0].corridors[0].layers == (1,)
    assert route(b, [th], Cfg().set("N0", layer_cost={1: 3.0}))[0].corridors[0].layers == (0,)
    # the real plan resolver produces a Config this stage understands
    pads = [{"ref": "R1", "num": "1", "net": "N0", "x": 5.5, "y": 10.5, "hw": 0.3, "hh": 0.3, "c": 1},
            {"ref": "R2", "num": "1", "net": "N0", "x": 25.5, "y": 10.5, "hw": 0.3, "hh": 0.3, "c": 2}]
    bp = board(pads)
    cfg = P.resolve(bp, {"nets": {"N0": {"layers": ["F.Cu"], "max_vias": 0}},
                         "regions": [{"rect": [14, 8, 16, 13], "forbid": True}]})
    c, rep = route(bp, [conn(0, "N0", (5.5, 10.5), (25.5, 10.5), pa="R1.1", pb="R2.1", ca=1, cb=2)], cfg)
    assert rep.routed == 1 and not any(14 <= x <= 15 and 8 <= y <= 12 for x, y in path(c, 0))
    assert c.corridors[0].layers == (0,) and rep.data["overflow_mm"] == 0.0


def test_v2_hints_waypoints_between_side_of_buses():
    b = board()
    cn = conn(0, "N0", (5.5, 10.5), (25.5, 10.5))
    # waypoints: forced tile visits in order
    x = SimpleNamespace(nets={"N0": {"waypoints": [[10.5, 20.5], [20.5, 3.5, "F.Cu"]]}})
    p = path(route(b, [cn], None, x)[0], 0)
    assert (10, 20) in p and (20, 3) in p and p.index((10, 20)) < p.index((20, 3)) and p[0] == (5, 10) and p[-1] == (25, 10)
    # between: the tile of the pad-pair cut is cheap (x 0.2). Among the equally long staircases from (5, 10) to
    # (25, 14) the route takes one through the cut, wherever it lies off the default path
    dg = conn(0, "N0", (5.5, 10.5), (25.5, 14.5))
    one = Cfg().set("N0", layers_ok=(0,))
    default = path(route(b, [dg], one)[0], 0)
    spots = [(tx, ty) for ty in (11, 12, 13) for tx in (9, 15, 21) if (tx, ty) not in default]
    assert len(spots) >= 3
    for tx, ty in spots[:3]:
        pads = [{"ref": "U1", "num": "1", "net": "A", "x": tx + 0.5, "y": ty + 0.2, "hw": 0.1, "hh": 0.1, "layer": 1},
                {"ref": "U1", "num": "2", "net": "B", "x": tx + 0.5, "y": ty + 0.8, "hw": 0.1, "hh": 0.1, "layer": 1}]
        bb = board(pads, nets={"A": {}, "B": {}, "N0": {}})
        assert path(route(bb, [dg], one)[0], 0) == default
        x = SimpleNamespace(nets={"N0": {"between": [["U1.1", "U1.2"]]}})
        p = path(route(bb, [dg], one, x)[0], 0)
        assert (tx, ty) in p and len(p) == len(default), (tx, ty, p)
    # side_of: pass a part clockwise (above it when travelling east, on screen) or counter-clockwise (below)
    part = [{"ref": "U9", "num": "1", "net": "A", "x": 15.5, "y": 10.9, "hw": 0.1, "hh": 0.1, "layer": 1}]
    bs = board(part, nets={"A": {}, "N0": {}})
    cw = path(route(bs, [cn], None, SimpleNamespace(nets={"N0": {"side_of": [{"ref": "U9", "side": "cw"}]}}))[0], 0)
    ccw = path(route(bs, [cn], None, SimpleNamespace(nets={"N0": {"side_of": [{"ref": "U9", "side": "ccw"}]}}))[0], 0)
    assert all(y <= 10 for x_, y in cw if x_ == 15) and all(y >= 11 for x_, y in ccw if x_ == 15)
    # buses: members share a corridor (a border a sibling uses costs half)
    two = [conn(0, "D0", (5.5, 5.5), (25.5, 5.5)), conn(1, "D1", (5.5, 8.5), (25.5, 8.5))]
    c, _ = route(b, two)
    assert all(y == 8 for _x, y in path(c, 1))
    c, rep = route(b, two, None, SimpleNamespace(buses=[{"nets": ["D0", "D1"]}]))
    assert sum(1 for _x, y in path(c, 1) if y == 5) >= 15 and rep.data["overflow_mm"] == 0.0
    # a bus layer keeps the members on it
    th = [conn(0, "D0", (5.5, 5.5), (25.5, 5.5), la=(0, 1), lb=(0, 1)), conn(1, "D1", (5.5, 8.5), (25.5, 8.5), la=(0, 1), lb=(0, 1))]
    c, rep = route(b, th, None, SimpleNamespace(buses=[{"nets": ["D0", "D1"], "layer": "B.Cu"}]))
    assert c.corridors[0].layers == (1,) and c.corridors[1].layers == (1,)


# ── the corpus ────────────────────────────────────────────────────────────
def test_corpus_boards_route_globally():
    """Design §8: <= 60 000 nodes, <= 10 s, <= 30 iterations; same twice."""
    names = ["ecc83", "stickhub", "multichannel_mixer"]
    if FULL:
        names = sorted(p.name for p in common.CORPUS.iterdir() if p.is_dir()) if common.CORPUS.exists() else []
    try:
        import tiles_cal
    except ImportError:
        tiles_cal = None
    names = [n for n in names if tiles_cal is not None and (common.CORPUS / n / f"{n}.kicad_pcb").exists()
             and ((tiles_cal.CACHE / f"ex_{n}.json").exists() or common.KICAD_PY.exists())]
    if not names:
        print("    (skipped: no benchmark corpus / no cached extracts)")
        return
    spent = []
    for n in names:
        runs = []
        for _ in range(2):
            b, o = tiles_cal.load(n)
            cfg, conns = tiles_cal.conns_v1(b)
            c = Ctx(board=b, cfg=cfg, oracle=o, conns=conns)
            t0 = time.perf_counter()
            rep = GR.run(c, Budget(int(0.10 * 90 * 200_000)))          # the stage's share of the default budget
            dt = time.perf_counter() - t0
            runs.append(({i: (sorted(k.tiles), k.layers, k.layer_seq, sorted(k.lateral.items()), k.overflow)
                          for i, k in c.corridors.items()}, rep.data, rep.work))
        d = rep.data
        print(f"    {n}: {len(conns)} conns, T={d['tile_mm']} {d['nx']}x{d['ny']}x{d['layers']}, {d['iterations']} "
              f"iteration(s), overflow {d['overflow_first_mm']} -> {d['overflow_mm']} mm, {d['vias']} vias, "
              f"{d['expansions']} expansions, {dt:.2f}s incl. tiles")
        assert runs[0] == runs[1], n
        assert d["nx"] * d["ny"] * d["layers"] <= 60_000 and d["iterations"] <= 30 and dt < 15.0
        assert rep.routed == len(conns) and rep.failed == 0, (n, rep.notes)
        assert d["overflow_mm"] <= d["overflow_first_mm"]
        if d["out_of_budget"]:
            # measured: tinytapeout (349 connections, a 99-connection +3V3 without a pour) does not converge inside
            # the stage's 10 % share of the default budget; the best iteration is kept and the rest reported as cuts
            print(f"      {n}: OUT OF BUDGET after {d['iterations']} iterations, {len(d['cuts'])} cuts left")
            spent.append(n)
            assert d["overflow_mm"] < 0.05 * d["overflow_first_mm"] and d["cuts"]
        assert d["crossings_left"] <= 10, (n, d["crossings_left"])
        for cn in conns:
            cor = c.corridors[cn.idx]
            assert c.tiles.tile_of(cn.a.x, cn.a.y) in cor.tiles and cor.path[0] == c.tiles.tile_of(cn.a.x, cn.a.y)
            assert all(0 <= i < len(cor.path) for i, _f, _t in cor.layer_seq)
            host = c.history["global"]["tee"].get(cn.idx)
            if host is None:
                assert cor.path[-1] == c.tiles.tile_of(cn.b.x, cn.b.y)
            else:                                    # ends on a connection that precedes it: its last tile is on that path
                hc = next(k for k in conns if k.idx == host)
                assert (hc.rank, hc.idx) < (cn.rank, cn.idx) and cor.path[-1] in c.corridors[host].path
    assert spent in ([], ["tinytapeout"]), spent


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
