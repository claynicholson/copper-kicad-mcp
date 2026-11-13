"""Router v2 repair ladder (design §4.9): probe, min-cut rip, cluster permutation, rescue, global feedback,
certificate.

Pure tests on synthetic boards with the real oracle (no KiCad). The sibling stages repair drives are replaced
through ``repair.DEPS`` by small doubles written here against the design §9 interfaces:

  State     a RouteState (transactions, snapshot, connected, score) on the real Oracle
  Detail    ``route_conn``: a 4-direction lattice router whose every step is judged by the oracle; in soft mode it
            crosses the pieces named in ``soft_cost`` at their price and reports ``crossed`` / ``crossed_pieces``
  Power, Fanout, Tiles, Global   scripted

``test_real_siblings`` runs one scenario on the real ``pieces`` / ``detail`` when they are no longer stubs.

Run:  python tests/test_v2_repair.py [test_name ...] [--perf] [--design-order]
"""
from __future__ import annotations

import heapq
import importlib
import math
import types
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.guided import model  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2 import repair as R  # noqa: E402
from copper_kicad.pcb.v2.types import (CHARGE_SEARCH, Budget, ConnV2, Corridor, Ctx, Cut, Piece, Score,  # noqa: E402
                                       Terminal, q)

W = 0.2          # track width of every test net (class clearance 0.2)


# ── synthetic board ─────────────────────────────────────────────────────────────────────────────────
def board(pads, *, size=(24.0, 12.0), layers=("F.Cu", "B.Cu")):
    """pads: dicts {ref, net, x, y, hw, hh, num='1', layer=0}."""
    names = sorted({p["net"] for p in pads if p.get("net")})
    rules = {"clearance": 0.1, "track_width": 0.1, "via": 0.4, "drill": 0.2, "edge_clearance": 0.3, "hole_clearance": 0.25,
             "hole_to_hole": 0.25, "annular": 0.05,
             "default": {"clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3, "dp_gap": 0.25, "dp_width": 0.2}}
    ex = {"layers": list(layers), "rules": rules,
          "nets": {n: {"code": i + 1, "class": "Default", "clearance": 0.2, "width": 0.2, "via": 0.6, "drill": 0.3,
                       "dp_gap": 0.25, "dp_width": 0.2} for i, n in enumerate(names)},
          "outline": {"closed": True, "polys": [{"o": [[0.0, 0.0], [size[0], 0.0], [size[0], size[1]], [0.0, size[1]]]}]},
          "pads": [], "footprints": [], "graphics": [], "tracks": [], "vias": [], "zones": [], "mask_openings": [],
          "unconnected": 0, "unconnected_by_net": {}, "totals": {"tracks": 0, "vias": 0, "track_length_mm": 0}}
    for i, p in enumerate(pads):
        lays = [p.get("layer", 0)]
        ex["pads"].append({"ref": p["ref"], "num": p.get("num", "1"), "net": p.get("net", ""), "x": p["x"], "y": p["y"],
                           "kind": "smd", "layers": lays, "c": i + 1,
                           "shapes": [[lays, "box", [p["x"], p["y"], 1.0, 0.0, p["hw"], p["hh"], 0.0]]]})
    return model.Board(ex)


def pad(ref, net, x, y, hw=0.3, hh=0.3, num="1", layer=0):
    return {"ref": ref, "net": net, "x": x, "y": y, "hw": hw, "hh": hh, "num": num, "layer": layer}


def wall(ref, x0, y0, x1, y1, layer=0):
    """A block of fixed foreign copper."""
    return {"ref": ref, "net": "WALL", "x": (x0 + x1) / 2, "y": (y0 + y1) / 2, "hw": (x1 - x0) / 2, "hh": (y1 - y0) / 2,
            "layer": layer}


# ── doubles ─────────────────────────────────────────────────────────────────────────────────────────
class State:
    """RouteState double (design §9 pieces.py) on the real oracle. ``journal_status=False`` models a state whose
    rollback() does not restore connection status (repair must cope with both)."""

    def __init__(self, oracle, conns, journal_status=True):
        self.o = oracle
        self.conns = {c.idx: c for c in conns}
        self._p = {}
        self._next = 1
        self._status = {c.idx: "todo" for c in conns}
        self.fails = {}
        self._log = []
        self._marks = []
        self.new_errors = 0
        self.journal_status = journal_status
        self.score_hook = None

    def begin(self):
        self._marks.append(len(self._log))
        return len(self._marks)

    def commit(self, tok):
        assert tok == len(self._marks), "commit out of order"
        self._marks.pop()
        if not self._marks:
            self._log.clear()

    def rollback(self, tok):
        assert tok == len(self._marks), "rollback out of order"
        mark = self._marks.pop()
        while len(self._log) > mark:
            op = self._log.pop()
            if op[0] == "add":
                self.o.remove_piece(op[1])
                del self._p[op[1].id]
            elif op[0] == "rem":
                self._p[op[1].id] = op[1]
                self.o.add_piece(op[1])
            elif self.journal_status:
                self._status[op[1]] = op[2]
                if op[3] is None:
                    self.fails.pop(op[1], None)
                else:
                    self.fails[op[1]] = op[3]

    def add(self, **f):
        p = Piece(id=self._next, **f)
        self._next += 1
        self._p[p.id] = p
        self.o.add_piece(p)
        if self._marks:
            self._log.append(("add", p))
        return p

    def remove(self, pid):
        p = self._p.pop(pid)
        self.o.remove_piece(p)
        if self._marks:
            self._log.append(("rem", p))

    def rip_conn(self, conn):
        out = self.pieces(conn=conn)
        for p in out:
            self.remove(p.id)
        return out

    def pieces(self, conn=None, net=None):
        return [p for _i, p in sorted(self._p.items()) if (conn is None or p.conn == conn) and (net is None or p.net == net)]

    def set_status(self, conn, status, fail=None):
        if self._marks:
            self._log.append(("st", conn, self._status[conn], self.fails.get(conn)))
        self._status[conn] = status
        if status == "failed" and fail is not None:
            self.fails[conn] = fail
        elif status != "failed":
            self.fails.pop(conn, None)

    def status(self, conn):
        return self._status[conn]

    def connected(self, conn):
        c = self.conns[conn]
        segs = [p for p in self._p.values() if p.net == c.net and p.kind == "seg"]

        def on(x, y, p):
            dx, dy = p.x2 - p.x1, p.y2 - p.y1
            L = dx * dx + dy * dy
            t = 0.0 if L == 0 else max(0.0, min(1.0, ((x - p.x1) * dx + (y - p.y1) * dy) / L))
            return math.hypot(p.x1 + t * dx - x, p.y1 + t * dy - y) < 2e-6
        seen = set()
        todo = [(c.a.x, c.a.y)]
        tee = c.b.pad is None and c.tree_target
        while todo:
            x, y = todo.pop()
            if abs(x - c.b.x) < 2e-6 and abs(y - c.b.y) < 2e-6:
                return True
            for p in segs:
                if p.id in seen:
                    continue
                if on(x, y, p):
                    seen.add(p.id)
                    if tee and p.conn != conn:
                        return True
                    todo += [(p.x1, p.y1), (p.x2, p.y2)]
                    if on(c.b.x, c.b.y, p):
                        return True
        return False

    def snapshot(self):
        return (tuple(self.pieces()), tuple(sorted(self._status.items())))

    def restore(self, snap):
        for p in self.pieces():
            self.remove(p.id)
        for p in snap[0]:
            self._p[p.id] = p
            self.o.add_piece(p)
        self._status = dict(snap[1])

    def score(self):
        s = Score(new_errors=self.new_errors, opens=sum(1 for v in self._status.values() if v != "routed"),
                  necked_mm=round(sum(p.length for p in self._p.values() if p.kind == "seg" and p.w < p.target_w), 6),
                  vias=sum(1 for p in self._p.values() if p.kind == "via" and p.conn >= 0),
                  length_mm=round(sum(p.length for p in self._p.values()), 6),
                  bends=sum(1 for p in self._p.values() if p.kind == "seg"))
        return self.score_hook(self, s) if self.score_hook else s

    def export(self):
        return [], [], [p.id for p in self.pieces()]

    def geometry(self):
        """Copper without piece ids (ids differ after a rollback and re-route)."""
        return sorted((p.net, p.conn, p.kind, p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.protected, p.target_w)
                      for p in self._p.values())


_STEPS = ((1, 0), (0, 1), (-1, 0), (0, -1))


class Detail:
    """detail.route_conn double: Dijkstra on a square lattice, 4 directions, one layer; every step is an oracle
    check at the connection's width. Soft mode (``soft_cost`` as the real detail stage takes it: a callable
    Piece -> mm | None, a {conn: mm} dict or a number): a step that is illegal only because of priced pieces may
    be taken; entering the copper of a connection costs the piece's price once."""

    def __init__(self, pitch=0.5, piece_info=True, dirty_soft=False, tile=None):
        self.pitch = pitch
        self.piece_info = piece_info
        self.dirty_soft = dirty_soft        # soft mode leaves its (illegal) pieces in the state: repair must roll back
        self.tile = tile                    # tile size: restrict the search to ctx.corridors[conn].tiles
        self.calls = []
        self.expanded = 0

    def route_conn(self, ctx, conn, budget, *, widen=0, pitch_scale=1.0, extra_vias=0, soft_cost=None):
        self.calls.append((conn.idx, widen, pitch_scale, extra_vias, bool(soft_cost)))
        budget.charge(CHARGE_SEARCH)
        o, st = ctx.oracle, ctx.state
        p = self.pitch * pitch_scale
        layer = conn.a.layers[0]
        w = conn.width
        soft = {}
        if soft_cost is not None:                         # W4's convention: number | {conn: mm, None: default} | callable
            for x in st.pieces():
                if x.net == conn.net:
                    continue
                mm = soft_cost(x) if callable(soft_cost) else (
                    soft_cost.get(x.conn, soft_cost.get(None)) if isinstance(soft_cost, dict) else float(soft_cost))
                if mm is not None:
                    soft[x.id] = mm
        soft_ids = frozenset(soft)
        owner = {x.id: x.conn for x in st.pieces()} if soft else {}
        bb = ctx.board.bbox
        cor = ctx.corridors.get(conn.idx) if self.tile else None
        start = (round(conn.a.x / p), round(conn.a.y / p))
        goal = (round(conn.b.x / p), round(conn.b.y / p))
        goals = {goal}
        if conn.b.pad is None and conn.tree_target:       # a tee: any lattice point on the net's other copper will do
            for t in st.pieces(net=conn.net):
                if t.conn != conn.idx and t.kind == "seg" and t.layer == layer:
                    n = max(1, round(t.length / p))
                    for i in range(n + 1):
                        x, y = t.x1 + (t.x2 - t.x1) * i / n, t.y1 + (t.y2 - t.y1) * i / n
                        if abs(x / p - round(x / p)) < 1e-6 and abs(y / p - round(y / p)) < 1e-6:
                            goals.add((round(x / p), round(y / p)))
        cache = {}

        def step(u, v):
            k = (u, v) if u < v else (v, u)
            r = cache.get(k)
            if r is None:
                x1, y1, x2, y2 = q(u[0] * p), q(u[1] * p), q(v[0] * p), q(v[1] * p)
                if o.seg_legal(layer, x1, y1, x2, y2, w, conn.net):
                    r = ()
                elif soft and o.seg_legal(layer, x1, y1, x2, y2, w, conn.net, ignore=soft_ids):
                    r = tuple(sorted({b.piece for b in o.blockers(layer, x1, y1, x2, y2, w, conn.net, limit=64)
                                      if b.piece is not None and b.piece in soft}))
                else:
                    r = False
                cache[k] = r
            return r
        heap = [(0.0, 0, start, -1, frozenset(), None)]
        seen = {}
        seq = 0
        best_near = (math.inf, start)
        end = None
        while heap:
            g, _s, u, d, cur, back = heapq.heappop(heap)
            key = (u, d, cur)
            if key in seen:
                continue
            seen[key] = back
            self.expanded += 1
            budget.charge(1)
            if u in goals:
                end = key
                break
            if budget.spent_out():
                return {"ok": False, "pieces": [], "crossed": {},
                        "fail": {"reason": "time budget used up", "where": [u[0] * p, u[1] * p], "tried": ["window"]}}
            dist = abs(u[0] - goal[0]) + abs(u[1] - goal[1])
            if (dist, u) < best_near:
                best_near = (dist, u)
            for nd, (dx, dy) in enumerate(_STEPS):
                v = (u[0] + dx, u[1] + dy)
                if not (bb[0] < v[0] * p < bb[2] and bb[1] < v[1] * p < bb[3]):
                    continue
                if cor is not None and (int(v[0] * p // self.tile), int(v[1] * p // self.tile)) not in cor.tiles:
                    continue
                r = step(u, v)
                if r is False:
                    continue
                conns_now = frozenset(owner[i] if owner[i] >= 0 else -i for i in r)
                cost = p + (0.05 if d not in (-1, nd) else 0.0)
                for i in r:
                    k = owner[i] if owner[i] >= 0 else -i
                    if k not in cur:                      # the price is charged once per entry
                        cost += soft[i]
                seq += 1
                heapq.heappush(heap, (g + cost, seq, v, nd, conns_now, (key, r)))
        if end is None:
            u = best_near[1]
            return {"ok": False, "pieces": [], "crossed": {},
                    "fail": {"reason": "blocked", "where": [q(u[0] * p), q(u[1] * p)], "blocked_by": [],
                             "tried": ["pattern", "window", "wide"]}}
        pts = [end[0]]
        crossed_p = {}
        k = end
        while seen[k] is not None:
            k, r = seen[k]
            for i in r:
                crossed_p[i] = crossed_p.get(i, 0) + 1
            pts.append(k[0])
        pts.reverse()
        crossed = {}
        for i in sorted(crossed_p):
            crossed[owner[i]] = crossed.get(owner[i], 0.0) + soft[i]
        runs = [pts[0]]
        for i in range(1, len(pts)):
            if i + 1 < len(pts) and (pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) == \
                    (pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]):
                continue
            runs.append(pts[i])
        out = {"ok": True, "pieces": [], "crossed": crossed, "fail": None}
        if self.piece_info:
            out["crossed_pieces"] = crossed_p
        if crossed and not self.dirty_soft:               # a plan, as the real detail stage returns it: nothing added
            out.update(ok=False, plan=True, fail={"reason": "blocked by routed copper (soft plan)", "code": "plan",
                                                  "where": [conn.a.x, conn.a.y], "tried": ["corridor"]})
            return out
        for a, b in zip(runs, runs[1:]):
            out["pieces"].append(st.add(net=conn.net, conn=conn.idx, kind="seg", layer=layer, x1=q(a[0] * p),
                                        y1=q(a[1] * p), x2=q(b[0] * p), y2=q(b[1] * p), w=w, stage="repair",
                                        target_w=w))
        return out


def steps(*names):
    """The ladder steps ``names`` in the order the ladder takes them (rescue before or after the rips)."""
    order = ("route", "probe", "rescue", "mincut", "permute", "feedback") if R.RESCUE_FIRST else R.LADDER
    return [n for n in order if n in names]


def conn(idx, net, a, b, **kw):
    ta = a if isinstance(a, Terminal) else Terminal(x=a[0], y=a[1], layers=(0,), pad=a[2] if len(a) > 2 else None)
    tb = b if isinstance(b, Terminal) else Terminal(x=b[0], y=b[1], layers=(0,), pad=b[2] if len(b) > 2 else None)
    est = abs(ta.x - tb.x) + abs(ta.y - tb.y)
    return ConnV2(idx=idx, net=net, a=ta, b=tb, est=est, width=kw.pop("width", W), neck=kw.pop("neck", W),
                  clearance=0.2, via=(0.6, 0.3), rank=(idx,), **kw)


def make(pads, conns, *, size=(24.0, 12.0), detail=None, journal_status=True, **deps):
    b = board(pads, size=size)
    o = O.Oracle(b)
    st = State(o, conns, journal_status=journal_status)
    ctx = Ctx(board=b, cfg=None, x=None, oracle=o, state=st, conns=list(conns))
    det = detail or Detail()
    R.DEPS.clear()
    R.DEPS.update({"detail": det, "fanout": None, "power": None, "globalroute": None})
    R.DEPS.update(deps)
    return ctx, det


def route_all(ctx, det, budget=None):
    """The detail stage in miniature: every connection once, in rank order."""
    budget = budget or Budget(10_000_000)
    for c in sorted(ctx.conns, key=lambda c: c.rank):
        r = det.route_conn(ctx, c, budget)
        ctx.state.set_status(c.idx, "routed" if r["ok"] else "failed", r.get("fail"))


def all_legal(ctx):
    for p in ctx.state.pieces():
        b = ctx.oracle.piece_ok(p)
        assert b is None, (p, b)


# ── scenarios ───────────────────────────────────────────────────────────────────────────────────────
def wall_scenario(**kw):
    """A (routed first, straight across the board between two pads that touch the edges) walls B's two pads off
    from each other. A can go round B's pad; B cannot go round A. C is a bystander."""
    pads = [pad("A1", "A", 1.0, 6.0, hw=0.5), pad("A2", "A", 19.0, 6.0, hw=0.5),
            pad("B1", "B", 10.0, 3.0), pad("B2", "B", 10.0, 9.0),
            pad("C1", "C", 13.0, 10.5), pad("C2", "C", 17.0, 10.5)]
    conns = [conn(0, "A", (1.0, 6.0, "A1.1"), (19.0, 6.0, "A2.1")),
             conn(1, "B", (10.0, 3.0, "B1.1"), (10.0, 9.0, "B2.1")),
             conn(2, "C", (13.0, 10.5, "C1.1"), (17.0, 10.5, "C2.1"))]
    return make(pads, conns, size=(20.0, 12.0), **kw)


def gap_scenario(**kw):
    """A wall across the board with one gap that takes exactly one track; A and B both have to cross it."""
    pads = [wall("W1", 0.2, 5.6, 9.65, 6.4), wall("W2", 10.35, 5.6, 19.8, 6.4),
            pad("A1", "A", 8.0, 3.0), pad("A2", "A", 8.0, 9.0), pad("B1", "B", 12.0, 3.0), pad("B2", "B", 12.0, 9.0)]
    conns = [conn(0, "A", (8.0, 3.0, "A1.1"), (8.0, 9.0, "A2.1")), conn(1, "B", (12.0, 3.0, "B1.1"), (12.0, 9.0, "B2.1"))]
    return make(pads, conns, size=(20.0, 12.0), **kw)


def puzzle_scenario(**kw):
    """Three lanes (y = 7.0, 7.5, 8.0) through a channel, three nets whose pads form a staircase on both sides:
    only C on the top lane, A on the middle one and B on the bottom one is planar. Routed in the order A, B, C the
    first two take the top two lanes and C is shut out; ripping A alone does not help (B has its lane)."""
    pads = [wall("WU", 10.0, 0.2, 14.0, 6.65), wall("WL", 10.0, 8.35, 14.0, 11.8),
            wall("LC", 0.2, 1.7, 6.5, 2.3), wall("LA", 0.2, 3.7, 3.5, 4.3),          # nobody passes behind a pad
            wall("RC", 17.5, 1.7, 23.8, 2.3), wall("RA", 20.5, 3.7, 23.8, 4.3),
            pad("C1", "C", 7.0, 2.0), pad("A1", "A", 4.0, 4.0), pad("B1", "B", 1.0, 6.0, hw=0.5),
            pad("C2", "C", 17.0, 2.0), pad("A2", "A", 20.0, 4.0), pad("B2", "B", 23.0, 6.0, hw=0.5)]
    conns = [conn(0, "A", (4.0, 4.0, "A1.1"), (20.0, 4.0, "A2.1")),
             conn(1, "B", (1.0, 6.0, "B1.1"), (23.0, 6.0, "B2.1")),
             conn(2, "C", (7.0, 2.0, "C1.1"), (17.0, 2.0, "C2.1"))]
    return make(pads, conns, size=(24.0, 12.0), **kw)


# ── tests ───────────────────────────────────────────────────────────────────────────────────────────
def test_rip_price_formula():
    ctx, det = wall_scenario()
    route_all(ctx, det)
    mem = R.memory(ctx)
    # straight A: routed_len == est -> 4 mm x (w / w_min) / 1
    assert abs(R.rip_price(ctx, 0) - 4.0) < 1e-9
    ctx.conns[2].width = 0.4                              # twice the minimum width costs twice as much
    assert abs(R.rip_price(ctx, 2) - 8.0) < 1e-9
    ctx.conns[2].width = W
    p = ctx.state.pieces(conn=0)[0]
    prot = Piece(id=999, net="A", conn=0, kind="seg", layer=0, x1=p.x1, y1=p.y1, x2=p.x2, y2=p.y2, w=W, protected=True)
    assert abs(R.rip_price(ctx, 0, prot) - 32.0) < 1e-9   # PROTECT_FACTOR
    mem.rip_hist[0] = 0.6
    assert abs(R.rip_price(ctx, 0) - 4.6) < 1e-9          # + history
    mem.rip_hist.clear()
    ctx.conns[0].est = 9.0                                # a detoured connection (18 mm for 9) is half the price
    assert abs(R.rip_price(ctx, 0) - 2.0) < 1e-9


def test_mincut_rips_only_the_blocker_and_both_route():
    ctx, det = wall_scenario()
    route_all(ctx, det)
    st = ctx.state
    assert [st.status(i) for i in (0, 1, 2)] == ["routed", "failed", "routed"]
    c_before = st.pieces(conn=2)
    rep = R.run(ctx, Budget(2_000_000))
    assert [st.status(i) for i in (0, 1, 2)] == ["routed", "routed", "routed"], rep
    assert rep.routed == 1 and rep.failed == 0 and rep.score.opens == 0
    assert st.pieces(conn=2) == c_before                  # the bystander kept its very pieces (ids included)
    assert all(st.connected(i) for i in (0, 1, 2))
    all_legal(ctx)
    mem = R.memory(ctx)
    assert mem.mincut[1] == [0] and mem.ripped == {0: 1}
    assert rep.data["stats"]["rips"] == 1 and rep.data["stats"]["accepted"] == 1 and rep.data["stats"]["rollbacks"] == 0
    assert (1, frozenset({0})) in mem.tabu
    assert rep.data["tried"][1] == steps("probe", "rescue" if R.RESCUE_FIRST else "", "mincut")
    assert not st._marks                                  # every transaction closed
    assert all(c[4] is False or c[1] == 2 for c in det.calls)       # soft searches run in the corridor + 2 rings


def test_mincut_copes_with_other_detail_and_state_conventions():
    # no crossed_pieces in the result; soft mode leaves its illegal pieces behind; rollback() forgets status
    for kw in ({"piece_info": False}, {"dirty_soft": True}, {"piece_info": False, "dirty_soft": True}):
        for journal in (True, False):
            ctx, det = wall_scenario(detail=Detail(**kw), journal_status=journal)
            route_all(ctx, det)
            R.run(ctx, Budget(2_000_000))
            assert [ctx.state.status(i) for i in (0, 1, 2)] == ["routed"] * 3, (kw, journal)
            all_legal(ctx)
            assert not ctx.state._marks


def test_tabu_terminates_a_ping_pong_pair():
    ctx, det = gap_scenario()
    route_all(ctx, det)
    st = ctx.state
    assert [st.status(i) for i in (0, 1)] == ["routed", "failed"]
    geo = st.geometry()
    a_pieces = st.pieces(conn=0)
    rep = R.run(ctx, Budget(2_000_000))
    mem = R.memory(ctx)
    # B may rip A once; A cannot be re-routed, so everything is rolled back and the pair is tabu
    assert rep.data["stats"]["rips"] == 1 and rep.data["stats"]["accepted"] == 0
    assert (1, frozenset({0})) in mem.tabu and mem.blame[(1, frozenset({0}))] == [0]
    assert [st.status(i) for i in (0, 1)] == ["routed", "failed"]
    assert st.geometry() == geo and st.pieces(conn=0) == a_pieces and st.connected(0)
    assert mem.rip_hist[0] == R.HISTORY_MM                # the rip that did not pay left history on the victim
    cert = st.fails[1]
    assert cert["mincut_nets"] == ["A"] and cert["blocked_by"][0] == "A" and "mincut" in cert["tried"]
    assert "cannot share this channel" in cert["reason"] and cert["static_share"] == 0.0
    # the named cut is the gap in the wall: 0.7 mm wide -> 0.5 mm of track + clearance, two tracks want 0.8 mm
    cut = cert["cut"]
    assert (cut["a"], cut["b"], cut["layer"]) == ("W1.1", "W2.1", "F.Cu"), cut
    assert abs(cut["capacity_mm"] - 0.5) < 1e-6 and abs(cut["demand_mm"] - 0.8) < 1e-6 and cut["nets"] == ["B", "A"]
    assert any(s["patch"] == {"nets": {"B": {"priority": 90}}} for s in cert["suggest"])
    # a second and a third run do not rip again: the pair is tabu, the cluster was permuted once
    calls = len(det.calls)
    for _ in range(2):
        rep2 = R.run(ctx, Budget(2_000_000))
        assert rep2.data["stats"]["rips"] == 0 and rep2.data["stats"]["tabu_hits"] == 1
        assert rep2.data["stats"]["perm_orders"] == 0
        assert st.geometry() == geo and [st.status(i) for i in (0, 1)] == ["routed", "failed"]
    assert len(det.calls) - calls <= 2 * 8                # bounded work per run
    assert not st._marks


def test_permutation_solves_a_three_net_order_puzzle():
    ctx, det = puzzle_scenario()
    route_all(ctx, det)
    st = ctx.state
    assert [st.status(i) for i in (0, 1, 2)] == ["routed", "routed", "failed"]
    # direct call: the cluster in any input order
    ok = R.permute_cluster(ctx, [ctx.conns[2], ctx.conns[0], ctx.conns[1]], Budget(3_000_000))
    assert ok and [st.status(i) for i in (0, 1, 2)] == ["routed"] * 3
    all_legal(ctx)
    lanes = {}
    for i in (0, 1, 2):
        ys = {p.y1 for p in st.pieces(conn=i) if p.y1 == p.y2 and min(p.x1, p.x2) <= 10.0 and max(p.x1, p.x2) >= 14.0}
        assert len(ys) == 1, (i, st.pieces(conn=i))
        lanes[i] = ys.pop()
    assert lanes == {2: 7.0, 0: 7.5, 1: 8.0}              # C top, A middle, B bottom: the only planar order
    assert R.memory(ctx).stats["perm_orders"] <= R.PERM_MAX and not st._marks


def test_ladder_reaches_the_permutation_by_itself():
    ctx, det = puzzle_scenario()
    route_all(ctx, det)
    st = ctx.state
    rep = R.run(ctx, Budget(4_000_000))
    mem = R.memory(ctx)
    assert [st.status(i) for i in (0, 1, 2)] == ["routed"] * 3, (rep, mem.mincut, mem.contested)
    assert all(st.connected(i) for i in (0, 1, 2))
    all_legal(ctx)
    # min-cut named A (and failed: A had nowhere to go), the victim's own soft search named B
    assert mem.mincut[2] == [0] and mem.contested[2] == [0, 1]
    assert rep.data["stats"]["accepted"] == 0 and rep.data["stats"]["perm_solved"] == 1
    assert rep.data["tried"][2] == steps("probe", "rescue" if R.RESCUE_FIRST else "", "mincut", "permute")
    assert rep.routed == 1 and rep.failed == 0 and not st._marks


def test_permutation_without_a_solution_rolls_back_and_names_the_channel():
    ctx, det = gap_scenario()
    route_all(ctx, det)
    st = ctx.state
    geo = st.geometry()
    assert R.permute_cluster(ctx, list(ctx.conns), Budget(2_000_000)) is False
    assert st.geometry() == geo and [st.status(i) for i in (0, 1)] == ["routed", "failed"]
    assert R.memory(ctx).channel[1]["nets"] == ["A", "B"] and 0 not in R.memory(ctx).channel
    assert not st._marks


class Power:
    """power.place_dogbone double: puts the dog-bone at the scripted site, or reports that there is none."""

    def __init__(self, site):
        self.site = site
        self.calls = []

    def place_dogbone(self, ctx, pad_, exclude=frozenset()):
        self.calls.append((pad_.name, exclude))
        if self.site is None or self.site in exclude:
            return None
        x, y = self.site
        if not ctx.oracle.via_legal(x, y, 0.6, 0.3, "GND") or not ctx.oracle.seg_legal(0, pad_.x, pad_.y, x, y, W, "GND"):
            return None
        return dogbone(ctx, pad_.x, pad_.y, [(x, y)])


def dogbone(ctx, px, py, pts):
    out = []
    x0, y0 = px, py
    for x, y in pts:
        out.append(ctx.state.add(net="GND", conn=-1, kind="seg", layer=0, x1=x0, y1=y0, x2=x, y2=y, w=W, stage="power",
                                 protected=True))
        x0, y0 = x, y
    out.append(ctx.state.add(net="GND", conn=-1, kind="via", layer=-1, x1=x0, y1=y0, x2=x0, y2=y0, w=0.6, drill=0.3,
                             stage="power", protected=True))
    return out


def dogbone_scenario(site):
    """A GND dog-bone via sits in the only gap of a wall; B has to pass that gap."""
    pads = [wall("W1", 0.2, 5.6, 9.4, 6.4), wall("W2", 10.6, 5.6, 19.8, 6.4), pad("G1", "GND", 8.0, 4.5),
            pad("B1", "B", 12.0, 3.0), pad("B2", "B", 12.0, 9.0)]
    conns = [conn(0, "B", (12.0, 3.0, "B1.1"), (12.0, 9.0, "B2.1"))]
    pw = Power(site)
    ctx, det = make(pads, conns, size=(20.0, 12.0), power=pw)
    bone = dogbone(ctx, 8.0, 4.5, [(10.0, 4.5), (10.0, 6.0)])
    all_legal(ctx)
    route_all(ctx, det)
    assert ctx.state.status(0) == "failed"
    return ctx, det, pw, bone


def test_protected_dogbone_is_only_moved_with_a_replacement():
    # 1. a replacement site exists: the dog-bone moves, B routes through the gap
    ctx, det, pw, bone = dogbone_scenario((6.0, 4.5))
    st = ctx.state
    rep = R.run(ctx, Budget(2_000_000))
    assert st.status(0) == "routed" and st.connected(0), rep
    assert pw.calls == [("G1.1", frozenset({(10.0, 6.0)}))]          # asked for the same pad, old via site excluded
    now = st.pieces(net="GND")
    assert not any(p.id in {b.id for b in bone} for p in now)
    assert len(now) == 2 and all(p.protected and p.stage == "power" and p.conn == -1 for p in now)
    assert [p for p in now if p.kind == "via"][0].x1 == 6.0
    all_legal(ctx)
    assert (0, frozenset({-1})) in R.memory(ctx).tabu
    # 2. no replacement: nothing moves, the transaction is rolled back, B stays failed
    ctx, det, pw, bone = dogbone_scenario(None)
    st = ctx.state
    geo = st.geometry()
    rep = R.run(ctx, Budget(2_000_000))
    assert st.status(0) == "failed" and st.geometry() == geo and st.pieces(net="GND") == bone
    assert len(pw.calls) == 1 and rep.data["stats"]["rollbacks"] >= 1
    # 3. no power stage at all: the dog-bone is never offered to the soft search
    ctx, det, pw, bone = dogbone_scenario((6.0, 4.5))
    R.DEPS["power"] = None
    R.run(ctx, Budget(2_000_000))
    assert ctx.state.status(0) == "failed" and ctx.state.pieces(net="GND") == bone and pw.calls == []
    assert not any(c[4] for c in det.calls)               # nothing rippable: not one soft search was paid for
    # 4. the detail stage does not say which piece was crossed: reservations stay hard
    ctx, det, pw, bone = dogbone_scenario((6.0, 4.5))
    det.piece_info = False
    R.run(ctx, Budget(2_000_000))
    assert ctx.state.status(0) == "failed" and ctx.state.pieces(net="GND") == bone and pw.calls == []
    assert not ctx.state._marks


def test_plan_protected_net_is_never_ripped():
    class NetX:
        def __init__(self, protect):
            self.protect = protect

    class X:
        def net(self, name):
            return NetX(name == "A")
    ctx, det = wall_scenario()
    ctx.x = X()
    route_all(ctx, det)
    a = ctx.state.pieces(conn=0)
    rep = R.run(ctx, Budget(2_000_000))
    assert ctx.state.status(1) == "failed" and ctx.state.pieces(conn=0) == a
    assert rep.data["stats"]["rips"] == 0 and 1 in R.memory(ctx).static_cut
    assert "fixed copper" in ctx.state.fails[1]["reason"]


def test_rollback_when_the_score_gets_worse():
    ctx, det = wall_scenario()
    route_all(ctx, det)
    st = ctx.state
    geo = st.geometry()
    ids = [p.id for p in st.pieces()]
    # a judge that dislikes any copper of B: routing it makes the score worse although opens go down
    st.score_hook = lambda s, sc: Score(new_errors=sc.new_errors + (1 if s.pieces(conn=1) else 0), opens=sc.opens,
                                        necked_mm=sc.necked_mm, vias=sc.vias, length_mm=sc.length_mm, bends=sc.bends)
    rep = R.run(ctx, Budget(2_000_000))
    assert rep.data["stats"]["rips"] >= 1 and rep.data["stats"]["accepted"] == 0 and rep.routed == 0
    assert st.geometry() == geo and [p.id for p in st.pieces()] == ids       # rollback restored the very pieces
    assert [st.status(i) for i in (0, 1, 2)] == ["routed", "failed", "routed"] and st.connected(0)
    assert not st._marks


def test_tee_dependent_of_a_victim_is_ripped_and_rerouted_too():
    ctx, det = wall_scenario()
    # D: a second connection of net A that tees onto A's straight track from a pad below it
    b = board([pad("A1", "A", 1.0, 6.0, hw=0.5), pad("A2", "A", 19.0, 6.0, hw=0.5), pad("A3", "A", 5.0, 8.0),
               pad("B1", "B", 10.0, 3.0), pad("B2", "B", 10.0, 9.0)], size=(20.0, 12.0))
    conns = [conn(0, "A", (1.0, 6.0, "A1.1"), (19.0, 6.0, "A2.1")), conn(1, "B", (10.0, 3.0, "B1.1"), (10.0, 9.0, "B2.1")),
             conn(2, "A", (5.0, 8.0, "A3.1"), (5.0, 6.0))]
    o = O.Oracle(b)
    ctx = Ctx(board=b, cfg=None, x=None, oracle=o, state=State(o, conns), conns=conns)
    route_all(ctx, det)
    st = ctx.state
    assert [st.status(i) for i in (0, 1, 2)] == ["routed", "failed", "routed"] and st.connected(2)
    rep = R.run(ctx, Budget(3_000_000))
    # ripping A leaves D's tee in the air: D is ripped with it and routed again (to its own end point)
    assert st.status(1) == "routed" and st.status(0) == "routed" and st.status(2) == "routed", rep
    assert all(st.connected(i) for i in (0, 1, 2))
    all_legal(ctx)


def test_connection_joined_through_a_victim_keeps_its_copper():
    """Trees laid by the v1 search join clusters, not the two pads a connection names: A4's connection names A1
    but its copper ends on A2, so its pads are joined through A's main track. Ripping that track must not drag it
    along — once the victim is routed again it is connected again, and it keeps its very pieces."""
    pads = [pad("A1", "A", 1.0, 6.0, hw=0.5), pad("A2", "A", 19.0, 6.0, hw=0.5), pad("A4", "A", 17.0, 10.0),
            pad("B1", "B", 10.0, 3.0), pad("B2", "B", 10.0, 9.0)]
    conns = [conn(0, "A", (1.0, 6.0, "A1.1"), (19.0, 6.0, "A2.1")), conn(1, "B", (10.0, 3.0, "B1.1"), (10.0, 9.0, "B2.1")),
             conn(2, "A", (17.0, 10.0, "A4.1"), (1.0, 6.0, "A1.1"))]
    ctx, det = make(pads, conns, size=(20.0, 12.0))
    st = ctx.state
    for c in conns[:2]:
        r = det.route_conn(ctx, c, Budget(1_000_000))
        st.set_status(c.idx, "routed" if r["ok"] else "failed", r.get("fail"))
    for x1, y1, x2, y2 in ((17.0, 10.0, 19.0, 10.0), (19.0, 10.0, 19.0, 6.0)):
        st.add(net="A", conn=2, kind="seg", layer=0, x1=x1, y1=y1, x2=x2, y2=y2, w=W, stage="legacy")
    st.set_status(2, "routed")
    assert [st.status(i) for i in (0, 1, 2)] == ["routed", "failed", "routed"] and st.connected(2)
    all_legal(ctx)
    before = st.pieces(conn=2)
    n = len(det.calls)
    assert R.LAZY_CASCADE
    ctx.x = types.SimpleNamespace(v1_trees=True)          # what the engine's second opinion sets
    rep = R.run(ctx, Budget(3_000_000))
    assert [st.status(i) for i in (0, 1, 2)] == ["routed", "routed", "routed"], rep
    assert all(st.connected(i) for i in (0, 1, 2))
    assert st.pieces(conn=2) == before                    # not ripped, not re-routed (ids included)
    assert all(c[0] != 2 for c in det.calls[n:])
    assert rep.data["stats"]["accepted"] == 1
    all_legal(ctx)
    assert not st._marks


def test_rescue_half_pitch():
    # the only gap is centred between two lattice lines: passable at half the window pitch only
    pads = [wall("W1", 0.2, 5.6, 9.95, 6.4), wall("W2", 10.55, 5.6, 19.8, 6.4),
            pad("B1", "B", 10.0, 3.0), pad("B2", "B", 10.0, 9.0)]
    conns = [conn(0, "B", (10.0, 3.0, "B1.1"), (10.0, 9.0, "B2.1"))]
    ctx, det = make(pads, conns, size=(20.0, 12.0))
    route_all(ctx, det)
    assert ctx.state.status(0) == "failed"
    rep = R.run(ctx, Budget(3_000_000))
    assert ctx.state.status(0) == "routed" and ctx.state.connected(0), rep
    all_legal(ctx)
    assert rep.data["tried"][0] == steps("probe", "rescue", "" if R.RESCUE_FIRST else "mincut")
    assert rep.data["stats"]["rescued"] == 1
    assert det.calls[-1] == (0, 2, 0.5, 0, False)
    assert any(abs(p.x1 - 10.25) < 1e-9 for p in ctx.state.pieces(conn=0))


def test_rescue_neck_width_keeps_the_target_width():
    # a 0.5 mm gap takes a 0.1 mm neck but not the 0.2 mm track (0.25 lattice so the lane is on a line)
    pads = [wall("W1", 0.2, 5.6, 9.75, 6.4), wall("W2", 10.25, 5.6, 19.8, 6.4),
            pad("B1", "B", 10.0, 3.0), pad("B2", "B", 10.0, 9.0)]
    conns = [conn(0, "B", (10.0, 3.0, "B1.1"), (10.0, 9.0, "B2.1"), neck=0.1)]
    ctx, det = make(pads, conns, size=(20.0, 12.0))
    route_all(ctx, det)
    assert ctx.state.status(0) == "failed"
    assert R.rescue(ctx, ctx.conns[0], Budget(3_000_000)) is True
    ps = ctx.state.pieces(conn=0)
    assert ps and all(p.w == 0.1 and p.target_w == W for p in ps)        # necked, polish knows the wanted width
    assert ctx.state.status(0) == "routed" and ctx.state.connected(0) and ctx.conns[0].width == W
    all_legal(ctx)
    assert ctx.state.score().necked_mm > 0


class Fanout:
    """fanout.re_escape double: moves the escape of end 'a' to the scripted point (a protected stub from the pad)."""

    def __init__(self, to):
        self.to = to
        self.calls = []

    def re_escape(self, ctx, c, end, avoid_side):
        self.calls.append((c.idx, end, avoid_side))
        if self.to is None:
            return False
        for p in ctx.state.pieces(conn=c.idx):
            if p.protected:
                ctx.state.remove(p.id)
        escape_stub(ctx, c, self.to)
        c.a = Terminal(x=self.to[0], y=self.to[1], layers=(1,), pad="B1.1", escape=True)
        return True


def escape_stub(ctx, c, to):
    """Via escape of pad B1 (top layer, at 10, 3): a stub on F.Cu and a via; the connection goes on on B.Cu."""
    return [ctx.state.add(net=c.net, conn=c.idx, kind="seg", layer=0, x1=10.0, y1=3.0, x2=to[0], y2=to[1], w=W,
                          stage="fanout", protected=True),
            ctx.state.add(net=c.net, conn=c.idx, kind="via", layer=-1, x1=to[0], y1=to[1], x2=to[0], y2=to[1], w=0.6,
                          drill=0.3, stage="fanout", protected=True)]


def escape_scenario(to):
    """B1 (top side) escapes east to a via; on the bottom layer that via sits in a closed ring of fixed copper.
    West of the pad the bottom layer is open."""
    pads = [wall("RN", 10.9, 1.4, 13.1, 2.2, 1), wall("RS", 10.9, 3.8, 13.1, 4.6, 1),
            wall("RW", 10.9, 2.2, 11.2, 3.8, 1), wall("RE", 12.8, 2.2, 13.1, 3.8, 1),
            pad("B1", "B", 10.0, 3.0), pad("B2", "B", 10.0, 9.0, layer=1)]
    a = Terminal(x=12.0, y=3.0, layers=(1,), pad="B1.1", escape=True)
    conns = [conn(0, "B", a, Terminal(x=10.0, y=9.0, layers=(1,), pad="B2.1"))]
    fan = Fanout(to)
    ctx, det = make(pads, conns, size=(20.0, 12.0), fanout=fan)
    stub = escape_stub(ctx, conns[0], (12.0, 3.0))
    all_legal(ctx)
    route_all(ctx, det)
    assert ctx.state.status(0) == "failed"                # a dead end on the bottom layer
    return ctx, det, fan, stub, a


def test_rescue_moves_only_its_own_escape():
    ctx, det, fan, stub, a = escape_scenario((8.0, 3.0))
    rep = R.run(ctx, Budget(3_000_000))
    st = ctx.state
    assert st.status(0) == "routed", rep
    assert fan.calls == [(0, "a", "e")]                   # the side the old stub left on is the one to avoid
    assert ctx.conns[0].a == Terminal(x=8.0, y=3.0, layers=(1,), pad="B1.1", escape=True)
    assert not any(ctx.oracle.has_piece(p.id) for p in stub) and rep.data["tried"][0][-1] == "rescue"
    assert sum(1 for p in st.pieces(conn=0) if p.protected) == 2
    all_legal(ctx)
    # the new escape does not help either (same dead end): stub and terminal are put back
    ctx, det, fan, stub, a = escape_scenario((12.0, 3.0))
    R.run(ctx, Budget(3_000_000))
    assert ctx.state.status(0) == "failed" and ctx.conns[0].a is a and ctx.state.pieces(conn=0) == stub
    assert fan.calls == [(0, "a", "e")] and not ctx.state._marks


class BookFanout:
    """fanout double in the shape of the real module: escape stubs are protected ``conn = -1`` pieces, the book
    (``registry(ctx)['by_pad']``) says which connections use them, ``sync`` re-aligns book and terminals after a
    rollback, ``re_escape`` refuses while a connection is still routed on the stub."""

    def __init__(self, alt):
        self.alt = alt                      # polyline of the other escape (from the pad), or None
        self.book = {"by_pad": {}, "stack": {}}
        self.calls = []
        self.synced = 0

    def registry(self, ctx):
        return self.book

    def place(self, ctx, c, end, key, pts):
        ids = []
        for a, b in zip(pts, pts[1:]):
            ids.append(ctx.state.add(net=c.net, conn=-1, kind="seg", layer=0, x1=a[0], y1=a[1], x2=b[0], y2=b[1], w=W,
                                     stage="fanout", protected=True).id)
        old = self.book["by_pad"].get(key)
        orig = dict(old["orig"]) if old else {(c.idx, end): getattr(c, end)}
        term = Terminal(x=pts[-1][0], y=pts[-1][1], layers=(0,), pad=key[0], escape=True)
        if old:
            self.book["stack"].setdefault(key, []).append(old)
        self.book["by_pad"][key] = {"pieces": ids, "orig": orig, "terminal": term, "net": c.net}
        setattr(c, end, term)

    def re_escape(self, ctx, c, end, avoid_side):
        self.calls.append((c.idx, end, avoid_side, ctx.state.status(c.idx)))
        key = next(k for k in sorted(self.book["by_pad"]) if (c.idx, end) in self.book["by_pad"][k]["orig"])
        if self.alt is None or ctx.state.status(c.idx) == "routed":
            return False
        for pid in self.book["by_pad"][key]["pieces"]:
            ctx.state.remove(pid)
        self.place(ctx, c, end, key, self.alt)
        return True

    def sync(self, ctx):
        self.synced += 1
        alive = {p.id for p in ctx.state.pieces()}
        by = {c.idx: c for c in ctx.conns}
        for key in sorted(self.book["by_pad"]):
            while not all(i in alive for i in self.book["by_pad"][key]["pieces"]):
                self.book["by_pad"][key] = self.book["stack"][key].pop()
            for (ci, end) in self.book["by_pad"][key]["orig"]:
                setattr(by[ci], end, self.book["by_pad"][key]["terminal"])


def stub_scenario(alt):
    """Pad A1 plugs a hole in a fence between two rooms. Its escape stub runs through the east room and the only
    gap B can use; the pad could just as well escape through the west room and its own gap."""
    pads = [wall("W1", 0.2, 5.6, 3.65, 6.4), wall("W2", 4.35, 5.6, 10.65, 6.4), wall("W3", 11.35, 5.6, 19.8, 6.4),
            wall("F1", 8.7, 0.2, 9.3, 2.4), wall("F2", 8.7, 3.6, 9.3, 5.6),
            pad("A1", "A", 9.0, 3.0), pad("A2", "A", 7.0, 9.0), pad("B1", "B", 13.0, 3.0), pad("B2", "B", 13.0, 9.0)]
    conns = [conn(0, "A", (9.0, 3.0, "A1.1"), (7.0, 9.0, "A2.1")), conn(1, "B", (13.0, 3.0, "B1.1"), (13.0, 9.0, "B2.1"))]
    fan = BookFanout(alt)
    ctx, det = make(pads, conns, size=(20.0, 12.0), fanout=fan)
    fan.place(ctx, conns[0], "a", ("A1.1", 9.0, 3.0), [(9.0, 3.0), (11.0, 3.0), (11.0, 7.5)])
    all_legal(ctx)
    route_all(ctx, det)
    assert [ctx.state.status(i) for i in (0, 1)] == ["routed", "failed"]
    return ctx, det, fan


def test_crossed_escape_stub_moves_with_its_connections():
    west = [(9.0, 3.0), (4.0, 3.0), (4.0, 7.5)]
    ctx, det, fan = stub_scenario(west)
    st = ctx.state
    old = list(fan.book["by_pad"][("A1.1", 9.0, 3.0)]["pieces"])
    rep = R.run(ctx, Budget(4_000_000))
    assert [st.status(i) for i in (0, 1)] == ["routed", "routed"], (rep, det.calls)
    # A (the stub's only user) was ripped before its pad escaped the other way; the side to avoid is the old one
    assert fan.calls == [(0, "a", "s", "todo")]
    assert not any(ctx.oracle.has_piece(i) for i in old)
    assert ctx.conns[0].a == Terminal(x=4.0, y=7.5, layers=(0,), pad="A1.1", escape=True)
    assert R.memory(ctx).ripped == {0: 1} and (1, frozenset({-1})) in R.memory(ctx).tabu
    assert sorted(p.id for p in st.pieces() if p.protected) == fan.book["by_pad"][("A1.1", 9.0, 3.0)]["pieces"]
    all_legal(ctx)
    # no other escape: nothing moves
    ctx, det, fan = stub_scenario(None)
    st = ctx.state
    geo, term = st.geometry(), ctx.conns[0].a
    rep = R.run(ctx, Budget(4_000_000))
    assert [st.status(i) for i in (0, 1)] == ["routed", "failed"] and st.geometry() == geo and ctx.conns[0].a is term
    assert fan.synced >= 1 and st.connected(0) and not st._marks
    # the pad can escape, but then its connection has nowhere to go (target walled off): rolled back, book re-synced
    ctx, det, fan = stub_scenario([(9.0, 3.0), (8.0, 3.0)])
    R.DEPS["detail"] = det2 = Detail()
    det2.route_conn_real = det2.route_conn

    def no_a(ctx_, c, budget, **kw):                      # A cannot be routed from its new escape
        if c.idx == 0 and not kw.get("soft_cost") and c.a.x == 8.0:
            return {"ok": False, "pieces": [], "crossed": {}, "fail": {"reason": "blocked", "where": [8.0, 3.0]}}
        return det2.route_conn_real(ctx_, c, budget, **kw)
    det2.route_conn = no_a
    st = ctx.state
    geo, term = st.geometry(), ctx.conns[0].a
    R.run(ctx, Budget(4_000_000))
    assert [st.status(i) for i in (0, 1)] == ["routed", "failed"] and st.geometry() == geo
    assert ctx.conns[0].a == term and fan.book["by_pad"][("A1.1", 9.0, 3.0)]["terminal"] == term
    assert st.connected(0) and not st._marks


class Tiles:
    def __init__(self, t=2.0, nx=10, ny=6, nl=2):
        self.t, self.nx, self.ny, self.nl = t, nx, ny, nl
        self.booked = {}

    def tile_of(self, x, y):
        return int(x // self.t), int(y // self.t)

    def book(self, layer, tx, ty, side, mm):
        k = (layer, tx, ty, side)
        self.booked[k] = round(self.booked.get(k, 0.0) + mm, 6)

    def cut_for_edge(self, layer, tx, ty, side, demand):
        return Cut(a=f"tile({tx},{ty})|{side}", b="", layer=str(layer), capacity_mm=0.0,
                   demand_mm=sum(d for _n, d in demand), nets=[n for n, _d in demand])

    def pad_cut(self, a, b, layer, w, cl):
        raise NotImplementedError


class Global:
    def __init__(self, new_tiles):
        self.new_tiles = new_tiles
        self.calls = []

    def run(self, ctx, budget, only=None):
        self.calls.append(sorted(only))
        for i in only:
            ctx.corridors[i] = Corridor(conn=i, tiles=frozenset(self.new_tiles), layers=(0,))


def feedback_scenario(new_tiles):
    """B's corridor runs straight through a wall without a gap; the gap is two tiles to the east."""
    pads = [wall("W1", 0.2, 5.6, 13.65, 6.4), wall("W2", 14.35, 5.6, 19.8, 6.4),
            pad("B1", "B", 5.0, 3.0), pad("B2", "B", 5.0, 9.0)]
    conns = [conn(0, "B", (5.0, 3.0, "B1.1"), (5.0, 9.0, "B2.1"))]
    g = Global(new_tiles)
    ctx, det = make(pads, conns, size=(20.0, 12.0), detail=Detail(tile=2.0), globalroute=g)
    ctx.tiles = Tiles()
    ctx.corridors[0] = Corridor(conn=0, tiles=frozenset((x, y) for x in (1, 2, 3) for y in range(6)), layers=(0,))
    route_all(ctx, det)
    assert ctx.state.status(0) == "failed"
    return ctx, det, g


def test_global_feedback_books_the_border_and_reroutes():
    east = frozenset((x, y) for x in range(1, 8) for y in range(6))
    ctx, det, g = feedback_scenario(east)
    rep = R.run(ctx, Budget(4_000_000))
    st = ctx.state
    assert st.status(0) == "routed" and st.connected(0), rep
    assert g.calls == [[0]] and rep.data["feedback_loops"] == 1 and rep.data["stats"]["feedback_gain"] == 1
    assert ctx.corridors[0].tiles == east
    # the failure was at the wall (tile row 2): its borders inside the old corridor were booked with w + cl
    assert ctx.tiles.booked and all(abs(v - 0.4) < 1e-9 for v in ctx.tiles.booked.values()), ctx.tiles.booked
    assert rep.data["tried"][0] == steps("probe", "mincut", "rescue", "feedback")
    all_legal(ctx)


def test_global_feedback_is_rolled_back_and_bounded():
    same = frozenset((x, y) for x in (1, 2, 3) for y in range(6))
    ctx, det, g = feedback_scenario(same)
    old = ctx.corridors[0]
    rep = R.run(ctx, Budget(4_000_000))
    st = ctx.state
    assert st.status(0) == "failed" and st.pieces() == [] and ctx.corridors[0] is old
    assert all(v == 0.0 for v in ctx.tiles.booked.values())                 # the booking was taken back
    assert rep.data["feedback_loops"] == 1 and len(g.calls) == 1
    R.run(ctx, Budget(4_000_000))
    R.run(ctx, Budget(4_000_000))
    R.run(ctx, Budget(4_000_000))
    assert R.memory(ctx).feedback_loops == R.FEEDBACK_LOOPS and len(g.calls) == R.FEEDBACK_LOOPS   # 2 per board
    cert = st.fails[0]
    assert cert["cut"] is not None and "feedback" in cert["tried"] and not st._marks


def boxed_scenario():
    """Pad P1 sits in a ring of four blocks of fixed copper with 0.15 mm slots: no exit."""
    pads = [wall("RN", 9.15, 4.2, 10.85, 5.0), wall("RS", 9.15, 7.0, 10.85, 7.8),
            wall("RW", 8.2, 4.2, 9.0, 7.8), wall("RE", 11.0, 4.2, 11.8, 7.8),
            pad("P1", "B", 10.0, 6.0), pad("P2", "B", 15.0, 6.0)]
    conns = [conn(0, "B", (10.0, 6.0, "P1.1"), (15.0, 6.0, "P2.1"))]
    return make(pads, conns, size=(20.0, 12.0))


def test_probe_reports_a_boxed_in_pad():
    ctx, det = boxed_scenario()
    b = Budget(100_000)
    pr = R.probe(ctx, ctx.conns[0], b)
    assert pr["enclosed"] == "a" and pr["ends"]["a"]["open"] is False and pr["ends"]["b"]["open"] is True
    assert pr["static_share"] == 1.0 and pr["ends"]["a"]["blocked"] == 16
    assert {x["name"] for x in pr["blockers"]} == {"RN.1", "RS.1", "RW.1", "RE.1"}
    assert all(x["static"] and x["rule"] in ("clearance", "short") for x in pr["blockers"])
    assert 0 < b.used <= 20                               # oracle checks are charged, 1 unit per 8
    ctx2, _ = wall_scenario()
    assert R.probe(ctx2, ctx2.conns[1])["enclosed"] == "none"


def test_certificate_is_complete_and_names_the_cut():
    ctx, det = boxed_scenario()
    route_all(ctx, det)
    rep = R.run(ctx, Budget(2_000_000))
    assert rep.routed == 0 and rep.failed == 1 and rep.data["failed"] == [0]
    cert = ctx.state.fails[0]
    assert cert is rep.data["certificates"][0]
    for k in ("reason", "where", "cut", "blocked_by", "blockers", "static_share", "mincut_nets", "probe", "tried", "suggest"):
        assert k in cert, k
    assert "boxed in" in cert["reason"] and "P1.1" in cert["reason"]
    assert cert["static_share"] == 1.0 and cert["mincut_nets"] == [] and cert["probe"]["enclosed"] == "a"
    assert set(cert["blocked_by"]) == {"RN.1", "RS.1", "RW.1", "RE.1"}
    assert cert["ladder"] == steps("probe", "mincut", "rescue")
    assert cert["tried"] == ["pattern", "window", "wide"] + cert["ladder"]               # the detail rungs, then ours
    cut = cert["cut"]
    assert {cut["a"], cut["b"]} <= {"RN.1", "RS.1", "RW.1", "RE.1"} and cut["a"] != cut["b"] and cut["layer"] == "F.Cu"
    assert cut["capacity_mm"] == 0.0 and abs(cut["demand_mm"] - 0.4) < 1e-9 and cut["nets"] == ["B"]
    # the suggestion is a placement move that opens exactly that slot
    mv = [s for s in cert["suggest"] if "move" in s["patch"]]
    assert mv and mv[0]["patch"]["move"]["ref"] in ("RN", "RS", "RW", "RE")
    assert math.hypot(*mv[0]["patch"]["move"]["by"]) >= 0.4
    assert isinstance(cert["where"], list) and len(cert["where"]) == 2
    # keys of the detail stage's own record survive
    assert cert["tried"] and "blocked_by" in cert


def test_budget_is_respected_and_nothing_is_left_half_done():
    ctx, det = wall_scenario()
    route_all(ctx, det)
    st = ctx.state
    geo = st.geometry()
    for total in (0, 3000, 4500, 9000, 14000, 20000):
        ctx.history.clear()
        b = Budget(total)
        rep = R.run(ctx, b)
        if st.status(1) != "routed":
            assert st.geometry() == geo and [st.status(i) for i in (0, 1, 2)] == ["routed", "failed", "routed"], total
            assert "reason" in st.fails[1]
        assert not st._marks and rep.work == b.used
        assert b.used <= total + 3 * (CHARGE_SEARCH + 300), (total, b.used)   # overshoot: the searches in flight
        if st.status(1) == "routed":
            break
    ctx, det = wall_scenario()
    route_all(ctx, det)
    rep = R.run(ctx, Budget(1000))
    assert "time budget" in ctx.state.fails[1]["reason"] and rep.notes and "not attempted" in rep.notes[0]
    assert any("time_budget_s" in str(s["patch"]) for s in ctx.state.fails[1]["suggest"]) or ctx.cfg is None


def test_todo_connections_get_a_plain_route_first():
    ctx, det = wall_scenario()
    ctx.state.set_status(0, "routed")                     # pretend A was routed elsewhere; B and C never tried
    for p in ():
        pass
    rep = R.run(ctx, Budget(2_000_000), only=[2])
    assert ctx.state.status(2) == "routed" and ctx.state.status(1) == "todo"     # 'only' is honoured
    assert rep.data["tried"] == {2: ["route"]} and rep.routed == 1


def test_failures_no_search_can_repair_cost_nothing():
    ctx, det = wall_scenario()
    route_all(ctx, det)
    why = "pad B2.1 is not on any layer this net may use (0,) — widen nets[...].layers / global.layers"
    ctx.state.set_status(1, "failed", {"reason": why, "code": "layer_pad", "where": [10.0, 9.0], "tried": []})
    calls = len(det.calls)
    b = Budget(2_000_000)
    rep = R.run(ctx, b)
    cert = ctx.state.fails[1]
    assert len(det.calls) == calls and b.used < 200       # not one search, no feedback loop
    assert cert["reason"] == why and cert["cut"] is None and cert["ladder"] == ["probe"] and cert["suggest"] == []
    assert rep.failed == 1 and rep.data["stats"]["rips"] == 0
    # an open target (pad -> pour: the far terminal has no layers) is not reported as enclosed
    c = conn(9, "B", (10.0, 3.0, "B1.1"), Terminal(x=10.0, y=9.0, layers=()))
    pr = R.probe(ctx, c)
    assert pr["enclosed"] == "none" and pr["ends"]["b"]["open"] is True


def test_same_twice():
    def once(scn):
        ctx, det = scn()
        route_all(ctx, det)
        rep = R.run(ctx, Budget(4_000_000))
        return ([(p.id, p.net, p.conn, p.x1, p.y1, p.x2, p.y2, p.w) for p in ctx.state.pieces()],
                {i: ctx.state.status(i) for i in range(len(ctx.conns))}, rep.work, rep.data["stats"],
                {i: (c["reason"], c["cut"], c["blocked_by"], c["suggest"]) for i, c in rep.data["certificates"].items()},
                det.calls)
    for scn in (wall_scenario, gap_scenario, puzzle_scenario, boxed_scenario):
        assert once(scn) == once(scn), scn.__name__


def test_nothing_to_do_and_no_detail_stage():
    ctx, det = wall_scenario()
    for i in (0, 1, 2):
        ctx.state.set_status(i, "routed")
    rep = R.run(ctx, Budget(1000))
    assert rep.routed == 0 and rep.failed == 0 and rep.work == 0 and det.calls == []
    ctx, det = wall_scenario()
    route_all(ctx, det)
    R.DEPS["detail"] = None
    rep = R.run(ctx, Budget(100_000))
    assert rep.failed == 1 and "detail stage unavailable" in rep.notes[0]
    assert ctx.state.fails[1]["cut"] is None or isinstance(ctx.state.fails[1]["cut"], dict)
    assert R.mincut_rip(ctx, ctx.conns[1], Budget(1000), set()) is False
    assert R.rescue(ctx, ctx.conns[1], Budget(1000)) is False


def test_real_siblings():
    """One scenario on the real pieces.RouteState / detail.route_conn once they have landed (skipped while stubs)."""
    R.DEPS.clear()
    pieces = importlib.import_module("copper_kicad.pcb.v2.pieces")
    detail = importlib.import_module("copper_kicad.pcb.v2.detail")
    if getattr(pieces, "STUB", False) or getattr(detail, "STUB", False):
        print("    (skipped: pieces/detail are still stubs)")
        return "skipped"
    pads = [pad("A1", "A", 1.0, 6.0, hw=0.5), pad("A2", "A", 19.0, 6.0, hw=0.5),
            pad("B1", "B", 10.0, 3.0), pad("B2", "B", 10.0, 9.0)]
    b = board(pads, size=(20.0, 12.0), layers=("F.Cu",))
    conns = [conn(0, "A", (1.0, 6.0, "A1.1"), (19.0, 6.0, "A2.1")), conn(1, "B", (10.0, 3.0, "B1.1"), (10.0, 9.0, "B2.1"))]
    o = O.Oracle(b)
    st = pieces.RouteState(o, conns)
    from copper_kicad.pcb.guided import plan as gplan
    ctx = Ctx(board=b, cfg=gplan.Config(), x=None, oracle=o, state=st, conns=conns)
    budget = Budget(5_000_000)
    for c in conns:
        r = detail.route_conn(ctx, c, budget)
        st.set_status(c.idx, "routed" if r["ok"] else "failed", r.get("fail"))
    rep = R.run(ctx, budget)
    assert rep.failed == 0 or st.status(1) == "failed"
    for p in st.pieces():
        assert o.piece_ok(p) is None, p
    print(f"    real siblings: statuses {[st.status(i) for i in (0, 1)]}, tried {rep.data.get('tried')}")
    return None


def perf():
    """Throughput of the ladder's own code (the doubles' search time is reported separately)."""
    rows = []
    for name, scn in (("wall", wall_scenario), ("gap", gap_scenario), ("puzzle", puzzle_scenario), ("boxed", boxed_scenario)):
        ctx, det = scn()
        route_all(ctx, det)
        calls0, exp0, chk0 = len(det.calls), det.expanded, ctx.oracle.checks
        orig = det.route_conn
        spent = [0.0]

        def timed(*a, _o=orig, **k):
            t = time.perf_counter()
            try:
                return _o(*a, **k)
            finally:
                spent[0] += time.perf_counter() - t
        det.route_conn = timed
        b = Budget(10_000_000)
        t0 = time.perf_counter()
        rep = R.run(ctx, b)
        dt = time.perf_counter() - t0
        rows.append((name, rep.routed, rep.failed, len(det.calls) - calls0, det.expanded - exp0, b.used,
                     ctx.oracle.checks - chk0, dt, dt - spent[0]))
    print(f"  {'scenario':8} {'fixed':>5} {'failed':>6} {'route_conn':>10} {'expansions':>10} {'work':>9} {'checks':>7} "
          f"{'wall s':>7} {'repair-only ms':>14}")
    for r in rows:
        print(f"  {r[0]:8} {r[1]:5d} {r[2]:6d} {r[3]:10d} {r[4]:10d} {r[5]:9d} {r[6]:7d} {r[7]:7.3f} {r[8] * 1000:14.2f}")
    # certificate cost alone
    ctx, det = gap_scenario()
    route_all(ctx, det)
    R.run(ctx, Budget(10_000_000))
    c0 = ctx.oracle.checks
    t0 = time.perf_counter()
    n = 20
    for _ in range(n):
        R.certificate(ctx, ctx.conns[1])
    dt = (time.perf_counter() - t0) / n
    print(f"  certificate (gap scenario): {dt * 1000:.2f} ms, {(ctx.oracle.checks - c0) // n} oracle checks each")
    t0 = time.perf_counter()
    for _ in range(200):
        R.probe(ctx, ctx.conns[1])
    print(f"  probe: {(time.perf_counter() - t0) / 200 * 1e6:.0f} us each")
    # soft table on a big state
    ctx, det = wall_scenario()
    for i in range(3000):
        ctx.state._p[10_000 + i] = Piece(id=10_000 + i, net="C", conn=2, kind="seg", layer=0, x1=0.0, y1=0.0, x2=1.0,
                                         y2=0.0, w=W)
    t0 = time.perf_counter()
    for _ in range(20):
        R._soft_table(ctx, ctx.conns[1], True)
    print(f"  soft table over 3000 pieces: {(time.perf_counter() - t0) / 20 * 1000:.2f} ms")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--design-order" in sys.argv:                      # the ladder order of design §4.9: rescue after the rips
        R.RESCUE_FIRST = False
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f) and (not args or n in args)]
    failed = 0
    for n, f in tests:
        try:
            r = f()
            print(f"ok    {n}" + ("  (skipped)" if r == "skipped" else ""))
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {n}")
            traceback.print_exc()
        finally:
            R.DEPS.clear()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    if "--perf" in sys.argv:
        perf()
    sys.exit(1 if failed else 0)
