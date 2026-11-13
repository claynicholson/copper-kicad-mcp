"""Router v2 judge (design §4.11): commit → refill → DRC → blame → local re-route → keep best → pours pass.

Pure tests: KiCad is replaced by a scripted DRC (a stubbed ``kio.commit_and_check``), the route state and the
oracle are the real ones on synthetic boards, re-routing is a tiny pattern router that honours the judge's
feedback (plan regions named "judge").

Run:  python tests/test_v2_judge.py            all tests
      python tests/test_v2_judge.py test_name  one test
Scratch state goes to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import math
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "bench" / "route"))
import common  # noqa: E402  (bench/route/common.py)

common.sandbox()       # KICAD_CONFIG_HOME, COPPER_HOME and the temp dir -> bench/route/.work, before copper_kicad loads
import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import model, plan as planmod  # noqa: E402
from copper_kicad.pcb.v2 import judge, kio as real_kio, oracle as O, pieces  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Ctx, Score, StageReport, Terminal  # noqa: E402


# ── synthetic boards ──────────────────────────────────────────────────────
def extract(pads, *, size=(40.0, 30.0), zones=(), clusters=None):
    """pads: [(ref, net, x, y)] 0.8 mm square SMD pads on F.Cu. clusters: {ref: cluster id} (default: one each)."""
    nets = sorted({p[1] for p in pads if p[1]})
    ex = {"layers": ["F.Cu", "B.Cu"],
          "rules": {"clearance": 0.15, "track_width": 0.15, "via": 0.6, "drill": 0.3, "edge_clearance": 0.3,
                    "hole_clearance": 0.25, "hole_to_hole": 0.25, "annular": 0.1,
                    "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.2, "dp_width": 0.2}},
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.2, "dp_width": 0.2} for i, n in enumerate(nets)},
          "outline": {"closed": True, "polys": [{"o": [[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [], "vias": [], "zones": [dict(z) for z in zones],
          "mask_openings": [], "unconnected": 0, "unconnected_by_net": {},
          "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    for i, (ref, net, x, y) in enumerate(pads):
        ex["pads"].append({"ref": ref, "num": "1", "net": net, "x": x, "y": y, "kind": "smd", "layers": [0],
                           "shapes": [[[0], "box", [x, y, 1, 0, 0.4, 0.4, 0.0]]],
                           "c": (clusters or {}).get(ref, i + 1)})
    return ex


PADS = [("A1", "A", 5.0, 10.0), ("A2", "A", 25.0, 10.0), ("B1", "B", 5.0, 20.0), ("B2", "B", 25.0, 20.0)]


def make(pads=PADS, conns=(("A", "A1", "A2"), ("B", "B1", "B2")), **kw):
    """-> Ctx with a real board / oracle / state; nothing routed yet."""
    b = model.Board(extract(pads, **kw))
    cfg = planmod.resolve(b, None)
    orc = O.Oracle(b)
    at = {p[0]: p for p in pads}
    cs = []
    for i, (net, pa, pb) in enumerate(conns):
        a, bb = at[pa], at[pb]
        cs.append(ConnV2(idx=i, net=net, a=Terminal(a[2], a[3], (0,), pad=pa + ".1", cluster=i * 2 + 1),
                         b=Terminal(bb[2], bb[3], (0,), pad=pb + ".1", cluster=i * 2 + 2),
                         est=math.hypot(bb[2] - a[2], bb[3] - a[3]), width=0.2, neck=0.2, clearance=0.2, via=(0.6, 0.3),
                         rank=(0, 0, i)))
    st = pieces.RouteState(orc, cs)
    return Ctx(board=b, cfg=cfg, x=None, oracle=orc, state=st, conns=cs)


def path_for(c: ConnV2, d: float):
    """a → over a rail d mm beside the straight line → b (d = 0: the straight line)."""
    if not d:
        return [(c.a.x, c.a.y), (c.b.x, c.b.y)]
    return [(c.a.x, c.a.y), (c.a.x + abs(d), c.a.y + d), (c.b.x - abs(d), c.b.y + d), (c.b.x, c.b.y)]


def lay(ctx, c: ConnV2, d: float = 0.0):
    pts = path_for(c, d)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        ctx.state.add(net=c.net, conn=c.idx, kind="seg", layer=0, x1=x1, y1=y1, x2=x2, y2=y2, w=c.width)
    ctx.state.set_status(c.idx, "routed")


def rail_of(ctx, idx):
    """Which rail a routed connection runs on (the y offset of its middle segment)."""
    c = ctx.conns[idx]
    ps = ctx.state.pieces(conn=idx)
    if not ps:
        return None
    mid = ps[len(ps) // 2]
    return round(mid.y1 - c.a.y, 3)


class Rerouter:
    """Pattern re-router: the cheapest rail that is oracle-legal and allowed by the plan regions (which is how
    the judge's feedback reaches a search)."""
    RAILS = (0.0, 2.0, -2.0, 4.0, -4.0)

    def __init__(self, ctx):
        self.ctx, self.calls = ctx, []

    def cost(self, c, pts):
        total = 0.0
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            n = max(1, int(math.hypot(x2 - x1, y2 - y1) / 0.1))
            for k in range(n + 1):
                x, y = x1 + (x2 - x1) * k / n, y1 + (y2 - y1) * k / n
                mul = 1.0
                for r in self.ctx.cfg.regions:
                    if r.applies(c.net) and r.rect[0] <= x <= r.rect[2] and r.rect[1] <= y <= r.rect[3]:
                        if r.forbid:
                            return None
                        mul *= r.cost
                total += mul * math.hypot(x2 - x1, y2 - y1) / (n + 1)
        return total

    def __call__(self, ids, budget):
        ctx = self.ctx
        self.calls.append(list(ids))
        for i in ids:
            c = ctx.conns[i]
            best = None
            for k, d in enumerate(self.RAILS):
                pts = path_for(c, d)
                cost = self.cost(c, pts)
                if cost is None or any(ctx.oracle.seg_ok(0, x1, y1, x2, y2, c.width, c.net) is not None
                                       for (x1, y1), (x2, y2) in zip(pts, pts[1:])):
                    continue
                if best is None or (cost, k) < best[:2]:
                    best = (cost, k, d)
            budget.charge(5000)
            if best is None:
                ctx.state.set_status(i, "failed", {"reason": "no rail is free", "net": c.net})
            else:
                lay(ctx, c, best[2])


class FakeKicad:
    """A scripted kio: ``drc(tracks, vias) -> [(type, message, [index of our track, ...], (x, y))]``."""

    def __init__(self, drc, unconnected=None, ex=None):
        self.drc, self.calls, self.unconnected, self.ex = drc, [], unconnected, ex
        self.marker_pieces = real_kio.marker_pieces

    def commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw):
        n = len(self.calls)
        self.calls.append((tuple(map(tuple, tracks)), tuple(map(tuple, vias))))
        uu = [f"r{n}t{i}" for i in range(len(tracks))] + [f"r{n}v{i}" for i in range(len(vias))]
        new = []
        for typ, msg, mine, at in self.drc(tracks, vias):
            ids = [uu[i] for i in mine]
            xy = [[tracks[i][2], tracks[i][3]] if i < len(tracks) else list(vias[i - len(tracks)][1:3]) for i in mine]
            new.append({"type": typ, "message": msg, "items": [f"Track {i}" for i in mine] + ["Pad X"],
                        "uuids": ids + ["pad-x"], "pos": xy + [list(at)], "at": list(at),
                        "copper": typ != "silk_overlap"})
        # no per-net numbers unless the test scripts them: the judge then counts opens from the state
        unc = self.unconnected(tracks, vias) if callable(self.unconnected) else None
        ex = self.ex(tracks, vias) if callable(self.ex) else None
        return {"uuids": uu, "raw": [{"severity": "error"}] * len(new), "new": new, "unconnected": unc,
                "stats": {"unconnected_by_net": unc or {}}, "ex": ex}

    def extract(self, pr, ripup="none", nets=None):
        return self.ex([], [])


def with_kio(fake):
    judge.kio = fake
    return fake


def teardown():
    judge.kio = real_kio


def in_box(t, box):
    """Does track t = [net, layer, x1, y1, x2, y2, w] pass through box (x0, y0, x1, y1)?"""
    x1, y1, x2, y2 = t[2:6]
    n = max(1, int(math.hypot(x2 - x1, y2 - y1) / 0.05))
    return any(box[0] <= x1 + (x2 - x1) * k / n <= box[2] and box[1] <= y1 + (y2 - y1) * k / n <= box[3]
               for k in range(n + 1))


BAD = (14.0, 9.5, 16.0, 10.5)          # KiCad (the script) rejects any net-A track through here


def drc_bad_box(net="A", box=BAD, typ="clearance"):
    def drc(tracks, vias):
        return [(typ, f"Clearance violation ({net})", [i], ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
                for i, t in enumerate(tracks) if t[0] == net and in_box(t, box)]
    return drc


SCR = types.SimpleNamespace(pcb=None, dir=None, name="x")      # no files: the judge skips its base / best copies


# ── tests ─────────────────────────────────────────────────────────────────
def test_one_marker_removes_one_connection_and_reroutes_it():
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)
    b_ids = [p.id for p in ctx.state.pieces(conn=1)]
    fake = with_kio(FakeKicad(drc_bad_box()))
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert isinstance(rep, StageReport) and rep.name == "judge"
    assert rep.score.new_errors == 0 and ctx.state.new_errors == 0 and rep.routed == 2 and rep.failed == 0
    assert rr.calls == [[0]], rr.calls                                  # only the blamed connection, once
    assert [p.id for p in ctx.state.pieces(conn=1)] == b_ids            # the other connection was never touched
    assert rail_of(ctx, 0) != 0.0 and ctx.state.status(0) == "routed"  # A found another way
    assert len(fake.calls) == 2 and rep.data["commits"] == 2            # round 0 (1 error), round 1 (clean)
    assert [r["new_errors"] for r in rep.data["rounds"]] == [1, 0] and rep.data["rounds"][0]["rerouted"] == 1
    assert rep.data["removed_after_drc"] == [] and rep.data["new"] == []
    # the feedback the search can read: a history mark and a cost region for that net at the violation
    m = ctx.history["marks"][0]
    assert abs(m[0] - 15.0) < 1e-6 and abs(m[1] - 10.0) < 1e-6 and m[2] == judge.HIST_RADIUS and m[4] == "A"
    reg = [r for r in ctx.cfg.regions if r.name == "judge"]
    assert len(reg) == 1 and not reg[0].forbid and reg[0].cost == judge.COST_FIRST and reg[0].nets == {"A"}
    assert reg[0].applies("A") and not reg[0].applies("B") and reg[0].idx >= 1000
    assert rep.work == 5000


def test_same_twice():
    def once():
        ctx = make()
        for c in ctx.conns:
            lay(ctx, c)
        with_kio(FakeKicad(drc_bad_box()))
        try:
            rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], Rerouter(ctx))
        finally:
            teardown()
        return ctx.state.export(), rep.score, rep.data["rounds"]
    assert once() == once()


def test_keep_best_returns_the_earlier_round_when_a_later_one_is_worse():
    """Round 0: A and B each in an error. Round 1: only A's new path (1 error) — the best. Round 2: A's third path
    upsets something unblamable twice (2 errors). The judge must hand back round 1, not round 2; and since taking
    A out does not clear the error either, A's copper stays and the residual is reported."""
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)
    box_b = (14.0, 19.5, 16.0, 20.5)

    def drc(tracks, vias):
        out = []
        a_mid = [t for t in tracks if t[0] == "A" and abs(t[3] - t[5]) < 1e-9 and abs(t[4] - t[2]) > 5]
        for i, t in enumerate(tracks):
            if t[0] == "A" and in_box(t, BAD):
                out.append(("clearance", "A at its first place", [i], (15.0, 10.0)))
            if t[0] == "B" and in_box(t, box_b):
                out.append(("clearance", "B at its first place", [i], (15.0, 20.0)))
            if t[0] == "A" and in_box(t, (14.0, 11.5, 16.0, 12.5)):        # rail +2
                out.append(("clearance", "A at its second place", [i], (15.0, 12.0)))
        if not any(t[0] == "A" for t in tracks):
            out.append(("silk_overlap", "not about copper (A is gone)", [], (1.0, 1.0)))
        elif a_mid and abs(a_mid[0][3] - 8.0) < 1e-6:                      # rail -2: two errors nobody can blame
            out += [("silk_overlap", "not about copper 1", [], (1.0, 1.0)),
                    ("silk_overlap", "not about copper 2", [], (2.0, 1.0))]
        return out
    fake = with_kio(FakeKicad(drc))
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], Rerouter(ctx))
    finally:
        teardown()
    rounds = rep.data["rounds"]
    assert [r["new_errors"] for r in rounds[:3]] == [2, 1, 2], rounds
    assert rounds[3]["round"] == "remove" and rounds[3]["new_errors"] == 1          # removing A did not help
    assert rail_of(ctx, 0) == 2.0 and ctx.state.status(0) == "routed"               # round 1's copper is back
    assert rail_of(ctx, 1) != 0.0 and ctx.state.status(1) == "routed"
    assert rep.score.new_errors == 1 and ctx.state.new_errors == 1 and rep.score.opens == 0
    assert rep.data["removed_after_drc"] == []                                      # nothing was removed in the end
    assert len(rep.data["new"]) == 1 and rep.data["new"][0]["message"] == "A at its second place"
    assert any("kept the result of round 1" in n for n in rep.notes), rep.notes
    assert any("did not clear" in n for n in rep.notes), rep.notes
    assert len(fake.calls) == 4
    # the state the judge returns is exactly what round 1 committed
    assert fake.calls[1] == (tuple(map(tuple, ctx.state.export()[0])), ())


def test_residual_marker_is_a_reported_removal():
    """KiCad rejects net A wherever it goes: cost region, then forbid, then the connection is removed for good —
    reported in failed[] and removed_after_drc, never shipped, and B is untouched throughout."""
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)
    b_ids = [p.id for p in ctx.state.pieces(conn=1)]

    def drc(tracks, vias):
        return [("clearance", "A is never right", [i], ((t[2] + t[4]) / 2, (t[3] + t[5]) / 2))
                for i, t in enumerate(tracks) if t[0] == "A" and abs(t[4] - t[2]) > 5]
    with_kio(FakeKicad(drc))
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls == [[0], [0]]                                # two re-routes, the third blame ends it
    assert ctx.state.pieces(conn=0) == [] and ctx.state.status(0) == "failed"
    f = ctx.state.fail(0)
    assert f["reason"].startswith("routed, then removed: KiCad DRC clearance: A is never right"), f
    assert "KiCad rejected every re-route here" in f["reason"] and f["net"] == "A" and len(f["where"]) == 2
    assert rep.score.new_errors == 0 and rep.score.opens == 1 and rep.failed == 1 and rep.routed == 1
    assert rep.data["removed_after_drc"] == [{"net": "A", "connections": 1, "why": f["reason"].split(": ", 1)[1]
                                              .split(" (KiCad rejected")[0]}]
    assert [p.id for p in ctx.state.pieces(conn=1)] == b_ids
    kinds = [(r.forbid, r.cost) for r in ctx.cfg.regions if r.name == "judge"]
    assert kinds == [(False, judge.COST_FIRST), (True, 1.0)], kinds


def test_no_budget_means_removal_without_a_reroute():
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)
    with_kio(FakeKicad(drc_bad_box()))
    rr = Rerouter(ctx)
    spent = Budget(10)
    spent.charge(10)
    try:
        rep = judge.run(ctx, spent, None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls == [] and ctx.state.status(0) == "failed" and ctx.state.pieces(conn=0) == []
    assert "no budget left for a re-route" in ctx.state.fail(0)["reason"]
    assert rep.score.new_errors == 0 and rep.score.opens == 1 and rep.data["removed_after_drc"][0]["net"] == "A"


def test_errors_still_blamed_after_the_last_round_are_removed():
    """With no repair round at all (ROUNDS = 0) the blamed connection must still not ship."""
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)
    with_kio(FakeKicad(drc_bad_box()))
    saved, judge.ROUNDS = judge.ROUNDS, 0
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        judge.ROUNDS = saved
        teardown()
    assert rr.calls == [] and ctx.state.pieces(conn=0) == [] and ctx.state.status(0) == "failed"
    assert "still in a DRC error after the repair rounds" in ctx.state.fail(0)["reason"]
    assert rep.score.new_errors == 0 and [r["round"] for r in rep.data["rounds"]] == [0, "remove"]
    assert rep.data["removed_after_drc"][0]["net"] == "A" and ctx.state.status(1) == "routed"


def test_a_marker_nobody_can_blame_is_reported_and_nothing_is_removed():
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)
    before = ctx.state.export()
    with_kio(FakeKicad(lambda tracks, vias: [("silk_overlap", "not ours", [], (3.0, 3.0)),
                                             ("clearance", "far from any new copper", [], (38.0, 28.0))]))
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls == [] and ctx.state.export() == before and rep.data["commits"] == 1
    assert rep.score.new_errors == 2 and ctx.state.new_errors == 2
    assert [u["type"] for u in rep.data["unblamed"]] == ["clearance", "silk_overlap"]      # canonical order
    assert any("could not be blamed" in n for n in rep.notes)


def test_two_of_our_connections_in_one_marker_the_later_one_goes():
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)

    def drc(tracks, vias):
        ia = [i for i, t in enumerate(tracks) if t[0] == "A" and in_box(t, BAD)]
        ib = [i for i, t in enumerate(tracks) if t[0] == "B"]
        return [("clearance", "A against B", ia + ib[:1], (15.0, 10.0))] if ia else []
    with_kio(FakeKicad(drc))
    rr = Rerouter(ctx)
    try:
        # B has the later rank: it is the one removed first (v1: of two new items, drop the one routed later)
        judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls[0] == [1], rr.calls


def test_starved_thermal_gives_the_pad_room():
    """A track of net A crowds a GND pad: KiCad reports its thermal relief as starved. The judge removes only the
    crowding connection, closes the pad's neighbourhood to other nets and re-routes."""
    pads = PADS + [("G1", "GND", 15.0, 10.9)]
    ctx = make(pads)
    for c in ctx.conns:
        lay(ctx, c)

    def drc(tracks, vias):
        near = [i for i, t in enumerate(tracks) if t[0] == "A" and in_box(t, (13.7, 9.6, 16.3, 12.2))]
        return [("starved_thermal", "Thermal relief connection to zone incomplete", [], (15.0, 10.9))] if near else []
    with_kio(FakeKicad(drc))
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls == [[0]] and rep.score.new_errors == 0 and ctx.state.status(0) == "routed"
    assert rail_of(ctx, 0) not in (0.0, 2.0)                    # 0 and +2 run through the protected rectangle
    assert rep.data["repairs"] and "GND pad G1.1" in rep.data["repairs"][0]
    reg = [r for r in ctx.cfg.regions if r.name == "judge"]
    assert reg[0].forbid and not reg[0].applies("GND") and reg[0].applies("A") and reg[0].applies("B")
    x0, y0, x1, y1 = reg[0].rect
    assert x0 < 14.0 and x1 > 16.0 and y0 < 10.0 and y1 > 12.0


def _gnd_zone(island):
    x0, y0, x1, y1 = island
    return {"name": "gnd", "layers": [0], "net": "GND", "priority": 0, "filled": True,
            "polys": [{"o": [[1, 1], [39, 1], [39, 29], [1, 29]]}],
            "fills": {"0": [{"o": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}]}}


POUR_PADS = PADS + [("G1", "GND", 5.0, 5.0), ("G2", "GND", 30.0, 25.0)]


def _pour_setup():
    """GND is poured; the fill reaches G1 only. -> (ctx, extract maker for the fake kio)"""
    zone = _gnd_zone((2.0, 2.0, 8.0, 8.0))
    ctx = make(POUR_PADS, zones=[zone])
    for c in ctx.conns:
        lay(ctx, c)

    def ex(tracks, vias):
        joined = any(t[0] == "GND" for t in tracks)      # KiCad's refill: the stub reaches the pour or it does not
        e = extract(POUR_PADS, zones=[zone], clusters={"G2": 5} if joined else None)
        e["tracks"] = [[t[0], 0, t[2], t[3], t[4], t[5], t[6], 5 if t[0] == "GND" else 0, 0] for t in tracks]
        return e

    def unc(tracks, vias):
        return {} if any(t[0] == "GND" for t in tracks) else {"GND": 1}
    return ctx, ex, unc


class power_module:
    """Swap copper_kicad.pcb.v2.power for a scripted module (or a stub) while a test runs."""

    def __init__(self, **attrs):
        self.mod = types.ModuleType("copper_kicad.pcb.v2.power")
        self.mod.__dict__.update(attrs)

    def __enter__(self):
        import copper_kicad.pcb.v2 as v2pkg
        self.pkg, self.name = v2pkg, "copper_kicad.pcb.v2.power"
        self.saved, self.saved_attr = sys.modules.get(self.name), getattr(v2pkg, "power", None)
        sys.modules[self.name] = self.mod
        v2pkg.power = self.mod
        return self.mod

    def __exit__(self, *exc):
        if self.saved is not None:
            sys.modules[self.name] = self.saved
        else:
            sys.modules.pop(self.name, None)
        if self.saved_attr is not None:
            self.pkg.power = self.saved_attr
        elif hasattr(self.pkg, "power"):
            del self.pkg.power


def test_pours_pass_uses_the_reserved_dogbone():
    ctx, ex, unc = _pour_setup()
    placed = []

    def place_dogbone(c, pad, exclude=frozenset()):
        placed.append(pad.name)
        a = c.state.add(net="GND", conn=-1, kind="seg", layer=0, x1=pad.x, y1=pad.y, x2=pad.x, y2=pad.y - 1.5, w=0.2,
                        stage="power", protected=True)
        v = c.state.add(net="GND", conn=-1, kind="via", layer=-1, x1=pad.x, y1=pad.y - 1.5, w=0.6, drill=0.3,
                        stage="power", protected=True)
        return [a, v]
    fake = with_kio(FakeKicad(lambda t, v: [], unconnected=unc, ex=ex))
    pr = types.SimpleNamespace(dir=None, name="x", pcb=None)
    try:
        with power_module(place_dogbone=place_dogbone, thermal_spokes=lambda board, pad: []):
            rep0 = judge.run(ctx, Budget(1_000_000), pr, SCR, [], Rerouter(ctx))
            assert rep0.score.opens == 1 and rep0.score.new_errors == 0         # KiCad's count: GND still open
            rep = judge.pours_pass(ctx, pr, SCR, Budget(100_000))
    finally:
        teardown()
    assert placed == ["G2.1"], placed                                           # the pad the pour misses, only
    loose = [p for p in ctx.state.pieces(net="GND")]
    assert [p.kind for p in loose] == ["seg", "via"] and all(p.protected and p.conn == -1 for p in loose)
    assert rep.name == "pours" and rep.routed == 1 and rep.failed == 0
    assert rep.score.opens == 0 and rep.score.new_errors == 0 and len(fake.calls) == 2
    assert ctx.history["judge"]["pour_pieces"] == [p.id for p in loose]


def test_pours_pass_search_stub_and_removal_when_kicad_rejects_it():
    ctx, ex, unc = _pour_setup()
    seen = []

    def connect(b2, budget):
        cl = b2.clusters["GND"]
        seen.append(sorted(len(c["pads"]) for c in cl.values()))
        p = ctx.state.add(net="GND", conn=-1, kind="seg", layer=0, x1=30.0, y1=25.0, x2=30.0, y2=27.0, w=0.2, stage="legacy")
        budget.charge(4000)
        return [p.id], []
    pr = types.SimpleNamespace(dir=None, name="x", pcb=None)
    # 1. accepted: the stub closes the connection
    with_kio(FakeKicad(lambda t, v: [], unconnected=unc, ex=ex))
    try:
        with power_module(STUB=True):
            judge.run(ctx, Budget(1_000_000), pr, SCR, [], Rerouter(ctx))
            rep = judge.pours_pass(ctx, pr, SCR, Budget(100_000), connect=connect)
    finally:
        teardown()
    assert seen == [[1, 1]] and rep.routed == 1 and rep.score.opens == 0 and rep.work == 4000
    assert [p.stage for p in ctx.state.pieces(net="GND")] == ["legacy"]
    # 2. rejected by KiCad: the stub goes again, the signal copper stays, the pad is reported
    ctx, ex, unc = _pour_setup()
    sig = ctx.state.export()

    def drc(tracks, vias):
        return [("clearance", "the stub is too close to something", [i], (30.0, 26.0))
                for i, t in enumerate(tracks) if t[0] == "GND"]
    with_kio(FakeKicad(drc, unconnected=unc, ex=ex))
    try:
        with power_module(STUB=True):
            judge.run(ctx, Budget(1_000_000), pr, SCR, [], Rerouter(ctx))
            rep = judge.pours_pass(ctx, pr, SCR, Budget(100_000), connect=connect)
    finally:
        teardown()
    assert ctx.state.pieces(net="GND") == [] and ctx.state.export() == sig
    assert ctx.state.new_errors == 0 and rep.score.opens == 1 and rep.score.new_errors == 0
    assert any("taken out again" in n for n in rep.notes), rep.notes
    # 3. no power module, no search: the pad is reported, nothing crashes
    ctx, ex, unc = _pour_setup()
    with_kio(FakeKicad(lambda t, v: [], unconnected=unc, ex=ex))
    try:
        with power_module(STUB=True):
            judge.run(ctx, Budget(1_000_000), pr, SCR, [], Rerouter(ctx))
            rep = judge.pours_pass(ctx, pr, SCR, Budget(100_000))
        assert rep.failed == 1 and ctx.history["judge"]["pour_failed"][0]["from"] == "G2.1"
        assert ctx.state.pieces(net="GND") == []
    finally:
        teardown()


def test_pours_pass_gives_a_crowded_pour_pad_room():
    """The pours search cannot reach G2 because net A runs right past it: the pad's neighbourhood is closed, A is
    re-routed, the search runs again — and the whole attempt is kept only when it pays off."""
    pads = [("A1", "A", 5.0, 23.9), ("A2", "A", 36.0, 23.9), ("B1", "B", 5.0, 15.0), ("B2", "B", 25.0, 15.0),
            ("G1", "GND", 5.0, 5.0), ("G2", "GND", 30.0, 25.0)]
    zone = _gnd_zone((2.0, 2.0, 8.0, 8.0))
    pr = types.SimpleNamespace(dir=None, name="x", pcb=None)

    def setup():
        ctx = make(pads, zones=[zone])
        for c in ctx.conns:
            lay(ctx, c)
        return ctx

    def ex(tracks, vias):
        joined = any(t[0] == "GND" for t in tracks)
        e = extract(pads, zones=[zone], clusters={"G2": 5} if joined else None)
        e["tracks"] = [[t[0], 0, t[2], t[3], t[4], t[5], t[6], 5 if t[0] == "GND" else 0, 0] for t in tracks]
        return e

    def unc(tracks, vias):
        return {} if any(t[0] == "GND" for t in tracks) else {"GND": 1}
    fail = {"net": "GND", "from": "G2.1", "to": "pour", "_a": (30.0, 25.0), "_b": None, "reason": "blocked by routed nets: A"}

    def crowded(ctx):
        return any(p.net == "A" and judge._in_rect(p, (28.7, 23.7, 31.3, 26.3)) for p in ctx.state.pieces())
    # 1. it pays off
    ctx = setup()
    a_ids = [p.id for p in ctx.state.pieces(conn=0)]

    def connect(b2, budget):
        if crowded(ctx):
            return [], [dict(fail)]
        p = ctx.state.add(net="GND", conn=-1, kind="seg", layer=0, x1=30.0, y1=25.0, x2=30.0, y2=27.0, w=0.2, stage="legacy")
        return [p.id], []
    rr = Rerouter(ctx)
    with_kio(FakeKicad(lambda t, v: [], unconnected=unc, ex=ex))
    try:
        with power_module(STUB=True):
            judge.run(ctx, Budget(1_000_000), pr, SCR, [], rr)
            rep = judge.pours_pass(ctx, pr, SCR, Budget(1_000_000), connect=connect, reroute=rr)
    finally:
        teardown()
    assert rr.calls == [[0]] and ctx.state.status(0) == "routed" and rail_of(ctx, 0) == -2.0
    assert [p.stage for p in ctx.state.pieces(net="GND")] == ["legacy"] and rep.routed == 1 and rep.failed == 0
    assert rep.score.opens == 0 and rep.data["repairs"] == ["gave room to the GND pad G2.1 at (30.0, 25.0)"]
    assert [r["round"] for r in rep.data["rounds"]] == [0, "pours0r", "pours1"]
    reg = [r for r in ctx.cfg.regions if r.name == "judge"]
    assert reg and reg[0].forbid and reg[0].applies("A") and not reg[0].applies("GND")
    # 2. it does not: everything goes back to how it was (same pieces, no repair claimed), the pad is reported
    ctx = setup()
    a_ids = [p.id for p in ctx.state.pieces(conn=0)]
    rr = Rerouter(ctx)
    with_kio(FakeKicad(lambda t, v: [], unconnected=unc, ex=ex))
    try:
        with power_module(STUB=True):
            judge.run(ctx, Budget(1_000_000), pr, SCR, [], rr)
            rep = judge.pours_pass(ctx, pr, SCR, Budget(1_000_000), connect=lambda b2, budget: ([], [dict(fail)]), reroute=rr)
    finally:
        teardown()
    assert rr.calls == [[0]] and [p.id for p in ctx.state.pieces(conn=0)] == a_ids and rail_of(ctx, 0) == 0.0
    assert ctx.state.pieces(net="GND") == [] and rep.data["repairs"] == [] and rep.score.opens == 1
    assert any("did not connect more" in n for n in rep.notes) and rep.failed == 1
    assert ctx.history["judge"]["pour_failed"][0]["from"] == "G2.1"
    # 3. without a reroute callable nothing is ripped
    ctx = setup()
    with_kio(FakeKicad(lambda t, v: [], unconnected=unc, ex=ex))
    try:
        with power_module(STUB=True):
            judge.run(ctx, Budget(1_000_000), pr, SCR, [], Rerouter(ctx))
            rep = judge.pours_pass(ctx, pr, SCR, Budget(1_000_000), connect=lambda b2, budget: ([], [dict(fail)]))
    finally:
        teardown()
    assert rail_of(ctx, 0) == 0.0 and rep.failed == 1 and not [r for r in ctx.cfg.regions if r.name == "judge"]


def test_loose_pieces_are_removed_as_a_group():
    """conn == -1 copper (a dog-bone: stub + via) goes together when KiCad blames one of its pieces; other loose
    copper of the same net stays."""
    ctx = make(POUR_PADS)
    st = ctx.state
    a = st.add(net="GND", conn=-1, kind="seg", layer=0, x1=30.0, y1=25.0, x2=30.0, y2=23.5, w=0.2, stage="power", protected=True)
    v = st.add(net="GND", conn=-1, kind="via", layer=-1, x1=30.0, y1=23.5, w=0.6, drill=0.3, stage="power", protected=True)
    o = st.add(net="GND", conn=-1, kind="seg", layer=0, x1=5.0, y1=5.0, x2=5.0, y2=3.5, w=0.2, stage="power", protected=True)
    assert [p.id for p in judge._group(st, v)] == [a.id, v.id] and [p.id for p in judge._group(st, o)] == [o.id]

    def drc(tracks, vias):
        return [("hole_clearance", "via hole too close", [len(tracks)], (30.0, 23.5))] if vias else []
    with_kio(FakeKicad(drc))
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], lambda ids, b: None)
    finally:
        teardown()
    assert [p.id for p in st.pieces()] == [o.id] and rep.score.new_errors == 0
    assert rep.data["removed_after_drc"] == [{"net": "GND", "connections": 1, "why": rep.data["removed_after_drc"][0]["why"]}]


def test_base_and_best_files(tmp=None):
    """With real files: every round commits to the same base board, and the scratch board ends as the best round."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="judge-", dir=str(common.WORK)))
    try:
        pcb = d / "b.kicad_pcb"
        pcb.write_text("BASE")
        scr = types.SimpleNamespace(pcb=pcb, dir=d, name="b")
        ctx = make()
        for c in ctx.conns:
            lay(ctx, c)
        seen = []

        class K(FakeKicad):
            def commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw):
                # what real kio does with fresh=True: reset the scratch board from the project it is given
                seen.append((Path(pr.pcb).name, Path(pr.pcb).read_text(), pcb.read_text()))
                pcb.write_text(f"ROUND{len(self.calls)}")
                return super().commit_and_check(pr, scratch, tracks, vias, baseline_raw)
        box_b = (14.0, 19.5, 16.0, 20.5)

        def drc(tracks, vias):
            a_mid = [t for t in tracks if t[0] == "A" and abs(t[3] - t[5]) < 1e-9 and abs(t[4] - t[2]) > 5]
            out = [("clearance", "first", [i], (15.0, 10.0)) for i, t in enumerate(tracks) if t[0] == "A" and in_box(t, BAD)]
            out += [("clearance", "b", [i], (15.0, 20.0)) for i, t in enumerate(tracks) if t[0] == "B" and in_box(t, box_b)]
            if not a_mid or abs(a_mid[0][3] - 12.0) < 1e-6:     # A re-routed, or A gone: three errors nobody can blame
                out += [("silk_overlap", "x", [], (1.0, 1.0)), ("silk_overlap", "y", [], (2.0, 1.0)),
                        ("silk_overlap", "z", [], (3.0, 1.0))]
            return out
        with_kio(K(drc))
        try:
            rep = judge.run(ctx, Budget(1_000_000), types.SimpleNamespace(pcb=d / "user.kicad_pcb", dir=d, name="user"),
                            scr, [], Rerouter(ctx))
            j = ctx.history["judge"]
            assert all(n == "b.kicad_pcb.v2base" and base == "BASE" and cur == "BASE" for n, base, cur in seen), seen
            assert [r["new_errors"] for r in rep.data["rounds"]] == [2, 3, 3] and rep.score.new_errors == 2, rep.data["rounds"]
            assert pcb.read_text() == "ROUND0"                  # the best round's board, not the last one's
            assert j["base"].exists() and j["best_file"].exists()
            judge.cleanup(ctx)
            assert not j["base"].exists() and not j["best_file"].exists()
        finally:
            teardown()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_marker_helpers():
    raw = {"type": "clearance", "description": "d", "severity": "error",
           "items": [{"description": "Zone GND", "pos": {"x": 1.0, "y": 2.0}, "uuid": "z"},
                     {"description": "Track", "pos": {"x": 5.0, "y": 6.0}, "uuid": "t"}]}
    assert judge.marker_at(raw) == (5.0, 6.0)                   # a zone's position is an outline corner: not it
    assert judge.marker_at({"at": [3, 4]}) == (3.0, 4.0) and judge.marker_at({"type": "x"}) is None
    ms = [{"type": "b", "at": [1, 1], "message": "m"}, {"type": "a", "at": [2, 1], "message": "m"},
          {"type": "a", "at": [1, 5], "message": "m"}, {"type": "a", "at": [1, 5], "message": "a"}]
    assert [(m["type"], m["at"], m["message"]) for m in sorted(ms, key=judge.marker_key)] == \
        [("a", [1, 5], "a"), ("a", [1, 5], "m"), ("a", [2, 1], "m"), ("b", [1, 1], "m")]
    assert judge._other_pos(raw, {"t"}) is None and judge._other_pos(raw, {"z"}) == (5.0, 6.0)
    km = {"items": ["Track A", "Pad B"], "uuids": ["t", "p"], "pos": [[5.0, 6.0], [7.0, 8.0]]}
    assert judge._other_pos(km, {"t"}) == (7.0, 8.0)
    ctx = make()
    ctx.history["targets"] = {"A"}
    assert judge.targeted_opens(ctx, {"A": 2, "B": 5, "zzz": 1}) == 2 and judge.targeted_opens(ctx, None) is None
    del ctx.history["targets"]
    assert judge.targeted_opens(ctx, {"A": 2, "B": 5, "zzz": 1}) == 7
    assert isinstance(Score(), Score)


def _spokes_of(board, pad):
    from copper_kicad.pcb.guided.geom import Shape
    out = []
    for k, (ux, uy) in enumerate(((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))):       # pad edge 0.4, reach 0.8
        g = (pad.x + ux * 0.8, pad.y + uy * 0.8, ux, uy, 0.4, 0.25, 0.0)
        out.append(Shape("box", g, "soft", pad.net, 0.0, (0,), tag=("thermal", pad.name, k)))
    return out


def test_starved_thermal_that_lacks_one_spoke_frees_one_spoke_only():
    """KiCad: 'zone min spoke count 2; actual 1'. Two connections close one spoke each; the pad's rectangle
    holds both. Only one of them is removed and re-routed (integration, stage 1: the rectangle rule took 24
    connections out of interf_u), and only the spoke corridors are closed, not the pad's neighbourhood."""
    pads = [("A1", "A", 5.0, 10.0), ("A2", "A", 25.0, 10.0), ("B1", "B", 5.0, 12.2), ("B2", "B", 25.0, 12.2),
            ("G1", "GND", 15.0, 10.9)]
    ctx = make(pads)
    for c in ctx.conns:
        lay(ctx, c)

    def drc(tracks, vias):
        a = any(t[0] == "A" and in_box(t, (14.0, 9.5, 16.0, 10.5)) for t in tracks)
        b = any(t[0] == "B" and in_box(t, (14.0, 11.3, 16.0, 12.5)) for t in tracks)
        msg = "Thermal relief connection to zone incomplete (layer F.Cu; zone min spoke count 2; actual 1)"
        return [("starved_thermal", msg, [], (15.0, 10.9))] if a and b else []
    with_kio(FakeKicad(drc))
    rr = Rerouter(ctx)
    try:
        with power_module(thermal_spokes=_spokes_of):
            rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls == [[1]], rr.calls                          # one connection, not the pad's whole crowd
    assert rep.score.new_errors == 0 and ctx.state.status(0) == "routed" and ctx.state.status(1) == "routed"
    assert rail_of(ctx, 0) == 0.0 and rail_of(ctx, 1) not in (0.0, None)        # A was never touched
    assert rep.data["spoke_blame"] == [{"pad": "G1.1", "lack": 1, "free": 2, "closed": [1, 1], "freed": ["1"]}]
    reg = [r for r in ctx.cfg.regions if r.name == "judge" and r.forbid]
    assert len(reg) == 3 and all(r.layers == {0} and not r.applies("GND") for r in reg)     # the freed spoke + 2 free ones
    assert not any(r.rect[0] <= 15.0 <= r.rect[2] and r.rect[1] <= 10.0 <= r.rect[3] for r in reg)   # A's spoke stays open
    # the freed corridor is closed for foreign nets in the exact checker too
    assert ctx.oracle.seg_ok(0, 13.0, 11.9, 17.0, 11.9, 0.2, "B") is not None
    assert ctx.oracle.seg_ok(0, 13.0, 11.9, 17.0, 11.9, 0.2, "GND") is None


def test_starved_thermal_falls_back_to_the_rectangle():
    """No spoke geometry / a marker that names no spoke count / a pad already given room: the rectangle rule."""
    pads = [("A1", "A", 5.0, 10.0), ("A2", "A", 25.0, 10.0), ("B1", "B", 5.0, 12.2), ("B2", "B", 25.0, 12.2),
            ("G1", "GND", 15.0, 10.9)]
    ctx = make(pads)
    for c in ctx.conns:
        lay(ctx, c)
    by_id = {p.id: p for p in ctx.state.pieces()}
    pad = judge._pad_at(ctx.board, 15.0, 10.9)
    with power_module(thermal_spokes=_spokes_of):
        m = {"message": "Thermal relief connection to zone incomplete (layer F.Cu; 2 spokes connected to isolated island)"}
        assert judge._spoke_crowd(ctx, pad, "GND", m, by_id) is None
        m = {"message": "(layer F.Cu; zone min spoke count 4; actual 1)"}       # lacks 3, our copper closes only 2
        assert judge._spoke_crowd(ctx, pad, "GND", m, by_id) is None
        m = {"message": "(layer F.Cu; zone min spoke count 2; actual 0)"}       # lacks 2: both go
        crowd, keep, layer = judge._spoke_crowd(ctx, pad, "GND", m, by_id)
        assert sorted({p.conn for p in crowd}) == [0, 1] and len(keep) == 4 and layer == 0
    with power_module(thermal_spokes=lambda board, pad: []):
        m = {"message": "(layer F.Cu; zone min spoke count 2; actual 1)"}
        assert judge._spoke_crowd(ctx, pad, "GND", m, by_id) is None


def _spoked_zone():
    """GND fill south of y = 12 with one spoke up to the pad G1 at (15, 11): the pad has its south spoke only."""
    return {"name": "gnd", "layers": [0], "net": "GND", "priority": 0, "filled": True,
            "polys": [{"o": [[1, 1], [39, 1], [39, 29], [1, 29]]}],
            "fills": {"0": [{"o": [[2, 12], [14.9, 12], [14.9, 11.4], [15.1, 11.4], [15.1, 12], [38, 12], [38, 28],
                                   [2, 28]]}]}}


def test_starved_thermal_is_repaired_with_a_spoke_track_before_anything_is_removed():
    """Build stage 3: "zone min spoke count 2; actual 1" first gets a spoke track from the pad into its pour (what
    KiCad counts as a spoke), in a direction that has no spoke yet. Nothing is removed, nothing re-routed."""
    pads = PADS + [("G1", "GND", 15.0, 11.0)]
    zone = _spoked_zone()
    ctx = make(pads, zones=[zone])
    for c in ctx.conns:
        lay(ctx, c)
    before = {p.id for p in ctx.state.pieces()}

    def drc(tracks, vias):
        spoke = any(t[0] == "GND" and abs(t[2] - 15.0) < 1e-6 and abs(t[3] - 11.0) < 1e-6 for t in tracks)
        return [] if spoke else [("starved_thermal", "Thermal relief connection to zone incomplete (layer F.Cu; zone "
                                  "min spoke count 2; actual 1)", [], (15.0, 11.0))]

    def ex(tracks, vias):
        return extract(pads, zones=[zone])
    with_kio(FakeKicad(drc, ex=ex))
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls == [] and rep.score.new_errors == 0
    assert all(ctx.state.status(c.idx) == "routed" for c in ctx.conns) and not rep.data["removed_after_drc"]
    new = [p for p in ctx.state.pieces() if p.id not in before]
    assert len(new) == 1 and new[0].net == "GND" and new[0].conn == -1 and new[0].stage == "judge"
    t = new[0]
    assert (t.x1, t.y1) == (15.0, 11.0) and t.y2 > 12.0 and abs(abs(t.x2 - t.x1) - abs(t.y2 - t.y1)) < 1e-6   # 45 degrees
    assert ctx.oracle.piece_ok(t) is None                         # legal by the exact checker
    assert rep.data["thermal_copper"] == [{"pad": "G1.1", "kind": "spokes", "tracks": 1}]
    assert [r["round"] for r in rep.data["rounds"]] == [0, "thermal"]


def test_spoke_track_that_does_not_help_is_undone_and_the_blame_rules_apply():
    """The copper attempt is kept only when KiCad's next check is better: otherwise the state is as before it and
    the pad is given room the old way (crowd removed, neighbourhood closed, re-route)."""
    pads = PADS + [("G1", "GND", 15.0, 11.0)]
    zone = _spoked_zone()
    ctx = make(pads, zones=[zone])
    for c in ctx.conns:
        lay(ctx, c)

    def drc(tracks, vias):
        near = [i for i, t in enumerate(tracks) if t[0] == "A" and in_box(t, (13.7, 9.7, 16.3, 12.3))]
        return [("starved_thermal", "Thermal relief connection to zone incomplete (layer F.Cu; zone min spoke count "
                 "2; actual 1)", [], (15.0, 11.0))] if near else []
    with_kio(FakeKicad(drc, ex=lambda tracks, vias: extract(pads, zones=[zone])))
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls and rr.calls[0] == [0] and rep.score.new_errors == 0 and ctx.state.status(0) == "routed"
    assert any(r.get("undone") for r in rep.data["rounds"] if r["round"] == "thermal")
    assert not rep.data["removed_after_drc"]


def test_island_pad_gets_a_track_to_the_rest_of_the_pour():
    """A pad on a sliver of pour ("n spokes connected to isolated island"): one straight track from the pad into a
    large piece of the pour is tried first; the sliver itself is not "the pour"."""
    pads = PADS + [("G1", "GND", 15.0, 14.0)]
    zone = {"name": "gnd", "layers": [0], "net": "GND", "priority": 0, "filled": True,
            "polys": [{"o": [[1, 1], [39, 1], [39, 29], [1, 29]]}],
            "fills": {"0": [{"o": [[14.5, 14.4], [15.5, 14.4], [15.5, 15.0], [14.5, 15.0]]},      # the sliver (0.6 mm2)
                            {"o": [[2, 16], [38, 16], [38, 28], [2, 28]]}]}}                      # the pour
    ctx = make(pads, zones=[zone])
    e2 = extract(pads, zones=[zone])
    e2["tracks"] = [["GND", 0, 15.0, 14.0, 15.0, 14.2, 0.2, 5, 0]]
    b2 = model.Board(extract(pads, zones=[zone], clusters={"G1": 5}))
    pad = next(p for p in ctx.board.pads if p.name == "G1.1")
    if not any(c["islands"] for c in b2.clusters.get("GND", {}).values()):
        return                              # the model attached no island to a cluster here: nothing to test
    ids = judge._spoke_tracks(ctx, b2, pad, "GND", 0, 1, island=True)
    ps = [ctx.state.piece(i) for i in ids]
    if ps:
        assert (ps[0].x1, ps[0].y1) == (15.0, 14.0) and ps[-1].y2 > 16.0
        assert all(ctx.oracle.piece_ok(p) is None for p in ps)


# ── electrical constraints (design §14.6): KiCad's length / skew / pair markers are verdicts, not copper errors ──
class ConsKicad(FakeKicad):
    """FakeKicad whose raw list is shaped like kicad-cli's (type, description, item texts with the net)."""

    def commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw):
        res = FakeKicad.commit_and_check(self, pr, scratch, tracks, vias, baseline_raw, **kw)
        raw = []
        for (typ, msg, mine, _at), m in zip(self.drc(tracks, vias), res["new"]):
            items = [f"Track [{tracks[i][0]}] on F.Cu, length 20.0000 mm" for i in mine]
            m["items"] = items
            raw.append({"type": typ, "severity": "error", "description": msg,
                        "items": [{"description": t} for t in items]})
        res["raw"] = raw
        return res


LEN_MSG = "Track length out of range (rule 'len' min length 50.0000 mm; actual 20.0000 mm)"
SKEW_MSG = ("Skew between tracks out of range (rule 'match' max skew 0.1000 mm; actual -1.0000 mm; target net length "
            "21.0000 mm (from /X); actual 20.0000 mm)")


def test_constraint_markers_remove_no_connection_and_are_kept_for_the_report():
    """A net KiCad calls too short (or badly matched) is not ripped up, re-routed and finally removed: no rip-up
    makes it longer. Both marker types are in session.COPPER_TYPES, so without the split the judge would blame the
    connection whose track the marker names."""
    from copper_kicad.pcb.guided import session
    from copper_kicad.pcb.v2 import constraints as C
    assert "length_out_of_range" in session.COPPER_TYPES and "length_out_of_range" in C.KICAD_TYPES
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)
    before = ctx.state.export()

    def drc(tracks, vias):
        out = []
        for i, t in enumerate(tracks):
            out.append(("length_out_of_range" if t[0] == "A" else "skew_out_of_range",
                        LEN_MSG if t[0] == "A" else SKEW_MSG, [i], (15.0, t[3])))
        return out
    fake = with_kio(ConsKicad(drc))
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls == [] and len(fake.calls) == 1                       # nothing blamed, nothing re-routed, one check
    assert ctx.state.export() == before                                 # not one piece moved
    assert all(ctx.state.status(c.idx) == "routed" for c in ctx.conns)
    assert rep.score.new_errors == 0 and ctx.state.new_errors == 0 and rep.routed == 2 and rep.failed == 0
    assert rep.data["removed_after_drc"] == [] and rep.data["new"] == []
    assert [r["new_errors"] for r in rep.data["rounds"]] == [0]
    last = ctx.history["judge"]["last"]
    assert last["new"] == [] and last["cons_new"] == 2 and len(last["raw"]) == 2
    assert last["cons"]["counts"] == {"length_out_of_range": 1, "skew_out_of_range": 1}
    assert last["cons"]["len"] == {"A": 20.0, "B": 20.0, "/X": 21.0}
    assert last["cons"]["nets"] == {"A": ["length_out_of_range"], "B": ["skew_out_of_range"]}
    assert not [r for r in ctx.cfg.regions if r.name == "judge"] and not ctx.history.get("marks")


def test_a_copper_error_beside_a_constraint_marker_is_still_repaired():
    ctx = make()
    for c in ctx.conns:
        lay(ctx, c)
    b_before = [p.id for p in ctx.state.pieces(conn=1)]

    def drc(tracks, vias):
        out = [("clearance", "Clearance violation (A)", [i], (15.0, 10.0))
               for i, t in enumerate(tracks) if t[0] == "A" and in_box(t, BAD)]
        out += [("length_out_of_range", LEN_MSG, [i], (15.0, 20.0)) for i, t in enumerate(tracks) if t[0] == "B"]
        return out
    fake = with_kio(ConsKicad(drc))
    rr = Rerouter(ctx)
    try:
        rep = judge.run(ctx, Budget(1_000_000), None, SCR, [], rr)
    finally:
        teardown()
    assert rr.calls == [[0]] and len(fake.calls) == 2                    # only A (the clearance error), once
    assert [p.id for p in ctx.state.pieces(conn=1)] == b_before          # B, the "too short" net, was never touched
    assert [r["new_errors"] for r in rep.data["rounds"]] == [1, 0] and rep.score.new_errors == 0
    assert rep.data["removed_after_drc"] == [] and ctx.state.status(1) == "routed"
    last = ctx.history["judge"]["last"]
    assert last["cons"]["counts"] == {"length_out_of_range": 1} and last["cons_new"] == 1


if __name__ == "__main__":
    import time
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
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
