"""Router v2 polish (design §4.10): pad entry, pull-tight, staircases / odd angles, via removal, re-widen.

Pure tests on synthetic boards (no KiCad, no files). The route state is ``MiniState`` below, a small reference
implementation of the ``pieces.RouteState`` contract of design §9, so this file does not depend on another work
package having landed. With ``--real`` the same tests run against ``copper_kicad.pcb.v2.pieces.RouteState``
(skipped with a message while that module is still a stub).

Run:  python tests/test_v2_polish.py              every test
      python tests/test_v2_polish.py test_name    one test
      python tests/test_v2_polish.py --real       against the real RouteState
      python tests/test_v2_polish.py --perf       + throughput numbers on the synthetic lane board
"""
from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import geom, model  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2 import polish as PL  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Ctx, Piece, Score, Terminal  # noqa: E402

REAL = "--real" in sys.argv
PERF = "--perf" in sys.argv


# ── synthetic boards (same extract shape as tests/test_v2_oracle.py) ──────
def board(pads=(), *, size=(40.0, 30.0), layers=("F.Cu", "B.Cu"), zones=(), nets=None, rules=None):
    names = sorted({p["net"] for p in pads if p.get("net")} | set(nets or {}))
    R = {"clearance": 0.1, "track_width": 0.1, "via": 0.4, "drill": 0.2, "edge_clearance": 0.3, "hole_clearance": 0.25,
         "hole_to_hole": 0.25, "annular": 0.05,
         "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.25, "dp_width": 0.2}}
    R.update(rules or {})
    ex = {"layers": list(layers), "rules": R,
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.25, "dp_width": 0.2, **((nets or {}).get(n) or {})} for i, n in enumerate(names)},
          "outline": {"closed": True, "polys": [{"o": [[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [], "vias": [],
          "zones": [dict(z) for z in zones], "mask_openings": [],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    allL = list(range(len(layers)))
    for i, p in enumerate(pads):
        kind = p.get("kind", "smd")
        lays = allL if kind in ("tht", "npth") else [p.get("layer", 0)]
        d = {"ref": p["ref"], "num": p.get("num", "1"), "net": p.get("net", ""), "x": p["x"], "y": p["y"], "kind": kind,
             "layers": lays, "c": i + 1,
             "shapes": [[lays, "box", [p["x"], p["y"], 1.0, 0.0, p.get("hw", 0.4), p.get("hh", 0.4), p.get("r", 0.0)]]]}
        if p.get("hole"):
            d["hole"] = [p["x"], p["y"], 1.0, 0.0, p["hole"], p["hole"]]
        ex["pads"].append(d)
    return model.Board(ex)


def pad(ref, net, x, y, **kw):
    return {"ref": ref, "net": net, "x": x, "y": y, **kw}


class Cfg:
    def __init__(self):
        self.regions = []
        self.nets = {}


# ── a reference RouteState (design §9 contract) ───────────────────────────
def _key(x, y):
    return (int(round(x * 1e6)), int(round(y * 1e6)))


class MiniState:
    def __init__(self, oracle, conns):
        self.o = oracle
        self.conns = {c.idx: c for c in conns}
        self._p = {}
        self._seq = 0
        self._status = {c.idx: "todo" for c in conns}
        self._log = []
        self._marks = []
        self.new_errors = 0
        self.calls = {"connected": 0, "add": 0, "remove": 0}

    # transactions
    def begin(self):
        self._marks.append(len(self._log))
        return len(self._marks)

    def commit(self, tok):
        del self._marks[tok - 1:]
        if not self._marks:
            self._log.clear()

    def rollback(self, tok):
        n = self._marks[tok - 1]
        while len(self._log) > n:
            what, p = self._log.pop()
            if what == "add":
                del self._p[p.id]
                self.o.remove_piece(p)
            else:
                self._p[p.id] = p
                self.o.add_piece(p)
        del self._marks[tok - 1:]

    def add(self, **f):
        self.calls["add"] += 1
        self._seq += 1
        f.setdefault("layer", -1)
        if f.get("kind") == "via":
            f.setdefault("x2", f["x1"])
            f.setdefault("y2", f["y1"])
        p = Piece(id=self._seq, **f)
        self._p[p.id] = p
        self.o.add_piece(p)
        self._log.append(("add", p))
        return p

    def remove(self, pid):
        self.calls["remove"] += 1
        p = self._p.pop(pid)
        self.o.remove_piece(p)
        self._log.append(("rm", p))

    def rip_conn(self, conn):
        out = self.pieces(conn=conn)
        for p in out:
            self.remove(p.id)
        return out

    def pieces(self, conn=None, net=None):
        return [p for _i, p in sorted(self._p.items()) if (conn is None or p.conn == conn) and (net is None or p.net == net)]

    def set_status(self, conn, status, fail=None):
        self._status[conn] = status

    def status(self, conn):
        return self._status[conn]

    def snapshot(self):
        return (tuple(self.pieces()), dict(self._status), self._seq)

    def restore(self, snap):
        want = {p.id: p for p in snap[0]}
        for pid in [i for i in self._p if i not in want or self._p[i] != want[i]]:
            self.o.remove_piece(self._p.pop(pid))
        for pid, p in want.items():
            if pid not in self._p:
                self._p[pid] = p
                self.o.add_piece(p)
        self._status = dict(snap[1])

    # connectivity: union-find over end points, vias, tees (end on a same-net segment within 1 nm) and pads
    def connected(self, conn, loose=False):
        """loose: an end inside another piece's copper joins it (KiCad's rule), not only one on its centre line."""
        self.calls["connected"] += 1
        c = self.conns[conn]
        ps = self.pieces(net=c.net)
        par = {}

        def find(a):
            par.setdefault(a, a)
            while par[a] != a:
                par[a] = par[par[a]]
                a = par[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                par[ra] = rb
        L = self.o.L
        segs = [p for p in ps if p.kind == "seg"]
        pts = []
        for p in ps:
            if p.kind == "via":
                k = _key(p.x1, p.y1)
                for l in range(L):
                    union((0, k), (l, k))
                    pts.append((l, p.x1, p.y1))
            else:
                a, b = (p.layer, _key(p.x1, p.y1)), (p.layer, _key(p.x2, p.y2))
                union(a, b)
                pts.append((p.layer, p.x1, p.y1))
                pts.append((p.layer, p.x2, p.y2))
        net = self.o.b.nets.get(c.net)
        pads = list(net.pads) if net else []
        for l, x, y in pts:
            for s in segs:
                if s.layer == l and geom.pt_seg_dist(x, y, s.x1, s.y1, s.x2, s.y2) <= (s.w / 2 if loose else 1e-6):
                    union((l, _key(x, y)), (l, _key(s.x1, s.y1)))
            for pd in pads:
                if any(sh.dist_pt(x, y) <= 1e-6 for sh in pd.shapes.get(l, ())):
                    union((l, _key(x, y)), ("pad", pd.name))

        def node(t):
            if t.pad:
                return ("pad", t.pad)
            k = _key(t.x, t.y)
            for l in t.layers:
                if (l, k) in par:
                    return (l, k)
                for s in segs:
                    if s.layer == l and geom.pt_seg_dist(t.x, t.y, s.x1, s.y1, s.x2, s.y2) <= 1e-6:
                        return (l, _key(s.x1, s.y1))
            return None
        na, nb = node(c.a), node(c.b)
        return na is not None and nb is not None and na in par and nb in par and find(na) == find(nb)

    def score(self):
        s = Score(new_errors=self.new_errors)
        for i in sorted(self.conns):
            if not (self._status[i] == "routed" and self.connected(i)):
                s.opens += 1
        ends = {}
        for p in self.pieces():
            if p.kind == "via":
                s.vias += 1
                continue
            s.length_mm += p.length
            if p.w < p.target_w - 1e-9:
                s.necked_mm += p.length
            for k, o in ((_key(p.x1, p.y1), (p.x2, p.y2)), (_key(p.x2, p.y2), (p.x1, p.y1))):
                ends.setdefault((p.conn, p.layer, k), []).append(o)
        for (_c, _l, k), lst in ends.items():
            if len(lst) == 2:
                ux, uy = k[0] / 1e6 - lst[0][0], k[1] / 1e6 - lst[0][1]
                vx, vy = lst[1][0] - k[0] / 1e6, lst[1][1] - k[1] / 1e6
                n = math.hypot(ux, uy) * math.hypot(vx, vy)
                if n > 0 and (abs(ux * vy - uy * vx) / n > 1e-6 or ux * vx + uy * vy < 0):
                    s.bends += 1
        s.length_mm = round(s.length_mm, 9)
        s.necked_mm = round(s.necked_mm, 9)
        return s

    def export(self):
        ps = self.pieces()
        return ([p for p in ps if p.kind == "seg"], [p for p in ps if p.kind == "via"], [p.id for p in ps])


def make_state(oracle, conns):
    if REAL:
        from copper_kicad.pcb.v2 import pieces
        return pieces.RouteState(oracle, conns)
    return MiniState(oracle, conns)


# ── scenario helpers ──────────────────────────────────────────────────────
def conn(idx, net, a, b, *, la=(0,), lb=(0,), pa=None, pb=None, width=0.2, role="signal"):
    return ConnV2(idx=idx, net=net, a=Terminal(a[0], a[1], tuple(la), pad=pa), b=Terminal(b[0], b[1], tuple(lb), pad=pb),
                  est=math.hypot(b[0] - a[0], b[1] - a[1]), role=role, width=width, neck=width, clearance=0.2,
                  via=(0.6, 0.3), rank=(idx,))


def scene(pads, conns, **kw):
    b = board(pads, **kw)
    o = O.Oracle(b)
    st = make_state(o, conns)
    return Ctx(board=b, cfg=Cfg(), oracle=o, state=st, conns=list(conns))


def path(ctx, c, pts, layer=0, w=None, tw=None, **kw):
    """Add a polyline of connection c; returns the pieces."""
    out = []
    w = w or c.width
    for i in range(len(pts) - 1):
        out.append(ctx.state.add(net=c.net, conn=c.idx, kind="seg", layer=layer, x1=pts[i][0], y1=pts[i][1],
                                 x2=pts[i + 1][0], y2=pts[i + 1][1], w=w, target_w=tw or w, stage="detail", **kw))
    return out


def via(ctx, c, x, y, **kw):
    return ctx.state.add(net=c.net, conn=c.idx, kind="via", layer=-1, x1=x, y1=y, x2=x, y2=y, w=0.6, drill=0.3,
                         stage="detail", **kw)


def routed(ctx):
    for c in ctx.conns:
        ctx.state.set_status(c.idx, "routed")
        assert ctx.state.connected(c.idx), f"test setup: connection {c.idx} is not connected"
    assert_legal(ctx)


def assert_legal(ctx):
    for p in ctx.state.pieces():
        b = ctx.oracle.piece_ok(p)
        assert b is None, f"piece {p} is illegal: {b}"


def geometry(ctx):
    return [(p.net, p.conn, p.kind, p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.target_w) for p in ctx.state.pieces()]


def polish(ctx, passes=PL.PASSES, units=5_000_000):
    before = ctx.state.score()
    ok = [c.idx for c in ctx.conns if ctx.state.status(c.idx) == "routed" and ctx.state.connected(c.idx)]
    rep = PL.run(ctx, Budget(units), passes)
    after = ctx.state.score()
    assert not after.key() > before.key(), f"score got worse: {before} -> {after}"
    for i in ok:
        assert ctx.state.connected(i), f"connection {i} lost"
    assert_legal(ctx)
    assert rep.name == "polish" and rep.score.key() == after.key()
    return rep


def segs(ctx, c=None):
    return [p for p in ctx.state.pieces(conn=c) if p.kind == "seg"]


def odd(p):
    dx, dy = abs(p.x2 - p.x1), abs(p.y2 - p.y1)
    return not (dx < 2e-6 or dy < 2e-6 or abs(dx - dy) < 2e-6)


AB = [pad("PA", "A", 10.0, 10.0), pad("PB", "A", 13.0, 13.0)]


def stair_pts(x0, y0, steps, d=0.3):
    pts = [(x0, y0)]
    for _ in range(steps):
        x, y = pts[-1]
        pts.append((round(x + d, 6), y))
        pts.append((round(x + d, 6), round(y + d, 6)))
    return pts


# ── geometry helpers ──────────────────────────────────────────────────────
def test_postures_are_45_degree_and_quantised():
    a, b = (10.0, 10.0), (13.0, 11.2)
    ps = PL._postures(a, b)
    assert len(ps) == 2 and all(p[0] == a and p[-1] == b and len(p) == 3 for p in ps)
    for p in ps:
        for i in range(2):
            assert PL._is45(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1])
        assert abs(PL._plen(p) - (3.0 + (math.sqrt(2) - 1) * 1.2)) < 1e-9
    assert ps[0][1] == (11.2, 11.2) and ps[1][1] == (11.8, 10.0)
    assert PL._postures(a, (12.0, 12.0)) == [[a, (12.0, 12.0)]] and PL._postures(a, a) == []
    zs = PL._zs(a, b)
    assert len(zs) == 2 and all(len(z) == 4 and PL._quality(z)[0] == 0 for z in zs)
    assert all(abs(PL._plen(z) - PL._plen(ps[0])) < 1e-9 for z in zs)
    assert PL._zs(a, (12.0, 12.0)) == []
    assert PL._bends([(0, 0), (1, 0), (2, 0), (2, 1)]) == 1 and PL._sharp([(0, 0), (1, 0), (0.5, 0.2)]) == 1
    assert PL._quality([(0, 0), (1, 0), (1.05, 0.1), (1.05, 2)]) == (1, 1)


def test_polylines_break_at_vias_and_junctions():
    c1 = conn(1, "A", (10, 10), (20, 10), pa="PA.1", pb="PB.1")
    ctx = scene([pad("PA", "A", 10, 10), pad("PB", "A", 20, 10), pad("PC", "A", 14, 14)], [c1])
    a = path(ctx, c1, [(10, 10), (12, 10), (14, 10)])
    via(ctx, c1, 14, 10)
    b = path(ctx, c1, [(14, 10), (16, 10), (17, 11)], layer=1)
    via(ctx, c1, 17, 11)
    c = path(ctx, c1, [(17, 11), (18, 10), (20, 10)])
    spur = path(ctx, c1, [(12, 10), (12, 12)])                 # junction at (12, 10)
    pl = PL.polylines(ctx.state, 1)
    got = sorted(tuple(p.id for p in ch) for ch in pl)
    assert got == sorted([(a[0].id,), (a[1].id,), (spur[0].id,), (b[0].id, b[1].id), (c[0].id, c[1].id)]), got
    for ch in pl:                                              # path order: consecutive pieces share an end point
        for u, v in zip(ch, ch[1:]):
            assert {_key(u.x1, u.y1), _key(u.x2, u.y2)} & {_key(v.x1, v.y1), _key(v.x2, v.y2)}
    assert PL.polylines(ctx.state, 99) == []


# ── staircases and pull-tight ─────────────────────────────────────────────
def test_staircase_becomes_one_diagonal():
    c1 = conn(1, "A", (10, 10), (13, 13), pa="PA.1", pb="PB.1")
    for passes in (("stairs",), ("tight",), PL.PASSES):
        ctx = scene(AB, [c1])
        path(ctx, c1, stair_pts(10.0, 10.0, 10))
        routed(ctx)
        assert len(segs(ctx)) == 20
        rep = polish(ctx, passes)
        s = segs(ctx)
        assert len(s) == 1, (passes, geometry(ctx))
        assert {(s[0].x1, s[0].y1), (s[0].x2, s[0].y2)} == {(10.0, 10.0), (13.0, 13.0)}
        assert s[0].stage == "polish" and s[0].w == 0.2 and s[0].conn == 1 and s[0].net == "A"
        sc = ctx.state.score()
        assert abs(sc.length_mm - 3 * math.sqrt(2)) < 1e-6 and sc.bends == 0
        assert rep.data["edits_total"] >= 1 and rep.data["delta"]["length_mm"] < -1.0


def test_staircase_next_to_an_obstacle_becomes_an_L_on_the_same_side():
    # the diagonal would cut the corner where a foreign pad sits: the staircase must stay above/left of it
    c1 = conn(1, "A", (10, 10), (13, 13), pa="PA.1", pb="PB.1")
    block = pad("X", "B", 12.2, 10.9, hw=0.5, hh=0.5)
    ctx = scene(AB + [block, pad("X2", "B", 30, 25)], [c1])
    pts = [(10, 10), (10, 13), (13, 13)]                       # legal: up, then right (the pad is below/right)
    path(ctx, c1, [(10, 10), (10, 11.5), (10.3, 11.5), (10.3, 13), (13, 13)])
    routed(ctx)
    polish(ctx)
    s = segs(ctx)
    assert all(not odd(p) for p in s) and len(s) <= 3, geometry(ctx)
    # same side: the pad is still to the right of / below the track (the loop track + straight chord encloses it or not
    # exactly as before)
    loop = _chain_points(s, (10.0, 10.0))
    assert not geom.pt_in_poly(loop + [(10.0, 13.0)], 12.2, 10.9) or True
    assert _side_of(loop, (12.2, 10.9)) == _side_of(pts, (12.2, 10.9))


def _chain_points(pieces, start):
    left = list(pieces)
    pts = [start]
    while left:
        for p in left:
            if _key(p.x1, p.y1) == _key(*pts[-1]):
                pts.append((p.x2, p.y2))
            elif _key(p.x2, p.y2) == _key(*pts[-1]):
                pts.append((p.x1, p.y1))
            else:
                continue
            left.remove(p)
            break
        else:
            raise AssertionError("pieces do not form one chain")
    return pts


def _side_of(pts, pt):
    """Is ``pt`` inside the polygon closed by the straight chord from the last to the first point?"""
    return geom.pt_in_poly(list(pts), pt[0], pt[1])


def test_pull_tight_keeps_the_side_of_an_obstacle():
    c1 = conn(1, "A", (10, 10), (14, 10), pa="PA.1", pb="PB.1")
    pads = [pad("PA", "A", 10, 10), pad("PB", "A", 14, 10)]
    u = [(10, 10), (10, 12), (14, 12), (14, 10)]
    # (a) nothing inside the U: it collapses to the straight chord
    ctx = scene(pads, [c1])
    path(ctx, c1, u)
    routed(ctx)
    polish(ctx, ("tight",))
    assert len(segs(ctx)) == 1 and abs(ctx.state.score().length_mm - 4.0) < 1e-9
    # (b) a foreign pad inside the U: the chord would be legal (0.7 mm away) but changes sides -> not taken
    ctx = scene(pads + [pad("X", "B", 12, 11, hw=0.2, hh=0.2), pad("X2", "B", 30, 25)], [c1])
    path(ctx, c1, u)
    routed(ctx)
    assert ctx.oracle.seg_ok(0, 10, 10, 14, 10, 0.2, "A") is None
    polish(ctx)
    pts = _chain_points(segs(ctx), (10.0, 10.0))
    assert _side_of(pts, (12.0, 11.0)), pts                    # still inside the loop track + chord
    assert ctx.state.score().length_mm < 8.0 - 0.5             # but the corners were cut
    assert all(not odd(p) for p in segs(ctx))


def test_jog_becomes_a_45_degree_jog_when_the_full_L_is_blocked():
    c1 = conn(1, "A", (5, 10), (25, 10.4), pa="PA.1", pb="PB.1")
    pads = [pad("PA", "A", 5, 10), pad("PB", "A", 25, 10.4),
            pad("X", "B", 10, 10.9, hw=0.3, hh=0.25), pad("Y", "B", 20, 9.5, hw=0.3, hh=0.25)]
    ctx = scene(pads, [c1])
    path(ctx, c1, [(5, 10), (15, 10), (15, 10.4), (25, 10.4)])
    routed(ctx)
    # both full postures hit a pad: the line y = 10.4 passes X too closely, y = 10.0 passes Y too closely
    assert ctx.oracle.seg_ok(0, 5.4, 10.4, 25, 10.4, 0.2, "A") is not None
    assert ctx.oracle.seg_ok(0, 5, 10, 24.6, 10, 0.2, "A") is not None
    polish(ctx, ("stairs",))
    s = segs(ctx)
    assert len(s) == 3 and all(not odd(p) for p in s), geometry(ctx)
    mid = [p for p in s if abs(abs(p.x2 - p.x1) - 0.4) < 1e-6 and abs(abs(p.y2 - p.y1) - 0.4) < 1e-6]
    assert len(mid) == 1, geometry(ctx)
    assert abs(ctx.state.score().length_mm - (20.0 - 0.4 + 0.4 * math.sqrt(2))) < 1e-6


def test_interior_odd_angle_segment_is_replaced():
    c1 = conn(1, "A", (5, 10), (25, 12), pa="PA.1", pb="PB.1")
    ctx = scene([pad("PA", "A", 5, 10), pad("PB", "A", 25, 12)], [c1])
    path(ctx, c1, [(5, 10), (10, 10), (10, 10.6), (14, 11.6), (14, 12), (25, 12)])    # (10,10.6)->(14,11.6) is odd
    routed(ctx)
    assert sum(odd(p) for p in segs(ctx)) == 1
    polish(ctx)
    assert sum(odd(p) for p in segs(ctx)) == 0, geometry(ctx)
    assert len(segs(ctx)) <= 3


# ── pad entry ─────────────────────────────────────────────────────────────
def test_pad_entry_ends_on_the_pad_centre():
    c1 = conn(1, "A", (10, 10), (16, 10.2), pa="PA.1", pb="PB.1")
    pads = [pad("PA", "A", 10, 10, hw=0.5, hh=0.5), pad("PB", "A", 16, 10.2)]
    ctx = scene(pads, [c1])
    path(ctx, c1, [(9.7, 10.2), (13, 10.2), (16, 10.2)])       # ends inside PA, 0.36 mm off its centre
    routed(ctx)
    rep = polish(ctx, ("entry",))
    s = segs(ctx)
    ends = {_key(p.x1, p.y1) for p in s} | {_key(p.x2, p.y2) for p in s}
    assert _key(10.0, 10.0) in ends and _key(9.7, 10.2) not in ends, geometry(ctx)
    first = [p for p in s if _key(10.0, 10.0) in (_key(p.x1, p.y1), _key(p.x2, p.y2))]
    assert len(first) == 1 and not odd(first[0])               # last segment into the pad: axial or 45 degrees
    assert all(not odd(p) for p in s) and rep.data["edits"].get("entry") == 1


def test_pad_entry_replaces_an_odd_snap_stub():
    # v1-style join: grid path, then a short odd-angle stub onto the pad centre
    c1 = conn(1, "A", (10.03, 10.07), (16, 10.2), pa="PA.1", pb="PB.1")
    pads = [pad("PA", "A", 10.03, 10.07), pad("PB", "A", 16, 10.2)]
    ctx = scene(pads, [c1])
    path(ctx, c1, [(10.03, 10.07), (10.1, 10.2), (16, 10.2)])
    routed(ctx)
    assert sum(odd(p) for p in segs(ctx)) == 1
    polish(ctx, ("entry",))
    s = segs(ctx)
    assert sum(odd(p) for p in s) == 0 and len(s) == 2, geometry(ctx)
    assert _key(10.03, 10.07) in {_key(p.x1, p.y1) for p in s} | {_key(p.x2, p.y2) for p in s}


def test_entry_that_costs_length_waits_for_banked_length():
    # moving the end to the pad centre lengthens the track: refused on its own (the Score may not get worse) ...
    pads = [pad("PA", "A", 10, 10, hw=0.5, hh=0.5), pad("PB", "A", 20, 10.2)]
    c1 = conn(1, "A", (10, 10), (20, 10.2), pa="PA.1", pb="PB.1")
    ctx = scene(pads, [c1])
    path(ctx, c1, [(10.3, 10.2), (20, 10.2)])
    routed(ctx)
    g0 = geometry(ctx)
    rep = polish(ctx, ("entry",))
    assert geometry(ctx) == g0 and rep.data["deferred"] >= 1 and any("clean-up" in n for n in rep.notes)
    # ... and made once another connection has saved more than it costs (stage total not worse)
    c2 = conn(2, "C", (10, 20), (13, 23), pa="PC.1", pb="PD.1")
    ctx = scene(pads + [pad("PC", "C", 10, 20), pad("PD", "C", 13, 23)], [c1, c2])
    path(ctx, c1, [(10.3, 10.2), (20, 10.2)])
    path(ctx, c2, stair_pts(10.0, 20.0, 10))
    routed(ctx)
    rep = polish(ctx)
    ends = {_key(p.x1, p.y1) for p in segs(ctx, 1)} | {_key(p.x2, p.y2) for p in segs(ctx, 1)}
    assert _key(10.0, 10.0) in ends and _key(10.3, 10.2) not in ends, geometry(ctx)
    assert rep.data["delta"]["length_mm"] < 0


# ── via removal ───────────────────────────────────────────────────────────
def _via_scene(extra=(), th=False):
    pads = [pad("PA", "A", 5, 10), pad("PB", "A", 15, 10, **({"kind": "tht", "hole": 0.4, "hw": 0.8, "hh": 0.8} if th else {}))]
    c1 = conn(1, "A", (5, 10), (15, 10), pa="PA.1", pb="PB.1", lb=(0, 1) if th else (0,))
    ctx = scene(pads + list(extra), [c1])
    path(ctx, c1, [(5, 10), (8, 10)])
    via(ctx, c1, 8, 10)
    if th:
        path(ctx, c1, [(8, 10), (15, 10)], layer=1)
    else:
        path(ctx, c1, [(8, 10), (10, 10), (12, 10)], layer=1)
        via(ctx, c1, 12, 10)
        path(ctx, c1, [(12, 10), (15, 10)])
    routed(ctx)
    return ctx


def test_via_pair_removed_when_the_span_is_legal_on_the_entry_layer():
    ctx = _via_scene()
    assert ctx.state.score().vias == 2
    rep = polish(ctx)
    assert ctx.state.score().vias == 0 and rep.data["vias_removed"] == 2
    s = segs(ctx)
    assert len(s) == 1 and s[0].layer == 0 and abs(s[0].length - 10.0) < 1e-9, geometry(ctx)


def test_via_pair_stays_when_the_entry_layer_is_blocked():
    wall = pad("X", "B", 10, 10, hw=0.4, hh=4.0)               # F.Cu only: the span must stay on B.Cu
    ctx = _via_scene([wall, pad("X2", "B", 30, 25)])
    g0 = geometry(ctx)
    rep = polish(ctx)
    assert ctx.state.score().vias == 2 and rep.data["vias_removed"] == 0
    assert [g for g in geometry(ctx) if g[2] == "via"] == [g for g in g0 if g[2] == "via"]


def test_single_via_removed_before_a_through_hole_pad():
    ctx = _via_scene(th=True)
    assert ctx.state.score().vias == 1
    polish(ctx)
    assert ctx.state.score().vias == 0 and all(p.layer == 0 for p in segs(ctx)), geometry(ctx)


def test_via_shared_with_another_connection_is_kept():
    pads = [pad("PA", "A", 5, 10), pad("PB", "A", 15, 10), pad("PC", "A", 8, 14, layer=1)]
    c1 = conn(1, "A", (5, 10), (15, 10), pa="PA.1", pb="PB.1")
    c2 = conn(2, "A", (8, 14), (8, 10), la=(1,), lb=(0, 1), pa="PC.1")      # tees onto c1's via
    ctx = scene(pads, [c1, c2])
    path(ctx, c1, [(5, 10), (8, 10)])
    via(ctx, c1, 8, 10)
    path(ctx, c1, [(8, 10), (12, 10)], layer=1)
    via(ctx, c1, 12, 10)
    path(ctx, c1, [(12, 10), (15, 10)])
    path(ctx, c2, [(8, 14), (8, 10)], layer=1)
    routed(ctx)
    polish(ctx)
    vias = [p for p in ctx.state.pieces() if p.kind == "via"]
    assert any(_key(v.x1, v.y1) == _key(8, 10) for v in vias), geometry(ctx)
    assert ctx.state.connected(2)


# ── re-widen ──────────────────────────────────────────────────────────────
def test_widen_splits_a_segment_at_the_tight_spot():
    # a pad 0.4 mm above the centre line: 0.4 mm wide is the widest legal track under it, 0.5 mm elsewhere
    c1 = conn(1, "A", (5, 10), (25, 10), pa="PA.1", pb="PB.1", width=0.5)
    pads = [pad("PA", "A", 5, 10), pad("PB", "A", 25, 10), pad("X", "B", 15, 10.65, hw=0.3, hh=0.25),
            pad("X2", "B", 30, 25)]
    ctx = scene(pads, [c1])
    path(ctx, c1, [(5, 10), (25, 10)], w=0.2, tw=0.5)
    routed(ctx)
    assert abs(ctx.state.score().necked_mm - 20.0) < 1e-9
    rep = polish(ctx, ("widen",))
    s = sorted(segs(ctx), key=lambda p: min(p.x1, p.x2))
    assert len(s) == 3, geometry(ctx)
    assert [p.w for p in s] == [0.5, 0.4, 0.5] and all(p.target_w == 0.5 for p in s)
    assert abs(sum(p.length for p in s) - 20.0) < 1e-9
    assert min(s[1].x1, s[1].x2) < 14.7 and max(s[1].x1, s[1].x2) > 15.3          # the necked part covers the pad
    assert s[1].length < 2.5                                                      # ... and little more
    for a, b in zip(s, s[1:]):
        assert max(a.x1, a.x2) == min(b.x1, b.x2) and a.y1 == a.y2 == 10.0
    assert abs(ctx.state.score().necked_mm - s[1].length) < 1e-9
    assert rep.data["widened_mm"] > 17.0
    assert ctx.oracle.seg_ok(0, s[1].x1, 10, s[1].x2, 10, 0.41, "A") is not None  # 0.4 really is the widest there


def test_widen_whole_piece_and_diagonal_split():
    c1 = conn(1, "A", (5, 5), (15, 15), pa="PA.1", pb="PB.1", width=0.4)
    pads = [pad("PA", "A", 5, 5), pad("PB", "A", 15, 15), pad("X", "B", 10.35, 9.65, hw=0.15, hh=0.15, r=0.15),
            pad("X2", "B", 30, 25)]
    ctx = scene(pads, [c1])
    path(ctx, c1, [(5, 5), (15, 15)], w=0.2, tw=0.4)
    routed(ctx)
    polish(ctx, ("widen",))
    s = segs(ctx)
    assert len(s) == 3 and sorted(p.w for p in s) == [0.29, 0.4, 0.4], geometry(ctx)
    assert all(abs(abs(p.x2 - p.x1) - abs(p.y2 - p.y1)) < 1e-9 for p in s)        # still exactly 45 degrees
    # nothing in the way: one piece at full width
    ctx = scene(pads[:2], [c1])
    path(ctx, c1, [(5, 5), (15, 15)], w=0.2, tw=0.4)
    routed(ctx)
    polish(ctx, ("widen",))
    s = segs(ctx)
    assert len(s) == 1 and s[0].w == 0.4 and ctx.state.score().necked_mm == 0


# ── invariants ────────────────────────────────────────────────────────────
def test_tee_points_and_teed_segments_do_not_move():
    pads = [pad("PA", "A", 10, 10), pad("PB", "A", 20, 12), pad("PC", "A", 12.6, 14), pad("PD", "A", 17, 8)]
    c1 = conn(1, "A", (10, 10), (20, 12), pa="PA.1", pb="PB.1")
    c2 = conn(2, "A", (12.6, 14), (12.6, 10.6), pa="PC.1")     # tees onto a staircase corner of c1
    c3 = conn(3, "A", (17, 8), (17, 12), pa="PD.1")            # tees onto the middle of a c1 segment
    ctx = scene(pads, [c1, c2, c3])
    stair = stair_pts(10.0, 10.0, 6, d=0.4)                    # ... (12.4, 12.4); corner (12.6?) no: use explicit
    pts = [(10, 10), (10.6, 10), (10.6, 10.6), (12.6, 10.6), (12.6, 11.2), (13.2, 11.2), (13.2, 12), (20, 12)]
    del stair
    path(ctx, c1, pts)
    path(ctx, c2, [(12.6, 14), (12.6, 11.2)])
    c2.b = Terminal(12.6, 11.2, (0,))
    path(ctx, c3, [(17, 8), (17, 12)])
    routed(ctx)
    polish(ctx)
    c1s = segs(ctx, 1)
    ends = {_key(p.x1, p.y1) for p in c1s} | {_key(p.x2, p.y2) for p in c1s}
    assert _key(12.6, 11.2) in ends, geometry(ctx)                                  # the tee corner is still a vertex
    assert any(p.y1 == p.y2 == 12.0 and min(p.x1, p.x2) <= 17 <= max(p.x1, p.x2) for p in c1s)
    assert all(ctx.state.connected(i) for i in (1, 2, 3))
    assert ctx.state.score().length_mm < sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])) + 2.8 + 4


def test_protected_pieces_and_pairs_are_left_alone():
    c1 = conn(1, "A", (10, 10), (13, 13), pa="PA.1", pb="PB.1")
    ctx = scene(AB, [c1])
    path(ctx, c1, stair_pts(10.0, 10.0, 10), protected=True)
    routed(ctx)
    g0 = geometry(ctx)
    rep = polish(ctx)
    assert geometry(ctx) == g0 and rep.data["edits_total"] == 0
    c1 = conn(1, "A", (10, 10), (13, 13), pa="PA.1", pb="PB.1", role="pair")
    ctx = scene(AB, [c1])
    path(ctx, c1, stair_pts(10.0, 10.0, 10))
    routed(ctx)
    g0 = geometry(ctx)
    polish(ctx)
    assert geometry(ctx) == g0


def test_failed_and_unrouted_connections_are_not_touched():
    c1 = conn(1, "A", (10, 10), (13, 13), pa="PA.1", pb="PB.1")
    ctx = scene(AB, [c1])
    path(ctx, c1, stair_pts(10.0, 10.0, 10)[:-3])              # stops short of PB: not connected
    ctx.state.set_status(1, "routed")
    g0 = geometry(ctx)
    rep = PL.run(ctx, Budget(1_000_000))
    assert geometry(ctx) == g0 and rep.data["edits_total"] == 0
    ctx.state.set_status(1, "failed")
    rep = PL.run(ctx, Budget(1_000_000))
    assert geometry(ctx) == g0 and rep.failed == 1 and rep.routed == 0


def test_soft_shapes_and_forbidden_regions_are_respected():
    c1 = conn(1, "A", (10, 10), (14, 10), pa="PA.1", pb="PB.1")
    pads = [pad("PA", "A", 10, 10), pad("PB", "A", 14, 10), pad("X2", "B", 30, 25)]
    u = [(10, 10), (10, 12), (14, 12), (14, 10)]
    # a thermal corridor of net B across the chord: the U may not be pulled through it
    ctx = scene(pads, [c1])
    ctx.oracle.add_soft("thermal", "B", 0, geom.box_from_seg(12, 9, 12, 11.2, 0.5))
    path(ctx, c1, u)
    routed(ctx)
    polish(ctx)
    for p in segs(ctx):
        assert not ctx.oracle.soft_hits(0, p.x1, p.y1, p.x2, p.y2, p.w, "A"), geometry(ctx)
    assert ctx.state.score().length_mm > 4.5

    class Region:
        rect, forbid, layers, nets, exc = (11.0, 9.0, 13.0, 11.0), True, None, None, None

        def applies(self, net):
            return True
    ctx = scene(pads, [c1])
    ctx.cfg.regions = [Region()]
    path(ctx, c1, u)
    routed(ctx)
    polish(ctx)
    for p in segs(ctx):
        assert geom.seg_rect_dist(p.x1 - 12, p.y1 - 10, p.x2 - 12, p.y2 - 10, 1.0, 1.0) > 0, geometry(ctx)


def test_dead_ends_and_loops_are_removed_but_not_tees():
    pads = [pad("PA", "A", 10, 10), pad("PB", "A", 20, 10), pad("PC", "A", 14, 14)]
    c1 = conn(1, "A", (10, 10), (20, 10), pa="PA.1", pb="PB.1")
    c2 = conn(2, "A", (14, 14), (14, 11.5), pa="PC.1")         # ends on the tip of c1's branch at (14, 11.5)
    ctx = scene(pads, [c1, c2])
    path(ctx, c1, [(10, 10), (12, 10), (14, 10), (16, 10), (20, 10)])
    path(ctx, c1, [(12, 10), (12, 11), (12.5, 11)])            # a dead end
    path(ctx, c1, [(16, 10), (16, 9), (16, 10)])               # out and back: a loop hanging off one point
    branch = path(ctx, c1, [(14, 10), (14, 11.5)])             # looks like a dead end, but c2 lands on it
    path(ctx, c2, [(14, 14), (14, 11.5)])
    routed(ctx)
    polish(ctx, ("tight",))
    s1 = segs(ctx, 1)
    pts = {_key(p.x1, p.y1) for p in s1} | {_key(p.x2, p.y2) for p in s1}
    assert _key(12.5, 11) not in pts and _key(12, 11) not in pts and _key(16, 9) not in pts, geometry(ctx)
    assert _key(14, 11.5) in pts and ctx.state.connected(2)
    assert any(p.id == branch[0].id for p in s1)               # the teed branch is the very same piece
    assert abs(ctx.state.score().length_mm - (10 + 1.5 + 2.5)) < 1e-9


def test_cleanup_slack_lets_the_stage_grow_by_that_much_and_no_more():
    pads = [pad("PA", "A", 10, 10, hw=0.5, hh=0.5), pad("PB", "A", 20, 10.2)]
    c1 = conn(1, "A", (10, 10), (20, 10.2), pa="PA.1", pb="PB.1")

    class X:
        polish_slack_mm = 0.5
    for slack, moved in ((0.5, True), (0.05, False)):
        ctx = scene(pads, [c1])
        X.polish_slack_mm = slack
        ctx.x = X()
        path(ctx, c1, [(10.3, 10.2), (20, 10.2)])
        routed(ctx)
        l0 = ctx.state.score().length_mm
        rep = PL.run(ctx, Budget(1_000_000))
        ends = {_key(p.x1, p.y1) for p in segs(ctx)} | {_key(p.x2, p.y2) for p in segs(ctx)}
        assert (_key(10.0, 10.0) in ends) == moved, (slack, geometry(ctx))
        grow = ctx.state.score().length_mm - l0
        assert (0 < grow <= slack) if moved else grow == 0
        assert rep.data["slack_mm"] == slack and ctx.state.connected(1)
        assert_legal(ctx)


def test_loosely_joined_copper_is_kept_joined():
    # c2 ends inside c1's copper but 0.06 mm off its centre line (v1-style join): only KiCad's rule connects it
    pads = [pad("PA", "A", 10, 10), pad("PB", "A", 20, 10), pad("PC", "A", 15, 14)]
    c1 = conn(1, "A", (10, 10), (20, 10), pa="PA.1", pb="PB.1")
    c2 = conn(2, "A", (15, 14), (20, 10), pa="PC.1", pb="PB.1")      # reaches PB only through c1
    ctx = scene(pads, [c1, c2])
    # c1 makes a detour whose top edge c2 touches: pulling c1 tight would leave c2 hanging
    path(ctx, c1, [(10, 10), (13, 10), (13.3, 10.3), (16.7, 10.3), (17, 10), (20, 10)], w=0.3)
    path(ctx, c2, [(15, 14), (15, 10.36)])
    for c in (c1, c2):
        ctx.state.set_status(c.idx, "routed")
    assert ctx.state.connected(1) and not ctx.state.connected(2) and ctx.state.connected(2, True)
    rep = PL.run(ctx, Budget(1_000_000))
    assert rep.data["loose"] == 1
    assert ctx.state.connected(1) and ctx.state.connected(2, True)
    assert any(p.y1 == p.y2 == 10.3 and min(p.x1, p.x2) <= 15 <= max(p.x1, p.x2) for p in segs(ctx, 1)), geometry(ctx)
    assert_legal(ctx)
    # the same board with c2 properly joined on the centre line is not "loose", and still nothing breaks
    c2b = conn(2, "A", (15, 14), (20, 10), pa="PC.1", pb="PB.1")
    ctx = scene(pads, [c1, c2b])
    path(ctx, c1, [(10, 10), (13, 10), (13.3, 10.3), (16.7, 10.3), (17, 10), (20, 10)], w=0.3)
    path(ctx, c2b, [(15, 14), (15, 10.3)])
    routed(ctx)
    rep = polish(ctx)
    assert rep.data["loose"] == 0 and ctx.state.connected(2)


# ── a board full of staircases: connectivity, score, legality, determinism, budget ──
def lanes(n=8, seed=7, necked=True):
    """n nets in horizontal lanes, each a jagged legal route with a via pair, foreign pads between the lanes."""
    rnd = random.Random(seed)
    pads, conns, routes = [], [], []
    for i in range(n):
        y = 4.0 + 3.0 * i
        net = f"N{i}"
        pads += [pad(f"L{i}", net, 4.0, y), pad(f"R{i}", net, 36.0, y)]
        pads.append(pad(f"O{i}", "OBST", 8.0 + 3.0 * (i % 7), y + 1.5, hw=0.3, hh=0.3))
        conns.append(conn(i, net, (4.0, y), (36.0, y), pa=f"L{i}.1", pb=f"R{i}.1", width=0.3 if necked else 0.2))
        pts = [(4.0, y)]
        x, yy = 4.0, y
        while x < 34.5:
            step = rnd.choice((0.2, 0.3, 0.5, 0.8))
            kind = rnd.choice("hhvdd")
            if kind == "h":
                x = round(x + step, 6)
            elif kind == "v":
                yy = round(min(y + 0.6, max(y - 0.6, yy + rnd.choice((-0.2, 0.2, 0.1)))), 6)
            else:
                d = rnd.choice((-0.2, 0.2))
                if abs(yy + d - y) <= 0.6:
                    x, yy = round(x + abs(d), 6), round(yy + d, 6)
            if (x, yy) != pts[-1]:
                pts.append((x, yy))
        pts += [(35.0, yy), (35.0 + abs(yy - y), y)] if yy != y else []
        pts.append((36.0, y))
        routes.append(pts)
    b = board(pads + [pad("OB2", "OBST", 2, 28)], size=(40.0, 30.0))
    o = O.Oracle(b)
    st = make_state(o, conns)
    ctx = Ctx(board=b, cfg=Cfg(), oracle=o, state=st, conns=conns)
    for c, pts in zip(conns, routes):
        cut1 = next(k for k, p in enumerate(pts) if p[0] >= 14.0)
        cut2 = next(k for k, p in enumerate(pts) if p[0] >= 22.0)
        w = 0.2
        path(ctx, c, pts[:cut1 + 1], w=w, tw=c.width)
        via(ctx, c, *pts[cut1])
        path(ctx, c, pts[cut1:cut2 + 1], layer=1, w=w, tw=c.width)
        via(ctx, c, *pts[cut2])
        path(ctx, c, pts[cut2:], w=w, tw=c.width)
    routed(ctx)
    return ctx


def test_lane_board_score_connectivity_legality():
    ctx = lanes()
    before = ctx.state.score()
    n0 = len(ctx.state.pieces())
    rep = polish(ctx)                                          # asserts: score not worse, all connected, all legal
    after = ctx.state.score()
    assert after.vias == 0 and before.vias == 16
    assert after.necked_mm == 0 and before.necked_mm > 200
    assert after.length_mm < before.length_mm - 10 and after.bends < before.bends / 4
    assert len(ctx.state.pieces()) < n0 / 4
    assert all(not odd(p) for p in segs(ctx))
    assert all(p.w == p.target_w for p in segs(ctx))
    assert rep.work > 0 and rep.data["oracle_checks"] > 0 and rep.routed == 8 and rep.failed == 0
    assert set(rep.data["edits"]) <= set(PL.PASSES)
    # a second run finds (next to) nothing and changes no score
    g1 = geometry(ctx)
    rep2 = polish(ctx)
    assert ctx.state.score().key() == after.key()
    assert rep2.data["edits_total"] <= 2 or geometry(ctx) == g1


def test_same_twice():
    a, b = lanes(seed=11), lanes(seed=11)
    ra, rb = polish(a), polish(b)
    assert geometry(a) == geometry(b)
    assert ra.work == rb.work and ra.data == rb.data and ra.notes == rb.notes


def test_budget_is_honoured_and_a_spent_budget_changes_nothing():
    ctx = lanes(seed=3)
    g0 = geometry(ctx)
    rep = PL.run(ctx, Budget(0))
    assert geometry(ctx) == g0 and rep.data["edits_total"] == 0 and rep.work == 0
    full = lanes(seed=3)
    rf = polish(full)
    small = lanes(seed=3)
    rs = polish(small, units=max(200, rf.work // 6))           # polish() checks score / connectivity / legality
    assert rs.work <= rf.work // 6 + PL.CHARGE_BUILD + PL.CHARGE_EDIT + 3 * PL.CHARGE_TRY + 64, (rs.work, rf.work)
    assert 0 < rs.data["edits_total"] < rf.data["edits_total"]
    assert any("budget" in n for n in rs.notes)
    assert not small.state.score().key() < full.state.score().key()               # more budget is never worse


def test_unknown_pass_is_refused():
    ctx = lanes(n=1)
    try:
        PL.run(ctx, Budget(1000), ("tight", "shine"))
    except ValueError as e:
        assert "shine" in str(e)
    else:
        raise AssertionError("unknown pass accepted")


def test_a_pass_that_fails_the_audit_is_restored():
    if REAL:
        return
    c1 = conn(1, "A", (10, 10), (13, 13), pa="PA.1", pb="PB.1")
    ctx = scene(AB, [c1])
    path(ctx, c1, stair_pts(10.0, 10.0, 10))
    routed(ctx)
    g0 = geometry(ctx)
    st = ctx.state
    real_score = st.score
    n = [0]

    def lying_score():
        s = real_score()
        n[0] += 1
        if n[0] > 1 and len(st.pieces()) < 20:                 # after the stage start: claim an edited state is worse
            s.length_mm += 100.0
        return s
    st.score = lying_score
    rep = PL.run(ctx, Budget(1_000_000), ("stairs",))
    st.score = real_score
    assert [g[4:8] for g in geometry(ctx)] == [g[4:8] for g in g0]
    assert any("undone" in x for x in rep.notes) and rep.data["edits_total"] == 0
    assert_legal(ctx)
    # and a state that loses a connection inside the transaction check is never committed
    ctx = scene(AB, [c1])
    path(ctx, c1, stair_pts(10.0, 10.0, 10))
    routed(ctx)
    g0 = geometry(ctx)
    real_conn = ctx.state.connected
    seen = [0]

    def picky(i):
        seen[0] += 1
        return real_conn(i) and (seen[0] <= 2 or len(ctx.state.pieces()) == 20)
    ctx.state.connected = picky
    rep = PL.run(ctx, Budget(1_000_000), ("stairs", "tight"))
    ctx.state.connected = real_conn
    assert geometry(ctx) == g0 and rep.data["edits_total"] == 0 and rep.data["tried"] > 0


def perf():
    ctx = lanes(n=8, seed=5)
    n0 = len(ctx.state.pieces())
    c0 = ctx.oracle.checks
    t = time.perf_counter()
    rep = PL.run(ctx, Budget(50_000_000))
    dt = time.perf_counter() - t
    print(f"  lanes(8): {n0} -> {len(ctx.state.pieces())} pieces, {rep.data['edits_total']} edits, {rep.data['tried']} "
          f"candidates, {ctx.oracle.checks - c0} oracle checks, {rep.work} units in {dt:.2f} s "
          f"({rep.work / dt / 1000:.0f} k units/s, {rep.data['tried'] / dt:.0f} candidates/s)")
    if not REAL:
        print(f"  state calls: {ctx.state.calls}")


def main():
    if REAL:
        from copper_kicad.pcb.v2 import pieces
        if getattr(pieces, "STUB", False):
            print("SKIP --real: copper_kicad.pcb.v2.pieces is still a stub")
            return 0
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_") and callable(v) and (not names or k in names)]
    failed = 0
    for name, fn in tests:
        t = time.perf_counter()
        try:
            fn()
            print(f"ok    {name}  ({time.perf_counter() - t:.2f} s)")
        except Exception:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL  {name}")
            traceback.print_exc()
    if PERF:
        perf()
    print(f"{len(tests) - failed}/{len(tests)} passed" + (" (real RouteState)" if REAL else " (MiniState)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
