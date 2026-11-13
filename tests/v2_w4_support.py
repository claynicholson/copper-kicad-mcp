"""Shared helpers of the W4 tests (window / search / detail): synthetic boards, a context builder and a small
RouteState stand-in used while ``v2/pieces.py`` is still the W0 stub (the real RouteState is used once it lands)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_v2_oracle as T0  # noqa: E402  (W0's synthetic board builder; also sandboxes the environment)
from copper_kicad.pcb.guided import geom  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2 import pieces as P  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Ctx, Piece, Score, Terminal  # noqa: E402

board = T0.board
keepout = T0.keepout
TOUCH = 2e-6


class Cfg:
    """The few guided.plan.Config fields detail reads."""
    def __init__(self, **kw):
        self.via_cost, self.bend_cost, self.style, self.direction_bias = 5.0, 0.3, "45", 0.5
        self.effort, self.hweight, self.regions, self.nets = 1.0, 1.15, [], {}
        self.__dict__.update(kw)


class FakeState:
    """RouteState stand-in with the §9 interface: transactional pieces, geometric ``connected``."""
    new_errors = 0

    def __init__(self, oracle, conns):
        self.o, self.conns = oracle, {c.idx: c for c in conns}
        self._p, self._seq, self._log, self._st, self._fail = {}, 0, [], {}, {}

    def begin(self):
        return len(self._log)

    def commit(self, tok):
        pass

    def rollback(self, tok):
        while len(self._log) > tok:
            op, p = self._log.pop()
            if op == "add":
                del self._p[p.id]
                self.o.remove_piece(p)
            else:
                self._p[p.id] = p
                self.o.add_piece(p)

    def add(self, **f):
        self._seq += 1
        p = Piece(id=self._seq, **f)
        self._p[p.id] = p
        self.o.add_piece(p)
        self._log.append(("add", p))
        return p

    def remove(self, pid):
        p = self._p.pop(pid)
        self.o.remove_piece(p)
        self._log.append(("del", p))

    def rip_conn(self, conn):
        out = [p for p in self._p.values() if p.conn == conn]
        for p in out:
            self.remove(p.id)
        self._st[conn] = "todo"
        return out

    def pieces(self, conn=None, net=None):
        return [p for p in sorted(self._p.values(), key=lambda p: p.id)
                if (conn is None or p.conn == conn) and (net is None or p.net == net)]

    def set_status(self, conn, status, fail=None):
        self._st[conn] = status
        self._fail[conn] = fail

    def status(self, conn):
        return self._st.get(conn, "todo")

    def fail(self, conn):
        return self._fail.get(conn)

    def piece(self, pid):
        return self._p.get(pid)

    def connected(self, conn):
        c = self.conns[conn]
        ps = self.pieces(net=c.net)

        def at(p, x, y, layers):
            if p.kind == "via":
                return abs(p.x1 - x) <= TOUCH and abs(p.y1 - y) <= TOUCH
            return (layers is None or p.layer in layers) and geom.pt_seg_dist(x, y, p.x1, p.y1, p.x2, p.y2) <= TOUCH

        def touch(a, b):
            if a.kind == "via":
                return at(b, a.x1, a.y1, None)
            if b.kind == "via":
                return at(a, b.x1, b.y1, None)
            return a.layer == b.layer and (at(b, a.x1, a.y1, None) or at(b, a.x2, a.y2, None)
                                           or at(a, b.x1, b.y1, None) or at(a, b.x2, b.y2, None))
        if abs(c.a.x - c.b.x) <= TOUCH and abs(c.a.y - c.b.y) <= TOUCH and set(c.a.layers) & set(c.b.layers):
            return True
        seen = [p for p in ps if at(p, c.a.x, c.a.y, c.a.layers)]
        todo = list(seen)
        while todo:
            p = todo.pop()
            if at(p, c.b.x, c.b.y, c.b.layers):
                return True
            for o in ps:
                if o not in seen and touch(p, o):
                    seen.append(o)
                    todo.append(o)
        return False

    def snapshot(self):
        return (tuple(self.pieces()), dict(self._st))

    def score(self):
        ps = self.pieces()
        return Score(new_errors=self.new_errors, opens=sum(1 for i in self.conns if self.status(i) != "routed"),
                     necked_mm=round(sum(p.length for p in ps if p.kind == "seg" and p.w < p.target_w - 1e-9), 6),
                     vias=sum(1 for p in ps if p.kind == "via"), length_mm=round(sum(p.length for p in ps), 6))


def make_state(oracle, conns):
    if getattr(P, "STUB", False):
        return FakeState(oracle, conns)
    return P.RouteState(oracle, conns)


def conn(idx, net, a, b, *, width=0.2, neck=0.0, clearance=0.2, via=(0.6, 0.3), tree=True, la=(0,), lb=(0,),
         pa=None, pb=None, ca=0, cb=0, rank=None):
    ta = Terminal(a[0], a[1], tuple(la), pad=pa, cluster=ca)
    tb = Terminal(b[0], b[1], tuple(lb), pad=pb, cluster=cb)
    dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
    return ConnV2(idx=idx, net=net, a=ta, b=tb, est=max(dx, dy) + 0.41421356 * min(dx, dy), width=width,
                  neck=neck or width, clearance=clearance, via=via, rank=rank or (0, 0, idx), tree_target=tree)


def ctx_for(b, conns, **cfg):
    o = O.Oracle(b)
    c = Ctx(board=b, cfg=Cfg(**cfg), oracle=o, conns=list(conns))
    c.state = make_state(o, c.conns)
    return c


def all_legal(ctx, pieces=None):
    """Every piece in the state passes the oracle (its own connection's copper ignored only for itself)."""
    bad = []
    for p in (pieces if pieces is not None else ctx.state.pieces()):
        b = ctx.oracle.piece_ok(p)
        if b is not None:
            bad.append((p, b))
    return bad


def run_tests(ns):
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    tests = [(k, v) for k, v in ns.items() if k.startswith("test_") and callable(v) and (not names or k in names)]
    failed = 0
    for k, v in tests:
        try:
            v()
            print(f"ok    {k}")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {k}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0
