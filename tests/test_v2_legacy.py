"""Router v2 legacy stage (design §9 legacy.py): v1's search on a v2 RouteState, with connection-level
verify and rip (never a whole net) and the oracle as the exact judge.

Pure tests on synthetic boards, plus corpus runs that need KiCad 10 and the benchmark corpus:

Run:  python tests/test_v2_legacy.py                 pure tests + corpus board ecc83 (worker + kicad-cli DRC)
      python tests/test_v2_legacy.py --full          ... + one_air_max, the measured whole-net-rip case (minutes)
      python tests/test_v2_legacy.py test_name ...   chosen tests
Scratch files go to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import json
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
from copper_kicad.pcb.guided import model, plan as planmod, router as v1  # noqa: E402
from copper_kicad.pcb.v2 import legacy, oracle as O, pieces as PS  # noqa: E402
from copper_kicad.pcb.v2.types import Blocker, Budget, ConnV2, Ctx, Terminal  # noqa: E402

FULL = "--full" in sys.argv or os.environ.get("COPPER_V2_LEGACY_FULL") == "1"


# ── synthetic boards (the fixture of tests/test_guided_route.py) ──────────
def synth(pads, size=(20.0, 15.0), layers=("F.Cu", "B.Cu"), walls=(), tracks=(), zones=()):
    """pads: (ref, net, x, y[, 'tht'[, cluster]]) — 0.8 mm square SMD pads on F.Cu (or THT, all layers)."""
    nets = sorted({p[1] for p in pads if p[1]})
    ex = {
        "layers": list(layers),
        "rules": {"clearance": 0.15, "track_width": 0.15, "via": 0.6, "drill": 0.3, "edge_clearance": 0.3,
                  "hole_clearance": 0.25, "hole_to_hole": 0.25, "annular": 0.1,
                  "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.2, "dp_width": 0.2}},
        "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                     "dp_gap": 0.2, "dp_width": 0.2} for i, n in enumerate(nets)},
        "outline": {"closed": True, "polys": [{"o": [[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]]}]},
        "pads": [], "footprints": [], "graphics": [], "tracks": list(tracks), "vias": [], "zones": [dict(z) for z in zones],
        "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0},
    }
    for i, p in enumerate(pads):
        ref, net, x, y = p[:4]
        tht = len(p) > 4 and p[4] == "tht"
        lays = list(range(len(layers))) if tht else [0]
        d = {"ref": ref, "num": "1", "net": net, "x": x, "y": y, "kind": "tht" if tht else "smd", "layers": lays,
             "shapes": [[lays, "box", [x, y, 1, 0, 0.4, 0.4, 0.1]]], "c": p[5] if len(p) > 5 else i + 1}
        if tht:
            d["hole"] = [x, y, 1, 0, 0.2, 0.2]
        ex["pads"].append(d)
    for w in walls:
        x0, y0, x1, y1 = w
        ex["zones"].append({"name": "wall", "layers": list(range(len(layers))), "rule_area": True, "no_tracks": True,
                            "no_vias": True, "polys": [{"o": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}]})
    return ex


def make(ex, plan=None, mode="signals"):
    b = model.Board(json.loads(json.dumps(ex)))
    cfg = planmod.resolve(b, plan)
    o = O.Oracle(b)
    conns = legacy.plan_connections(b, cfg, mode)
    st = PS.RouteState(o, conns)
    return Ctx(board=b, cfg=cfg, oracle=o, state=st, conns=conns)


def run_v1(ex, plan=None, mode="signals"):
    b = model.Board(json.loads(json.dumps(ex)))
    cfg = planmod.resolve(b, plan)
    b.make_grid(cfg.step)
    return v1.Router(b, cfg, mode=mode).run()


def ctx_with_grid(ctx):
    if ctx.board.grid is None:
        ctx.board.make_grid(ctx.cfg.step)
    return ctx


def r4(rows):
    return [[round(v, 4) if isinstance(v, float) else v for v in row] for row in rows]


def assert_legal(ctx):
    """Every piece is legal for the oracle against everything else, and every routed connection is connected."""
    st, o = ctx.state, ctx.oracle
    for p in st.pieces():
        blk = o.piece_ok(p)
        assert blk is None, (p, blk)
    for c in ctx.conns:
        if st.status(c.idx) == "routed":
            assert st.connected(c.idx, loose=True), c


CROSS = [("A1", "A", 3, 7.5), ("A2", "A", 17, 7.5), ("B1", "B", 10, 2), ("B2", "B", 10, 13),
         ("C1", "C", 4, 3), ("C2", "C", 16, 12), ("C3", "C", 16, 3)]
WALLS = [[10, 0.2, 10.6, 6.9], [10, 8.1, 10.6, 14.8]]
GAP = [("A1", "A", 9.3, 5.5), ("A2", "A", 9.3, 9.5), ("B1", "B", 4, 7.5), ("B2", "B", 16, 7.5)]
STAR = [("N1", "N", 3, 3), ("N2", "N", 9, 3), ("N3", "N", 15, 3), ("N4", "N", 9, 9), ("N5", "N", 15, 9),
        ("M1", "M", 3, 12), ("M2", "M", 17, 12)]


# ── tests ─────────────────────────────────────────────────────────────────
def test_plan_connections_is_v1s_plan():
    ctx = make(synth(CROSS))
    b = ctx.board
    b.make_grid(ctx.cfg.step)
    r = v1.Router(b, ctx.cfg)
    ref = r.plan_connections()
    assert len(ctx.conns) == len(ref) == 4
    for c, v in zip(ctx.conns, ref):
        assert (c.idx, c.net, c.a.pad, c.b.pad, c.a.cluster, c.b.cluster) == (v.idx, v.net, v.pa.name, v.pb.name, v.a, v.b)
        assert c.rank == v.key and c.est == v.est and c.role == "signal" and c.tree_target
        assert c.width == 0.2 and c.clearance == 0.2 and c.via == (0.6, 0.3) and c.a.layers == (0,)
    assert [c.idx for c in legacy.plan_connections(b, ctx.cfg, start=10)] == [10, 11, 12, 13]
    # a skipped net is not planned, and is listed in v1's format
    ctx2 = make(synth(CROSS), {"nets": {"A": {"skip": True}}})
    assert {c.net for c in ctx2.conns} == {"B", "C"}
    assert legacy.skipped(ctx2.board, ctx2.cfg) == [{"net": "A", "connections": 1, "reason": "plan: skip"}]


def test_clean_board_gives_v1s_copper():
    for pads, plan in ((CROSS, None), (STAR, None), (CROSS, {"global": {"layers": ["F.Cu", "B.Cu"], "via_cost": 1}})):
        ex = synth(pads)
        res = run_v1(ex, plan)
        ctx = make(ex, plan)
        bud = Budget(10_000_000)
        rep = legacy.route_all(ctx, bud)
        assert rep.name == "legacy" and rep.failed == 0 and rep.routed == len(ctx.conns) == len(res["conns"])
        tracks, vias, ids = ctx.state.export()
        assert r4(tracks) == res["tracks"] and r4(vias) == res["vias"], "legacy copper differs from v1 on a clean board"
        assert len(ids) == len(tracks) + len(vias)
        assert all(p.stage == "legacy" and p.conn >= 0 for p in ctx.state.pieces())
        assert all(ctx.state.status(c.idx) == "routed" for c in ctx.conns)
        assert rep.score == ctx.state.score() and rep.score.opens == 0 and rep.score.vias == len(vias)
        assert rep.data["not_connected"] == [] and rep.data["stats"]["verify_rips"] == 0
        # the budget is charged with v1's work plus the oracle checks
        assert bud.used == rep.work >= res["work"] and rep.data["pass"]["routed"] == rep.routed
        assert_legal(ctx)


def test_one_bad_segment_rips_one_connection_not_the_net():
    """Fault injection into the final self-check: the oracle rejects one piece of ONE connection of a
    four-connection net. v1 dropped the net (rip_net); here the other three keep their very pieces."""
    saved = legacy.EMIT_CHECK
    legacy.EMIT_CHECK = False                # let the bad piece reach the final self-check
    try:
        # 1. a fault that goes away: the connection is re-queued once and routed again
        ctx = make(synth(STAR))
        o = ctx.oracle
        seen = {"n": 0, "victim": None, "others": None}
        real = o.piece_ok

        def flaky(p, ignore=frozenset(), clearance=None):
            if p.net == "N" and seen["victim"] is None:
                seen["victim"] = p.conn
                seen["others"] = {q.id for q in ctx.state.pieces(net="N") if q.conn != p.conn}
                seen["n"] += 1
                return Blocker("clearance", "M", "X.1", True, None, 0.19, 0.2, (p.x1, p.y1), p.layer)
            return real(p, ignore, clearance)
        o.piece_ok = flaky
        rep = legacy.route_all(ctx, Budget(10_000_000))
        del o.piece_ok
        assert seen["n"] == 1 and rep.data["stats"]["verify_rips"] == 1
        assert rep.failed == 0 and all(ctx.state.status(c.idx) == "routed" for c in ctx.conns)
        now = {p.id for p in ctx.state.pieces(net="N")}
        assert seen["others"] <= now, "pieces of the net's other connections were ripped"
        assert len(now - seen["others"]) >= 1 and all(p.conn == seen["victim"] for p in ctx.state.pieces()
                                                      if p.id in now - seen["others"])
        assert_legal(ctx)

        # 2. a fault that stays: that connection alone fails, with v1's phrase; the net keeps the rest
        ctx = make(synth(STAR))
        o = ctx.oracle
        real = o.piece_ok
        bad_conn = [c.idx for c in ctx.conns if c.net == "N"][1]

        def broken(p, ignore=frozenset(), clearance=None):
            if p.conn == bad_conn:
                return Blocker("clearance", "M", "X.1", True, None, 0.19, 0.2, (p.x1, p.y1), p.layer)
            return real(p, ignore, clearance)
        o.piece_ok = broken
        rep = legacy.route_all(ctx, Budget(10_000_000))
        del o.piece_ok
        st = ctx.state
        assert rep.failed == 1 and st.status(bad_conn) == "failed" and st.pieces(conn=bad_conn) == []
        f = st.fail(bad_conn)
        assert f["reason"] == legacy.SELF_CHECK and f["net"] == "N" and f["conn"] == bad_conn and len(f["where"]) == 2
        assert rep.data["failed"] == [f] and set(f) >= {"net", "from", "to", "_a", "_b", "reason", "where"}
        n_conns = [c.idx for c in ctx.conns if c.net == "N"]
        assert len(n_conns) == 4 and sum(1 for i in n_conns if st.status(i) == "routed") >= 2
        assert all(st.status(c.idx) == "routed" for c in ctx.conns if c.net == "M")
        # whatever is still 'routed' is really connected: nothing dangles from the ripped connection
        for i in n_conns:
            if st.status(i) == "routed":
                assert st.connected(i, loose=True)
        assert rep.data["stats"]["verify_rips"] == 2            # first failure, then the one retry
    finally:
        legacy.EMIT_CHECK = saved


def test_oracle_rejection_at_emit_time_blocks_cells_and_researches():
    """A rule only the oracle knows (here: a soft shape hardened into a keep-out for net A after the grid was
    rasterised) is found when the path is emitted; the cells are closed and the search goes round."""
    ex = synth([("A1", "A", 3, 7.5), ("A2", "A", 17, 7.5), ("B1", "B", 3, 3), ("B2", "B", 17, 3)])
    ctx = make(ex, {"global": {"layers": ["F.Cu"]}})
    o = ctx.oracle
    sid = o.add_soft("reserve", "B", 0, (10.0, 7.5, 1.0, 0.0, 0.3, 1.2, 0.0))      # a 0.6 x 2.4 mm box across A's line
    o.harden(sid)
    assert o.seg_ok(0, 3, 7.5, 17, 7.5, 0.2, "A") is not None
    rep = legacy.route_all(ctx, Budget(10_000_000))
    assert rep.failed == 0, rep.data["failed"]
    assert rep.data["stats"]["emit_rejects"] >= 1 and rep.data["stats"]["emit_restamps"] >= 1
    assert rep.data["stats"]["verify_rips"] == 0
    assert_legal(ctx)
    ys = [y for p in ctx.state.pieces(net="A") for y in (p.y1, p.y2)]
    assert max(abs(y - 7.5) for y in ys) > 1.2                 # it went round the box
    # when no detour exists the connection fails alone, and says why
    ex = synth([("A1", "A", 3, 7.5), ("A2", "A", 17, 7.5), ("B1", "B", 3, 3), ("B2", "B", 17, 3)])
    ctx = make(ex, {"global": {"layers": ["F.Cu"]}})
    o = ctx.oracle
    o.harden(o.add_soft("reserve", "B", 0, (10.0, 7.5, 1.0, 0.0, 0.3, 7.6, 0.0)))  # edge to edge
    rep = legacy.route_all(ctx, Budget(10_000_000))
    a = next(c.idx for c in ctx.conns if c.net == "A")
    assert ctx.state.status(a) == "failed" and ctx.state.pieces(net="A") == []
    assert "exact clearance check" in ctx.state.fail(a)["reason"], ctx.state.fail(a)
    assert all(ctx.state.status(c.idx) == "routed" for c in ctx.conns if c.net == "B")


def test_state_pieces_are_fixed_copper():
    ex = synth([("A1", "A", 3, 7.5), ("A2", "A", 17, 7.5), ("B1", "B", 10, 2, "tht"), ("B2", "B", 10, 13, "tht")])
    ctx = make(ex)
    st = ctx.state
    # a foreign reservation across A's straight line (not tied to a connection), on both layers
    wall = [st.add(net="B", conn=-1, kind="seg", layer=l, x1=10.0, y1=2.0, x2=10.0, y2=10.0, w=0.3, stage="power",
                   protected=True) for l in (0, 1)]
    # an escape stub of A1 that the route should start from
    stub = st.add(net="A", conn=-1, kind="seg", layer=0, x1=3.0, y1=7.5, x2=3.0, y2=9.5, w=0.2, stage="fanout",
                  protected=True)
    before = st.snapshot().pieces
    rep = legacy.route_all(ctx, Budget(10_000_000))
    assert rep.failed == 0, rep.data["failed"]
    assert all(st.piece(p.id) is p for p in before), "fixed copper was touched"
    assert_legal(ctx)
    a = [p for p in st.pieces(net="A") if p.stage == "legacy"]
    assert a and all(p.conn == next(c.idx for c in ctx.conns if c.net == "A") for p in a)
    # A crossed the wall: it had to leave the wall's span (or go to where the wall is not)
    for p in a:
        if p.kind == "seg":
            assert ctx.oracle.piece_ok(p) is None
    # B's own fixed copper (it starts on pad B1) is a target for B: only the last 3 mm to B2 are new
    lb = sum(p.length for p in st.pieces(net="B") if p.stage == "legacy")
    assert lb < 3.5, lb                                         # the pads are 11 mm apart
    assert st.connected(next(c.idx for c in ctx.conns if c.net == "B"), loose=True)
    assert stub in st.pieces(net="A") and wall[0] in st.pieces(net="B")


def test_only_reroutes_one_connection_among_fixed_copper():
    ctx = make(synth(STAR))
    st = ctx.state
    rep = legacy.route_all(ctx, Budget(10_000_000))
    assert rep.failed == 0 and rep.routed == len(ctx.conns)
    first = st.export()
    # nothing to do: no work, nothing changes
    bud = Budget(10_000_000)
    rep2 = legacy.route_all(ctx, bud)
    assert rep2.routed == 0 and rep2.work == 0 and bud.used == 0 and st.export() == first
    # rip the last-planned connection of N (a leaf of v1's tree), route only that one again
    victim = [c.idx for c in ctx.conns if c.net == "N"][-1]
    others = [p for p in st.pieces() if p.conn != victim]
    dangling_before = st.dangling("N", loose=True)
    assert dangling_before == []
    st.rip_conn(victim)
    for i in st.dangling("N", loose=True):                      # what teed onto it goes too (the caller's job)
        st.rip_conn(i)
    todo = [c.idx for c in ctx.conns if st.status(c.idx) != "routed"]
    assert victim in todo
    rep3 = legacy.route_all(ctx, Budget(10_000_000), only=todo)
    assert rep3.failed == 0 and rep3.routed == len(todo)
    kept = [p for p in others if st.piece(p.id) is not None]
    assert all(st.piece(p.id) is p for p in kept)
    assert_legal(ctx)
    assert all(st.status(c.idx) == "routed" for c in ctx.conns)


def test_negotiation_rips_connections_not_nets():
    # v1's single-layer rip-up case: A (routed first) runs across the mouth of the only gap in a wall, so B has
    # to rip it up. A has a second connection (A3 - A2) that is not in the way.
    plan = {"order": ["A", "B"], "global": {"layers": ["F.Cu"]}}
    pads = GAP + [("A3", "A", 5, 12.5)]
    for level in ("conn", "net"):
        saved, legacy.RIP_LEVEL = legacy.RIP_LEVEL, level
        log = []
        real = legacy.LegacyRouter._rip

        def spy(self, c, _real=real, _log=log):
            _log.append((c.pa.name, c.pb.name))
            _real(self, c)
        legacy.LegacyRouter._rip = spy
        try:
            ctx = make(synth(pads, walls=WALLS), plan)
            rep = legacy.route_all(ctx, Budget(10_000_000))
        finally:
            legacy.LegacyRouter._rip = real
            legacy.RIP_LEVEL = saved
        assert rep.failed == 0, rep.data["failed"]
        assert rep.data["stats"]["ripups"] >= 1 and rep.score.vias == 0
        assert_legal(ctx)
        if level == "conn":
            assert log == [("A2.1", "A1.1")], log                   # the crossed connection, not the net
        else:
            assert sorted(log) == [("A2.1", "A1.1"), ("A3.1", "A2.1")], log     # v1: the whole net


def test_ripping_a_trunk_takes_the_connections_that_teed_onto_it():
    """Connection-level rip must not leave copper dangling: what teed onto the ripped track goes with it."""
    # N1 - N2 is a long trunk along y = 5; N3 sits below its middle and tees onto it
    pads = [("N1", "N", 3, 5), ("N2", "N", 17, 5), ("N3", "N", 10, 8), ("M1", "M", 3, 12), ("M2", "M", 17, 12)]
    ctx = make(synth(pads), {"global": {"layers": ["F.Cu"]}})

    def t(name):
        p = next(p for p in ctx.board.pads if p.name == name)
        return Terminal(x=p.x, y=p.y, layers=tuple(p.layers), pad=name, cluster=p.cluster)
    m = next(c for c in ctx.conns if c.net == "M")
    ctx.conns[:] = [ConnV2(idx=0, net="N", a=t("N1.1"), b=t("N2.1"), est=14.0, width=0.2, clearance=0.2, via=(0.6, 0.3)),
                    ConnV2(idx=1, net="N", a=t("N3.1"), b=t("N2.1"), est=8.2, width=0.2, clearance=0.2, via=(0.6, 0.3)),
                    ConnV2(idx=2, net="M", a=m.a, b=m.b, est=m.est, width=0.2, clearance=0.2, via=(0.6, 0.3))]
    ctx.cfg.nets["N"].rank = -1                 # N first, in idx order (no rank given: v1's key, est then idx)
    ctx.conns[0].est = 1.0
    r = legacy.LegacyRouter(ctx_with_grid(ctx), list(ctx.conns), work=10_000_000)
    tok = ctx.state.begin()
    r.run()
    st = ctx.state
    trunk = next(c for c in r.conns if {c.pa.name, c.pb.name} == {"N1.1", "N2.1"})
    tee = next(c for c in r.conns if "N3.1" in (c.pa.name, c.pb.name))
    assert trunk.state == tee.state == "routed" and st.connected(tee.v2.idx, loose=True)
    # the tee really ends on the trunk, not on a pad
    ends = [(p.x1, p.y1) for p in st.pieces(conn=tee.v2.idx)] + [(p.x2, p.y2) for p in st.pieces(conn=tee.v2.idx)]
    assert any(abs(y - 5.0) < 1e-6 and 3.5 < x < 16.5 for x, y in ends), ends
    ripped = r._rip_conns([trunk])
    assert ripped == [trunk, tee] and r.stats["dependent_rips"] == 1
    assert st.pieces(net="N") == [] and trunk.state == tee.state == "pending"
    assert all(c.state == "routed" for c in r.conns if c.net == "M")
    # and both can be routed again from the rebuilt components
    assert r.route_conn(trunk) and r.route_conn(tee)
    assert st.connected(trunk.v2.idx, loose=True) and st.connected(tee.v2.idx, loose=True)
    st.rollback(tok)
    assert len(st) == 0


def test_open_terminal_reaches_the_pour():
    # GND has a filled island on B.Cu around pad G2 (THT, far away); pad G1 (SMD, F.Cu) drops a via into it
    zone = {"net": "G", "layers": [1], "name": "P", "filled": True, "polys": [{"o": [[8, 1], [39, 1], [39, 14], [8, 14]]}],
            "fills": {"1": [{"o": [[8, 1], [39, 1], [39, 14], [8, 14]]}]}}
    ex = synth([("G1", "G", 10, 7.5), ("G2", "G", 38, 7.5, "tht"), ("S1", "S", 3, 3), ("S2", "S", 3, 12)],
               size=(40.0, 15.0), zones=[zone])
    ctx = make(ex, None, mode="all")
    g = [c for c in ctx.conns if c.net == "G"]
    assert len(g) == 1 and g[0].role == "plane" and PS.is_open(g[0].b) and g[0].a.pad == "G1.1"
    assert (g[0].b.x, g[0].b.y) == (38, 7.5)                    # the aim: the nearest other pad of the net
    rep = legacy.route_all(ctx, Budget(10_000_000))
    assert rep.failed == 0, rep.data["failed"]
    st = ctx.state
    assert st.connected(g[0].idx, loose=True) and rep.data["not_connected"] == []
    assert sum(1 for p in st.pieces(net="G") if p.kind == "via") == 1
    assert sum(p.length for p in st.pieces(net="G")) < 3.0      # a short stub and a via, not a track to G2
    assert_legal(ctx)
    # the same copper as v1's pours pass
    res = run_v1(ex, None, "pours")
    tracks, vias, _ids = st.export(only=[p.id for p in st.pieces(net="G")])
    assert r4(tracks) == res["tracks"] and r4(vias) == res["vias"]


def test_budget_failures_and_rollback_on_error():
    ctx = make(synth(CROSS))
    bud = Budget(0)                                             # v1 stops before the first search
    rep = legacy.route_all(ctx, bud)
    assert rep.routed == 0 and rep.failed == len(ctx.conns) and len(ctx.state) == 0
    assert all("time budget" in ctx.state.fail(c.idx)["reason"] for c in ctx.conns)
    # failed connections are tried again by the next call
    rep = legacy.route_all(ctx, Budget(10_000_000))
    assert rep.failed == 0 and rep.routed == len(ctx.conns)
    # an exception in the middle leaves the state exactly as it was
    ctx = make(synth(CROSS))
    st = ctx.state
    keep = st.add(net="A", conn=-1, kind="seg", layer=1, x1=1.0, y1=1.0, x2=2.0, y2=1.0, w=0.2)
    snap = st.snapshot()
    real = legacy.LegacyRouter._verify

    def boom(self):
        raise RuntimeError("boom")
    legacy.LegacyRouter._verify = boom
    try:
        legacy.route_all(ctx, Budget(10_000_000))
        raise AssertionError("the error must propagate")
    except RuntimeError:
        pass
    finally:
        legacy.LegacyRouter._verify = real
    assert st.snapshot() == snap and st.piece(keep.id) is keep and st.depth == 0
    assert not any(ctx.oracle.has_piece(i) for i in range(2, 200))


def test_unroutable_terminals_are_reported_not_guessed():
    ctx = make(synth(CROSS))
    b = ctx.board
    a1 = next(p for p in b.pads if p.name == "A1.1")
    ctx.conns.append(ConnV2(idx=90, net="A", a=Terminal(x=1.0, y=1.0, layers=(0,)),
                            b=Terminal(x=a1.x, y=a1.y, layers=(0,), pad="A1.1", cluster=a1.cluster)))
    ctx.conns.append(ConnV2(idx=91, net="A", a=Terminal(x=a1.x, y=a1.y, layers=(0,), pad="A1.1", cluster=a1.cluster),
                            b=Terminal(x=a1.x, y=a1.y, layers=(0,), pad="A1.1", cluster=a1.cluster)))
    rep = legacy.route_all(ctx, Budget(10_000_000))
    st = ctx.state
    assert st.status(90) == "failed" and "pad cluster" in st.fail(90)["reason"]
    assert st.status(91) == "routed" and st.pieces(conn=91) == []     # both ends are one cluster already
    assert rep.failed == 1 and rep.routed == len(ctx.conns) - 1


def test_short_odd_angle_segments_are_emitted_on_the_45_degree_directions():
    """v1's snap from a grid point onto an off-grid pad centre is an odd-angle stub. It is emitted as two segments
    on the 45-degree directions when the oracle accepts one posture, and left alone otherwise."""
    import math
    from types import SimpleNamespace

    def octi(legal, *seg):
        calls = []

        def seg_legal(lay, x1, y1, x2, y2, w, net, clearance=None):
            calls.append((x1, y1, x2, y2))
            return legal(len(calls))
        me = SimpleNamespace(oracle=SimpleNamespace(seg_legal=seg_legal))
        return legacy.LegacyRouter._octi(me, 0, *seg, 0.2, "A", 0.2), calls

    def octilinear(x1, y1, x2, y2):
        a = math.degrees(math.atan2(abs(y2 - y1), abs(x2 - x1))) % 45.0
        return min(a, 45.0 - a) < 0.01
    out, _c = octi(lambda n: True, 10.0, 5.0, 10.1, 5.05)
    assert len(out) == 2 and out[0][:2] == (10.0, 5.0) and out[1][2:] == (10.1, 5.05) and out[0][2:] == out[1][:2]
    assert all(octilinear(*sg) for sg in out)
    out2, calls = octi(lambda n: n > 1, 10.0, 5.0, 10.1, 5.05)          # first posture illegal: the other one
    assert len(out2) == 2 and out2 != out and all(octilinear(*sg) for sg in out2)
    assert octi(lambda n: False, 10.0, 5.0, 10.1, 5.05)[0] == [(10.0, 5.0, 10.1, 5.05)]      # no legal posture
    assert octi(lambda n: True, 10.0, 5.0, 12.0, 6.0)[0] == [(10.0, 5.0, 12.0, 6.0)]         # long: not a snap
    for seg in ((10.0, 5.0, 10.3, 5.3), (10.0, 5.0, 10.3, 5.0), (10.0, 5.0, 10.0, 4.7)):     # already octilinear
        out, calls = octi(lambda n: True, *seg)
        assert out == [seg] and not calls
    saved, legacy.OCTI = legacy.OCTI, False
    try:
        assert octi(lambda n: True, 10.0, 5.0, 10.1, 5.05)[0] == [(10.0, 5.0, 10.1, 5.05)]
    finally:
        legacy.OCTI = saved


def test_same_twice():
    def run():
        ctx = make(synth(GAP, walls=WALLS), {"order": ["A", "B"], "global": {"layers": ["F.Cu"]}})
        rep = legacy.route_all(ctx, Budget(10_000_000))
        return ctx.state.export(), rep.work, rep.score.key(), rep.data["stats"]
    assert run() == run()


# ── corpus boards: legacy + kio + KiCad as the judge ──────────────────────
def have_kicad() -> bool:
    return common.KICAD_PY.exists() and common.KICAD_CLI.exists()


def corpus_run(name, budget_s=None, pours=True, verbose=True):
    """Signals with legacy.route_all, commit with kio, pours pass on the refilled board, DRC. No repair loop:
    this is W1's parts only (the engine and the judge are W2's). The code is bench/route/legacy_probe.py."""
    import legacy_probe
    out = legacy_probe.run(name, budget_s=budget_s, pours=pours, tag="test_v2_legacy")
    if verbose:
        print("    " + json.dumps(out))
    return out


def test_corpus_ecc83_routes_clean():
    if not have_kicad() or not (common.CORPUS / "ecc83").exists():
        print("    (skipped: KiCad or the benchmark corpus not found)")
        return
    out = corpus_run("ecc83")
    assert out["signals"]["failed"] == 0 and out["illegal_pieces"] == 0 and out["signals"]["not_connected"] == 0
    assert out["unconnected_after"] == 0 and out["new_errors"] == [] and out["routed_but_open_nets"] == []


def test_corpus_one_air_max_loses_no_net_to_the_self_check():
    """Baseline (v1): 75 connections lost to two segments, 86.4 % done. Here: no connection may be reported as
    'self-check failed', nothing illegal may be left in the state, and KiCad must agree with 'routed'."""
    if not FULL:
        print("    (skipped: needs --full)")
        return
    if not have_kicad() or not (common.CORPUS / "one_air_max").exists():
        print("    (skipped: KiCad or the benchmark corpus not found)")
        return
    out = corpus_run("one_air_max")
    assert out["illegal_pieces"] == 0
    assert out["signals"]["self_check"] == 0 and all(p["self_check"] == 0 for p in out.get("pours", []))
    assert all(p["illegal_pieces"] == 0 for p in out.get("pours", []))
    assert out["done_pct"] > 86.4, out


if __name__ == "__main__":
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t:.1f}s)")
            except Exception:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"{'FAILED' if fails else 'OK'}: {fails} failure(s)")
    sys.exit(1 if fails else 0)
