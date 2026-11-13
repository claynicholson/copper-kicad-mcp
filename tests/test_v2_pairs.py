"""Router v2 differential pairs, width rules and keep-short nets (design §14.8, v2/pairs.py; step C).

Unit tests run the real pair router / oracle / route state / detail search on synthetic boards (no KiCad): lane
offsetting, which connections form a pair, the fan-in, a straight pair, a bend, a layer change through two vias,
the fallbacks (swapped pads, a fenced-in fan-in) that must still route both nets, names KiCad does not read as a
pair, skew bumps, rip-up as one object, the plan's forbid regions, determinism; per-layer widths, the plan's
max_length contract, keep-short ordering, class-width necks in the report. The end-to-end tests route the USB
pair board and the two-pairs-cross board through ``guided.session.route`` with KiCad 10 and ask KiCad's own DRC
— the boards' diff-pair gap / uncoupled / skew rules included — about the written files (skipped without KiCad
/ the corpus).

Run:  python tests/test_v2_pairs.py             all
      python tests/test_v2_pairs.py --pure      no KiCad
      python tests/test_v2_pairs.py test_name   one test
"""
from __future__ import annotations

import math
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench" / "route"))
sys.path.insert(0, str(ROOT / "tests"))
import common  # noqa: E402  (bench/route/common.py)

common.sandbox()       # KICAD_CONFIG_HOME, COPPER_HOME and the temp dir -> bench/route/.work, before copper_kicad loads
import test_v2_constraints as TC  # noqa: E402  (the synthetic board builder)
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import geom, plan as P  # noqa: E402
from copper_kicad.pcb.v2 import classify, constraints as C, detail, engine, judge, oracle as O, pairs, pieces  # noqa: E402
from copper_kicad.pcb.v2 import polish, repair, tune  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Ctx, Terminal  # noqa: E402

PURE = "--pure" in sys.argv
W, GAP = 0.2, 0.15
DP = {"class": "DP", "dp_gap": GAP, "dp_width": W}
CLS = {"/D_P": DP, "/D_N": DP}
PLAN = {"diff_pairs": [{"p": "/D_P", "n": "/D_N"}]}
# J1 on the west, U1 on the east, the two nets on neighbouring pads (1.2 x 0.6 mm, 1 mm apart)
STRAIGHT = [("J1", 1, "/D_P", 5, 10), ("J1", 2, "/D_N", 5, 11), ("U1", 1, "/D_P", 35, 10), ("U1", 2, "/D_N", 35, 11)]
# ... U1 turned by 90 degrees to the south: one bend, the inner lane comes out shorter
BEND = [("J1", 1, "/D_P", 5, 10), ("J1", 2, "/D_N", 5, 11), ("U1", 1, "/D_P", 31, 30, 0.3, 0.6),
        ("U1", 2, "/D_N", 30, 30, 0.3, 0.6)]
SWAPPED = [("J1", 1, "/D_P", 5, 10), ("J1", 2, "/D_N", 5, 11), ("U1", 1, "/D_N", 35, 10), ("U1", 2, "/D_P", 35, 11)]


def scene(pads, plan=PLAN, classes=CLS, tracks=(), layers=("F.Cu", "B.Cu"), dru=""):
    """-> Ctx with the real oracle and an empty route state: one connection per two-pad net, nothing routed."""
    b = TC.board(pads, classes=classes, layers=layers, tracks=tracks)
    cfg = P.resolve(b, plan)
    cfg.x.cons = C.resolve(b, cfg, cfg.x, C.ProjectRules(rules=C.parse_dru(dru), stack=C.Stackup.default(b.L)))
    cfg.x.cons_env = {"stack": C.Stackup.default(b.L), "die": {}, "use_height": True, "tuning": {}}
    orc = O.Oracle(b)
    orc.set_plan(cfg)
    conns = []
    for n in sorted(b.nets):
        ps = b.nets[n].pads
        if len(ps) != 2:
            continue
        pa, pb = ps[0], ps[-1]
        nc = cfg.nets.get(n)
        w = nc.width if nc is not None else W
        conns.append(ConnV2(idx=len(conns), net=n, a=Terminal(pa.x, pa.y, tuple(pa.layers), pad=pa.name, cluster=pa.cluster),
                            b=Terminal(pb.x, pb.y, tuple(pb.layers), pad=pb.name, cluster=pb.cluster), width=w, neck=w,
                            clearance=0.2, via=(0.6, 0.3), tree_target=False,
                            est=max(abs(pa.x - pb.x), abs(pa.y - pb.y))))
    st = pieces.RouteState(orc, conns)
    return Ctx(board=b, cfg=cfg, x=cfg.x, oracle=orc, state=st, conns=conns, history={})


def lay(ctx, budget=10 ** 7):
    js = pairs.jobs(ctx)
    assert len(js) == 1, js
    return js[0], pairs.route_pair(ctx, js[0], Budget(budget))


def copper(ctx):
    return [(p.net, p.kind, p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.stage) for p in ctx.state.pieces()]


def all_legal(ctx):
    for p in ctx.state.pieces():
        b = ctx.oracle.piece_ok(p)
        assert b is None, (p, b)


def stats(ctx, gap=GAP, p="/D_P", n="/D_N"):
    sp, _ = C.net_copper(ctx, p)
    sn, _ = C.net_copper(ctx, n)
    return C.coupled_stats(sp, sn, W, gap)


def length(ctx, net):
    return C.net_length(ctx, net).total


def min_gap(ctx, p="/D_P", n="/D_N"):
    """Smallest copper-edge distance between the tracks of the two nets on one layer."""
    best = math.inf
    for a in ctx.state.pieces(net=p):
        for b in ctx.state.pieces(net=n):
            if a.kind == "seg" and b.kind == "seg" and a.layer == b.layer:
                best = min(best, geom.seg_seg_dist(a.x1, a.y1, a.x2, a.y2, b.x1, b.y1, b.x2, b.y2) - (a.w + b.w) / 2)
    return best


# ── offset ────────────────────────────────────────────────────────────────
def _dist(a, b, c, d):
    return geom.seg_seg_dist(a[0], a[1], b[0], b[1], c[0], c[1], d[0], d[1])


def test_offset_keeps_the_distance_on_l_z_and_diagonal_lines():
    d = 0.175
    for pts in ([(0, 0), (10, 0), (10, 10)],                               # L, 90 degrees
                [(0, 0), (10, 0), (15, 5), (25, 5)],                        # Z through a diagonal
                [(0, 0), (5, 5), (5, 15), (0, 20)],                         # diagonal, straight, diagonal back
                [(0, 0), (10, 0)]):
        left, right = pairs.offset(pts, d), pairs.offset(pts, -d)
        assert left is not None and right is not None and len(left) == len(pts) == len(right)
        for k in range(len(pts) - 1):
            (ax, ay, _), (bx, by, _) = left[k], left[k + 1]
            (cx, cy, _), (ex, ey, _) = right[k], right[k + 1]
            # each lane piece is parallel to its centre piece at exactly d: the two lanes are 2d apart
            for x, y in ((ax, ay), (bx, by)):
                assert abs(geom.pt_seg_dist(x, y, cx, cy, ex, ey) - 2 * d) < 2e-6 or \
                    abs(abs((x - cx) * (ey - cy) - (y - cy) * (ex - cx)) / math.hypot(ex - cx, ey - cy) - 2 * d) < 2e-6
            e = ((pts[k + 1][0] - pts[k][0]), (pts[k + 1][1] - pts[k][1]))
            assert abs((bx - ax) * e[1] - (by - ay) * e[0]) < 1e-5              # parallel to the centre piece
        # never nearer than 2d anywhere
        for k in range(len(left) - 1):
            for j in range(len(right) - 1):
                assert _dist(left[k], left[k + 1], right[j], right[j + 1]) >= 2 * d - 2e-6


def test_offset_drops_a_piece_too_short_for_its_corners_and_refuses_a_line_that_doubles_back():
    d = 0.175
    # two 45-degree turns the same way, 0.1 mm apart: the inner lane has no room for that piece
    pts = [(0, 0), (10, 0), (10.07, 0.07), (10.07, 10)]
    inner, outer = pairs.offset(pts, d), pairs.offset(pts, -d)          # +d = left of travel = the inside of this turn?
    lens = sorted((len(inner), len(outer)))
    assert lens == [3, 4], lens                                          # one lane lost the short piece, the other kept it
    short = inner if len(inner) == 3 else outer
    long_ = outer if short is inner else inner
    for k in range(len(short) - 1):
        for j in range(len(long_) - 1):
            assert _dist(short[k], short[k + 1], long_[j], long_[j + 1]) >= 2 * d - 2e-6   # never closer
    assert pairs.offset([(0, 0), (10, 0), (5, 0)], d) is None            # doubles back
    assert pairs.offset([(0, 0)], d) is None
    # a kept vertex in the middle of a straight stays a vertex, and says so
    vs = pairs.offset([(0, 0), (5, 0), (10, 0)], d, keep={1})
    assert [v[2] for v in vs] == [False, True, False] and abs(vs[1][0] - 5) < 1e-9 and abs(abs(vs[1][1]) - d) < 1e-9


# ── which connections form a pair ─────────────────────────────────────────
def test_jobs_pair_connections_end_for_end_and_leave_the_rest_alone():
    ctx = scene(STRAIGHT)
    (j,) = pairs.jobs(ctx)
    assert {ctx.conns[j.p].net, ctx.conns[j.n].net} == {"/D_P", "/D_N"} and ctx.conns[j.p].net == "/D_P"
    assert j.swap is False and abs(j.gap - GAP) < 1e-9 and abs(j.width - W) < 1e-9
    assert set(pairs.job_map(ctx)) == {j.p, j.n}
    # coupled: false in the plan, constraints 'report' / 'off', and a plan without the pair in 'plan' mode: no job
    assert pairs.jobs(scene(STRAIGHT, {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "coupled": False}]})) == []
    for mode in ("report", "off"):
        assert pairs.jobs(scene(STRAIGHT, {"global": {"constraints": mode}, **PLAN})) == []
    assert pairs.jobs(scene(STRAIGHT, {"diff_pairs": []})) == []
    # the two nets on different parts at one end (4.5 mm apart there): not a pair's end
    far = [("J1", 1, "/D_P", 5, 10), ("J1", 2, "/D_N", 5, 11), ("U1", 1, "/D_P", 35, 10), ("U2", 1, "/D_N", 35, 14.5)]
    assert pairs.jobs(scene(far)) == []


# ── a straight pair ───────────────────────────────────────────────────────
def test_straight_pair_is_two_coupled_lanes_from_pad_breakout_to_pad_entry():
    ctx = scene(STRAIGHT)
    job, r = lay(ctx)
    assert r["ok"] and r["coupled"] and r["why"] is None
    assert ctx.state.connected(job.p) and ctx.state.connected(job.n)
    assert ctx.state.status(job.p) == ctx.state.status(job.n) == "routed"
    all_legal(ctx)
    s = stats(ctx)
    assert s["coupled_pct"] > 95 and s["gap_seen"] == GAP and max(s["uncoupled_mm"]) < 1.2, s
    assert abs(min_gap(ctx) - GAP) < 2e-6                                # never closer than the gap, and at it
    assert abs(length(ctx, "/D_P") - length(ctx, "/D_N")) < 1e-6         # mirror images
    # each track leaves its pad centre on a diagonal straight to its lane (the fan-in KiCad counts as coupled)
    for net, y in (("/D_P", 10.0), ("/D_N", 11.0)):
        first = next(p for p in ctx.state.pieces(net=net) if (p.x1, p.y1) == (5.0, y))
        assert abs(abs(first.x2 - first.x1) - abs(first.y2 - first.y1)) < 1e-6 and first.x2 > first.x1
        assert abs(abs(first.y2 - 10.5) - (W + GAP) / 2) < 1e-6
    # each lane is broken where the rung between the lanes is clear of the pads (x > 5.6 + a margin)
    xs = sorted({p.x2 for p in ctx.state.pieces(net="/D_P") if abs(p.y1 - p.y2) < 1e-9})
    assert any(5.65 <= x <= 7.0 for x in xs), xs
    assert all(p.stage == "detail" and abs(p.w - W) < 1e-9 for p in ctx.state.pieces())
    assert pairs.coupled_conns(ctx) == {job.p, job.n} and pairs.coupled_nets(ctx) == {"/D_P", "/D_N"}
    rep = pairs.report(ctx)["/D_P"]
    assert rep["coupled"] == 1 and rep["of"] == 1 and rep["why"] is None


def test_a_refused_pair_leaves_nothing_behind():
    """One transaction: when the lanes cannot be added the state is exactly what it was."""
    # a foreign track across the board on BOTH layers' way: no path at all for the pair on F.Cu, vias not allowed
    wall = [["/X", 0, 20.0, 1.0, 20.0, 59.0, 0.3, 1, False], ["/X", 1, 20.0, 1.0, 20.0, 59.0, 0.3, 2, False]]
    pads = STRAIGHT + [("T1", 1, "/X", 20, 0.8), ("T2", 1, "/X", 20, 59.2)]
    ctx = scene(pads, tracks=wall)
    before = copper(ctx)
    job, r = lay(ctx, budget=3 * 10 ** 6)
    assert not r["ok"] and r["why"] and copper(ctx) == before == []
    assert ctx.state.status(job.p) != "routed" and ctx.state.status(job.n) != "routed"
    assert pairs.report(ctx)["/D_P"]["coupled"] == 0 and "path" in pairs.report(ctx)["/D_P"]["why"]


# ── a bend, and the skew it costs ─────────────────────────────────────────
def test_bend_pair_stays_at_its_gap_and_gets_skew_bumps_on_the_outer_side_of_the_shorter_net():
    plain = scene(BEND)                                                   # no skew limit: laid, measured, no bump
    _job, r = lay(plain)
    assert r["ok"]
    all_legal(plain)
    skew0 = abs(length(plain, "/D_P") - length(plain, "/D_N"))
    assert 0.3 < skew0 < 1.5, skew0                                       # the inner lane of the turn is shorter
    assert not [p for p in plain.state.pieces() if p.stage == "tune"]
    assert abs(min_gap(plain) - GAP) < 2e-6

    ctx = scene(BEND, {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "skew": 0.2}]})
    job, r = lay(ctx)
    assert r["ok"]
    all_legal(ctx)
    lp, ln = length(ctx, "/D_P"), length(ctx, "/D_N")
    assert abs(lp - ln) <= 0.2 + 1e-9, (lp, ln)
    short = "/D_N" if length(plain, "/D_N") < length(plain, "/D_P") else "/D_P"
    bumps = [p for p in ctx.state.pieces() if p.stage == "tune"]
    assert bumps and {p.net for p in bumps} == {short}
    assert max(lp, ln) == max(length(plain, "/D_P"), length(plain, "/D_N"))          # the longer net is untouched
    assert min_gap(ctx) >= GAP - 2e-6                                     # a bump never closes on the partner
    rec = ctx.history["tune"]["nets"][short]
    assert rec["group"] == "pair:/D_P" and rec["bumps"] >= 1 and "short_by" not in rec
    assert abs(rec["added"] - (rec["after"] - rec["before"])) < 1e-4
    # no bump is higher than the limit, so the flat of a bump stays near its partner
    other = "/D_P" if short == "/D_N" else "/D_N"
    for p in bumps:
        d = min(geom.seg_seg_dist(p.x1, p.y1, p.x2, p.y2, o.x1, o.y1, o.x2, o.y2)
                for o in ctx.state.pieces(net=other) if o.kind == "seg")
        assert d <= W + GAP + tune.SKEW_H + 1e-6, (p, d)
    # the report: skew inside the limit, the bumps counted, uncoupled length given
    row = C.measure(ctx)["pairs"][0]
    assert row["ok"] is True and row["skew"] <= 0.2 and row["tuned"] == rec["bumps"] and row["added"] > 0
    assert row["lanes"] is True and row["coupled"] >= 80 and row["uncoupled"] > 0
    # the judged pass later finds nothing left to do, and does not touch the bumps
    before = copper(ctx)
    rep = tune.run(ctx, Budget(10 ** 7))
    assert rep.data["changed"] == [] and copper(ctx) == before


def test_skew_that_cannot_be_met_is_reported_with_its_amount():
    ctx = scene(BEND, {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "skew": 0.05}]})
    job, r = lay(ctx)
    assert r["ok"]
    short = min(("/D_P", "/D_N"), key=lambda n: length(ctx, n))
    # take the bumps out again and close every place a bump could go: the tuner must say what is missing
    tune.untune(ctx, [short], avoid=[(0.0, 0.0, 60.0, 60.0)])
    skew = abs(length(ctx, "/D_P") - length(ctx, "/D_N"))
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"][short]
    assert rec["bumps"] == 0 and abs(rec["short_by"] - (skew - 0.05)) < 1e-3 and "no free straight run" in rec["why"]
    assert rep.failed == 1
    row = C.measure(ctx)["pairs"][0]
    assert row["ok"] is False and abs(row["skew_over"] - (skew - 0.05)) < 2e-3 and "skew_why" in row
    assert C.measure(ctx)["missed"] == 1


def test_a_skew_limit_too_tight_to_meet_never_ends_worse_than_a_looser_one():
    """Fix round 1: with diff_pairs[].skew = 0.005 the aim lay inside the tuner's own guard below the longer net;
    the three bumps that fit were all taken out again ("would have made the net the longer one") and the pair
    ended at 0.39 mm where a limit of 0.15 gives 0.05 — with a KiCad skew error against the board's own rule.
    Whatever the limit, the bumps that fit stay, the shorter net stays the shorter one, and a miss says by how
    much."""
    plain = scene(BEND)
    lay(plain)
    skew0 = abs(length(plain, "/D_P") - length(plain, "/D_N"))
    short = min(("/D_P", "/D_N"), key=lambda n: length(plain, n))
    long_ = "/D_P" if short == "/D_N" else "/D_N"
    seen = {}
    for lim in (0.2, 0.05, 0.01, 0.005, 0.002, 0.0005):
        if lim >= 0.005:                                              # the plan's own range ends at 0.005 mm
            ctx = scene(BEND, {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "skew": lim}]})
        else:                                                         # ... a KiCad rule may ask for less
            ctx = scene(BEND, dru=f"(rule s (condition \"A.inDiffPair('/D_')\") (constraint skew (max {lim}mm)))")
            assert ctx.x.cons.pairs[0].skew == lim and ctx.x.cons.pairs[0].src == "dru"
        job, r = lay(ctx)
        assert r["ok"]
        all_legal(ctx)
        ls, ll = length(ctx, short), length(ctx, long_)
        skew = ll - ls
        seen[lim] = skew
        assert 0.0 <= skew < skew0 and [p for p in ctx.state.pieces() if p.stage == "tune"], (lim, skew)
        assert abs(ll - length(plain, long_)) < 1e-9                  # the longer net is untouched
        assert skew <= max(lim, 1.5 * tune.GUARD) + 1e-6, (lim, skew) # inside the limit, or at the tuner's guard
        row = C.measure(ctx)["pairs"][0]
        rec = ctx.history["tune"]["nets"][short]
        assert rec["bumps"] >= 1 and row["tuned"] == rec["bumps"] and "undone" not in str(rec.get("why"))
        if skew <= lim + 1e-9:
            assert row["ok"] is True and "skew_over" not in row, (lim, row)
        else:
            assert row["ok"] is False and abs(row["skew_over"] - (skew - lim)) < 1.1e-3, (lim, row)
        # the judged pass later leaves the bumps where they are
        before = copper(ctx)
        tune.run(ctx, Budget(10 ** 7))
        assert copper(ctx) == before
    assert all(seen[a] >= seen[b] - 1e-6 for a, b in zip(list(seen), list(seen)[1:])), seen   # tighter is never worse


# ── a layer change ────────────────────────────────────────────────────────
VIA_WALL = [["/X", 0, 20.0, 1.0, 20.0, 59.0, 0.3, 1, False]]
VIA_PADS = STRAIGHT + [("T1", 1, "/X", 20, 0.8), ("T2", 1, "/X", 20, 59.2)]


def test_pair_changes_layer_through_two_vias_side_by_side():
    ctx = scene(VIA_PADS, tracks=VIA_WALL)                                # a wall on F.Cu from edge to edge
    job, r = lay(ctx)
    assert r["ok"], r
    all_legal(ctx)
    assert ctx.state.connected(job.p) and ctx.state.connected(job.n)
    vp = [p for p in ctx.state.pieces(net="/D_P") if p.kind == "via"]
    vn = [p for p in ctx.state.pieces(net="/D_N") if p.kind == "via"]
    assert len(vp) == len(vn) == 2                                        # down and up again, as a pair each time
    for a in vp:
        b = min(vn, key=lambda v: math.hypot(v.x1 - a.x1, v.y1 - a.y1))
        d = math.hypot(b.x1 - a.x1, b.y1 - a.y1)
        assert 0.6 + 0.25 - 1e-6 <= d <= 1.2, d                           # via + via gap apart, side by side
        assert abs(a.x1 - b.x1) < 1e-6                                    # across the direction of travel (east)
    # lanes on both layers, and on B.Cu they are a pair as well
    for lay_ in (0, 1):
        sp = [(p.layer, p.x1, p.y1, p.x2, p.y2, p.w) for p in ctx.state.pieces(net="/D_P") if p.kind == "seg" and p.layer == lay_]
        sn = [(p.layer, p.x1, p.y1, p.x2, p.y2, p.w) for p in ctx.state.pieces(net="/D_N") if p.kind == "seg" and p.layer == lay_]
        assert sp and sn
        assert C.coupled_stats(sp, sn, W, GAP)["coupled_pct"] > 40, lay_
    assert stats(ctx)["coupled_pct"] > 75
    assert abs(length(ctx, "/D_P") - length(ctx, "/D_N")) < 1e-6
    row = C.measure(ctx)["pairs"][0]
    assert row["vias"] == [2, 2] and row["lanes"] is True


# ── fallbacks: the pair still routes, as two nets, and says why ───────────
def route_all(ctx, budget=2 * 10 ** 7):
    return detail.run(ctx, Budget(budget))


def pair_counts(rep):
    return {k: rep.data["pairs"][k] for k in ("coupled", "sequential")}


def test_swapped_pads_fall_back_to_two_nets_and_both_route():
    ctx = scene(SWAPPED)
    job, r = lay(ctx)
    assert not r["ok"] and "swapped" in r["why"] and copper(ctx) == []
    rep = route_all(ctx)
    assert rep.failed == 0 and ctx.state.connected(job.p) and ctx.state.connected(job.n)
    all_legal(ctx)
    assert pair_counts(rep) == {"coupled": 0, "sequential": 1}
    assert pairs.coupled_conns(ctx) == set()
    row = C.measure(ctx)["pairs"][0]
    assert row["lanes"] is False and "swapped" in row["why"] and row["coupled"] < 50
    assert row["ok"] is None                                              # no limit declared: information, not a pass


def test_fenced_in_fan_in_falls_back_to_two_nets_and_both_route():
    # foreign pads right in front of J1's two pads, with room for one track each side but not for two lanes
    block = [("B1", 1, "/X", 6.6, 10.5, 0.2, 0.2), ("B2", 1, "/X", 9.0, 10.5, 0.2, 0.2)]
    ctx = scene(STRAIGHT + block)
    job, r = lay(ctx)
    if r["ok"]:                                                           # lanes found a legal way round: still a pair
        all_legal(ctx)
        return
    assert r["why"] and copper(ctx) == []
    rep = route_all(ctx)
    assert ctx.state.connected(job.p) and ctx.state.connected(job.n) and rep.failed <= 1
    all_legal(ctx)
    assert C.measure(ctx)["pairs"][0]["lanes"] is False


def test_detail_run_lays_the_pair_whichever_member_comes_first_and_is_deterministic():
    a, b = scene(BEND, {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "skew": 0.2}]}), \
        scene(BEND, {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "skew": 0.2}]})
    ra, rb = route_all(a), route_all(b)
    assert ra.failed == rb.failed == 0 and pair_counts(ra) == {"coupled": 1, "sequential": 0}
    assert copper(a) == copper(b) and copper(a)
    assert stats(a)["coupled_pct"] > 80
    # routing again is a no-op (both are routed: nothing to do, nothing laid twice)
    before = copper(a)
    assert route_all(a).failed == 0 and copper(a) == before


def test_names_kicad_does_not_read_as_a_pair_are_laid_at_the_clearance_and_say_so():
    pads = [(r_, n_, {"/D_P": "/USB_DP", "/D_N": "/USB_DM"}[net], x, y) for r_, n_, net, x, y in STRAIGHT]
    cls = {"/USB_DP": DP, "/USB_DM": DP}
    ctx = scene(pads, {"diff_pairs": [{"p": "/USB_DP", "n": "/USB_DM", "gap": GAP}]}, classes=cls)
    (job,) = pairs.jobs(ctx)
    assert job.pair.kicad is False and abs(job.gap - 0.2) < 1e-9          # the ordinary clearance, not the pair gap
    r = pairs.route_pair(ctx, job, Budget(10 ** 7))
    assert r["ok"]
    all_legal(ctx)
    assert abs(min_gap(ctx, "/USB_DP", "/USB_DM") - 0.2) < 2e-6
    row = C.measure(ctx)["pairs"][0]
    assert row["kicad"] is False and row["gap"] == GAP and row["gap_seen"] == 0.2 and row["coupled"] > 90


# ── one object: ripped together, protected, left alone by polish ──────────
def test_a_ripped_lane_takes_its_partner_and_repair_never_rips_a_pair_for_another_net():
    ctx = scene(BEND)
    job, r = lay(ctx)
    assert r["ok"]
    assert repair._net_protected(ctx, "/D_P") and repair._net_protected(ctx, "/D_N")
    assert polish._coupled(ctx) == {job.p, job.n}
    # the judge removes one lane (a DRC marker on it): engine.reroute's first step takes the partner out too
    ctx.state.rip_conn(job.p)
    ids = pairs.with_partners(ctx, [job.p])
    assert sorted(ids) == sorted([job.p, job.n])
    assert not ctx.state.pieces(net="/D_P") and not ctx.state.pieces(net="/D_N")
    assert ctx.state.status(job.n) == "todo" and pairs.coupled_conns(ctx) == set()
    assert not repair._net_protected(ctx, "/D_P")
    # ... and the two are laid together again
    rep = detail.run(ctx, Budget(2 * 10 ** 7), only=ids)
    assert rep.failed == 0 and pair_counts(rep) == {"coupled": 1, "sequential": 0}
    assert pairs.coupled_conns(ctx) == {job.p, job.n}
    all_legal(ctx)
    # a connection that is no lane is not touched by with_partners
    assert pairs.with_partners(ctx, []) == []


def test_no_lane_enters_a_forbid_region():
    """The plan's forbidden areas are rules of the oracle: a pair goes round them or is not laid as lanes; no
    piece of either net ever lies inside one."""
    rect = [15.0, 6.0, 25.0, 13.0]                                        # across the straight line between the parts
    plan = {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "skew": 0.2}],
            "regions": [{"rect": rect, "forbid": True, "name": "keepout"}]}
    ctx = scene(STRAIGHT, plan)
    rep = route_all(ctx)
    assert rep.failed == 0
    all_legal(ctx)
    assert ctx.state.pieces()
    for p in ctx.state.pieces():
        assert ctx.oracle.plan_hit(p.net, p.layer if p.kind == "seg" else None, p.x1, p.y1, p.x2, p.y2) is None, p
        for x, y in ((p.x1, p.y1), (p.x2, p.y2)):
            assert not (rect[0] < x < rect[2] and rect[1] < y < rect[3]), p
    assert pair_counts(rep)["coupled"] == 1 and stats(ctx)["coupled_pct"] > 70


# ── width rules ───────────────────────────────────────────────────────────
WPADS = [("J1", 1, "/CLK", 5, 10), ("U1", 1, "/CLK", 35, 10), ("J1", 2, "/S", 5, 20), ("U1", 2, "/S", 35, 20)]


def test_per_layer_widths_search_at_the_widest_and_lay_each_layers_own_never_necked():
    plan = {"nets": {"/CLK": {"widths": {"F.Cu": 0.15, "B.Cu": 0.3}, "z": 50}}}
    b = TC.board(WPADS)
    cfg = P.resolve(b, plan)
    cfg.x.cons = C.resolve(b, cfg, cfg.x, C.ProjectRules(stack=C.Stackup.default(b.L)))
    rule = cfg.x.cons.widths["/CLK"]
    assert rule.neck is False and dict(rule.by_layer) == {0: 0.15, 1: 0.3}
    table = classify.net_table(b, cfg, cfg.x)
    assert table["/CLK"]["width"] == 0.3 and table["/CLK"]["neck"] == 0.3          # the widest layer; never thinner
    assert table["/S"]["width"] == 0.2
    # constraints off / report: the rule changes nothing in the copper
    for mode in ("off", "report"):
        c2 = P.resolve(b, {"global": {"constraints": mode}, **plan})
        c2.x.cons = C.resolve(b, c2, c2.x, C.ProjectRules(stack=C.Stackup.default(b.L)))
        assert classify.net_table(b, c2, c2.x)["/CLK"]["width"] == 0.2
    ctx = scene(WPADS, plan, classes=None)
    clk = next(c for c in ctx.conns if c.net == "/CLK")
    clk.width = clk.neck = 0.3
    assert detail.layer_width(ctx, clk, 0, 0.3) == 0.15 and detail.layer_width(ctx, clk, 1, 0.3) == 0.3
    assert detail.layer_width(ctx, clk, 0, 0.2) == 0.2                    # not the full-width rung: untouched
    other = next(c for c in ctx.conns if c.net == "/S")
    assert detail.layer_width(ctx, other, 0, other.width) == other.width
    rep = route_all(ctx)
    assert rep.failed == 0
    segs = [p for p in ctx.state.pieces(net="/CLK") if p.kind == "seg"]
    assert segs and all(abs(p.w - 0.15) < 1e-9 and abs(p.target_w - 0.15) < 1e-9 for p in segs if p.layer == 0)
    all_legal(ctx)
    w = C.measure(ctx)["widths"]
    assert w["n"] == 1 and w["off"] == []
    # a segment at another width is a miss with its mm
    p = segs[0]
    ctx.state.remove(p.id)
    ctx.state.add(net=p.net, conn=p.conn, kind="seg", layer=p.layer, x1=p.x1, y1=p.y1, x2=p.x2, y2=p.y2, w=0.25)
    m = C.measure(ctx)
    assert m["widths"]["off"] and m["widths"]["off"][0][:4] == ["/CLK", "F.Cu", 0.25, 0.15] and m["missed"] == 1


def test_class_width_may_neck_at_a_pad_and_only_there():
    pads = [("J1", 1, "/DRV", 5, 10), ("U1", 1, "/DRV", 35, 10)]
    b = TC.board(pads, classes={"/DRV": {"class": "WIDE", "width": 0.5}})
    cfg = P.resolve(b, None)
    cons = C.resolve(b, cfg, cfg.x, C.ProjectRules(stack=C.Stackup.default(b.L)))
    rule = cons.widths["/DRV"]
    assert rule.src == "class" and rule.neck is True and rule.width == 0.5
    assert classify.net_table(b, cfg, types.SimpleNamespace(cons=cons))["/DRV"]["width"] == 0.5

    def report(segs):
        st = TC.State(TC.seg("/DRV", [s[:2] for s in segs][:1] + [s[1] for s in segs], w=0.5))
        st._p = [type(st._p[0])(id=i + 1, net="/DRV", conn=-1, kind="seg", layer=0, x1=a[0], y1=a[1], x2=c[0], y2=c[1], w=w)
                 for i, (a, c, w) in enumerate(segs)]
        ctx = types.SimpleNamespace(board=b, state=st, conns=[], history={}, x=types.SimpleNamespace(cons=cons, cons_env={}))
        return C.measure(ctx, cons=cons)
    # thin for the last 2 mm into the pad: a neck, reported as such, not a miss
    m = report([((5, 10), (33, 10), 0.5), ((33, 10), (35, 10), 0.2)])
    assert m["widths"]["off"] == [] and m["widths"]["necked"] == 2.0 and m["missed"] == 0
    # thin in the middle of the board: listed (as a class row), still not a broken rule of the plan
    m = report([((5, 10), (15, 10), 0.5), ((15, 10), (25, 10), 0.2), ((25, 10), (35, 10), 0.5)])
    assert m["widths"]["off"] == [["/DRV", "F.Cu", 0.2, 0.5, 10.0, "class"]] and m["missed"] == 0


# ── keep-short ────────────────────────────────────────────────────────────
def test_keep_short_nets_are_ordered_with_the_critical_ones_and_report_their_length():
    pads = [("J1", 1, "/A", 5, 10), ("U1", 1, "/A", 35, 10), ("J1", 2, "/B", 5, 20), ("U1", 2, "/B", 25, 20),
            ("J1", 3, "/ANT", 5, 30), ("U1", 3, "/ANT", 45, 30)]
    b = TC.board(pads)
    for plan, short in (({"nets": {"/A": {"max_length": 40}}}, {"/A", "/ANT"}),
                        ({"global": {"constraints": "off"}, "nets": {"/A": {"max_length": 40}}}, set())):
        cfg = P.resolve(b, plan)
        cfg.x.cons = C.resolve(b, cfg, cfg.x, C.ProjectRules(stack=C.Stackup.default(b.L)))
        assert set(cfg.x.cons.short) == short
        conns = classify.connections(b, cfg, cfg.x, {})
        order = [c.net for c in sorted(conns, key=lambda c: c.rank)]
        if short:
            assert set(order[:2]) == {"/A", "/ANT"} and order[2] == "/B", order      # before the shorter plain net
            assert cfg.x.cons.short["/A"].hard and cfg.x.cons.short["/A"].max_len == 40
            assert cfg.x.cons.short["/ANT"].max_len is None
        else:
            assert order.index("/B") < order.index("/A"), order                      # by estimate, as before §14
    # the report: length against the maximum; a soft keep-short net that took a detour says by how much
    ctx = scene(pads, {"nets": {"/A": {"max_length": 40}}}, classes=None)
    for c in ctx.conns:
        pts = [(c.a.x, c.a.y), (c.b.x, c.b.y)] if c.net != "/ANT" else [(5, 30), (5, 50), (45, 50), (45, 30)]
        for p0, p1 in zip(pts, pts[1:]):
            ctx.state.add(net=c.net, conn=c.idx, kind="seg", layer=0, x1=p0[0], y1=p0[1], x2=p1[0], y2=p1[1], w=W)
        ctx.state.set_status(c.idx, "routed")
    rows = {r_["net"]: r_ for r_ in C.measure(ctx)["short"]}
    assert rows["/A"]["ok"] is True and rows["/A"]["max"] == 40 and rows["/A"]["len"] == 30.0
    assert rows["/ANT"]["detour"] == 40.0 and "ok" not in rows["/ANT"]


def test_plan_max_length_is_a_contract_a_longer_net_is_failed_with_the_excess_and_not_kept():
    pads = [("J1", 1, "/A", 5, 10), ("U1", 1, "/A", 35, 10), ("J1", 2, "/B", 5, 20), ("U1", 2, "/B", 35, 20)]
    ctx = scene(pads, {"nets": {"/A": {"max_length": 32}, "/B": {"max_length": 80}}}, classes=None)
    for c in ctx.conns:                                                   # both routed with a 10 mm detour: 50 mm
        for p0, p1 in zip([(5, c.a.y), (5, c.a.y + 5), (35, c.a.y + 5), (35, c.a.y)][:-1],
                          [(5, c.a.y), (5, c.a.y + 5), (35, c.a.y + 5), (35, c.a.y)][1:]):
            ctx.state.add(net=c.net, conn=c.idx, kind="seg", layer=0, x1=p0[0], y1=p0[1], x2=p1[0], y2=p1[1], w=W)
        ctx.state.set_status(c.idx, "routed")
        assert ctx.state.connected(c.idx)
    run = types.SimpleNamespace(ctx=ctx, x=ctx.x, cfg=ctx.cfg, failed_seen={}, notes=[])
    cut = engine._Run.enforce_max_length(run)
    a = next(c for c in ctx.conns if c.net == "/A")
    bconn = next(c for c in ctx.conns if c.net == "/B")
    assert cut == 1 and ctx.state.status(a.idx) == "failed" and not ctx.state.pieces(net="/A")
    f = judge.fail_of(ctx.state, a.idx)
    assert f["code"] == "max_length" and abs(f["over_mm"] - 8.0) < 0.2 and "max_length=32" in f["reason"]
    assert ctx.state.status(bconn.idx) == "routed" and ctx.state.pieces(net="/B")      # within its 80 mm: kept
    assert run.notes and list(run.failed_seen.values())[0]["code"] == "max_length"
    # constraints 'off': nothing is enforced
    ctx2 = scene(pads, {"global": {"constraints": "off"}, "nets": {"/A": {"max_length": 32}}}, classes=None)
    run2 = types.SimpleNamespace(ctx=ctx2, x=ctx2.x, cfg=ctx2.cfg, failed_seen={}, notes=[])
    assert engine._Run.enforce_max_length(run2) == 0


# ── the judged pass: skew bumps never cost the pair its KiCad pair rules ──
def test_pair_rule_markers_that_appear_with_the_bumps_are_blamed_on_them():
    ctx = scene(BEND, {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "skew": 0.2}]})
    _job, r = lay(ctx)
    assert r["ok"]
    short = next(iter(ctx.history["tune"]["edits"]))
    acc = types.SimpleNamespace(chk={"cons": {"nets": {}}})
    chk = {"cons": {"nets": {"/D_P": ["diff_pair_uncoupled_length_too_long"], "/D_N": ["skew_out_of_range"]}}}
    assert judge._pair_rules_worse(ctx, chk, acc, tune) == {short: "diff_pair_uncoupled_length_too_long"}
    # the same marker on the accepted board already: not the bumps' doing
    assert judge._pair_rules_worse(ctx, chk, types.SimpleNamespace(chk=chk), tune) == {}
    # a skew marker alone is not a pair-rule regression
    assert judge._pair_rules_worse(ctx, {"cons": {"nets": {"/D_N": ["skew_out_of_range"]}}}, acc, tune) == {}


def test_completion_first_only_detected_pairs_on_an_incomplete_result_ask_for_the_two_net_run():
    """engine._pairs_wanted: the board is routed again with its pairs as two nets only when the result is not
    complete and clean AND lanes were laid for pairs nobody declared (net names / classes)."""
    open_out = {"_judged": True, "drc": {"new_errors": 0}, "total": 10, "routed": 9}
    done_out = {"_judged": True, "drc": {"new_errors": 0}, "total": 10, "routed": 10}
    det = scene(STRAIGHT, None)                                           # no plan: the pair comes from its class / names
    (pair,) = det.x.cons.pairs
    assert pair.src in ("class", "name") and pair.coupled
    _job, r = lay(det)
    assert r["ok"]
    run = types.SimpleNamespace(x=det.x, ctx=det)
    assert engine._pairs_wanted(run, open_out) is True
    assert engine._pairs_wanted(run, done_out) is False                   # complete and clean: nothing to gain
    assert engine._pairs_wanted(run, dict(open_out, _judged=False)) is False
    # constraint errors alone (a skew miss) are not "incomplete": no second run for those
    assert engine._pairs_wanted(run, dict(done_out, drc={"new_errors": 2, "constraint_errors": 2})) is False
    # a pair the plan declares stays coupled whatever the result
    dec = scene(STRAIGHT, {"diff_pairs": [{"p": "/D_P", "n": "/D_N", "skew": 0.2}]})
    assert dec.x.cons.pairs[0].src == "plan"
    _job, r = lay(dec)
    assert r["ok"] and engine._pairs_wanted(types.SimpleNamespace(x=dec.x, ctx=dec), open_out) is False
    # detected, but not laid as lanes (swapped pads): nothing to undo
    sw = scene(SWAPPED, None)
    lay(sw)
    assert engine._pairs_wanted(types.SimpleNamespace(x=sw.x, ctx=sw), open_out) is False
    # the report of an un-coupled detected pair carries the reason the engine wrote
    import dataclasses
    cons2 = dataclasses.replace(det.x.cons, pairs=(dataclasses.replace(
        pair, coupled=False, why=engine.PAIRS_FIRST + ": laid as coupled lanes the board ended with 1 open connection(s)"),))
    two = scene(STRAIGHT, None)
    two.x.cons = cons2
    assert pairs.jobs(two) == []
    route_all(two)
    row = C.measure(two)["pairs"][0]
    assert row["lanes"] is False and row["why"].startswith("completion first")


# ── end to end: the pair bench boards, judged by KiCad ────────────────────
_TMP = None


def have_kicad(board: str) -> bool:
    return common.KICAD_CLI.exists() and common.KICAD_PY.exists() and \
        (common.CORPUS / board / f"{board}.kicad_pcb").exists()


def corpus_project(board: str, tag: str):
    from copper_kicad import project as projmod
    global _TMP
    if _TMP is None:
        _TMP = Path(tempfile.mkdtemp(prefix="pairs-", dir=str(common.WORK)))
    work = _TMP / f"{board}-{tag}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for f in (common.CORPUS / board).iterdir():
        if f.is_file() and f.name not in ("meta.json", "reference.json"):
            shutil.copy2(f, work / f.name)
    return projmod.Project(work, board)


def copper_of(pr):
    from copper_kicad.pcb.v2 import kio
    ex = kio.extract(pr)
    return (sorted(tuple(round(v, 6) if isinstance(v, float) else v for v in t[:7]) for t in ex["tracks"]),
            sorted(tuple(round(v, 6) if isinstance(v, float) else v for v in t[:5]) for t in ex["vias"]))


def _route(board, tag, mutate=None, **kw):
    from copper_kicad.pcb import guided
    from copper_kicad.pcb.v2 import planx
    pr = corpus_project(board, tag)
    plan = planx.v2_plan(guided.analyze(pr, ripup="none")["suggested_plan"])
    if mutate is not None:
        mutate(plan)
    return pr, guided.route(pr, plan, **kw)


def _kicad_pair_check(pr, nets_pairs, max_skew):
    """KiCad's own verdict on the written file with the board's own rule file: no error at all — the diff-pair
    gap, uncoupled-length and skew rules included — nothing unconnected, and its own lengths inside the skew."""
    d = common.drc(pr.pcb)
    assert d["errors"] == 0 and d["unconnected"] == 0, d.get("by_type")
    assert not any(t in str(d.get("by_type")) for t in C.KICAD_TYPES), d.get("by_type")
    kl = common.kicad_lengths(pr.pcb, [n for pn in nets_pairs for n in pn])
    for p, n in nets_pairs:
        assert abs(kl[p] - kl[n]) <= max_skew + 1e-6, (p, n, kl)
    return kl


def test_e2e_usb_pair_is_coupled_skew_compensated_clean_by_kicads_own_drc_and_deterministic():
    """cons_usb_pair: /USB_D_P + /USB_D_N with KiCad rules skew 0.15, gap 0.15 .. 0.9, uncoupled 6 mm, a class
    pair gap (0.15) below the clearance (0.2), one 90-degree turn. A dry run writes nothing; the real run writes
    a board KiCad's DRC has nothing to say about, the lanes run at the pair's gap, the same board again gives
    the same copper, and with constraints 'off' the pair is two nets and KiCad's rules fail."""
    if PURE or not have_kicad("cons_usb_pair"):
        print("  (skipped: needs KiCad 10 and the constraint bench boards)")
        return
    pair = ("/USB_D_P", "/USB_D_N")
    pr = corpus_project("cons_usb_pair", "dry")
    before = pr.pcb.read_bytes()
    from copper_kicad.pcb import guided
    from copper_kicad.pcb.v2 import planx
    out = guided.route(pr, planx.v2_plan(guided.analyze(pr, ripup="none")["suggested_plan"]), dry_run=True)
    sc = out.pop("_scratch", None)
    try:
        assert pr.pcb.read_bytes() == before and out["written"] is False and out["dry_run"] is True
        assert out["constraints"]["pairs"][0]["lanes"] is True
    finally:
        if sc is not None:
            shutil.rmtree(sc.dir, ignore_errors=True)
    pr, out = _route("cons_usb_pair", "a")
    assert out["written"] is True and out["routed"] == out["total"] == 6
    assert out["drc"]["new_errors"] == 0 and "constraint_errors" not in out["drc"]
    cons = out["constraints"]
    row = cons["pairs"][0]
    assert row["ok"] is True and row["lanes"] is True and row["src"] == "dru" and "gap_seen" not in row
    assert row["skew"] <= 0.15 and row["coupled"] >= 70 and row["uncoupled"] <= 6.0 and row.get("tuned", 0) >= 1
    assert cons["missed"] == 0 and cons["kicad"] == {} and cons.get("len_model_err", 0.0) <= 0.005
    kl = _kicad_pair_check(pr, [pair], 0.15)
    for n in pair:                                                        # the router's figure is KiCad's
        assert abs(cons["nets"][n][0] - kl[n]) <= 0.005, (n, cons["nets"][n], kl[n])
    tracks, vias = copper_of(pr)
    sp = [t[1:7] for t in tracks if t[0] == pair[0]]
    sn = [t[1:7] for t in tracks if t[0] == pair[1]]
    cs = C.coupled_stats(sp, sn, 0.2, 0.15)
    assert cs["coupled_pct"] >= 70 and cs["gap_seen"] == 0.15, cs         # at the class's pair gap, below the clearance
    assert sum(1 for v in vias if v[0] == pair[0]) == sum(1 for v in vias if v[0] == pair[1])
    # the same board again: the same copper
    pr2, out2 = _route("cons_usb_pair", "b")
    assert out2["written"] is True and copper_of(pr2) == copper_of(pr)
    # constraints 'off': two nets as before §14, and KiCad's pair rules are not met (the rules are real)
    def off(plan):
        plan.setdefault("global", {})["constraints"] = "off"
    pr3, out3 = _route("cons_usb_pair", "c", off)
    assert "constraints" not in out3 and out3["routed"] == out3["total"]
    d3 = common.drc(pr3.pcb)
    assert any(t in str(d3.get("by_type")) for t in C.KICAD_TYPES), d3.get("by_type")
    # coupled: false in the plan: routed as two nets, reported as such, never as coupled lanes
    def two(plan):
        for dp in plan.get("diff_pairs") or []:
            dp["coupled"] = False
    pr4, out4 = _route("cons_usb_pair", "d", two)
    assert out4["routed"] == out4["total"] and out4["constraints"]["pairs"][0].get("lanes") is not True


def test_e2e_two_pairs_cross_through_paired_vias_clean_by_kicads_own_drc_and_deterministic():
    """cons_pairs_cross: pair A west-east, pair B north-south on SMD pads — they must cross, so one changes layer
    twice through a pair of vias. Both stay coupled, P and N use the same number of vias, KiCad's DRC (gap 0.15 ..
    1.2, uncoupled 8 mm, skew 0.2 per pair) is clean, and a second run lays the same copper."""
    if PURE or not have_kicad("cons_pairs_cross"):
        print("  (skipped: needs KiCad 10 and the constraint bench boards)")
        return
    prs = [("/A_P", "/A_N"), ("/B_P", "/B_N")]
    pr, out = _route("cons_pairs_cross", "a")
    assert out["written"] is True and out["routed"] == out["total"] == 4
    assert out["drc"]["new_errors"] == 0 and "constraint_errors" not in out["drc"]
    cons = out["constraints"]
    assert cons["missed"] == 0 and cons["kicad"] == {} and len(cons["pairs"]) == 2
    for row in cons["pairs"]:
        assert row["ok"] is True and row["lanes"] is True and row["skew"] <= 0.2 and row["coupled"] >= 60, row
    _kicad_pair_check(pr, prs, 0.2)
    tracks, vias = copper_of(pr)
    nv = {n: sum(1 for v in vias if v[0] == n) for pn in prs for n in pn}
    assert nv["/A_P"] == nv["/A_N"] and nv["/B_P"] == nv["/B_N"] and sum(nv.values()) >= 4, nv   # they cross: paired vias
    for p, n in prs:
        sp = [t[1:7] for t in tracks if t[0] == p]
        sn = [t[1:7] for t in tracks if t[0] == n]
        assert C.coupled_stats(sp, sn, 0.2, 0.15)["coupled_pct"] >= 60, (p, n)
    pr2, out2 = _route("cons_pairs_cross", "b")
    assert out2["written"] is True and copper_of(pr2) == copper_of(pr)


if __name__ == "__main__":
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
             and (not names or k in names)]
    fails = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            import traceback
            fails += 1
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    if _TMP is not None:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
