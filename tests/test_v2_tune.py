"""Router v2 length tuning (design §14.7, v2/tune.py) and the judged pass that keeps it honest
(judge.tune_pass, §14.6).

Unit tests run the real tuner / oracle / route state on synthetic boards (no KiCad): bump geometry and gain,
allocation, legality against foreign copper, the board edge, the plan's forbid regions and avoid rectangles, the
tuner's own-net spacing rule, teed runs, untune, honest shortfalls, determinism. The judge tests script KiCad.
The end-to-end test routes the matched-bus bench board through ``guided.session.route`` with KiCad 10 and asks
KiCad's own DRC — the board's skew rule included — about the written file (skipped without KiCad / the corpus).

Run:  python tests/test_v2_tune.py             all
      python tests/test_v2_tune.py --pure      no KiCad
      python tests/test_v2_tune.py test_name   one test
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
import test_v2_judge as TJ  # noqa: E402  (the scripted KiCad)
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import geom, plan as P  # noqa: E402
from copper_kicad.pcb.v2 import constraints as C, judge, oracle as O, pieces, tune  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Ctx, Terminal  # noqa: E402

PURE = "--pure" in sys.argv
W = 0.2
# two nets between J1 (west) and U1 (east): /A is 30 mm, /B 36 mm; every pad 1.2 x 0.6 mm
PADS = [("J1", 1, "/A", 5.0, 10.0), ("U1", 1, "/A", 35.0, 10.0), ("J1", 2, "/B", 5.0, 20.0), ("U1", 2, "/B", 41.0, 20.0)]
PLAN = {"groups": [{"nets": ["/A", "/B"], "tol": 0.1}]}


def scene(pads=PADS, routes=None, plan=PLAN, tracks=(), dru="", tuning=None):
    """-> Ctx with the real oracle and route state; every net of ``routes`` {net: [points]} (default: a straight
    line between its two pads) is laid as one routed connection."""
    b = TC.board(pads, tracks=tracks)
    cfg = P.resolve(b, plan)
    cfg.x.cons = C.resolve(b, cfg, cfg.x, C.ProjectRules(rules=C.parse_dru(dru), stack=C.Stackup.default(b.L)))
    cfg.x.cons_env = {"stack": C.Stackup.default(b.L), "die": {}, "use_height": True,
                      "tuning": {"single": dict(tuning)} if tuning else {}}
    orc = O.Oracle(b)
    orc.set_plan(cfg)
    conns = []
    routes = dict(routes) if routes is not None else {n: None for n in sorted(b.nets) if len(b.nets[n].pads) == 2}
    for n in sorted(routes):
        pa, pb = b.nets[n].pads[0], b.nets[n].pads[-1]
        conns.append(ConnV2(idx=len(conns), net=n, a=Terminal(pa.x, pa.y, (0,), pad=pa.name, cluster=pa.cluster),
                            b=Terminal(pb.x, pb.y, (0,), pad=pb.name, cluster=pb.cluster), width=W, neck=W,
                            clearance=0.2, via=(0.6, 0.3)))
    st = pieces.RouteState(orc, conns)
    for c in conns:
        pts = routes[c.net] or [(c.a.x, c.a.y), (c.b.x, c.b.y)]
        for a, bb in zip(pts, pts[1:]):
            st.add(net=c.net, conn=c.idx, kind="seg", layer=0, x1=a[0], y1=a[1], x2=bb[0], y2=bb[1], w=W, stage="detail")
        st.set_status(c.idx, "routed")
        assert st.connected(c.idx), c.net
    return Ctx(board=b, cfg=cfg, x=cfg.x, oracle=orc, state=st, conns=conns, history={})


def length(ctx, net):
    return C.net_length(ctx, net).total


def tuned(ctx, net=None):
    return [p for p in ctx.state.pieces(net=net) if p.stage == "tune"]


def path_len(pts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def octilinear(p, u=(1.0, 0.0)):
    """Is the piece axial or at 45 degrees relative to the run direction u?"""
    dx, dy = p.x2 - p.x1, p.y2 - p.y1
    a, b = abs(dx * u[0] + dy * u[1]), abs(-dx * u[1] + dy * u[0])
    return a < 2e-6 or b < 2e-6 or abs(a - b) < 2e-6


# ── geometry ──────────────────────────────────────────────────────────────
def test_gain_formula_holds_to_a_nanometre_on_axial_and_diagonal_runs():
    """A U bump adds 2a - 4c(2 - sqrt 2), a shallow bump 2h(sqrt 2 - 1): the laid points say so (design §14.7;
    KiCad printed the same figure for the same shape, §14.9)."""
    s2 = math.sqrt(0.5)
    for u in ((1.0, 0.0), (0.0, -1.0), (s2, s2), (-s2, s2)):
        for w, clr, a in ((0.2, 0.2, 1.0), (0.2, 0.2, 0.6), (0.15, 0.15, 2.37), (0.5, 0.3, 3.0)):
            Pm = tune.params(w, clr, amp=max(a, 1.0))
            for side in (1, -1):
                pts = tune.bump_points((3.0, 7.0), u, side, 1.3, a, Pm)
                assert len(pts) == 8
                add = path_len(pts) - Pm.foot
                assert abs(add - tune.gain(a, Pm.c)) < 8e-6, (u, a, add, tune.gain(a, Pm.c))   # 8 points at 1 nm
                assert abs(tune.gain(tune.amp_for(1.234, Pm.c), Pm.c) - 1.234) < 1e-12
                # the feet stand on the run, the legs are S apart, no straight piece is shorter than 0.2 mm
                t = [(x - 3.0) * u[0] + (y - 7.0) * u[1] for x, y in pts]
                h = [side * (-(x - 3.0) * u[1] + (y - 7.0) * u[0]) for x, y in pts]
                assert abs(h[0]) < 2e-6 and abs(h[-1]) < 2e-6 and abs(t[-1] - t[0] - Pm.foot) < 2e-6
                assert abs((t[5] - t[2]) - Pm.S) < 2e-6 and abs(max(h) - a) < 2e-6
                for i in (1, 3, 5):                                     # leg, top, leg
                    assert math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) >= 0.2 - 2e-6
        pts = tune.shallow_points((3.0, 7.0), u, 1, 2.0, 0.3)
        assert abs(path_len(pts) - (2 * 0.3 + tune.MIN_STRAIGHT) - tune.shallow_gain(0.3)) < 4e-6
    assert abs(tune.gain(1.0, 0.2) - (2.0 - 0.8 * (2 - math.sqrt(2)))) < 1e-12


def test_amplitude_and_pitch_come_from_width_and_clearance():
    near = lambda a, b: abs(a - b) < 1e-12                           # noqa: E731
    Pm = tune.params(0.2, 0.2)
    assert near(Pm.S, 0.6) and near(Pm.c, 0.2) and near(Pm.a_min, 0.6) and Pm.a_max == 1.0 and near(Pm.keep, 0.4)
    assert near(Pm.foot, 1.0) and near(Pm.gap, 0.2)
    wide = tune.params(0.5, 0.3)                     # 3 x width wins: legs 1.5 mm apart, 1.0 mm of gap between them
    assert near(wide.S, 1.5) and near(wide.c, 0.5) and wide.S - wide.w >= wide.clr and near(wide.keep, 1.0)
    assert near(wide.a_min, 1.2) and wide.a_max == wide.a_min        # the default 1 mm is below its smallest bump
    big = tune.params(0.2, 0.6)                      # width + clearance wins
    assert near(big.S, 0.8) and near(big.c, 0.3)
    assert near(tune.params(0.2, 0.2, spacing=1.0).S, 1.0) and tune.params(0.2, 0.2, amp=2.5).a_max == 2.5
    assert tune.params(0.2, 0.2, amp=0.1).a_max == Pm.a_min          # never below the smallest bump
    lad = tune.params(0.2, 0.2, amp=4.0).ladder
    assert lad[0] == 4.0 and lad[-1] == Pm.a_min and list(lad) == sorted(lad, reverse=True)


def test_allocation_uses_the_fewest_bumps_and_is_exact():
    g = tune.gain(1.0, 0.2)                          # 1.531
    gmin = tune.gain(0.6, 0.2)                       # 0.731
    gains, rest = tune.allocate([g] * 6, gmin, 5.0)  # 4 bumps (3 would give 4.59)
    assert rest == 0.0 and sum(1 for v in gains if v) == 4 and abs(sum(gains) - 5.0) < 1e-9
    assert all(v == 0.0 or gmin - 1e-9 <= v <= g + 1e-9 for v in gains)
    gains, rest = tune.allocate([g, gmin, g], gmin, 2.0)             # the two tall slots, not the low one
    assert gains[1] == 0.0 and abs(sum(gains) - 2.0) < 1e-9 and rest == 0.0
    gains, rest = tune.allocate([g] * 2, gmin, 9.0)                  # capacity short: everything, and what is missing
    assert gains == [g, g] and abs(rest - (9.0 - 2 * g)) < 1e-9
    gains, rest = tune.allocate([gmin, gmin], gmin, 1.0)             # 2 x the smallest bump is too much: 1 + shallow
    assert sum(1 for v in gains if v) == 1 and abs(rest - (1.0 - gmin)) < 1e-9 and rest < gmin
    gains, rest = tune.allocate([g], gmin, 0.3)                      # less than one U bump: shallow bumps only
    assert gains == [0.0] and abs(rest - 0.3) < 1e-9
    assert tune.allocate([], gmin, 1.0) == ([], 1.0)


# ── the tuner on a free run ───────────────────────────────────────────────
def test_six_mm_on_a_free_run_lands_in_the_window_and_every_piece_is_legal():
    ctx = scene()
    assert abs(length(ctx, "/A") - 30.0) < 1e-9 and abs(length(ctx, "/B") - 36.0) < 1e-9
    b_before = [(p.x1, p.y1, p.x2, p.y2) for p in ctx.state.pieces(net="/B")]
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/A"]
    la = length(ctx, "/A")
    assert rep.data["changed"] == ["/A"] and rec["bumps"] == 4 and rec["runs"] == 1 and "short_by" not in rec
    assert 36.0 - 0.1 <= la <= 36.0 and abs(la - rec["aim"]) < 1e-5 and abs(rec["added"] - (la - 30.0)) < 1e-4
    assert abs(rec["aim"] - (36.0 - 0.025)) < 1e-9                    # tol / 4 below the longest member
    assert [(p.x1, p.y1, p.x2, p.y2) for p in ctx.state.pieces(net="/B")] == b_before      # the longest: untouched
    ps = ctx.state.pieces(net="/A")
    assert all(p.stage == "tune" and p.conn == 0 and p.w == W and p.target_w == W and not p.protected for p in ps)
    assert ctx.state.connected(0) and ctx.state.status(0) == "routed"
    for p in ps:
        assert ctx.oracle.piece_ok(p) is None and octilinear(p)
        assert p.length >= 0.2 - 2e-6, p                               # no stub
    # a chain from pad to pad: consecutive pieces share their end points, the run's own two ends are kept
    assert (ps[0].x1, ps[0].y1) == (5.0, 10.0) and (ps[-1].x2, ps[-1].y2) == (35.0, 10.0)
    assert all((a.x2, a.y2) == (b.x1, b.y1) for a, b in zip(ps, ps[1:]))
    # nothing above the amplitude, nothing nearer the pads than the keep-off
    assert max(abs(p.y1 - 10.0) for p in ps) <= 1.0 + 1e-6
    assert min(p.x1 for p in ps if abs(p.y1 - 10.0) > 1e-6 or abs(p.y2 - 10.0) > 1e-6) >= 5.0 + 0.4
    m = C.measure(ctx)["groups"][0]
    assert m["ok"] is True and m["tuned"] == 1 and abs(m["added"] - rec["added"]) < 1e-3
    assert C.measure(ctx)["tuning"]["/A"] == [30.0, rec["after"], 4]


def test_same_board_same_copper_and_a_second_pass_changes_nothing():
    a, b = scene(), scene()
    tune.run(a, Budget(10 ** 7))
    tune.run(b, Budget(10 ** 7))
    assert a.state.export() == b.state.export()
    first = [(p.x1, p.y1, p.x2, p.y2) for p in a.state.pieces()]
    rep = tune.run(a, Budget(10 ** 7))               # tuned nets are put back and tuned afresh: the same shape again
    assert sorted((p.x1, p.y1, p.x2, p.y2) for p in a.state.pieces()) == sorted(first)
    assert abs(length(a, "/A") - length(b, "/A")) < 1e-9 and rep.data["nets"]["/A"]["bumps"] == 4
    src = Path(tune.__file__).read_text(encoding="utf-8")
    for banned in ("import time", "from time", "import random", "datetime", "perf_counter", "monotonic"):
        assert banned not in src, banned
    assert not getattr(tune, "STUB", False)


def test_small_deficits_use_shallow_bumps_and_stay_under_the_longest():
    for want in (0.31, 0.62, 0.9, 1.7, 3.33):
        pads = [("J1", 1, "/A", 5.0, 10.0), ("U1", 1, "/A", 35.0, 10.0), ("J1", 2, "/B", 5.0, 20.0),
                ("U1", 2, "/B", 35.0 + want, 20.0)]
        ctx = scene(pads, plan={"groups": [{"nets": ["/A", "/B"], "tol": 0.04}]})
        tune.run(ctx, Budget(10 ** 7))
        la, lb = length(ctx, "/A"), length(ctx, "/B")
        assert lb - 0.04 <= la <= lb - tune.GUARD + 1e-9, (want, la, lb)
        assert all(ctx.oracle.piece_ok(p) is None and octilinear(p) for p in tuned(ctx))
    # 0.1 mm short with a 0.2 mm tolerance: inside the window, nothing is laid
    pads = [("J1", 1, "/A", 5.0, 10.0), ("U1", 1, "/A", 35.0, 10.0), ("J1", 2, "/B", 5.0, 20.0), ("U1", 2, "/B", 35.1, 20.0)]
    ctx = scene(pads, plan={"groups": [{"nets": ["/A", "/B"], "tol": 0.2}]})
    rep = tune.run(ctx, Budget(10 ** 7))
    assert rep.data["changed"] == [] and not tuned(ctx) and rep.data["nets"] == {}


def test_explicit_target_tunes_every_member_and_never_passes_the_window():
    ctx = scene(plan={"groups": [{"nets": ["/A", "/B"], "tol": 0.2, "len": 38.0}]})
    rep = tune.run(ctx, Budget(10 ** 7))
    for n in ("/A", "/B"):
        assert abs(length(ctx, n) - 38.0) < 1e-4, (n, length(ctx, n))
    assert rep.data["changed"] == ["/A", "/B"] and C.measure(ctx)["groups"][0]["ok"] is True
    # a member that is already too long cannot be fixed by adding copper: said so, with the amount, and not touched
    ctx = scene(plan={"groups": [{"nets": ["/A", "/B"], "tol": 0.2, "len": 33.0}]})
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/B"]
    assert abs(rec["long_by"] - (36.0 - 33.2)) < 1e-6 and "longer than its window" in rec["why"] and not tuned(ctx, "/B")
    assert abs(length(ctx, "/A") - 33.0) < 1e-4 and rep.failed == 1
    row = C.measure(ctx)["groups"][0]
    assert row["ok"] is False and abs(row["long_by"] - 2.8) < 1e-3 and "longer than its window" in row["why"]


# ── what a bump must stay away from ───────────────────────────────────────
def gap_to(ctx, pieces_, shape_seg):
    x1, y1, x2, y2, w = shape_seg
    box = geom.Shape("box", geom.box_from_seg(x1, y1, x2, y2, w), "cu", 0, 0.0, (0,))
    return min(box.dist_seg(p.x1, p.y1, p.x2, p.y2) - p.w / 2 for p in pieces_)


def test_bumps_keep_the_clearance_to_other_copper_and_go_to_the_free_side():
    """/C runs 0.6 mm beside /A (y = 10.6): bumps towards it are illegal, so every bump is on the other side."""
    pads = PADS + [("J1", 3, "/C", 5.0, 12.0), ("U1", 3, "/C", 35.0, 12.0)]
    wall = [(5.0, 12.0), (6.4, 10.6), (33.6, 10.6), (35.0, 12.0)]
    ctx = scene(pads, routes={"/A": None, "/B": None, "/C": wall})
    tune.run(ctx, Budget(10 ** 7))
    assert 35.9 <= length(ctx, "/A") <= 36.0
    ps = tuned(ctx, "/A")
    assert max(p.y1 for p in ps) <= 10.0 + 1e-6 and min(p.y1 for p in ps) < 9.3        # all on the free (north) side
    assert all(ctx.oracle.piece_ok(p) is None for p in ps)
    assert gap_to(ctx, ps, (6.4, 10.6, 33.6, 10.6, W)) >= 0.2 - 1e-6


def test_no_bump_enters_a_forbid_region_an_avoid_rectangle_or_leaves_the_board():
    """The plan's forbidden areas are rules of the oracle: the tuner cannot lay copper there, and what it cannot
    lay it reports. The run is fenced in from x = 9 on: only the first 4 mm can carry bumps."""
    plan = {"groups": [{"nets": ["/A", "/B"], "tol": 0.1, "amp": 1.0}],
            "regions": [{"rect": [9.0, 8.0, 36.0, 9.9], "forbid": True, "name": "north"},
                        {"rect": [9.0, 10.1, 36.0, 12.0], "forbid": True, "name": "south"}]}
    ctx = scene(plan=plan)
    assert ctx.oracle.plan_on
    rep = tune.run(ctx, Budget(10 ** 7))
    ps = tuned(ctx, "/A")
    assert ps and all(ctx.oracle.piece_ok(p) is None for p in ps)
    for p in ps:
        for r in plan["regions"]:
            x0, y0, x1, y1 = r["rect"]
            assert geom.seg_rect_dist(p.x1 - (x0 + x1) / 2, p.y1 - (y0 + y1) / 2, p.x2 - (x0 + x1) / 2,
                                      p.y2 - (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2) > 0, (p, r["name"])
    off_run = [p for p in ps if abs(p.y1 - 10.0) > 1e-6 or abs(p.y2 - 10.0) > 1e-6]
    assert max(max(p.x1, p.x2) for p in off_run) < 9.0
    # ... and that is not enough for 6 mm: the shortfall is reported in mm, with what could be added, never as ok
    rec = rep.data["nets"]["/A"]
    la = length(ctx, "/A")
    assert 30.5 < la < 35.9 and abs(rec["short_by"] - (35.9 - la)) < 1e-3 and rec["capacity"] > 0
    assert abs(rec["capacity"] - rec["added"]) < 1e-3 and "no free straight run" in rec["why"] and rep.failed == 1
    row = C.measure(ctx)["groups"][0]
    assert row["ok"] is False and abs(row["short_by"] - rec["short_by"]) < 1e-3 and row["why"] == rec["why"]
    assert row["capacity"] == rec["capacity"] and row["tuned"] == 1
    assert abs(tune.capacity(ctx, "/A")) < 1e-9                       # nothing more fits now
    # an avoid rectangle of the net (nets[..].avoid) closes the rest as well
    plan2 = dict(plan, nets={"/A": {"avoid": [[5.5, 8.0, 9.0, 9.95], [5.5, 10.05, 9.0, 12.0]]}})
    ctx = scene(plan=plan2)
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/A"]
    assert not tuned(ctx) and abs(rec["short_by"] - 5.9) < 1e-6 and rec["capacity"] == 0.0 and rec["added"] == 0.0
    assert C.measure(ctx)["groups"][0]["ok"] is False
    # the board edge: a run 0.5 mm inside the outline gets its bumps on the inner side only
    pads = [("J1", 1, "/A", 5.0, 0.6), ("U1", 1, "/A", 35.0, 0.6), ("J1", 2, "/B", 5.0, 20.0), ("U1", 2, "/B", 41.0, 20.0)]
    ctx = scene(pads)
    tune.run(ctx, Budget(10 ** 7))
    assert 35.9 <= length(ctx, "/A") <= 36.0 and min(min(p.y1, p.y2) for p in tuned(ctx, "/A")) >= 0.6 - 1e-6


def test_meanders_keep_leg_spacing_from_their_own_net():
    """The oracle has no rule between copper of one net; the tuner must not let a meander touch its own net.
    /A is a hairpin: out along y = 10, back along y = 11.2 — bumps between the two arms would short-cut it."""
    pads = [("J1", 1, "/A", 5.0, 10.0), ("J1", 3, "/A", 5.0, 11.2), ("J1", 2, "/B", 5.0, 20.0), ("U1", 2, "/B", 71.0, 20.0)]
    hair = [(5.0, 10.0), (30.0, 10.0), (30.6, 10.6), (30.0, 11.2), (5.0, 11.2)]
    ctx = scene(pads[:2] + [("J1", 2, "/B", 5.0, 20.0), ("U1", 2, "/B", 45.0, 20.0)],
                routes={"/A": hair, "/B": None}, plan={"groups": [{"nets": ["/A"], "tol": 0.2, "len": 58.0}]})
    l0 = length(ctx, "/A")
    tune.run(ctx, Budget(10 ** 7))
    assert abs(length(ctx, "/A") - 58.0) < 1e-3 and length(ctx, "/A") > l0 + 5
    bumps = tuned(ctx, "/A")
    assert bumps and ctx.state.connected(0)
    Pm = tune.params(W, 0.2)
    # every piece that leaves its run keeps S - w (0.4 mm edge to edge) from the other arm and from the U-turn
    arms = {10.0: [(5.0, 11.2, 30.0, 11.2), (30.0, 10.0, 30.6, 10.6), (30.6, 10.6, 30.0, 11.2)],
            11.2: [(5.0, 10.0, 30.0, 10.0), (30.0, 10.0, 30.6, 10.6), (30.6, 10.6, 30.0, 11.2)]}
    off = [p for p in bumps if abs(p.y1 - p.y2) > 1e-6 or (abs(p.y1 - 10.0) > 1e-6 and abs(p.y1 - 11.2) > 1e-6)]
    assert off
    for p in off:
        mine = 10.0 if min(p.y1, p.y2) < 10.0 + 1e-6 else 11.2
        if p.x1 > 29.0 and p.x2 > 29.0:
            continue
        for x1, y1, x2, y2 in arms[mine][:1]:
            assert gap_to(ctx, [p], (x1, y1, x2, y2, W)) >= Pm.S - W - 1e-5, p
        for x1, y1, x2, y2 in arms[mine][1:]:
            if (p.x2, p.y2) != (x1, y1) and (p.x1, p.y1) != (x2, y2):
                assert gap_to(ctx, [p], (x1, y1, x2, y2, W)) >= Pm.S - W - 1e-5, p
    # ... and nothing between the arms (0.2 mm apart edge to edge at best): every bump points outwards
    assert all(not (10.0 + 1e-6 < p.y1 < 11.2 - 1e-6) for p in bumps if p.x1 < 29.0)
    # the pads of the own net are kept at the same distance
    for pad in ctx.board.nets["/A"].pads:
        for sh in pad.shapes[0]:
            assert min(sh.dist_seg(p.x1, p.y1, p.x2, p.y2) - p.w / 2 for p in bumps
                       if abs(p.y1 - p.y2) > 1e-6 or abs(p.y1 - pad.y) > 1e-6) >= Pm.S - W - 1e-5


def test_a_run_another_connection_tees_onto_is_not_touched():
    """/A has three pads: the second connection tees onto the middle of the first one's run. That run stays as
    it is (the tee point would hang in the air); the tee's own stem is a free run and may carry bumps."""
    pads = [("J1", 1, "/A", 5.0, 10.0), ("U1", 1, "/A", 35.0, 10.0), ("U2", 1, "/A", 20.0, 16.0),
            ("J1", 2, "/B", 5.0, 20.0), ("U1", 2, "/B", 49.0, 20.0)]
    b = TC.board(pads)
    cfg = P.resolve(b, {"groups": [{"nets": ["/A", "/B"], "tol": 0.2, "amp": 1.0}]})
    cfg.x.cons = C.resolve(b, cfg, cfg.x, C.ProjectRules(stack=C.Stackup.default(b.L)))
    cfg.x.cons_env = {"stack": C.Stackup.default(b.L), "die": {}, "use_height": True, "tuning": {}}
    orc = O.Oracle(b)
    pa, pb, pc = b.nets["/A"].pads
    qa, qb = b.nets["/B"].pads
    T = lambda p: Terminal(p.x, p.y, (0,), pad=p.name, cluster=p.cluster)   # noqa: E731
    conns = [ConnV2(idx=0, net="/A", a=T(pa), b=T(pb), width=W, neck=W, clearance=0.2),
             ConnV2(idx=1, net="/A", a=T(pc), b=T(pb), width=W, neck=W, clearance=0.2),
             ConnV2(idx=2, net="/B", a=T(qa), b=T(qb), width=W, neck=W, clearance=0.2)]
    st = pieces.RouteState(orc, conns)
    trunk = st.add(net="/A", conn=0, kind="seg", layer=0, x1=5.0, y1=10.0, x2=35.0, y2=10.0, w=W)
    st.add(net="/A", conn=1, kind="seg", layer=0, x1=20.0, y1=16.0, x2=20.0, y2=10.0, w=W)
    st.add(net="/B", conn=2, kind="seg", layer=0, x1=5.0, y1=20.0, x2=49.0, y2=20.0, w=W)
    for c in conns:
        st.set_status(c.idx, "routed")
    ctx = Ctx(board=b, cfg=cfg, x=cfg.x, oracle=orc, state=st, conns=conns, history={})
    assert st.connected(0) and st.connected(1)
    rep = tune.run(ctx, Budget(10 ** 7))
    assert st.piece(trunk.id) is not None                              # the teed-onto run: exactly as routed
    assert all(p.conn == 1 for p in tuned(ctx)) and tuned(ctx)
    assert st.connected(0) and st.connected(1) and st.connected(2)
    rec = rep.data["nets"]["/A"]                                       # 36 mm + what the 6 mm stem can hold: short
    assert rec["short_by"] > 0 and rec["added"] > 0 and "no free straight run" in rec["why"]
    assert C.measure(ctx)["groups"][0]["ok"] is False


def test_untune_restores_the_routed_shape_piece_for_piece():
    ctx = scene()
    fields = lambda: sorted((p.net, p.conn, p.kind, p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.stage, p.protected,   # noqa: E731
                             p.target_w) for p in ctx.state.pieces())
    before, score = fields(), ctx.state.score()
    tune.run(ctx, Budget(10 ** 7))
    assert fields() != before and ctx.history["tune"]["edits"]["/A"]
    assert tune.tuned_conns(ctx) == {0}
    n = tune.untune(ctx, ["/A", "/B"], avoid=[(9.0, 8.0, 11.0, 12.0)])
    assert n == 1 and fields() == before and ctx.state.score() == score and ctx.state.connected(0)
    assert not ctx.history["tune"]["edits"].get("/A") and not tune.tuned_conns(ctx)
    assert tune.untune(ctx, ["/A"]) == 0
    # the avoid rectangle stays closed for the next attempt
    tune.run(ctx, Budget(10 ** 7))
    assert 35.9 <= length(ctx, "/A") <= 36.0
    for p in tuned(ctx, "/A"):
        if abs(p.y1 - 10.0) > 1e-6 or abs(p.y2 - 10.0) > 1e-6:
            assert max(p.x1, p.x2) < 9.0 - W / 2 + 1e-6 or min(p.x1, p.x2) > 11.0 + W / 2 - 1e-6, p


def test_copper_that_was_on_the_board_before_is_measured_never_altered():
    """/A arrives routed (kept tracks, no connection of this run): it is short, and the report says why nothing
    was done about it."""
    pads = PADS
    ctx = scene(pads, routes={"/B": None}, tracks=[["/A", 0, 5.0, 10.0, 35.0, 10.0, W, 1, False]])
    assert abs(length(ctx, "/A") - 30.0) < 1e-9
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/A"]
    assert rep.data["changed"] == [] and not tuned(ctx) and abs(rec["short_by"] - 5.9) < 1e-6
    assert "on the board before this run" in rec["why"]
    row = C.measure(ctx)["groups"][0]
    assert row["ok"] is False and "on the board before" in row["why"]


def test_groups_that_get_no_copper_are_left_alone():
    """A name-detected group (mode 'report'), 'report' mode and constraints 'off' lay nothing."""
    pads = []
    for i, n in enumerate(["/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3", "/SD_CLK"]):
        pads += [("J1", i + 1, n, 5.0, 4.0 + 2 * i), ("U1", i + 1, n, 30.0 + 2 * i, 4.0 + 2 * i)]
    for plan in (None, {"global": {"constraints": "report"}, "groups": [{"nets": ["/SD_*"], "tol": 0.1}]},
                 {"global": {"constraints": "off"}, "groups": [{"nets": ["/SD_*"], "tol": 0.1}]}):
        ctx = scene(pads, plan=plan)
        before = ctx.state.export()
        rep = tune.run(ctx, Budget(10 ** 7))
        assert ctx.state.export() == before and rep.data["changed"] == [] and rep.work == 0, plan
    ctx = scene(pads, plan={"groups": [{"nets": ["/SD_*"], "tol": 0.1}]})
    tune.run(ctx, Budget(10 ** 7))
    ls = [length(ctx, n) for n in sorted(ctx.board.nets)]
    assert max(ls) - min(ls) <= 0.1 and C.measure(ctx)["groups"][0]["ok"] is True


def test_amplitude_grows_only_when_the_plan_gave_none_and_the_run_is_too_short():
    """/A's only run is 8 mm: at 1 mm amplitude it cannot hold the 14 mm it lacks. Without groups[].amp the tuner
    lays it again with a larger amplitude; with amp: 1.0 the plan's limit stands and the shortfall is reported."""
    pads = [("J1", 1, "/A", 5.0, 10.0), ("U1", 1, "/A", 13.0, 10.0), ("J1", 2, "/B", 5.0, 20.0), ("U1", 2, "/B", 27.0, 20.0)]
    ctx = scene(pads, plan={"groups": [{"nets": ["/A", "/B"], "tol": 0.1, "amp": 1.0}]})
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/A"]
    assert rec["short_by"] > 0.5 and "amp" not in rec and max(abs(p.y1 - 10.0) for p in tuned(ctx)) <= 1.0 + 1e-6
    ctx = scene(pads, plan={"groups": [{"nets": ["/A", "/B"], "tol": 0.1}]})
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/A"]
    assert "short_by" not in rec and rec["amp"] == 2.0 and 21.9 <= length(ctx, "/A") <= 22.0
    assert 1.0 < max(abs(p.y1 - 10.0) for p in tuned(ctx)) <= 2.0 + 1e-6
    assert all(ctx.oracle.piece_ok(p) is None for p in tuned(ctx))


def test_polish_and_the_via_pass_leave_tuned_connections_alone():
    from copper_kicad.pcb.v2 import polish
    ctx = scene()
    tune.run(ctx, Budget(10 ** 7))
    before = ctx.state.export()
    polish.run(ctx, Budget(10 ** 7))
    polish.revia(ctx, Budget(10 ** 7))
    assert ctx.state.export() == before and abs(length(ctx, "/A") - 35.975) < 1e-4


def test_members_of_a_tuned_group_are_routed_as_one_block_longest_first():
    """The longest member sets the group's target: it is routed first so that no other net pushes it into a
    detour every member would then have to match. The block sits where its shortest member would have come,
    at priority 70; nets outside the group keep their rank; COPPER_V2_GROUP_BIAS=none is the order of step A."""
    import os
    from copper_kicad.pcb.v2 import classify
    pads = []
    for i, (n, x2) in enumerate([("/M0", 20.0), ("/M1", 35.0), ("/M2", 27.0), ("/X", 12.0), ("/Y", 50.0)]):
        pads += [("J1", i + 1, n, 5.0, 4.0 + 2 * i), ("U1", i + 1, n, x2, 4.0 + 2 * i)]
    b = TC.board(pads)

    def order(plan, bias=None):
        old = os.environ.pop("COPPER_V2_GROUP_BIAS", None)
        if bias:
            os.environ["COPPER_V2_GROUP_BIAS"] = bias
        try:
            cfg = P.resolve(b, plan)
            cfg.x.cons = C.resolve(b, cfg, cfg.x, C.ProjectRules())
            conns = classify.connections(b, cfg, cfg.x, {})
            return [c.net for c in sorted(conns, key=lambda c: c.rank)], {c.net: c.rank for c in conns}
        finally:
            os.environ.pop("COPPER_V2_GROUP_BIAS", None)
            if old is not None:
                os.environ["COPPER_V2_GROUP_BIAS"] = old
    plan = {"groups": [{"nets": ["/M?"], "tol": 0.5}]}
    base, rb = order(None)
    assert base == ["/X", "/M0", "/M2", "/M1", "/Y"]                   # short first, as ever
    got, rk = order(plan)
    assert got == ["/M1", "/M2", "/M0", "/X", "/Y"]                    # the group first (70), longest member first
    assert rk["/X"] == rb["/X"] and rk["/Y"] == rb["/Y"]
    assert order(plan, "long")[0] == ["/X", "/M1", "/M2", "/M0", "/Y"]  # one block where /M0 (15 mm) would have come
    assert order(plan, "prio")[0] == ["/M0", "/M2", "/M1", "/X", "/Y"]
    none, rn = order(plan, "none")
    assert none == base and rn == rb
    assert order({"global": {"constraints": "report"}, "groups": plan["groups"]})[1] == rb     # nothing tuned: no bias
    assert classify.GROUP_BIAS == "long+prio"


# ── the judged pass (scripted KiCad) ──────────────────────────────────────
PR = types.SimpleNamespace(dir=None, name="x", pcb=None)


def judged(ctx, drc, cls=None):
    """judge.run on the routed scene with a scripted KiCad -> the fake (its calls list starts with that check)."""
    fake = TJ.with_kio((cls or TJ.FakeKicad)(drc))
    judge.run(ctx, Budget(1_000_000), PR, TJ.SCR, [], lambda ids, budget: None)
    return fake


def test_tune_pass_keeps_meanders_kicad_accepts_and_asks_once():
    ctx = scene()
    try:
        fake = judged(ctx, lambda t, v: [])
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    assert len(fake.calls) == 2 and rep.name == "tune" and rep.routed == 1 and rep.failed == 0
    assert 35.9 <= length(ctx, "/A") <= 36.0 and rep.score.new_errors == 0 and rep.score.opens == 0
    assert rep.data["tune"]["nets"]["/A"]["bumps"] == 4 and rep.data["tune"]["rejected"] == {}
    assert [r["round"] for r in rep.data["tune"]["rounds"]] == ["tune0"]
    assert ctx.history["judge"]["last"]["pids"] == ctx.state.export()[2]        # the accepted check is the tuned board
    # a board without a tuned group: KiCad is not asked again, nothing changes
    ctx = scene(plan=None)
    try:
        fake = judged(ctx, lambda t, v: [])
        before = ctx.state.export()
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    assert len(fake.calls) == 1 and ctx.state.export() == before and "tune" not in ctx.history


def test_tune_pass_untunes_only_the_blamed_net_and_tunes_it_elsewhere():
    """KiCad (the script) rejects any /A copper off the run between x = 6 and x = 12. Round 1 lays bumps there,
    the marker names one of them: only /A loses its meanders, the place is closed, round 2 lays them elsewhere and
    is accepted. /B (tuned to an explicit target) keeps its meanders throughout."""
    BAD = (6.0, 10.05, 12.0, 11.5)

    def drc(tracks, vias):
        out = []
        for i, t in enumerate(tracks):
            if t[0] == "/A" and TJ.in_box(t, BAD):
                out.append(("clearance", "Clearance violation (/A meander)", [i], ((t[2] + t[4]) / 2, (t[3] + t[5]) / 2)))
        return out
    ctx = scene(plan={"groups": [{"nets": ["/A", "/B"], "tol": 0.1, "len": 37.0}]})
    try:
        fake = judged(ctx, drc)
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    errs = [r["new_errors"] for r in rep.data["tune"]["rounds"]]
    assert len(errs) == 2 and errs[0] >= 1 and errs[1] == 0 and len(fake.calls) == 3
    assert rep.data["tune"]["rounds"][0]["removed"] == 1 and rep.score.new_errors == 0
    assert abs(length(ctx, "/A") - 37.0) < 1e-3 and abs(length(ctx, "/B") - 37.0) < 1e-3
    assert not any(TJ.in_box([p.net, 0, p.x1, p.y1, p.x2, p.y2], BAD) for p in tuned(ctx, "/A"))
    assert ctx.history["tune"]["avoid"]["/A"] and "/B" not in ctx.history["tune"]["avoid"]
    assert "/A" in rep.data["tune"]["rejected"] and "clearance" in rep.data["tune"]["rejected"]["/A"]
    assert rep.routed == 2 and rep.failed == 0 and all(ctx.state.connected(c.idx) for c in ctx.conns)


def test_tune_pass_gives_up_on_a_net_kicad_rejects_every_time_and_says_so():
    def drc(tracks, vias):
        for i, t in enumerate(tracks):
            if t[0] == "/A" and abs(t[3] - t[5]) > 1e-6:               # any /A piece that leaves the run
                return [("clearance", "Clearance violation", [i], ((t[2] + t[4]) / 2, (t[3] + t[5]) / 2))]
        return []
    ctx = scene(plan={"groups": [{"nets": ["/A", "/B"], "tol": 0.1, "len": 37.0}]})
    try:
        judged(ctx, drc)
        a_before = [(p.x1, p.y1, p.x2, p.y2) for p in ctx.state.pieces(net="/A")]
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    assert [(p.x1, p.y1, p.x2, p.y2) for p in ctx.state.pieces(net="/A")] == a_before       # /A: as routed
    assert abs(length(ctx, "/B") - 37.0) < 1e-3                                             # /B: still tuned
    assert rep.score.new_errors == 0 and rep.data["tune"]["rounds"][-1]["new_errors"] == 0
    rec = rep.data["tune"]["nets"]["/A"]
    assert abs(rec["short_by"] - 6.9) < 1e-3 and "rejected by KiCad" in rec["why"] and rec["bumps"] == 0
    row = C.measure(ctx)["groups"][0]
    assert row["ok"] is False and "rejected by KiCad" in row["why"] and row["tuned"] == 1
    assert rep.failed == 1 and rep.routed == 1


def test_tune_pass_restores_the_accepted_board_when_nobody_can_be_blamed():
    """A new marker far from every meander (and naming none), and a board that would have more open connections:
    both put the accepted board back, piece for piece."""
    def far(tracks, vias):
        return [("clearance", "Clearance violation", [], (55.0, 55.0))] if any(abs(t[3] - t[5]) > 1e-6 for t in tracks) else []
    ctx = scene()
    try:
        fake = judged(ctx, far)
        before = ctx.state.export()
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    assert ctx.state.export() == before and len(fake.calls) == 2 and not tuned(ctx)
    assert rep.score.new_errors == 0 and any("no meander could be blamed" in n for n in rep.notes)
    assert ctx.history["judge"]["last"]["pids"] == before[2]
    row = C.measure(ctx)["groups"][0]                                  # still short, and reported as such
    assert row["ok"] is False and abs(row["short_by"] - 5.9) < 1e-3 and "no single meander could be blamed" in row["why"]
    # opens: KiCad's per-net count says /A is open once it carries meanders -> /A is untuned, never left open
    ctx = scene()
    unc = lambda t, v: {"/A": 1 if any(x[0] == "/A" and abs(x[3] - x[5]) > 1e-6 for x in t) else 0, "/B": 0}   # noqa: E731
    try:
        TJ.with_kio(TJ.FakeKicad(lambda t, v: [], unconnected=unc))
        judge.run(ctx, Budget(1_000_000), PR, TJ.SCR, [], lambda ids, budget: None)
        before = ctx.state.export()[:2]
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    after = ctx.state.export()[:2]
    assert sorted(after[0]) == sorted(before[0]) and after[1] == before[1] and rep.score.opens == 0 and not tuned(ctx)
    assert "unconnected" in rep.data["tune"]["rejected"]["/A"]


def test_tune_pass_takes_a_stretch_on_another_layer_out_first_when_nobody_can_be_blamed():
    """KiCad reports a new error far from every meander once a net carries vias (think: a pour on the other
    layer cut off by the stretch). Before the accepted board is put back — and every other net's meanders with
    it — the stretches on another layer go, for good; the nets that tuned beside their own runs keep theirs,
    and the net that lost its stretch is reported short, with that reason."""
    pads = BOX_PADS + [("J1", 5, "/E", 5.0, 30.0), ("U1", 5, "/E", 35.0, 30.0)]
    plan = {"groups": [{"nets": ["/A", "/B", "/E"], "tol": 0.1}]}

    def far(tracks, vias):
        return [("clearance", "Clearance violation", [], (55.0, 55.0))] if vias else []
    ctx = scene(pads, routes=dict(BOX, **{"/E": None}), plan=plan)
    try:
        fake = judged(ctx, far)
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    assert rep.score.new_errors == 0 and len(fake.calls) == 3          # the judge's check, with the hop, without
    assert not [p for p in ctx.state.pieces() if p.kind == "via"] and not tuned(ctx, "/A")
    assert tuned(ctx, "/E") and 35.9 <= length(ctx, "/E") <= 36.0      # the other member keeps its meanders
    assert "/A" in ctx.history["tune"]["nohop"]
    rec = rep.data["tune"]["nets"]["/A"]
    assert abs(rec["short_by"] - 5.9) < 1e-3 and "stretch on another layer was taken out" in rec["why"], rec
    row = C.measure(ctx)["groups"][0]
    assert row["ok"] is False and row["worst"] == "/A" and "hops" not in row and row["tuned"] == 1
    # with a KiCad that accepts the vias the same board is met, and says that vias were added
    ctx = scene(pads, routes=dict(BOX, **{"/E": None}), plan=plan)
    try:
        fake = judged(ctx, lambda t, v: [])
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    assert len(fake.calls) == 2 and C.measure(ctx)["groups"][0]["ok"] is True
    assert C.measure(ctx)["groups"][0]["hops"] == 1 and any("stretch on another layer" in n for n in rep.notes)


def test_the_note_that_vias_were_added_survives_the_compact_result():
    """The default result keeps four notes; the one that says the router put vias into a length-matched net is
    one the caller acts on (max_vias: 0 undoes it) and must not be the fifth."""
    from copper_kicad.tools import route_tools as RT
    hop = ("length tuning: 1 net(s) had no room beside them on their own layer and carry a stretch on another "
           "layer through two vias each: /A (B.Cu); nets[..].max_vias: 0 keeps a net on its layer")
    out = {"notes": sorted([f"a note {i}" for i in range(6)] + [hop])}
    assert out["notes"].index(hop) >= RT.NOTES_MAX
    RT._compact_v2(out)
    assert len(out["notes"]) == RT.NOTES_MAX and out["notes"][0] == hop and "notes (+3)" in out["more"]


class SkewKicad(TJ.FakeKicad):
    """KiCad that measures /A ``off`` mm shorter than the router does and flags it while it is more than 0.1 mm
    below /B (the longest): a skew marker in kicad-cli's shape, constraint type (never blamed)."""
    off = 0.09

    def commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw):
        res = TJ.FakeKicad.commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw)
        ln = {}
        for t in tracks:
            ln[t[0]] = ln.get(t[0], 0.0) + math.hypot(t[4] - t[2], t[5] - t[3])
        mine, ref = ln["/A"] - self.off, ln["/B"]
        res["raw"] = []
        if ref - mine > 0.1 + 1e-9:
            res["raw"].append({"type": "skew_out_of_range", "severity": "error", "description":
                               f"Skew between tracks out of range (rule 'm' max skew 0.1000 mm; actual {mine - ref:.4f} mm; "
                               f"target net length {ref:.4f} mm (from /B); actual {mine:.4f} mm)",
                               "items": [{"description": f"Track [/A] on F.Cu, length {mine:.4f} mm"}]})
        return res


def test_tune_pass_closes_the_loop_on_kicads_own_length():
    """The router's figure fits (35.975 of 36 +- 0.1) but KiCad measures 0.09 mm less and flags the net: the next
    round tunes it against KiCad's number, i.e. 0.09 mm longer by the router's own figure."""
    ctx = scene()
    try:
        fake = judged(ctx, lambda t, v: [], cls=SkewKicad)
        rep = judge.tune_pass(ctx, PR, TJ.SCR, Budget(10 ** 7))
    finally:
        TJ.teardown()
    assert len(fake.calls) == 3 and abs(rep.data["tune"]["cal"]["/A"] + 0.09) < 1e-4
    assert abs(length(ctx, "/A") - (35.975 + 0.09)) < 2e-3            # moved by KiCad's difference
    last = ctx.history["judge"]["last"]
    assert last["cons"]["counts"] == {} and rep.score.new_errors == 0
    assert any("tuned again to KiCad's" in n for n in rep.notes)


# ── end to end: the matched-bus bench board, judged by KiCad ──────────────
_TMP = None


# ── a stretch on another layer ("hop"): the boxed-in member (fix round 1) ──
# /A (30 mm) with a foreign track 0.6 mm away on either side of its whole run: no bump fits beside it on F.Cu
BOX_PADS = PADS + [("J1", 3, "/C", 5.0, 12.0), ("U1", 3, "/C", 35.0, 12.0), ("J1", 4, "/D", 5.0, 8.0),
                   ("U1", 4, "/D", 35.0, 8.0)]
BOX = {"/A": None, "/B": None, "/C": [(5.0, 12.0), (6.4, 10.6), (33.6, 10.6), (35.0, 12.0)],
       "/D": [(5.0, 8.0), (6.4, 9.4), (33.6, 9.4), (35.0, 8.0)]}


def fields_of(ctx):
    return sorted((p.net, p.conn, p.kind, p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.drill, p.stage, p.protected,
                   p.target_w) for p in ctx.state.pieces())


def test_a_boxed_in_member_takes_a_stretch_on_another_layer_through_two_vias():
    """A track with a neighbour at the clearance on both sides has no room for a single bump. Before this, the
    tuner reported it short on a board whose other layer was empty (cons_mixed /Q_CLK). Now a stretch of the run
    moves to the other layer through two vias and the bumps go there; the vias' height is length (KiCad counts
    it), and the net lands in its window."""
    ctx = scene(BOX_PADS, routes=BOX)
    before = fields_of(ctx)
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/A"]
    assert "short_by" not in rec and rec["hops"] == 1 and rec["hop_layers"] == ["B.Cu"], rec
    assert 35.9 <= length(ctx, "/A") <= 36.0 and rep.failed == 0
    ps = tuned(ctx, "/A")
    vias = [p for p in ps if p.kind == "via"]
    assert len(vias) == 2 and all(abs(v.y1 - 10.0) < 1e-9 and v.w == 0.6 and v.drill == 0.3 for v in vias)
    assert all(ctx.oracle.piece_ok(p) is None for p in ps) and ctx.state.connected(0)
    # on its own layer the net is exactly the straight run it was (cut at the two vias): every bump is on B.Cu
    assert all(abs(p.y1 - 10.0) < 1e-9 and abs(p.y2 - 10.0) < 1e-9 for p in ps if p.kind == "seg" and p.layer == 0)
    back = [p for p in ps if p.kind == "seg" and p.layer == 1]
    x0, x1 = sorted(v.x1 for v in vias)
    assert back and any(abs(p.y1 - 10.0) > 0.3 for p in back)
    assert all(x0 - 1e-6 <= min(p.x1, p.x2) and max(p.x1, p.x2) <= x1 + 1e-6 for p in back)
    assert not [p for p in ps if p.kind == "seg" and p.layer == 0 and x0 + 1e-6 < (p.x1 + p.x2) / 2 < x1 - 1e-6]
    # the vias keep the clearance from the net's own pads (no via in or against a pad)
    for pad in ctx.board.nets["/A"].pads:
        for sh in pad.shapes[0]:
            assert min(sh.dist_seg(v.x1, v.y1, v.x1, v.y1) - v.w / 2 for v in vias) >= 0.2 - 1e-6
    # the length is KiCad's: track copper + the stack height of both vias, nothing cut off inside a via pad
    parts = C.net_length(ctx, "/A")
    h = C.Stackup.default(2).height(0, 1)
    assert parts.vias == 2 and abs(parts.via - 2 * h) < 1e-9 and abs(parts.inpad) < 1e-9
    assert abs(parts.total - (parts.track + 2 * h)) < 1e-9
    row = C.measure(ctx)["groups"][0]
    assert row["ok"] is True and row["hops"] == 1 and row["tuned"] == 1 and "short_by" not in row
    # the same board again: the same copper; a second pass changes nothing; untune puts the routed shape back
    again = scene(BOX_PADS, routes=BOX)
    tune.run(again, Budget(10 ** 7))
    assert fields_of(again) == fields_of(ctx)
    laid = fields_of(ctx)
    tune.run(ctx, Budget(10 ** 7))
    assert fields_of(ctx) == laid
    assert tune.untune(ctx, ["/A"]) == 1 and fields_of(ctx) == before and ctx.state.connected(0)


def test_no_stretch_on_another_layer_when_the_plan_or_the_length_forbids_it():
    """max_vias: 0 keeps the net on its layer; two vias that alone would overshoot are not laid; a no_vias
    region and a forbid region keep the vias out. Each time the net is reported short, with the reason."""
    grp = {"groups": [{"nets": ["/A", "/B"], "tol": 0.1}]}
    ctx = scene(BOX_PADS, routes=BOX, plan=dict(grp, nets={"/A": {"max_vias": 0}}))
    rec = tune.run(ctx, Budget(10 ** 7)).data["nets"]["/A"]
    assert not tuned(ctx) and abs(rec["short_by"] - 5.9) < 1e-6 and "hops" not in rec
    assert C.measure(ctx)["groups"][0]["ok"] is False
    # /B only 1.5 mm longer: the two vias of a hop add 3.16 mm, which would make /A the longest
    pads = [p if p[:3] != ("U1", 2, "/B") else ("U1", 2, "/B", 36.5, 20.0) for p in BOX_PADS]
    ctx = scene(pads, routes=BOX)
    rec = tune.run(ctx, Budget(10 ** 7)).data["nets"]["/A"]
    assert not tuned(ctx) and abs(rec["short_by"] - 1.4) < 1e-6 and "more than is missing" in rec["why"], rec
    # vias forbidden along the whole run (regions[].no_vias), and a forbid region on every layer
    for reg in ({"rect": [4.0, 9.0, 36.0, 11.0], "no_vias": True, "cost": 1.0},
                {"rect": [5.7, 9.75, 34.3, 9.85], "forbid": True}):
        ctx = scene(BOX_PADS, routes=BOX, plan=dict(grp, regions=[reg]))
        rec = tune.run(ctx, Budget(10 ** 7)).data["nets"]["/A"]
        assert not tuned(ctx) and abs(rec["short_by"] - 5.9) < 1e-6 and "no two legal via sites" in rec["why"], rec


def test_a_short_free_stretch_takes_one_loop_between_two_vias():
    """Where the other layer is taken along the run (a foreign track on B.Cu under most of it) only a short
    stretch can change layer: two vias 1 mm apart and one loop at right angles between them. Its legs leave
    the via centres radially, so the figure is exact: 2A - 2c(2 - sqrt 2) on top of the via height."""
    under = [["/C", 1, 8.2, 10.0, 35.0, 10.0, 0.2, 0, False]]
    ctx = scene(BOX_PADS, routes=BOX, tracks=under)
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/A"]
    assert "short_by" not in rec and rec["hops"] == 1 and 35.9 <= length(ctx, "/A") <= 36.0, rec
    ps = tuned(ctx, "/A")
    vias = sorted((p for p in ps if p.kind == "via"), key=lambda p: p.x1)
    assert len(vias) == 2 and abs((vias[1].x1 - vias[0].x1) - 1.0) < 1e-9 and vias[1].x1 < 8.2 - 0.6
    assert all(ctx.oracle.piece_ok(p) is None for p in ps) and ctx.state.connected(0)
    back = [p for p in ps if p.kind == "seg" and p.layer == 1]
    assert len(back) == 5                                              # leg, chamfer, top, chamfer, leg
    legs = [p for p in back if abs(p.x1 - p.x2) < 1e-9]
    assert sorted(round(p.x1, 6) for p in legs) == [round(v.x1, 6) for v in vias]     # straight out of the via centres
    amp = max(abs(p.y1 - 10.0) for p in back)
    loop = sum(p.length for p in back) - 1.0
    assert abs(loop - (2 * amp - 2 * 0.2 * (2 - math.sqrt(2)))) < 1e-5
    assert abs(length(ctx, "/A") - (30.0 + 2 * C.Stackup.default(2).height(0, 1) + loop)) < 1e-5
    assert amp > 1.0 and rec["amp"] in (2.0, 4.0)                      # the plan gave no amplitude: it grew


def test_a_hop_and_bumps_on_what_it_left_of_the_run_are_undone_together():
    """After a hop the two parts of the run before and behind the vias are still straight copper on the run's
    own layer: bumps go there too (a second edit, on a piece the first one made). ``untune`` undoes both and
    the routed shape is back piece for piece."""
    pads = [p if p[:3] != ("U1", 2, "/B") else ("U1", 2, "/B", 50.0, 20.0) for p in PADS]      # /B 45 mm: 15 to add
    under = [["/B", 1, 9.0, 10.0, 35.0, 10.0, 0.2, 0, False]]                                 # B.Cu taken from x = 9 on
    plan = {"groups": [{"nets": ["/A", "/B"], "tol": 0.1, "amp": 1.0}],
            "regions": [{"rect": [12.0, 8.0, 36.0, 9.85], "forbid": True}, {"rect": [12.0, 10.15, 36.0, 12.0], "forbid": True}]}
    ctx = scene(pads, plan=plan, tracks=under)
    before = fields_of(ctx)
    rep = tune.run(ctx, Budget(10 ** 7))
    rec = rep.data["nets"]["/A"]
    ps = tuned(ctx, "/A")
    assert rec["hops"] == 1 and len([p for p in ps if p.kind == "via"]) == 2
    assert [p for p in ps if p.kind == "seg" and p.layer == 0 and abs(p.y1 - p.y2) > 1e-6]     # bumps on F.Cu as well
    assert len(ctx.history["tune"]["edits"]["/A"]) >= 2
    assert all(ctx.oracle.piece_ok(p) is None for p in ps) and ctx.state.connected(0)
    # more was added than either way alone could, and what is still missing is said (amp 1.0 is the plan's limit)
    assert rec["added"] > 2 * C.Stackup.default(2).height(0, 1) + 3.0 and rec["short_by"] > 0 and rec["why"]
    assert abs(length(ctx, "/A") - (30.0 + rec["added"])) < 1e-3
    assert tune.untune(ctx, ["/A"]) >= 2 and fields_of(ctx) == before and ctx.state.connected(0)
    assert not ctx.history["tune"]["edits"].get("/A") and not ctx.history["tune"].get("free", {}).get("/A")


def test_tuner_copper_stays_out_of_forbidden_areas_with_its_whole_width():
    """The oracle keeps centre lines out of the plan's forbidden areas. What the tuner adds — bumps, a hop's
    vias — stays out with all of its copper: no edge of a bump and no part of a via pad is inside one."""
    plan = {"groups": [{"nets": ["/A", "/B"], "tol": 0.1}],
            "regions": [{"rect": [9.0, 8.0, 36.0, 9.9], "forbid": True}, {"rect": [9.0, 10.1, 36.0, 12.0], "forbid": True}]}
    ctx = scene(plan=plan)
    tune.run(ctx, Budget(10 ** 7))
    ps = tuned(ctx, "/A")
    assert ps
    for p in ps:
        hw = p.w / 2
        for r in plan["regions"]:
            x0, y0, x1, y1 = r["rect"]
            d = geom.seg_rect_dist(p.x1 - (x0 + x1) / 2, p.y1 - (y0 + y1) / 2, p.x2 - (x0 + x1) / 2,
                                   p.y2 - (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2)
            on_run = abs(p.y1 - 10.0) < 1e-9 and abs(p.y2 - 10.0) < 1e-9 and p.kind == "seg"
            assert on_run or d >= hw - 1e-6, (p, d)                   # (the routed run itself lies in the 0.2 mm channel)


def test_skew_bumps_go_lower_where_a_high_one_does_not_fit():
    """Fix round 1: skew compensation tried one bump height (the highest that covers the need with the fewest
    bumps) and gave up where a neighbour stood too near for it. A track 0.5 mm beside the run on either side
    leaves 0.1 mm for a bump: several of the lowest bumps add what one high bump would have."""
    pads = PADS + [("J1", 3, "/C", 5.0, 12.0), ("U1", 3, "/C", 35.0, 12.0), ("J1", 4, "/D", 5.0, 8.0),
                   ("U1", 4, "/D", 35.0, 8.0)]
    walls = {"/A": None, "/B": None, "/C": [(5.0, 12.0), (6.5, 10.5), (33.5, 10.5), (35.0, 12.0)],
             "/D": [(5.0, 8.0), (6.5, 9.5), (33.5, 9.5), (35.0, 8.0)]}
    ctx = scene(pads, routes=walls)
    T_ = tune._Tuner(ctx, Budget(10 ** 7))
    l0 = length(ctx, "/A")
    rec = T_.skew_net("/A", l0 + 0.30, l0 + 0.25, l0 + 0.35, 0.3)
    assert "short_by" not in rec and rec["bumps"] >= 4, rec
    assert l0 + 0.25 <= length(ctx, "/A") <= l0 + 0.35
    ps = tuned(ctx, "/A")
    assert all(ctx.oracle.piece_ok(p) is None for p in ps)
    assert max(abs(p.y1 - 10.0) for p in ps) <= 0.1 + 1e-6            # no bump higher than the room there is
    # nothing fits at all (walls at the clearance): reported, with the amount
    walls2 = dict(walls, **{"/C": [(5.0, 12.0), (6.6, 10.4), (33.4, 10.4), (35.0, 12.0)],
                            "/D": [(5.0, 8.0), (6.6, 9.6), (33.4, 9.6), (35.0, 8.0)]})
    ctx = scene(pads, routes=walls2)
    rec = tune._Tuner(ctx, Budget(10 ** 7)).skew_net("/A", l0 + 0.30, l0 + 0.25, l0 + 0.35, 0.3)
    assert rec["bumps"] == 0 and abs(rec["short_by"] - 0.25) < 1e-6 and "no free straight run" in rec["why"]


def have_kicad() -> bool:
    return common.KICAD_CLI.exists() and common.KICAD_PY.exists() and \
        (common.CORPUS / "cons_bus_match" / "cons_bus_match.kicad_pcb").exists()


def corpus_project(board: str, tag: str):
    from copper_kicad import project as projmod
    global _TMP
    if _TMP is None:
        _TMP = Path(tempfile.mkdtemp(prefix="tune-", dir=str(common.WORK)))
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


def test_e2e_matched_bus_is_tuned_clean_by_kicads_own_drc_and_deterministic():
    """cons_bus_match: five SD nets with a KiCad rule `skew (max 0.5mm)` and no plan constraint at all. A dry run
    writes nothing; the real run writes a board on which KiCad's DRC — the skew rule included — has no error and
    nothing is unconnected; KiCad's own net lengths lie within 0.5 mm; a second project gets the same copper."""
    if PURE or not have_kicad():
        print("  (skipped: needs KiCad 10 and the constraint bench boards)")
        return
    from copper_kicad.pcb import guided
    from copper_kicad.pcb.v2 import planx
    nets = ["/SD_CLK", "/SD_D0", "/SD_D1", "/SD_D2", "/SD_D3"]
    pr = corpus_project("cons_bus_match", "a")
    before = pr.pcb.read_bytes()
    plan = planx.v2_plan(guided.analyze(pr, ripup="none")["suggested_plan"])
    assert "groups" not in plan                                        # the rule file is the only source
    out = guided.route(pr, plan, dry_run=True)
    sc = out.pop("_scratch", None)
    try:
        assert pr.pcb.read_bytes() == before and out["written"] is False and out["dry_run"] is True
        g = out["constraints"]["groups"][0]
        assert g["ok"] is True and g["src"] == "dru" and g["tuned"] >= 3 and g["added"] > 5 and "short_by" not in g
        assert out["drc"]["new_errors"] == 0 and "constraint_errors" not in out["drc"]
        assert sc is not None and common.drc(sc.pcb)["errors"] == 0
    finally:
        if sc is not None:
            shutil.rmtree(sc.dir, ignore_errors=True)
    assert not list(pr.dir.glob("*.v2base")) and not list(pr.dir.glob("*.v2best"))
    out = guided.route(pr, plan)
    assert out["written"] is True and out["routed"] == out["total"] == 6 and out["drc"]["new_errors"] == 0
    assert any(p["pass"] == "tune" for p in out["passes"])
    g = out["constraints"]["groups"][0]
    assert g["ok"] is True and g["max"] - g["min"] <= 0.5 and out["constraints"]["missed"] == 0
    assert out["constraints"]["kicad"] == {} and out["constraints"].get("len_model_err", 0.0) <= 0.005
    # KiCad's own verdict on the written file, with the board's own rule file
    d = common.drc(pr.pcb)
    assert d["errors"] == 0 and d["unconnected"] == 0, d
    assert not any(t in str(d.get("by_type")) for t in C.KICAD_TYPES), d.get("by_type")
    kl = common.kicad_lengths(pr.pcb, nets)
    assert set(kl) == set(nets) and max(kl.values()) - min(kl.values()) <= 0.5, kl
    for n in nets:                                                     # the router's figure is KiCad's
        assert abs(out["constraints"]["nets"][n][0] - kl[n]) <= 0.005, (n, out["constraints"]["nets"][n], kl[n])
    # the same board again: the same copper
    pr2 = corpus_project("cons_bus_match", "b")
    out2 = guided.route(pr2, planx.v2_plan(guided.analyze(pr2, ripup="none")["suggested_plan"]))
    assert out2["written"] is True and copper_of(pr2) == copper_of(pr)
    # constraints 'off' lays no meander and leaves KiCad's skew errors standing (the rule is real)
    pr3 = corpus_project("cons_bus_match", "c")
    plan3 = planx.v2_plan(guided.analyze(pr3, ripup="none")["suggested_plan"])
    plan3.setdefault("global", {})["constraints"] = "off"
    out3 = guided.route(pr3, plan3)
    assert not any(p["pass"] == "tune" for p in out3["passes"]) and "constraints" not in out3
    assert common.drc(pr3.pcb)["errors"] >= 1


if __name__ == "__main__":
    import time
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v) and (not only or k in only)]
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.1f}s)")
        except Exception as e:  # noqa: BLE001
            fails += 1
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    if _TMP is not None:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
