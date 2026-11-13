"""Stage-1 detail engine: v1's search (``guided.router.Router``) run on a v2 ``RouteState`` (design §4.9 note, §9).

What is v1 here: the grid, the A* search, the rip-up negotiation, the order of work, the budget in work
units. What is v2:

* **Copper lives in the RouteState.** Every committed connection is added as ``Piece``s (``stage='legacy'``,
  ``target_w`` = the full width), every rip removes them, so the oracle always sees the run's copper.
* **The oracle is the only exact judge.** v1's ``Board.seg_clear`` (rule + 2 um, no mask / override / pair
  rules) is replaced by ``Oracle.seg_legal`` for escape stubs and end snaps, and every path is checked
  segment by segment and via by via *when it is found* (not at the end of the pass): a rejected step blocks
  its cells in this run's occupancy map and the search runs again (``EMIT_RETRIES`` times), then the
  connection fails alone. This is the measured ``one_air_max`` failure (design Appendix A1): an on-grid 45
  degree step past a pad corner dips up to ``raster_guard(pitch, reach)`` into the clearance, more than
  v1's fixed grid EPS on a coarse grid.
* **Rips act on connections, never on nets.** The negotiation rips the connections whose copper is crossed
  (v1: their whole nets), the final self-check rips the one connection that fails (v1: its whole net —
  75 connections lost for two segments on ``one_air_max``) and re-queues it once. A rip also takes the
  connections that teed onto the ripped copper and are no longer ``connected()`` — nothing is left dangling.
* **Pieces already in the state are fixed copper**: obstacles for other nets (stamped as static into the
  occupancy maps), targets for their own net (merged into the net's components).

v1's files are not modified: ``LegacyRouter`` subclasses ``Router`` and hands it a ``Board`` view whose
``seg_clear`` asks the oracle.
"""
from __future__ import annotations

import math
from array import array

from ..guided import geom, model
from ..guided import router as v1
from ..guided.geom import Shape
from ..guided.grid import EPS, ClassMap
from .oracle import NO_NEED, raster_guard
from .pieces import is_open
from .types import GUARD_MM, ORACLE_CHECKS_PER_UNIT, Budget, ConnV2, Ctx, StageReport, Terminal, q

RIP_LEVEL = "conn"        # 'conn' (v2) | 'net' (v1's negotiation: rip every connection of a crossed net)
FAIR_FACTOR = 1.0         # fallback mode: first attempt of a connection <= this x (work left / connections not tried)
FAIR_MIN = 12000.0        # ... but never less than this many work units
FAIR_FLOOR = 0.05         # ... and never below this share of v1's own expansion limit per search
OCTI = True               # short odd-angle segments are emitted as two 45-degree-direction segments when legal
OCTI_MAX = 0.6            # mm: ... up to this length
EMIT_CHECK = True         # oracle check of every path when it is found (False: only the final self-check)
EMIT_RETRIES = 3          # re-searches after the oracle rejected a path (design §4.8)
SELF_CHECK = "internal clearance self-check failed — connection dropped rather than written"
TINY = 1e-5               # mm: v1 drops segments shorter than this when it writes
RESTAMP_REACH = 1.0       # mm: how far round a rejected piece the violated shapes are looked for


class _View(model.Board):
    """The Board the v1 router sees: the same data, but exact clearance questions go to the oracle."""

    def __init__(self, board, router):                      # noqa: super().__init__ is not called on purpose
        self.__dict__.update(board.__dict__)
        self._router = router

    def seg_clear(self, lay, x1, y1, x2, y2, hw, net_id, clr, extra=None):
        r = self._router
        net = self.net_by_id.get(net_id)
        # extra=None is v1's "static copper only" (planning a rip-up): this run's copper is not looked at
        return r.oracle.seg_legal(lay, x1, y1, x2, y2, 2 * hw, net.name if net else "", clr,
                                  r.no_ignore if extra is not None else r.live)


class LConn(v1.Conn):
    __slots__ = ("v2", "pids", "exact")

    def __init__(self, idx, conn: ConnV2, a, b, pa, pb):
        super().__init__(idx, conn.net, a, b, pa, pb, conn.est, pour=b is None)
        self.v2 = conn
        self.pids = []
        self.exact = None


def _line(c):
    line = []
    for _lay, x1, y1, x2, y2, _w in c.segs:
        if not line or line[-1] != (x1, y1):
            line.append((x1, y1))
        line.append((x2, y2))
    return line


class LegacyRouter(v1.Router):
    """v1 Router over a v2 RouteState. One instance = one ``route_all`` call."""

    def __init__(self, ctx: Ctx, todo: list[ConnV2], work: float):
        board = ctx.board
        self.state = ctx.state
        self.oracle = ctx.oracle
        cons = getattr(getattr(ctx, "x", None), "cons", None)
        self._cons = cons if hasattr(cons, "lay_width") else None      # per-layer width rules (design §14.8)
        self.live = set()               # piece ids of connections routed (and still standing) in this run
        self.no_ignore = frozenset()
        self.owner = {}                 # piece id -> LConn
        self._victim_conns = {}         # net -> [LConn] the last soft path runs through
        self._ok_path = None
        self.fair = False               # fallback mode: cap a connection's first attempt at its share of the work
        self._tried = set()
        self._cap = None
        self.unmapped = []              # (ConnV2, reason): terminals the v1 search cannot start from
        self.joined = []                # ConnV2 whose two terminals are one static cluster already
        super().__init__(_View(board, self), ctx.cfg, mode="signals", progress=ctx.progress, work=work)
        self.stats.update({"emit_rejects": 0, "emit_restamps": 0, "emit_blocked_cells": 0, "verify_rips": 0,
                           "dependent_rips": 0})
        g = self.g
        g.classes = {}                  # working maps are per run; the static rasters are cached on the grid
        if not hasattr(g, "_v2_static"):
            g._v2_static = {}
        # copper already in the state: fixed
        self.fixed = self.state.pieces()
        self.fixed_groups = {}
        for net in sorted({p.net for p in self.fixed}):
            self.fixed_groups[net] = self.state.net_groups(net, loose=True)
        self._lconns = self._map(todo)

    # ── connections: ConnV2 -> v1 Conn ────────────────────────────────────
    def _map(self, todo):
        b, st = self.b, self.state
        out = []
        ranked = all(c.rank for c in todo)
        for conn in todo:
            cl = b.clusters.get(conn.net) or {}

            def end(t: Terminal):
                pad = st._pad(t.pad, t.x, t.y, conn.net) if t.pad else None
                cid = t.cluster if t.cluster in cl else (pad.cluster if pad is not None else 0)
                if cid not in cl:
                    return None, None
                if pad is None or pad.cluster != cid:
                    pads = cl[cid]["pads"]
                    pad = min(pads, key=lambda p: (abs(p.x - t.x) + abs(p.y - t.y), p.name)) if pads else None
                return cid, pad
            a, pa = end(conn.a)
            if a is None or pa is None:
                self.unmapped.append((conn, f"terminal a ({conn.a.pad or 'point'}) is not a pad cluster of {conn.net}: "
                                            f"the legacy engine routes between pad clusters only"))
                continue
            if is_open(conn.b):
                bcl, pb = None, None
            else:
                bcl, pb = end(conn.b)
                if bcl is None or pb is None:
                    self.unmapped.append((conn, f"terminal b ({conn.b.pad or 'point'}) is not a pad cluster of "
                                                f"{conn.net}: the legacy engine routes between pad clusters only"))
                    continue
                if bcl == a:
                    self.joined.append(conn)
                    continue
            c = LConn(len(out), conn, a, bcl, pa, pb)
            if ranked:
                c.key = tuple(conn.rank)
            else:
                nc = self.cfg.net(conn.net)
                c.key = (nc.rank, -nc.priority, round(conn.est, 3), conn.net, conn.idx)
            out.append(c)
        return out

    def plan_connections(self):
        self.conns = self._lconns
        return self.conns

    # ── occupancy maps: static raster cached on the grid, fixed copper stamped as static ──
    def cm_for(self, key):
        g = self.g
        cm = g.classes.get(key)
        if cm is None:
            base = g._v2_static.get(key)
            if base is None:
                built = self.b._build_class(key)
                base = g._v2_static[key] = (built.S, built.SV)
            cm = ClassMap(key, 0, 0)
            cm.S, cm.SV = array("i", base[0]), array("i", base[1])
            z = array("H", [0])
            zq = array("q", [0])
            cm.cnt, cm.s1, cm.s2 = z * g.N, zq * g.N, zq * g.N
            cm.vcnt, cm.vs1, cm.vs2 = z * g.G, zq * g.G, zq * g.G
            self._stamp_fixed(cm)
            cm.freeze()
            g.classes[key] = cm
            for c in self.routed:
                self._mark(c, cm)
        return cm

    def _stamp_fixed(self, cm):
        g, R = self.g, self.b.rules
        G, L = g.G, g.L
        h, c, vr, vd = cm.key
        S, SV = cm.S, cm.SV
        hole_t = R["hole_clearance"] + h + EPS
        hole_v = max(R["hole_clearance"] + vr, R["hole_to_hole"] + vd / 2) + EPS
        for p in self.fixed:
            own = self.oracle.net_id(p.net) or -1
            sep = max(c, self.oracle.clearance(p.net))
            if p.kind == "via":
                cu = Shape("box", geom.box_circle(p.x1, p.y1, p.w / 2), "cu", 0, 0.0, ())
                spans = list(g.shape_spans(cu, h + sep + EPS))
                hole = Shape("box", geom.box_circle(p.x1, p.y1, p.drill / 2), "hole", 0, 0.0, ())
                hspans = list(g.shape_spans(hole, hole_t))
                for l in range(L):
                    g.stamp(S, l * G, spans, own)
                    g.stamp(S, l * G, hspans, own)
                g.stamp(SV, 0, g.shape_spans(cu, vr + sep + EPS), own)
                g.stamp(SV, 0, g.shape_spans(hole, hole_v), -1)
            else:
                cu = Shape("box", geom.box_from_seg(p.x1, p.y1, p.x2, p.y2, p.w), "cu", 0, 0.0, ())
                g.stamp(S, p.layer * G, g.shape_spans(cu, h + sep + EPS), own)
                g.stamp(SV, 0, g.shape_spans(cu, vr + sep + EPS), own)

    # ── components: static clusters + fixed copper + what this run has routed ──
    def _comps(self, name):
        cs = self.comp.get(name)
        if cs is not None:
            return cs
        cs = super()._comps(name)
        g = self.g
        G, W, step = g.G, g.W, g.step
        P = self.state._pieces
        for cids, pids in self.fixed_groups.get(name, ()):
            cids = [c for c in cids if c in cs]
            if not cids:
                continue                    # copper that touches no pad cluster: nothing to connect to
            for cid in cids[1:]:
                self._merge(name, cids[0], cid, ())
            cells = cs[cids[0]]["cells"]
            for pid in pids:
                p = P.get(pid)
                if p is None:
                    continue
                if p.kind == "via":
                    cx, cy = g.cell(p.x1, p.y1)
                    if math.hypot(g.mx(cx) - p.x1, g.my(cy) - p.y1) < p.w / 2 - 0.03:
                        for l in range(g.L):
                            cells[l * G + cy * W + cx] = (p.x1, p.y1)
                else:
                    rad = max(0.02, min(p.w / 2 - 0.03, 0.72 * step))
                    base = p.layer * G
                    for i in g.cells(g.capsule_spans(p.x1, p.y1, p.x2, p.y2, rad)):
                        cells.setdefault(base + i, (p.x1, p.y1, p.x2, p.y2))
        for c in self.routed:
            if c.net == name:
                self._join(c)
        return cs

    def _join(self, c):
        """What v1's _finish does to the components when ``c`` is committed."""
        cs = self._comps(c.net)
        path = c.states
        if not path:
            return
        if c.b is None:
            end = path[-1]
            for cid, comp in cs.items():
                if comp is not cs[c.a] and end in comp["cells"]:
                    self._merge(c.net, c.a, cid, path)
                    break
        else:
            self._merge(c.net, c.a, c.b, path)

    # ── copper <-> RouteState ─────────────────────────────────────────────
    def _commit(self, c):
        super()._commit(c)
        st = self.state
        conn = c.v2
        full = conn.width or self.cfg.net(c.net).width
        cons = self._cons
        pids = []
        clr = self.cfg.net(c.net).clearance
        for lay, x1, y1, x2, y2, w in c.segs:
            if math.hypot(x2 - x1, y2 - y1) < TINY:
                continue
            # a per-layer width rule: this layer's own width is what the segment is laid at (when the search ran
            # at the full width) and what polish may widen a thinner one back to — never the widest layer's
            lay_full = full
            if cons is not None and cons.layered(c.net):
                lay_full = cons.lay_width(c.net, lay, full)
                if abs(w - full) <= 1e-9 or w > lay_full:
                    w = lay_full
            for xa, ya, xb, yb in self._octi(lay, x1, y1, x2, y2, w, c.net, clr):
                pids.append(st.add(net=c.net, conn=conn.idx, kind="seg", layer=lay, x1=xa, y1=ya, x2=xb, y2=yb, w=w,
                                   stage="legacy", target_w=max(lay_full, w)).id)
        for x, y, size, drill in c.vias:
            pids.append(st.add(net=c.net, conn=conn.idx, kind="via", x1=x, y1=y, w=size, drill=drill,
                               stage="legacy").id)
        c.pids = pids
        self.live.update(pids)
        for pid in pids:
            self.owner[pid] = c

    def _octi(self, lay, x1, y1, x2, y2, w, net, clr):
        """A short odd-angle segment (v1's snap from a grid point onto an off-grid pad centre / tee point) as two
        segments on the 45-degree directions, when the oracle accepts one of the two postures; else as it is.
        The copper moves by less than OCTI_MAX / 2; later searches of this run see v1's own shape of it and every
        path they find is checked against the real pieces when it is found (EMIT_CHECK), like all others."""
        dx, dy = x2 - x1, y2 - y1
        adx, ady = abs(dx), abs(dy)
        ln = math.hypot(dx, dy)
        if not OCTI or ln > OCTI_MAX or min(adx, ady) < 1e-6:
            return [(x1, y1, x2, y2)]
        ang = math.degrees(math.atan2(ady, adx)) % 45.0
        if min(ang, 45.0 - ang) <= 0.5:
            return [(x1, y1, x2, y2)]
        d = min(adx, ady)
        sx, sy = (1.0 if dx > 0 else -1.0), (1.0 if dy > 0 else -1.0)
        o = self.oracle
        for mx, my in ((q(x1 + sx * d), q(y1 + sy * d)), (q(x2 - sx * d), q(y2 - sy * d))):
            if math.hypot(mx - x1, my - y1) < 2e-6 or math.hypot(x2 - mx, y2 - my) < 2e-6:
                continue
            if o.seg_legal(lay, x1, y1, mx, my, w, net, clearance=clr)                     and o.seg_legal(lay, mx, my, x2, y2, w, net, clearance=clr):
                return [(x1, y1, mx, my), (mx, my, x2, y2)]
        return [(x1, y1, x2, y2)]

    def _rip(self, c):
        st = self.state
        for pid in c.pids:
            st.remove(pid)
            self.live.discard(pid)
            self.owner.pop(pid, None)
        c.pids = []
        super()._rip(c)

    def _rip_conns(self, conns) -> list:
        """Rip these connections and every routed connection of their nets that is no longer connected
        (it teed onto ripped copper). Returns everything ripped, in rip order."""
        out = []
        nets = []
        for c in conns:
            if c.state == "routed":
                self._rip(c)
                out.append(c)
                if c.net not in nets:
                    nets.append(c.net)
        st = self.state
        again = True
        while again:
            again = False
            for name in nets:
                for r in [r for r in self.routed if r.net == name]:
                    if not st.connected(r.v2.idx, loose=True):
                        self._rip(r)
                        out.append(r)
                        self.stats["dependent_rips"] += 1
                        again = True
        for name in nets:
            self.comp.pop(name, None)
            if name in self.dp_done:
                lines = [_line(r) for r in self.routed if r.net == name]
                if lines:
                    self.dp_done[name] = lines
                else:
                    del self.dp_done[name]
        return out

    def rip_net(self, name):
        """v1's negotiation asks for a net; rip only the connections the soft path runs through (RIP_LEVEL)."""
        conns = self._victim_conns.pop(name, None)
        if conns is None or RIP_LEVEL == "net":
            conns = [c for c in self.routed if c.net == name]
        return self._rip_conns(conns)

    def _victims(self, c, path, info, segs):
        G = self.g.G
        key = info["key"]
        P = set(path)
        PV = set()
        for i in range(1, len(path)):
            if path[i] // G != path[i - 1] // G:
                PV.add(path[i] % G)
        out = []
        self._victim_conns = {}
        for r in self.routed:
            if r.net == c.net:
                continue
            st = r.sets.get(key)
            if st is None:
                continue
            if not st[0].isdisjoint(P) or (PV and not st[1].isdisjoint(PV)):
                self._victim_conns.setdefault(r.net, []).append(r)
                if r.net not in out:
                    out.append(r.net)
        return out

    def _dyn_offenders(self, c, nc, segs, vias, width):
        b = self.b
        nid = b.nets[c.net].id
        out = set()
        for lay, x1, y1, x2, y2, w in segs:
            bb = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            for s in self.dyn.near(lay, bb, w / 2 + nc.clearance + 0.6):
                if s.net == nid:
                    continue
                need = w / 2 + (max(nc.clearance, s.clr) if s.cls == "cu" else b.rules["hole_clearance"])
                if s.dist_seg(x1, y1, x2, y2) < need + 0.002:
                    r = s.tag
                    out.add(r.net)
                    lst = self._victim_conns.setdefault(r.net, [])
                    if r not in lst:
                        lst.append(r)
        return out

    # ── the oracle judges every path when it is found ─────────────────────
    def _exact(self, c, nc, segs, vias):
        """First piece of this geometry the oracle rejects: ('seg' | 'via', index, Blocker), or None."""
        o = self.oracle
        for k, (lay, x1, y1, x2, y2, w) in enumerate(segs):
            if math.hypot(x2 - x1, y2 - y1) < TINY:
                continue
            if not o.seg_legal(lay, x1, y1, x2, y2, w, c.net, nc.clearance):
                return "seg", k, o.seg_ok(lay, x1, y1, x2, y2, w, c.net, nc.clearance)
        for k, (x, y, size, drill) in enumerate(vias):
            if not o.via_legal(x, y, size, drill, c.net, clearance=nc.clearance):
                return "via", k, o.via_ok(x, y, size, drill, c.net, clearance=nc.clearance)
        return None

    def _restamp(self, c, nc, cm, kind, lay, x1, y1, x2, y2, hw, drill) -> int:
        """Rasterise again, into this run's map of the probe class, every shape the rejected piece violates:
        with the oracle's need and the guard that makes cell-to-cell steps legal (oracle.raster_guard), where
        v1's raster used the class clearance and a fixed EPS. Copper of this run is left to the cell fallback
        (it may be ripped again). Returns the number of shapes stamped."""
        g, o = self.g, self.oracle
        G = g.G
        bb = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        via = kind == "via"
        n = 0
        for layer in (range(g.L) if via else (lay,)):
            for s in o.near(layer, bb, hw + RESTAMP_REACH):
                if isinstance(s.tag, int) and s.tag in self.live:
                    continue
                need = o.need(c.net, hw, s, layer, "via" if via else "seg", drill, nc.clearance)
                if need == NO_NEED or s.dist_seg(x1, y1, x2, y2) - hw >= need - 1e-9:
                    continue
                reach = hw + need
                grow = reach + max(GUARD_MM, raster_guard(g.step, reach))
                own = (s.net or -1) if s.cls in ("cu", "hole", "soft") else -1
                spans = list(g.shape_spans(s, grow))
                if via:
                    g.stamp(cm.SV, 0, spans, own)
                    g.stamp(cm.vocc, 0, spans, own)
                else:
                    g.stamp(cm.S, lay * G, spans, own)
                    g.stamp(cm.occ, lay * G, spans, own)
                n += 1
        return n

    def _block(self, c, nc, path, info, bad, segs, vias) -> bool:
        """Close the grid cells behind a rejected piece in this run's map of the probe class. False = nothing
        could be closed (searching again would find the same path)."""
        g, cm, o = self.g, info["cm"], self.oracle
        G, W = g.G, g.W
        nid = self.b.nets[c.net].id
        kind, k, _blk = bad
        if kind == "via":
            x, y, size, drill = vias[k]
            ix, iy = g.cell(x, y)
            i = iy * W + ix
            if self._restamp(c, nc, cm, "via", 0, x, y, x, y, size / 2, drill) and cm.vocc[i] not in (0, nid):
                self.stats["emit_restamps"] += 1
                return True
            if cm.SV[i] == -1 and cm.vocc[i] == -1:
                return False
            cm.SV[i] = cm.vocc[i] = -1
            self.stats["emit_blocked_cells"] += 1
            return True
        lay, x1, y1, x2, y2, w = segs[k]
        on = []
        for s in path:
            if s // G != lay:
                continue
            i = s - lay * G
            x, y = g.mx(i % W), g.my(i // W)
            if geom.pt_seg_dist(x, y, x1, y1, x2, y2) < 1e-6:
                on.append((s, x, y))
        if self._restamp(c, nc, cm, "seg", lay, x1, y1, x2, y2, w / 2, 0.0) \
                and any(cm.occ[s] not in (0, nid) for s, _x, _y in on):
            self.stats["emit_restamps"] += 1
            return True
        ends = (path[0], path[-1])
        close = []
        if len(on) >= 2:
            for (sa, ax, ay), (sb, bx, by) in zip(on, on[1:]):
                if abs(ax - bx) > 1.01 * g.step or abs(ay - by) > 1.01 * g.step:
                    continue
                if not o.seg_legal(lay, ax, ay, bx, by, w, c.net, nc.clearance):
                    # keep the path's own first / last cell open when the other end of the step can be closed
                    pair = [s for s in (sa, sb) if s not in ends] or [sa, sb]
                    close += [s for s in pair if s not in close]
        if not close and on:
            # an off-grid stub (snap / escape) or a run that is only illegal as a whole: close its grid end
            at = _blk.at if _blk is not None else (x1, y1)
            close = [min(on, key=lambda e: (math.hypot(e[1] - at[0], e[2] - at[1]), e[0]))[0]]
        n = 0
        for s in close:
            if cm.occ[s] != -1 or cm.S[s] != -1:
                cm.S[s] = cm.occ[s] = -1
                n += 1
        self.stats["emit_blocked_cells"] += n
        return n > 0

    def _search(self, c, nc, width, soft, via_mult=1.0):
        if self._cap is None:
            return self._search1(c, nc, width, soft, via_mult)
        # fair share (fallback mode): scale v1's expansion limit so this attempt stays inside its allowance
        room = self._cap - self.work()
        cfg = self.cfg
        base = 120000.0 * max(cfg.effort, 1e-6)
        saved = cfg.effort
        cfg.effort = saved * max(FAIR_FLOOR, min(1.0, room / base))
        try:
            return self._search1(c, nc, width, soft, via_mult)
        finally:
            cfg.effort = saved

    def _search1(self, c, nc, width, soft, via_mult=1.0):
        if soft or not EMIT_CHECK:
            return super()._search(c, nc, width, soft, via_mult)
        bad = info = None
        for attempt in range(EMIT_RETRIES + 1):
            path, info = super()._search(c, nc, width, False, via_mult)
            if path is None:
                return path, info
            segs, vias = self._geometry(c, nc, path, info, width)
            bad = self._exact(c, nc, segs, vias)
            if bad is None:
                self._ok_path = path
                return path, info
            self.stats["emit_rejects"] += 1
            c.exact = bad[2]
            if attempt == EMIT_RETRIES or self.spent() or not self._block(c, nc, path, info, bad, segs, vias):
                break
        blk = bad[2]
        where = [round(blk.at[0], 2), round(blk.at[1], 2)] if blk is not None else [c.pa.x, c.pa.y]
        return None, {"reason": "blocked", "where": where, "cm": info["cm"]}

    def _finish(self, c, nc, path, info, width, recheck=False):
        if EMIT_CHECK and self._ok_path is not path:
            g = self.g
            if recheck:
                occ = info["cm"].occ
                nid = self.b.nets[c.net].id
                for s in path:
                    o = occ[s]
                    if o and o != nid:
                        c.reason, c.where = "blocked", list(g.xy(s % g.G))
                        return False
                recheck = False
            segs, vias = self._geometry(c, nc, path, info, width)
            bad = self._exact(c, nc, segs, vias)
            if bad is not None:
                self.stats["emit_rejects"] += 1
                c.exact = bad[2]
                self._block(c, nc, path, info, bad, segs, vias)
                blk = bad[2]
                c.reason = "blocked"
                c.where = [round(blk.at[0], 2), round(blk.at[1], 2)] if blk is not None else [c.pa.x, c.pa.y]
                return False
        self._ok_path = None
        return super()._finish(c, nc, path, info, width, recheck)

    def route_conn(self, c):
        c.exact = None
        self._cap = None
        if self.fair:
            # fallback mode: every connection gets its turn. The first attempt of a connection may use at most its
            # fair share of the work that is left (never less than FAIR_MIN); a later attempt (after a rip-up, or
            # v1's second chance once everybody had a turn) is not capped
            first = c.idx not in self._tried
            self._tried.add(c.idx)
            if first:
                untried = 1 + sum(1 for o in self.conns if o.state == "pending" and o.idx not in self._tried)
                room = max(0.0, self.work_limit - self.work())
                self._cap = self.work() + max(FAIR_MIN, FAIR_FACTOR * room / untried)
        try:
            ok = super().route_conn(c)
        finally:
            self._cap = None
        if not ok and c.exact is not None and (c.reason or "").startswith("no path"):
            blk = c.exact
            c.reason = (f"no path the exact clearance check accepts: the routes found pass {blk.tag or 'copper'}"
                        f"{' of ' + blk.net if blk.net else ''} {round(max(0.0, blk.need - blk.dist) * 1000, 1)} um "
                        f"inside its {blk.rule} distance (grid {self.g.step} mm; a finer global.grid_mm may help)")
        return ok

    # ── final self-check: per connection, re-queued once ──────────────────
    def _violation(self, c):
        o, P = self.oracle, self.state._pieces
        clr = self.cfg.net(c.net).clearance
        for pid in c.pids:
            p = P[pid]
            blk = o.piece_ok(p, clearance=clr)
            if blk is not None:
                return p, blk
        return None

    def _verify(self):
        """Exact re-check of every routed piece. A connection that fails is taken out alone (with whatever
        teed onto it), routed once more, and reported if it fails again. v1 dropped the whole net here."""
        retried = set()
        for _round in range(3):
            ripped, why = [], {}
            i = 0
            while i < len(self.routed):
                c = self.routed[i]
                v = self._violation(c)
                if v is None:
                    i += 1
                    continue
                p, blk = v
                victim = c
                if blk.piece is not None and blk.piece in self.owner:
                    d = self.owner[blk.piece]
                    if d is not c and self.routed.index(d) > i:
                        victim = d          # of two connections of this run, the one routed later goes
                if p.kind == "via":
                    where = [round(p.x1, 2), round(p.y1, 2)]
                else:
                    where = [round((p.x1 + p.x2) / 2, 2), round((p.y1 + p.y2) / 2, 2)]
                self.stats["verify_rips"] += 1
                why[victim.idx] = where
                ripped += self._rip_conns([victim])
            if not ripped:
                return
            saved, self.cfg.ripup_rounds = self.cfg.ripup_rounds, 0
            try:
                for r in sorted(ripped, key=lambda r: (r.key, r.idx)):
                    if r.state != "pending":
                        continue
                    blamed = r.idx in why
                    if (blamed and r.idx in retried) or self.spent():
                        r.state = "failed"
                        r.reason = SELF_CHECK if blamed else "time budget used up before this connection was tried"
                        r.where = why.get(r.idx) or [r.pa.x, r.pa.y]
                        continue
                    if blamed:
                        retried.add(r.idx)
                    if not self.route_conn(r) and blamed:
                        r.reason, r.where = SELF_CHECK, why[r.idx]
            finally:
                self.cfg.ripup_rounds = saved
        # still not clean after three rounds: whatever fails now is dropped for good
        i = 0
        while i < len(self.routed):
            c = self.routed[i]
            v = self._violation(c)
            if v is None:
                i += 1
                continue
            p = v[0]
            for r in self._rip_conns([c]):
                r.state = "failed"
                r.reason = SELF_CHECK
                r.where = [round((p.x1 + p.x2) / 2, 2), round((p.y1 + p.y2) / 2, 2)]


# ── public ────────────────────────────────────────────────────────────────
def _fail_record(c) -> dict:
    """A failed connection in v1's ``failed[]`` format (session._failed) plus the v2 connection idx."""
    e = {"conn": c.v2.idx, "net": c.net, "from": c.pa.name, "to": c.pb.name if c.pb else "pour",
         "_a": (c.pa.x, c.pa.y), "_b": (c.pb.x, c.pb.y) if c.pb else None,
         "reason": c.reason or "not routed"}
    if c.where:
        e["where"] = [round(c.where[0], 2), round(c.where[1], 2)]
    if c.blocked_by:
        e["blocked_by"] = c.blocked_by[:6]
    return e


def route_all(ctx: Ctx, budget: Budget, only: list[int] | None = None, fair: bool = False) -> StageReport:
    """Route the connections of ``ctx.conns`` that are not 'routed' (``only``: restrict to these idx) with
    v1's search, on ``ctx.board`` with every piece already in ``ctx.state`` as fixed copper.

    Results go straight into ``ctx.state``: pieces with ``stage='legacy'``, status 'routed' or 'failed' with
    a v1-format failure record (``state.fail(idx)``; also listed in ``report.data['failed']``). The whole call
    is one transaction: an exception leaves the state as it was. ``fair`` (the engine's per-connection fallback after
    the window search): the first attempt of each connection is capped at its share of the budget that is left, so
    that every connection gets a turn before the budget is gone. The work done (v1 work units + 1 per 8
    oracle checks) is charged to ``budget``; ``budget.left()`` is the limit v1 stops at.

    report.data: failed [records], notes ['net: note'], stats (searches, expansions, ripups, emit_rejects,
    emit_restamps, emit_blocked_cells, verify_rips, dependent_rips), clock_hit, seconds, pass (v1's passes[] entry),
    not_connected [idx routed by v1 but not ``state.connected(idx, loose=True)``].
    """
    state, oracle, board = ctx.state, ctx.oracle, ctx.board
    sel = None if only is None else set(only)
    todo = [c for c in ctx.conns if (sel is None or c.idx in sel) and state.status(c.idx) != "routed"]
    rep = StageReport(name="legacy")
    if not todo:
        rep.score = state.score()
        return rep
    step = ctx.cfg.step
    if board.grid is None or board.step != step:
        board.make_grid(step)
    checks0 = oracle.checks
    tok = state.begin()
    try:
        r = LegacyRouter(ctx, todo, work=budget.left())
        r.fair = bool(fair)
        res = r.run()
        failed = []
        for conn, reason in r.unmapped:
            e = {"conn": conn.idx, "net": conn.net, "from": conn.a.pad or "point", "to": conn.b.pad or "pour",
                 "_a": (conn.a.x, conn.a.y), "_b": None if is_open(conn.b) else (conn.b.x, conn.b.y),
                 "reason": reason, "where": [round(conn.a.x, 2), round(conn.a.y, 2)]}
            state.set_status(conn.idx, "failed", e)
            failed.append(e)
        for conn in r.joined:
            state.set_status(conn.idx, "routed")
        n_ok = len(r.joined)
        notes, loose = [], []
        for c in r.conns:
            if c.state == "routed":
                state.set_status(c.v2.idx, "routed")
                n_ok += 1
                notes += [f"{c.net}: {n}" for n in c.notes]
                if not state.connected(c.v2.idx, loose=True):
                    loose.append(c.v2.idx)
            else:
                e = _fail_record(c)
                prev = state.fail(c.v2.idx) if state.status(c.v2.idx) == "failed" else None
                if prev and prev.get("reason") and (c.reason or "").startswith("time budget used up before"):
                    # an earlier stage tried this connection and said why it failed; this search did not get to
                    # it: the earlier diagnosis stands (it was tried)
                    e = dict(prev)
                    e.setdefault("conn", c.v2.idx)
                    e["fallback"] = "not reached inside the budget"
                state.set_status(c.v2.idx, "failed", e)
                failed.append(e)
    except BaseException:
        state.rollback(tok)
        raise
    state.commit(tok)
    work = int(res["work"]) + (oracle.checks - checks0) // ORACLE_CHECKS_PER_UNIT
    budget.charge(work)
    rep.routed, rep.failed, rep.work = n_ok, len(failed), work
    rep.score = state.score()
    rep.notes = sorted(set(notes))[:20]
    if res["clock_hit"]:
        rep.notes.append("the wall-clock safety limit stopped the v1 search before its work budget was used up: "
                         "this result may not be reproducible")
    rep.data = {"failed": failed, "stats": dict(res["stats"]), "clock_hit": res["clock_hit"],
                "seconds": res["seconds"], "not_connected": loose,
                "pass": {"pass": "legacy", "connections": len(todo), "routed": n_ok, "seconds": res["seconds"],
                         "ripups": res["stats"]["ripups"]}}
    return rep


def plan_connections(board, cfg, mode: str = "signals", start: int = 0) -> list[ConnV2]:
    """v1's connection plan (``Router.plan_connections``) as ConnV2 — the minimal ``classify`` of build stage 1.

    mode 'signals': nets without a pour, an MST over each net's pad clusters; 'pours': every cluster of a
    poured net but the main one, to an *open* terminal (``pieces.is_open``: anything else of the net) — only
    meaningful on a board extracted after the zones were refilled; 'all': both. ``rank`` is v1's routing
    order key, ``idx`` counts from ``start``. Nets the plan skips are not planned (see ``skipped``)."""
    out = []
    for m in (("signals", "pours") if mode == "all" else (mode,)):
        r = object.__new__(v1.Router)           # plan_connections needs no grid: skip Router.__init__
        r.b, r.cfg, r.mode, r.conns, r.skipped = board, cfg, m, [], []
        base = start + len(out)
        for c in r.plan_connections():
            nc = cfg.net(c.net)
            a = Terminal(x=c.pa.x, y=c.pa.y, layers=tuple(c.pa.layers), pad=c.pa.name, cluster=c.a)
            if c.pb is None:
                # where "anything else" is nearest: only an aim for estimates, the search takes any target
                best = None
                for cid, cl in sorted(board.clusters[c.net].items()):
                    if cid == c.a:
                        continue
                    for p in cl["pads"]:
                        d = abs(p.x - c.pa.x) + abs(p.y - c.pa.y)
                        if best is None or d < best[0]:
                            best = (d, p.x, p.y)
                b = Terminal(x=best[1] if best else c.pa.x, y=best[2] if best else c.pa.y, layers=())
            else:
                b = Terminal(x=c.pb.x, y=c.pb.y, layers=tuple(c.pb.layers), pad=c.pb.name, cluster=c.b)
            necks = [w for w in nc.neck_widths if w < nc.width - 1e-6]
            out.append(ConnV2(idx=base + c.idx, net=c.net, a=a, b=b, est=c.est, role="plane" if c.pour else "signal",
                              width=nc.width, neck=min(necks) if necks else nc.width, clearance=nc.clearance,
                              via=(nc.via, nc.drill), rank=tuple(c.key[:-1]) + (base + c.idx,), tree_target=True))
    return out


def skipped(board, cfg) -> list[dict]:
    """Nets the plan leaves out, in v1's ``skipped[]`` format: [{net, connections, reason}]."""
    out = []
    for name in sorted(board.clusters):
        cl = board.clusters[name]
        if len(cl) >= 2 and cfg.net(name).skip:
            out.append({"net": name, "connections": len(cl) - 1, "reason": cfg.net(name).skip_reason or "plan: skip"})
    return out
