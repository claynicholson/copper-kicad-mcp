"""S6 polish: pad entry, pull-tight, staircase / odd-angle removal, via removal, re-widen (design §4.10).

Polish only ever *replaces* copper of one routed connection by other copper of the same connection:

    edit = remove a run of pieces + add new pieces, inside one ``RouteState`` transaction

and an edit is kept only when all of these hold:

    legal        every new segment passes ``oracle.seg_legal`` (the single legality test), stays out of the
                 plan's forbidden regions and crosses no thermal / reserve shape the old copper did not cross
    topology     same-layer edits keep the side of every obstacle: the loop between the old and the new path
                 contains no foreign shape (pads, holes, keep-outs, other nets' copper)
    connectivity ``state.connected`` still holds for the connection and for every same-net connection that
                 touches it (tee points and shared end points are never moved; a segment another piece tees
                 onto is never touched)
    score        the ``Score`` delta (necked mm, vias, length, bends) is not worse. One documented exception:
                 a *clean-up* edit (fewer odd-angle segments or stubs shorter than 0.2 mm and no new ones, or
                 a track end moved onto its pad centre) may cost length as long as the *stage's cumulative*
                 delta stays not worse (the engine gates the stage on exactly that). Refused ones are retried
                 once after the other passes have earned length. ``CLEANUP_SLACK_MM`` /
                 ``ctx.x.polish_slack_mm`` (default 0) lets the stage's length grow by that much for clean-ups.

After every pass the whole state is audited (every connection that was connected still is, ``state.score()``
is not worse than at stage start); a pass that fails the audit is restored from its snapshot.

Passes (``run(ctx, budget, passes)``, each to a fixed point or until the budget is spent):

    entry    last segment into a pad is axial / 45 degrees and ends on the pad centre; stubs < 0.2 mm merged
    tight    replace point runs i..j by the two 45-degree postures between them, longest span first
    stairs   three segments with two bends < 0.6 mm apart -> one diagonal or one L (or a 45-degree jog);
             interior odd-angle segments and segments < 0.2 mm -> 45-degree postures
    vias     a span that lives on another layer between a via and a via / through-hole pad is moved to the
             entry layer when a direct pattern (straight, L, Z, or the same polyline) is legal there and
             shorter than 1.3x; the vias it frees are removed
    widen    a piece with w < target_w gets the widest legal width (0.01 mm steps); axial / 45-degree
             segments are split so only the tight span stays necked

Every geometry pass first tidies the connection: dead ends and loops that lead nowhere are removed, collinear
neighbours merged. Connections that are only joined by KiCad's rule (an end inside other copper, v1's legacy
output: ``RouteState.connected(conn, loose=True)``) are polished too and judged by that rule.

Not done here (see docs/internal/v2-interface-notes.md, W8): window search for via removal (patterns only),
differential pairs (role 'pair' is left exactly as routed), protected pieces (escape stubs, dog-bones),
connections that carry length-tuning meanders (pieces of stage 'tune', design §14.7: left exactly as tuned).

Deterministic: connections by index, pieces by id, candidates in generation order; no clock, no randomness.
Budget: ``CHARGE_BUILD`` per connection view, ``CHARGE_TRY`` per candidate that reaches the oracle,
``CHARGE_EDIT`` per transaction, plus 1 unit per 8 oracle checks (calibrated to ~200 000 units per second).
"""
from __future__ import annotations

import inspect
import math

from ..guided import geom
from .oracle import NO_NEED
from .types import ORACLE_CHECKS_PER_UNIT, Budget, Ctx, Piece, StageReport, q

PASSES = ("entry", "tight", "stairs", "vias", "widen")

EPS = 2e-6            # mm: two points this close are the same point (KiCad's unit is 1 nm)
MIN_SEG = 0.2         # mm: a segment shorter than this is a "stub" (benchmark metric "segments < 0.2 mm")
STAIR_MM = 0.6        # mm: two bends closer than this are a staircase
VIA_GROW = 1.3        # via removal: the moved span may be at most this much longer
WIDEN_STEP = 0.01     # mm: width resolution of re-widening
WIDEN_RES = 0.1       # mm: resolution of the legality boundary when a necked segment is split
MAX_SPAN = 48         # pull-tight: longest point run tried at once
ENTRY_SPAN = 4        # pad entry: how many segments from the pad may be replaced
CLEANUP_SLACK_MM = 0.0   # mm the stage's total length may GROW for clean-up edits (odd angles, stubs, pad centres).
#                          0 = the design's rule (Score never worse). ``ctx.x.polish_slack_mm`` overrides it.
GROW_CAP = 0.01       # clean-up paid for by removed vias / re-widened length may add at most 1 % of the routed length
MAX_ROUNDS = 2        # sweeps over all connections per pass (each connection is polished to its own fixed point)
CHARGE_TRY = 6        # work units per candidate that reaches the oracle (geometry, 1-3 oracle checks, topology test)
CHARGE_EDIT = 60      # work units per transaction (remove + add + connectivity check of the connection)
CHARGE_BUILD = 60     # work units per connection view (pieces, attachment index, chains)
CELL = 1.0            # mm: bucket size of the per-net attachment index
_SQ2 = math.sqrt(2.0)
_STATIC = -2          # "connection" of static attachment points (pad centres, terminals, kept copper)


# ── small geometry ────────────────────────────────────────────────────────
def _k(x: float, y: float) -> tuple[int, int]:
    """Point key in nanometres."""
    return (int(round(x * 1e6)), int(round(y * 1e6)))


def _is45(dx: float, dy: float) -> bool:
    adx, ady = abs(dx), abs(dy)
    return adx <= EPS or ady <= EPS or abs(adx - ady) <= EPS


def _plen(pts) -> float:
    s = 0.0
    for i in range(len(pts) - 1):
        s += math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
    return s


def _turn(a, b, c):
    """(cross, dot) of the unit directions a->b and b->c; None when either is degenerate."""
    ux, uy = b[0] - a[0], b[1] - a[1]
    vx, vy = c[0] - b[0], c[1] - b[1]
    lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
    if lu < EPS or lv < EPS:
        return None
    return (ux * vy - uy * vx) / (lu * lv), (ux * vx + uy * vy) / (lu * lv)


def _bends(pts) -> int:
    n = 0
    for i in range(1, len(pts) - 1):
        t = _turn(pts[i - 1], pts[i], pts[i + 1])
        if t is not None and (abs(t[0]) > 1e-6 or t[1] < 0):
            n += 1
    return n


def _sharp(pts) -> int:
    """Joints that turn by more than 90 degrees (acute corners)."""
    n = 0
    for i in range(1, len(pts) - 1):
        t = _turn(pts[i - 1], pts[i], pts[i + 1])
        if t is not None and t[1] < -1e-6:
            n += 1
    return n


def _quality(pts) -> tuple[int, int]:
    """(odd-angle segments, blemished segments) of a polyline; blemished = odd-angle or shorter than MIN_SEG."""
    odd = bad = 0
    for i in range(len(pts) - 1):
        dx, dy = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        o = not _is45(dx, dy)
        if o:
            odd += 1
        if o or math.hypot(dx, dy) < MIN_SEG - 1e-9:
            bad += 1
    return odd, bad


def _postures(a, b) -> list[list[tuple[float, float]]]:
    """The 45-degree postures from a to b: one straight segment when aligned, else diagonal-first and
    axial-first (one 45-degree corner each). Points are quantised."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    adx, ady = abs(dx), abs(dy)
    if adx <= EPS and ady <= EPS:
        return []
    if _is45(dx, dy):
        return [[a, b]]
    s = adx if adx < ady else ady
    sx, sy = math.copysign(s, dx), math.copysign(s, dy)
    return [[a, (q(a[0] + sx), q(a[1] + sy)), b], [a, (q(b[0] - sx), q(b[1] - sy)), b]]


def _zs(a, b) -> list[list[tuple[float, float]]]:
    """The two Z postures from a to b (axial-diagonal-axial and diagonal-axial-diagonal, middle leg centred)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    adx, ady = abs(dx), abs(dy)
    if _is45(dx, dy):
        return []
    s = adx if adx < ady else ady
    r = abs(adx - ady)
    sx, sy = math.copysign(s, dx), math.copysign(s, dy)
    ax, ay = (math.copysign(1.0, dx), 0.0) if adx > ady else (0.0, math.copysign(1.0, dy))
    h = q(r / 2)
    p1 = (q(a[0] + ax * h), q(a[1] + ay * h))
    p2 = (q(p1[0] + sx), q(p1[1] + sy))
    hs = q(s / 2)
    q1 = (q(a[0] + math.copysign(hs, dx)), q(a[1] + math.copysign(hs, dy)))
    q2 = (q(q1[0] + ax * r), q(q1[1] + ay * r))
    return [[a, p1, p2, b], [a, q1, q2, b]]


def _dedupe(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) > EPS / 2 or abs(p[1] - out[-1][1]) > EPS / 2:
            out.append(p)
    return out


def _lex(d, eps: float = 1e-9) -> int:
    """Sign of a Score delta compared lexicographically (-1 better, 0 equal, +1 worse)."""
    for v in d:
        if v < -eps:
            return -1
        if v > eps:
            return 1
    return 0


def _cell(x: float, y: float) -> tuple[int, int]:
    return (int(math.floor(x / CELL)), int(math.floor(y / CELL)))


def _seg_cells(x1, y1, x2, y2, hw: float = 0.0) -> set:
    """Cells that hold every point within ``hw`` of the segment (sampled every half cell, dilated by one cell,
    by more for copper wider than a cell)."""
    r = 1 if hw <= 0.5 * CELL else 1 + int(math.ceil(hw / CELL))
    ax, ay = int(math.floor(x1 / CELL)), int(math.floor(y1 / CELL))
    bx, by = int(math.floor(x2 / CELL)), int(math.floor(y2 / CELL))
    if -1 <= ax - bx <= 1 and -1 <= ay - by <= 1:            # short segment (the usual case): its bounding cells
        return {(i, j) for i in range(min(ax, bx) - r, max(ax, bx) + r + 1)
                for j in range(min(ay, by) - r, max(ay, by) + r + 1)}
    n = int(math.hypot(x2 - x1, y2 - y1) / (CELL * 0.5)) + 1
    rng = range(-r, r + 1)
    cells = set()
    last = None
    for t in range(n + 1):
        f = t / n
        c = _cell(x1 + (x2 - x1) * f, y1 + (y2 - y1) * f)
        if c == last:
            continue
        last = c
        for dx in rng:
            for dy in rng:
                cells.add((c[0] + dx, c[1] + dy))
    return cells


# ── topological chains (public) ───────────────────────────────────────────
def _walk(segs, is_break, frozen=frozenset()):
    """Maximal chains of ``segs`` ([(piece, key_a, key_b)]) on one layer through nodes where ``is_break(layer,
    key)`` is false. -> [(layer, [keys], [pieces])], deterministic (nodes sorted, pieces by id)."""
    adj = {}
    for p, ka, kb in segs:
        adj.setdefault((p.layer, ka), []).append((p, kb))
        adj.setdefault((p.layer, kb), []).append((p, ka))
    visited = set()
    out = []
    for node in sorted(adj):
        if not is_break(node[0], node[1], adj[node]):
            continue
        for p, other in sorted(adj[node], key=lambda e: e[0].id):
            if p.id in visited or p.id in frozen:
                continue
            keys, pieces = [node[1]], []
            cur, nk = p, other
            while True:
                visited.add(cur.id)
                pieces.append(cur)
                keys.append(nk)
                lst = adj[(node[0], nk)]
                if is_break(node[0], nk, lst):
                    break
                nxt = lst[1][0] if lst[0][0].id == cur.id else lst[0][0]
                if nxt.id in visited or nxt.id in frozen:
                    break
                nk = lst[1][1] if lst[0][0].id == cur.id else lst[0][1]
                cur = nxt
            out.append((node[0], keys, pieces))
    return out, adj


def _conn_parts(pieces):
    segs, vias = [], {}
    for p in sorted(pieces, key=lambda p: p.id):
        if p.kind == "via":
            vias[_k(p.x1, p.y1)] = p
            continue
        ka, kb = _k(p.x1, p.y1), _k(p.x2, p.y2)
        if ka != kb:
            segs.append((p, ka, kb))
    return segs, vias


def polylines(state, conn: int) -> list[list[Piece]]:
    """The segments of one connection as polylines: maximal runs of segments joined end to end on one layer,
    in path order, broken at vias, at junctions (three or more segments at a point) and at open ends.
    Closed loops without such a node are not returned. Deterministic."""
    segs, vias = _conn_parts(state.pieces(conn=conn))
    chains, _adj = _walk(segs, lambda layer, key, lst: len(lst) != 2 or key in vias)
    return [pieces for _layer, _keys, pieces in chains]


# ── per-net attachment index ──────────────────────────────────────────────
class _NetIndex:
    """Where copper of one net attaches to other copper of that net: end points of every piece (with the
    connection that owns it), every segment, plus static points (pad centres, terminals, kept copper ends).
    Kept current across polish's own edits (add / remove)."""
    __slots__ = ("pb", "sb")

    def __init__(self):
        self.pb = {}          # cell -> [(x, y, layer mask, conn, piece id)]
        self.sb = {}          # (layer, cell) -> [(piece id, conn, x1, y1, x2, y2, half width)]

    def point(self, x, y, mask, conn, pid):
        self.pb.setdefault(_cell(x, y), []).append((x, y, mask, conn, pid))

    def add(self, p: Piece):
        if p.kind == "via":
            self.point(q(p.x1), q(p.y1), -1, p.conn, p.id)
            return
        x1, y1, x2, y2 = q(p.x1), q(p.y1), q(p.x2), q(p.y2)
        m = 1 << p.layer
        self.point(x1, y1, m, p.conn, p.id)
        self.point(x2, y2, m, p.conn, p.id)
        e = (p.id, p.conn, x1, y1, x2, y2, p.w / 2)
        for c in _seg_cells(x1, y1, x2, y2, p.w / 2):
            self.sb.setdefault((p.layer, c), []).append(e)

    def remove(self, p: Piece):
        pid = p.id
        pts = [(q(p.x1), q(p.y1))] if p.kind == "via" else [(q(p.x1), q(p.y1)), (q(p.x2), q(p.y2))]
        for x, y in pts:
            c = _cell(x, y)
            lst = self.pb.get(c)
            if lst:
                self.pb[c] = [e for e in lst if e[4] != pid]
        if p.kind != "via":
            for c in _seg_cells(pts[0][0], pts[0][1], pts[1][0], pts[1][1], p.w / 2):
                lst = self.sb.get((p.layer, c))
                if lst:
                    self.sb[(p.layer, c)] = [e for e in lst if e[0] != pid]


class _Chain:
    """A free run of one connection: fixed end points, movable interior points, one layer, one width."""
    __slots__ = ("layer", "pts", "pieces", "w", "tw", "before", "after")


class _Cx:
    """Working view of one connection during one pass."""
    __slots__ = ("conn", "me", "net", "nid", "clr", "segs", "vias", "adj", "frozen", "chains", "watch", "memo", "ni",
                 "own_pads", "wset", "tolerant")


# ── the polisher ──────────────────────────────────────────────────────────
class _Polisher:
    def __init__(self, ctx: Ctx, budget: Budget):
        self.ctx = ctx
        self.board = ctx.board
        self.oracle = ctx.oracle
        self.state = ctx.state
        self.budget = budget
        self.used0 = budget.used
        self.checks0 = self.oracle.checks
        self._checks_seen = self.oracle.checks
        self.bank = [0.0, 0, 0.0, 0]          # cumulative Score delta of the stage: necked mm, vias, length mm, bends
        self.deferred = 0                     # clean-up edits refused only because the stage had no length to spend
        self.rejects = {}                     # why candidates were dropped (diagnostics)
        slack = getattr(ctx.x, "polish_slack_mm", None)
        self.slack = max(0.0, float(CLEANUP_SLACK_MM if slack is None else slack))
        self.grow_cap = 0.0                   # when vias / necked length pay for clean-up: at most this much more length
        self.edits = {}
        self.tried = 0
        self.vias_removed = 0
        self.widened_mm = 0.0
        self.notes = []
        self._idx = {}                        # net -> _NetIndex
        self._hold = {}                       # (net id, layer, key) -> (static non-pad copper there, pad names)
        self._isl = {}                        # net -> pour islands
        self._padc = {}                       # (net id, layer, key) -> pads whose copper holds the point
        self._nver = {}                       # net -> edits made to it (invalidates cached connection views)
        self._cxc = {}                        # conn idx -> (net version, connection view)
        self._blocked = set()                 # connections that had a candidate refused by the oracle this round
        self._nl = {}                         # net -> holds a loosely joined connection
        self.loose = set()                    # connections that are joined only by KiCad's rule (end inside copper)
        self._passes = ()
        self._pass = ""
        self.regions = [r for r in (getattr(ctx.cfg, "regions", None) or ()) if getattr(r, "forbid", False)]
        self.conns = sorted(ctx.conns, key=lambda c: c.idx)
        self.by_net = {}
        for c in self.conns:
            self.by_net.setdefault(c.net, []).append(c)
        self.ok = set()                       # connections that are routed and connected (the only ones touched)

    # ── budget ────────────────────────────────────────────────────────────
    def _sync(self):
        d = self.oracle.checks - self._checks_seen
        if d >= ORACLE_CHECKS_PER_UNIT:
            self.budget.charge(d // ORACLE_CHECKS_PER_UNIT)
            self._checks_seen += (d // ORACLE_CHECKS_PER_UNIT) * ORACLE_CHECKS_PER_UNIT

    def _out(self) -> bool:
        self._sync()
        return self.budget.spent_out()

    def _try(self):
        self.tried += 1
        self.budget.charge(CHARGE_TRY)

    # ── net index ─────────────────────────────────────────────────────────
    def _index(self, net: str) -> _NetIndex:
        ni = self._idx.get(net)
        if ni is not None:
            return ni
        ni = self._idx[net] = _NetIndex()
        for p in sorted(self.state.pieces(net=net), key=lambda p: p.id):
            ni.add(p)
        L = self.board.L
        full = (1 << L) - 1

        def mask(layers):
            m = 0
            for l in layers or ():
                m |= 1 << l
            return m or full
        n = self.board.nets.get(net)
        for pad in (n.pads if n else ()):
            ni.point(q(pad.x), q(pad.y), mask(pad.layers), _STATIC, -1)
        for c in self.by_net.get(net, ()):
            for t in (c.a, c.b):
                ni.point(q(t.x), q(t.y), mask(t.layers), _STATIC, -1)
        for t in getattr(self.board, "kept_tracks", ()):
            if t[0] == net:
                ni.point(q(t[2]), q(t[3]), 1 << t[1], _STATIC, -1)
                ni.point(q(t[4]), q(t[5]), 1 << t[1], _STATIC, -1)
        for v in getattr(self.board, "kept_vias", ()):
            if v[0] == net:
                ni.point(q(v[1]), q(v[2]), -1, _STATIC, -1)
        return ni

    def _touching(self, cx: _Cx, layer: int, x: float, y: float, hw: float, skip=()) -> bool:
        """Does anything of the net that is not this connection's own chain attach at (x, y)? Static points
        (pad centres, terminals, kept copper ends) exactly there; ends of other connections inside the copper
        (radius ``hw``); segments whose centre line (own) or copper (other connections) holds the point.
        ``layer`` -1: a via, every layer. Connections found are added to the watch set."""
        ni, me, watch = cx.ni, cx.me, cx.wset
        bit = -1 if layer < 0 else (1 << layer)
        cx0, cy0 = _cell(x, y)
        tol = hw if (cx.tolerant and hw > EPS) else EPS
        hit = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for px, py, m, oc, _pid in ni.pb.get((cx0 + dx, cy0 + dy), ()):
                    if oc == me or not (m & bit):
                        continue
                    d = math.hypot(px - x, py - y)
                    if d > (tol if oc >= 0 else EPS):
                        continue
                    if d > EPS and layer >= 0 and self._in_static(cx, layer, px, py):
                        continue                  # a loose neighbour that a pad (or kept copper) holds anyway
                    hit = True
                    if oc >= 0:
                        watch.add(oc)
        for lay in (range(self.board.L) if layer < 0 else (layer,)):
            for e in ni.sb.get((lay, (cx0, cy0)), ()):
                if e[0] in skip:
                    continue
                d = geom.pt_seg_dist(x, y, e[2], e[3], e[4], e[5])
                if e[1] == me:
                    if layer >= 0 and d <= EPS and math.hypot(x - e[2], y - e[3]) > EPS \
                            and math.hypot(x - e[4], y - e[5]) > EPS:
                        hit = True
                elif d <= (e[6] if (cx.tolerant and e[6] > EPS) else EPS):
                    hit = True
                    if e[1] >= 0:
                        watch.add(e[1])
        return hit

    def _net_loose(self, net: str) -> bool:
        v = self._nl.get(net)
        if v is None:
            v = self._nl[net] = any(c.idx in self.loose for c in self.by_net.get(net, ()))
        return v

    def _in_static(self, cx: _Cx, layer: int, x: float, y: float) -> bool:
        """Is (x, y) inside any static copper of the net (pad, kept copper, pour island) on ``layer``?"""
        self._static_hold(cx, layer, x, y)
        v = self._hold[(cx.nid, layer, _k(x, y))]
        return v[0] or bool(v[1])

    def _islands(self, net: str) -> list:
        """Pour islands of the net: [(layer, bbox, points)] (an end inside one is joined to its cluster)."""
        v = self._isl.get(net)
        if v is None:
            v = self._isl[net] = []
            for _cid, c in sorted((getattr(self.board, "clusters", None) or {}).get(net, {}).items()):
                for lay, pts in c.get("islands") or ():
                    v.append((lay, geom.poly_bbox(pts), pts))
        return v

    def _static_hold(self, cx: _Cx, layer: int, x: float, y: float) -> bool:
        """Is (x, y) inside static copper of the connection's own net that is not one of its terminal pads?
        (Other connections may rely on copper touching it there, so the point stays where it is.)"""
        k = (cx.nid, layer, _k(x, y))
        v = self._hold.get(k)
        if v is None:
            other, names = False, []
            for s in self.oracle.near(layer, (x, y, x, y), 0.0):
                if s.cls != "cu" or s.net != cx.nid or not cx.nid or isinstance(s.tag, int):
                    continue
                if s.dist_pt(x, y) > 1e-6:
                    continue
                nm = getattr(s.tag, "name", None)
                if nm is None:
                    other = True
                else:
                    names.append(nm)
            if not other:
                for lay, bb, pts in self._islands(cx.net):
                    if lay == layer and bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] and geom.pt_in_poly(pts, x, y):
                        other = True
                        break
            v = self._hold[k] = (other, tuple(names))
        if v[0]:
            return True
        for nm in v[1]:
            if nm not in cx.own_pads:
                return True
        return False

    def _pad_at(self, cx: _Cx, layer: int, pt):
        """The same-net pad whose copper on ``layer`` contains ``pt`` (centre match, then own terminal, then first)."""
        x, y = pt
        kp = _k(x, y)
        ck = (cx.nid, layer, kp)
        found = self._padc.get(ck)
        if found is None:
            found = self._padc[ck] = []
            for s in self.oracle.near(layer, (x, y, x, y), 0.0):
                if s.cls != "cu" or s.net != cx.nid or not cx.nid or isinstance(s.tag, int):
                    continue
                if getattr(s.tag, "name", None) is None or s.dist_pt(x, y) > 1e-6:
                    continue
                if s.tag not in found:
                    found.append(s.tag)
        if not found:
            return None
        for pad in found:
            if _k(pad.x, pad.y) == kp:
                return pad
        for pad in found:
            if pad.name in cx.own_pads:
                return pad
        return found[0]

    # ── connection view ───────────────────────────────────────────────────
    def _build(self, conn, memo=None) -> _Cx:
        ver = self._nver.get(conn.net, 0)
        hit = self._cxc.get(conn.idx)
        if hit is not None and hit[0] == ver:         # nothing of this net changed since: the view is still exact
            cx = hit[1]
            cx.memo = memo if memo is not None else set()
            return cx
        self.budget.charge(CHARGE_BUILD)
        cx = _Cx()
        self._cxc[conn.idx] = (ver, cx)
        cx.conn, cx.me, cx.net = conn, conn.idx, conn.net
        cx.nid = self.oracle.net_id(conn.net)
        cx.clr = conn.clearance if conn.clearance and conn.clearance > 0 else None
        cx.memo = memo if memo is not None else set()
        cx.own_pads = tuple(t.pad for t in (conn.a, conn.b) if t.pad)
        cx.tolerant = self._net_loose(conn.net)
        ni = cx.ni = self._index(conn.net)
        me = cx.me
        cx.segs, cx.vias = _conn_parts(self.state.pieces(conn=me))
        watch = cx.wset = set()
        frozen = set()
        pb = ni.pb
        for p, _ka, _kb in cx.segs:
            if p.protected:
                frozen.add(p.id)
                continue
            x1, y1, x2, y2 = q(p.x1), q(p.y1), q(p.x2), q(p.y2)
            bit = 1 << p.layer
            hw = p.w / 2
            hit = False
            for c in _seg_cells(x1, y1, x2, y2, hw):      # order-free: only booleans and a set come out
                for px, py, m, oc, pid in pb.get(c, ()):
                    if pid == p.id or not (m & bit):
                        continue
                    # joins are on centre lines; on a net with loosely joined (v1) copper an end of another
                    # connection also attaches wherever it lies inside this copper, unless a pad holds it anyway
                    tol = hw if (cx.tolerant and oc >= 0 and oc != me and hw > EPS) else EPS
                    d = geom.pt_seg_dist(px, py, x1, y1, x2, y2)
                    if d > tol or (d > EPS and self._in_static(cx, p.layer, px, py)):
                        continue
                    if oc >= 0 and oc != me:
                        watch.add(oc)
                    if math.hypot(px - x1, py - y1) > tol and math.hypot(px - x2, py - y2) > tol:
                        hit = True
            if hit:
                frozen.add(p.id)
        for v in cx.vias.values():
            self._touching(cx, -1, q(v.x1), q(v.y1), v.w / 2)       # fills the watch set
        cx.frozen = frozen
        vias = cx.vias
        brk = {}

        def is_break(layer, key, lst):
            node = (layer, key)
            v = brk.get(node)
            if v is not None:
                return v
            v = True
            while True:
                if len(lst) != 2 or key in vias:
                    break
                p1, p2 = lst[0][0], lst[1][0]
                if p1.id in frozen or p2.id in frozen:
                    break
                if abs(p1.w - p2.w) > 1e-9 or abs(p1.target_w - p2.target_w) > 1e-9:
                    break
                x, y = key[0] / 1e6, key[1] / 1e6
                if self._touching(cx, layer, x, y, p1.w / 2, (p1.id, p2.id)):
                    break
                if self._static_hold(cx, layer, x, y):
                    break
                v = False
                break
            brk[node] = v
            return v

        raw, adj = _walk(cx.segs, is_break, frozen)
        cx.adj = adj
        cx.watch = sorted(c for c in watch if c in self.ok)
        cx.chains = []
        for layer, keys, pieces in raw:
            ch = _Chain()
            ch.layer = layer
            ch.pieces = pieces
            ch.w, ch.tw = pieces[0].w, pieces[0].target_w
            pts = []
            for i, p in enumerate(pieces):
                a = (q(p.x1), q(p.y1))
                b = (q(p.x2), q(p.y2))
                if _k(*a) != keys[i]:
                    a, b = b, a
                if not pts:
                    pts.append(a)
                pts.append(b)
            ch.pts = pts
            ch.before = self._beyond(adj, layer, keys[0], pieces[0])
            ch.after = self._beyond(adj, layer, keys[-1], pieces[-1])
            cx.chains.append(ch)
        return cx

    @staticmethod
    def _beyond(adj, layer, key, piece):
        """The far point of the one other segment at a chain end (for bend counting), or None."""
        lst = adj.get((layer, key), ())
        if len(lst) != 2:
            return None
        o = lst[1][0] if lst[0][0].id == piece.id else lst[0][0]
        a, b = (q(o.x1), q(o.y1)), (q(o.x2), q(o.y2))
        return b if _k(*a) == key else a

    # ── checks shared by the passes ───────────────────────────────────────
    def _forbidden(self, net: str, layer: int, pts) -> bool:
        for r in self.regions:
            if not r.applies(net) or (r.layers is not None and layer not in r.layers):
                continue
            x0, y0, x1, y1 = r.rect
            cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2
            for i in range(len(pts) - 1):
                if geom.seg_rect_dist(pts[i][0] - cx, pts[i][1] - cy, pts[i + 1][0] - cx, pts[i + 1][1] - cy, hw, hh) <= 0:
                    return True
        return False

    def _soft(self, net, layer, pts, w) -> set:
        out = set()
        for i in range(len(pts) - 1):
            for b in self.oracle.soft_hits(layer, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], w, net):
                out.add(b.tag)
        return out

    def _legal(self, cx: _Cx, layer: int, pts, w: float, skip=frozenset()) -> bool:
        seg_legal = self.oracle.seg_legal
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            if (a, b) in skip:
                continue
            if not seg_legal(layer, a[0], a[1], b[0], b[1], w, cx.net, cx.clr):
                return False
        return not self._forbidden(cx.net, layer, pts)

    def _same_side(self, cx: _Cx, layer: int, hw: float, old, new) -> bool:
        """Keep-topology test: no foreign shape lies in the loop between the old and the new path."""
        loop = _dedupe(list(old) + list(new)[::-1])
        if len(loop) > 1 and loop[0] == loop[-1]:
            loop.pop()
        if len(loop) < 3:
            return True
        xs = [p[0] for p in loop]
        ys = [p[1] for p in loop]
        bb = (min(xs), min(ys), max(xs), max(ys))
        if (bb[2] - bb[0]) < EPS or (bb[3] - bb[1]) < EPS:
            return True
        need = self.oracle.need
        for s in self.oracle.near(layer, bb, 0.0):
            if s.kind == "box":
                px, py = s.g[0], s.g[1]
            else:
                px, py = s.g[0]
            if px < bb[0] or px > bb[2] or py < bb[1] or py > bb[3]:
                continue
            if need(cx.net, hw, s, layer, clearance=cx.clr) == NO_NEED:
                continue
            if geom.pt_in_poly(loop, px, py):
                return False
        return True

    def _connected(self, idx: int) -> bool:
        if idx in self.loose:
            return self.state.connected(idx, True)
        return self.state.connected(idx)

    def _apply(self, cx: _Cx, old: list, specs: list):
        """Remove ``old`` and add ``specs`` in one transaction; keep it only when the connection and its
        dependants stay connected. -> the added pieces, or None."""
        st = self.state
        self.budget.charge(CHARGE_EDIT)
        tok = st.begin()
        try:
            for p in old:
                st.remove(p.id)
            added = [st.add(**s) for s in specs]
            good = self._connected(cx.me) and all(self._connected(c) for c in cx.watch)
        except Exception:
            st.rollback(tok)
            raise
        if not good:
            st.rollback(tok)
            return None
        st.commit(tok)
        self._nver[cx.net] = self._nver.get(cx.net, 0) + 1
        for p in old:
            cx.ni.remove(p)
        for p in added:
            cx.ni.add(p)
        self.edits[self._pass] = self.edits.get(self._pass, 0) + 1
        return added

    def _spec(self, cx: _Cx, layer, a, b, w, tw) -> dict:
        return {"net": cx.net, "conn": cx.me, "kind": "seg", "layer": layer, "x1": a[0], "y1": a[1], "x2": b[0],
                "y2": b[1], "w": w, "stage": "polish", "target_w": tw}

    def _gate(self, d, cleanup: bool, fewer: bool):
        """May an edit with Score delta ``d`` be made? True / False; None = refused only for lack of banked length."""
        s = _lex(d)
        if s < 0 or (s == 0 and (cleanup or fewer)):
            return True
        if not cleanup:
            return False
        tot = [self.bank[i] + d[i] for i in range(4)]
        if _lex(tot, 1e-6) < 0 and tot[2] <= self.grow_cap:
            return True                       # paid for by what the stage has saved so far
        if self.slack > 0 and tot[0] <= 1e-9 and tot[1] <= 0 and tot[2] <= self.slack - 1e-6:
            return True
        return None

    def _no(self, cx: _Cx, mk, why: str) -> bool:
        cx.memo.add(mk)
        self.rejects[why] = self.rejects.get(why, 0) + 1
        return False

    def _replace(self, cx: _Cx, ch: _Chain, i: int, j: int, pts, force: bool = False) -> bool:
        """Replace the points i..j of a chain (pieces i..j-1) by the polyline ``pts``; the chain is updated in
        place on success. Applies every check of the module docstring."""
        new = _dedupe(pts)
        if len(new) < 2:
            return False
        old = ch.pts[i:j + 1]
        if new == old:
            return False
        mk = (ch.layer, ch.w, tuple(old), tuple(new))
        if mk in cx.memo:
            return False
        n = len(ch.pieces)
        pre = [ch.pts[i - 1]] if i > 0 else ([ch.before] if ch.before is not None else [])
        post = [ch.pts[j + 1]] if j < n else ([ch.after] if ch.after is not None else [])
        eo, en = pre + old, pre + new
        eo += post
        en += post
        if _sharp(en) > _sharp(eo):
            return self._no(cx, mk, "acute")
        lo, ln = _plen(old), _plen(new)
        necked = ch.w < ch.tw - 1e-9
        d = ((ln - lo) if necked else 0.0, 0, ln - lo, _bends(en) - _bends(eo))
        qo, qn = _quality(old), _quality(new)
        # a clean-up never adds blemishes: no more odd angles and no more odd-or-short segments than before
        cleanup = qn[0] <= qo[0] and qn[1] <= qo[1] and (qn != qo or force)
        g = self._gate(d, cleanup, len(new) < len(old))
        if not g:
            if g is None:
                self.deferred += 1
                return False
            return self._no(cx, mk, "score")
        self._try()
        layer, w = ch.layer, ch.w
        skip = {(old[t], old[t + 1]) for t in range(len(old) - 1)}
        if not self._legal(cx, layer, new, w, skip):
            self._blocked.add(cx.me)
            return self._no(cx, mk, "illegal")
        if self._soft(cx.net, layer, new, w) - self._soft(cx.net, layer, old, w):
            return self._no(cx, mk, "soft")
        if not self._same_side(cx, layer, w / 2, old, new):
            return self._no(cx, mk, "topology")
        specs = [self._spec(cx, layer, new[t], new[t + 1], w, ch.tw) for t in range(len(new) - 1)]
        added = self._apply(cx, ch.pieces[i:j], specs)
        if added is None:
            return self._no(cx, mk, "connectivity")
        ch.pts[i:j + 1] = new
        ch.pieces[i:j] = added
        for t in range(4):
            self.bank[t] += d[t]
        return True

    # ── tidy: dangling copper and collinear joints (start of every geometry pass) ──
    def _free_end(self, cx: _Cx, ch: _Chain, pt) -> bool:
        """Does the chain end at ``pt`` touch nothing at all (no pad, via, terminal, tee, other copper)?"""
        key = _k(*pt)
        layer = ch.layer
        if len(cx.adj.get((layer, key), ())) != 1 or key in cx.vias:
            return False
        if self._touching(cx, layer, pt[0], pt[1], ch.w / 2, {p.id for p in ch.pieces}):
            return False
        return self._pad_at(cx, layer, pt) is None and not self._static_hold(cx, layer, pt[0], pt[1])

    def _prune_once(self, cx: _Cx) -> bool:
        """Remove one chain that leads nowhere: a closed loop hanging off one point, or a dead end."""
        for ch in cx.chains:
            mk = ("prune", ch.pieces[0].id, ch.pieces[-1].id)
            if mk in cx.memo:
                continue
            loop = _k(*ch.pts[0]) == _k(*ch.pts[-1])
            if not loop and not self._free_end(cx, ch, ch.pts[0]) and not self._free_end(cx, ch, ch.pts[-1]):
                continue
            cx.memo.add(mk)
            self._try()
            if self._apply(cx, list(ch.pieces), []) is None:
                continue
            L = _plen(ch.pts)
            if ch.w < ch.tw - 1e-9:
                self.bank[0] -= L
            self.bank[2] -= L
            self.bank[3] -= _bends(ch.pts)
            return True
        return False

    def _merge(self, cx: _Cx, ch: _Chain) -> int:
        """Join collinear neighbours into one segment."""
        n, t = 0, 1
        P = ch.pts
        keep = [P[0]]
        for i in range(1, len(P) - 1):
            tr = _turn(keep[-1], P[i], P[i + 1])
            if not (tr is not None and abs(tr[0]) <= 1e-6 and tr[1] > 0):
                keep.append(P[i])
        keep.append(P[-1])
        if len(keep) == len(P):
            return 0
        if self._replace(cx, ch, 0, len(ch.pieces), keep):       # every collinear joint in one transaction
            return len(P) - len(keep)
        while t < len(ch.pieces) and not self._out():
            tr = _turn(P[t - 1], P[t], P[t + 1])
            if tr is not None and abs(tr[0]) <= 1e-6 and tr[1] > 0 and self._replace(cx, ch, t - 1, t + 1,
                                                                                    [P[t - 1], P[t + 1]]):
                n += 1
            else:
                t += 1
        return n

    def _tidy(self, conn, memo=None):
        """-> (fresh connection view, edits made): dead ends and loops removed, collinear joints merged."""
        memo = set() if memo is None else memo
        n = 0
        while True:
            cx = self._build(conn, memo)
            if self._out() or not self._prune_once(cx):
                break
            n += 1
        for ch in cx.chains:
            if self._out():
                break
            n += self._merge(cx, ch)
        return cx, n

    # ── pass: pad entry ───────────────────────────────────────────────────
    def _p_entry(self, conn) -> int:
        cx = self._build(conn)
        n = 0
        for ch in cx.chains:
            for end in (0, 1):
                if self._out():
                    return n
                if self._entry_end(cx, ch, end):
                    n += 1
        return n

    def _entry_end(self, cx: _Cx, ch: _Chain, end: int) -> bool:
        P = ch.pts
        npc = len(ch.pieces)
        E = P[0] if end == 0 else P[-1]
        ke = _k(*E)
        layer = ch.layer
        if len(cx.adj.get((layer, ke), ())) != 1 or ke in cx.vias:
            return False
        pad = self._pad_at(cx, layer, E)
        if pad is None:
            return False
        T = (q(pad.x), q(pad.y))
        if _k(*T) != ke:
            inside = any(s.dist_pt(T[0], T[1]) < -1e-6 for s in pad.shapes.get(layer, ()))
            held = self._touching(cx, layer, E[0], E[1], ch.w / 2, {p.id for p in ch.pieces})
            if not inside or held:
                T = E
        moved = _k(*T) != ke
        nxt = P[1] if end == 0 else P[-2]
        dx, dy = nxt[0] - E[0], nxt[1] - E[1]
        if not moved and _is45(dx, dy) and not (math.hypot(dx, dy) < MIN_SEG - 1e-9 and npc >= 2):
            return False
        top = min(npc, ENTRY_SPAN)
        for k in list(range(2, top + 1)) + [1]:   # a longer run first: one L instead of two stubs
            if k > top:
                continue
            tgt = P[k] if end == 0 else P[npc - k]
            for pp in _postures(T, tgt):
                if end == 0:
                    done = self._replace(cx, ch, 0, k, pp, force=True)
                else:
                    done = self._replace(cx, ch, npc - k, npc, pp[::-1], force=True)
                if done:
                    return True
        return False

    # ── pass: pull-tight ──────────────────────────────────────────────────
    def _p_tight(self, conn) -> int:
        cx, n = self._tidy(conn)
        for ch in cx.chains:
            n += self._tight_chain(cx, ch)
        return n

    def _tight_chain(self, cx: _Cx, ch: _Chain) -> int:
        edits = 0
        while not self._out():
            n = len(ch.pieces)
            if n < 2:
                break
            P = ch.pts
            cum, bad, bnd = [0.0], [0], [0, 0]
            for t in range(n):
                dx, dy = P[t + 1][0] - P[t][0], P[t + 1][1] - P[t][1]
                L = math.hypot(dx, dy)
                cum.append(cum[-1] + L)
                bad.append(bad[-1] + (0 if (_is45(dx, dy) and L >= MIN_SEG - 1e-9) else 1))
            for t in range(1, n):
                tr = _turn(P[t - 1], P[t], P[t + 1])
                bnd.append(bnd[-1] + (1 if tr is not None and (abs(tr[0]) > 1e-6 or tr[1] < 0) else 0))
            # bnd[t + 1] = bends at interior points 1..t
            done = False
            for span in range(min(n, MAX_SPAN), 1, -1):
                for i in range(0, n - span + 1):
                    j = i + span
                    a, b = P[i], P[j]
                    adx, ady = abs(b[0] - a[0]), abs(b[1] - a[1])
                    octile = (adx + ady) - (2.0 - _SQ2) * (adx if adx < ady else ady)
                    if cum[j] - cum[i] - octile < 1e-7 and bad[j] == bad[i] and bnd[j] - bnd[i + 1] < 2:
                        continue                  # already tight, clean, and at most one bend: nothing to gain
                    for pts in _postures(a, b):
                        if self._replace(cx, ch, i, j, pts):
                            done = True
                            break
                    if done:
                        break
                if done or self._out():
                    break
            if not done:
                break
            edits += 1
        return edits

    # ── pass: staircases, odd angles, short interior segments ─────────────
    def _p_stairs(self, conn) -> int:
        cx, n = self._tidy(conn)
        for ch in cx.chains:
            while not self._out() and self._stairs_once(cx, ch):
                n += 1
        return n

    def _stairs_once(self, cx: _Cx, ch: _Chain) -> bool:
        P = ch.pts
        n = len(ch.pieces)
        for k in range(n):
            dx, dy = P[k + 1][0] - P[k][0], P[k + 1][1] - P[k][1]
            L = math.hypot(dx, dy)
            odd = not _is45(dx, dy)
            inner = 0 < k < n - 1
            cands = []
            if inner and L < STAIR_MM and _bends(P[k - 1:k + 3]) == 2:
                for pts in _postures(P[k - 1], P[k + 2]):
                    cands.append((k - 1, k + 2, pts))
                cands += self._jogs(P, k, L)
            if odd or (L < MIN_SEG - 1e-9 and n > 1):
                runs = [(k - 1, k + 2), (k - 1, k + 1), (k, k + 2)]
                if odd:
                    runs.append((k, k + 1))
                for i, j in runs:
                    if i < 0 or j > n:
                        continue
                    for pts in _postures(P[i], P[j]):
                        cands.append((i, j, pts))
            for i, j, pts in cands:
                if self._replace(cx, ch, i, j, pts):
                    return True
        return False

    @staticmethod
    def _jogs(P, k, h):
        """A 90-degree jog between two parallel segments -> the two 45-degree jogs (when the full L is blocked)."""
        a, b, c, d = P[k - 1], P[k], P[k + 1], P[k + 2]
        t1, t2 = _turn(a, b, c), _turn(b, c, d)
        if t1 is None or t2 is None or abs(t1[1]) > 1e-6 or abs(t2[1]) > 1e-6 or t1[0] * t2[0] > 0:
            return []
        l1, l2 = math.hypot(b[0] - a[0], b[1] - a[1]), math.hypot(d[0] - c[0], d[1] - c[1])
        ux, uy = (b[0] - a[0]) / l1, (b[1] - a[1]) / l1
        out = []
        if l2 >= h - EPS:
            out.append((k - 1, k + 2, [a, b, (q(c[0] + ux * h), q(c[1] + uy * h)), d]))
        if l1 >= h - EPS:
            out.append((k - 1, k + 2, [a, (q(b[0] - ux * h), q(b[1] - uy * h)), c, d]))
        return out

    # ── pass: via removal ─────────────────────────────────────────────────
    def _p_vias(self, conn) -> int:
        memo = set()
        n = 0
        if not any(p.kind == "via" for p in self.state.pieces(conn=conn.idx)):
            return 0
        while not self._out():
            cx, m = self._tidy(conn, memo)
            n += m
            if not cx.vias or not self._via_once(cx):
                break
            n += 1
        if n:
            for follow in ("tight", "stairs"):
                if follow in self._passes and not self._out():
                    getattr(self, "_p_" + follow)(conn)
        return n

    def _layer_ok(self, net: str, layer: int) -> bool:
        nc = (getattr(self.ctx.cfg, "nets", None) or {}).get(net)
        ok = getattr(nc, "layers_ok", None) if nc is not None else None
        return ok is None or layer in ok

    def _via_once(self, cx: _Cx) -> bool:
        adj = cx.adj
        layers_at = {}
        for layer, key in sorted(adj):
            layers_at.setdefault(key, []).append(layer)
        for kv, V in sorted(cx.vias.items(), key=lambda e: e[1].id):
            if V.protected or self._touching(cx, -1, q(V.x1), q(V.y1), V.w / 2):
                continue
            lv = layers_at.get(kv, [])
            if len(lv) != 2:
                continue
            for ls, le in ((lv[0], lv[1]), (lv[1], lv[0])):
                if len(adj[(ls, kv)]) != 1 or not self._layer_ok(cx.net, le):
                    continue
                ch = None
                for c in cx.chains:
                    if c.layer == ls and (_k(*c.pts[0]) == kv or _k(*c.pts[-1]) == kv):
                        ch = c
                        break
                if ch is None:
                    continue
                pts = ch.pts if _k(*ch.pts[0]) == kv else ch.pts[::-1]
                kb = _k(*pts[-1])
                if kb == kv:
                    continue
                drop = [V]
                far = None                      # the entry-layer point beyond B, when its via goes too
                W = cx.vias.get(kb)
                if W is not None:
                    if len(adj[(ls, kb)]) != 1:
                        continue
                    rest = [l for l in layers_at.get(kb, []) if l != ls]
                    held = W.protected or self._touching(cx, -1, q(W.x1), q(W.y1), W.w / 2)
                    if not held and rest and all(l == le for l in rest):
                        drop.append(W)
                        if len(adj[(le, kb)]) == 1:
                            far = self._beyond_pt(adj[(le, kb)][0][0], kb)
                elif self._pad_at(cx, le, pts[-1]) is None or self._pad_at(cx, ls, pts[-1]) is None:
                    continue
                near = self._beyond_pt(adj[(le, kv)][0][0], kv) if len(adj[(le, kv)]) == 1 else None
                if self._move_span(cx, ch, pts, le, drop, near, far):
                    return True
        return False

    @staticmethod
    def _beyond_pt(piece, key):
        a, b = (q(piece.x1), q(piece.y1)), (q(piece.x2), q(piece.y2))
        return b if _k(*a) == key else a

    def _move_span(self, cx: _Cx, ch: _Chain, pts, le: int, drop: list, near, far) -> bool:
        a, b = pts[0], pts[-1]
        lo = _plen(pts)
        necked = ch.w < ch.tw - 1e-9
        bo = _bends(pts)
        for cand in _postures(a, b) + _zs(a, b) + [list(pts)]:
            new = _dedupe(cand)
            mk = ("via", drop[0].id, le, tuple(new))
            if len(new) < 2 or mk in cx.memo:
                continue
            cx.memo.add(mk)
            ln = _plen(new)
            if ln >= VIA_GROW * lo:
                continue
            ext = ([near] if near is not None else []) + new + ([far] if far is not None else [])
            d = ((ln - lo) if necked else 0.0, -len(drop), ln - lo, _bends(ext) - bo)
            if _lex(d) >= 0 or _sharp(ext) > 0:
                continue
            self._try()
            if not self._legal(cx, le, new, ch.w) or self._soft(cx.net, le, new, ch.w):
                continue
            specs = [self._spec(cx, le, new[t], new[t + 1], ch.w, ch.tw) for t in range(len(new) - 1)]
            if self._apply(cx, list(ch.pieces) + drop, specs) is None:
                continue
            for t in range(4):
                self.bank[t] += d[t]
            self.vias_removed += len(drop)
            return True
        return False

    # ── pass: re-widen ────────────────────────────────────────────────────
    def _p_widen(self, conn) -> int:
        if not any(p.kind == "seg" and p.w < p.target_w - 1e-9 for p in self.state.pieces(conn=conn.idx)):
            return 0
        cx = self._build(conn)
        n = 0
        for p, _ka, _kb in list(cx.segs):
            if p.w >= p.target_w - 1e-9 or p.protected:
                continue
            if self._out():
                break
            if self._widen_piece(cx, p):
                n += 1
        return n

    def _best_width(self, legal, a, b, w: float, tw: float) -> float:
        """Widest legal width in [w, tw] on the WIDEN_STEP ladder above w (w itself is taken as legal)."""
        if legal(a, b, tw):
            return tw
        lo, hi = 0, int(math.floor((tw - w) / WIDEN_STEP + 1e-9))
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if legal(a, b, q(w + mid * WIDEN_STEP)):
                lo = mid
            else:
                hi = mid - 1
        return q(w + lo * WIDEN_STEP)

    def _widen_piece(self, cx: _Cx, p: Piece) -> bool:
        seg_legal = self.oracle.seg_legal
        lay, net, clr = p.layer, cx.net, cx.clr
        A, B = (q(p.x1), q(p.y1)), (q(p.x2), q(p.y2))
        w, tw = p.w, p.target_w
        self._try()

        def legal(a, b, width):
            return seg_legal(lay, a[0], a[1], b[0], b[1], width, net, clr) and not self._forbidden(net, lay, (a, b))

        dx, dy = B[0] - A[0], B[1] - A[1]
        L = math.hypot(dx, dy)
        minrun = max(2.0 * tw, 0.3)
        parts = None
        if legal(A, B, tw):
            parts = [(A, B, tw)]
        elif p.id not in cx.frozen and _is45(dx, dy) and L >= 2.0 * minrun:
            adx, ady = abs(dx), abs(dy)
            U = adx if adx > ady else ady
            sx = math.copysign(1.0, dx) if adx > EPS else 0.0
            sy = math.copysign(1.0, dy) if ady > EPS else 0.0
            scale = L / U
            res = WIDEN_RES / scale

            def pt(u):
                if u <= 0.0:
                    return A
                if u >= U:
                    return B
                return (q(A[0] + sx * u), q(A[1] + sy * u))

            iv = []

            def split(u0, u1):
                if legal(pt(u0), pt(u1), tw):
                    iv.append([u0, u1, True])
                elif u1 - u0 <= res:
                    iv.append([u0, u1, False])
                else:
                    m = q((u0 + u1) / 2)
                    split(u0, m)
                    split(m, u1)

            split(0.0, U)

            def merged(lst):
                out = []
                for e in lst:
                    if out and out[-1][2] == e[2]:
                        out[-1][1] = e[1]
                    else:
                        out.append(list(e))
                return out

            iv = merged(iv)
            for e in iv:
                if e[2] and (e[1] - e[0]) * scale < minrun:
                    e[2] = False
            iv = merged(iv)
            if any(e[2] for e in iv):
                parts = []
                for u0, u1, wide in iv:
                    a, b = pt(u0), pt(u1)
                    width = self._best_width(legal, a, b, w, tw)
                    if parts and abs(parts[-1][2] - width) < 1e-9:
                        parts[-1] = (parts[-1][0], b, width)
                    else:
                        parts.append((a, b, width))
        if parts is None:
            bw = self._best_width(legal, A, B, w, tw)
            parts = [(A, B, bw)]
        if all(pw <= w + 1e-9 for _a, _b, pw in parts):
            return False
        if len(parts) > 1 and not all(pw <= w + 1e-9 or legal(a, b, pw) for a, b, pw in parts):
            return False
        still = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b, pw in parts if pw < tw - 1e-9)
        specs = [self._spec(cx, lay, a, b, pw, tw) for a, b, pw in parts]
        if self._apply(cx, [p], specs) is None:
            return False
        self.bank[0] += still - L
        self.widened_mm += L - still
        return True

    # ── driver ────────────────────────────────────────────────────────────
    def _sweep(self, name: str) -> int:
        self._pass = name
        fn = getattr(self, "_p_" + name)
        total = 0
        only = None
        tuned = {p.conn for p in self.state.pieces() if p.stage == "tune"} | _coupled(self.ctx)
        for _round in range(MAX_ROUNDS):
            changed = 0
            self._blocked = set()
            for conn in self.conns:
                if self._out():
                    break
                if conn.idx not in self.ok or conn.role == "pair" or (only is not None and conn.idx not in only):
                    continue
                if conn.idx in tuned:
                    continue                # carries length-tuning meanders (pulling it tight would undo them),
                    #                         or is a lane of a coupled differential pair (left exactly as laid)
                changed += fn(conn)
            total += changed
            # again only for connections the oracle held back: the copper in their way may have moved meanwhile
            only = self._blocked
            if not changed or not only or self._out():
                break
        return total

    def _worse(self, a, b) -> bool:
        """Score a worse than b, floats compared at 1 nm (sums of lengths carry float noise); with a clean-up
        slack, up to that much more length is not worse."""
        ka = (a.new_errors, a.opens, round(a.necked_mm, 6), a.vias)
        kb = (b.new_errors, b.opens, round(b.necked_mm, 6), b.vias)
        if ka != kb:
            return ka > kb
        la, lb = round(a.length_mm, 6), round(b.length_mm, 6)
        if la != lb:
            return la > lb + self.slack
        return a.bends > b.bends

    def run(self, passes) -> StageReport:
        st = self.state
        for name in passes:
            if name not in PASSES:
                raise ValueError(f"unknown polish pass {name!r}; passes are {PASSES}")
        self._passes = tuple(passes)
        try:
            has_loose = "loose" in inspect.signature(st.connected).parameters
        except (TypeError, ValueError):
            has_loose = False
        for c in self.conns:
            if st.status(c.idx) != "routed":
                continue
            if st.connected(c.idx):
                self.ok.add(c.idx)
            elif has_loose and st.connected(c.idx, True):   # v1 copper: ends inside pads / tracks, not on centre lines
                self.ok.add(c.idx)
                self.loose.add(c.idx)
        score0 = st.score()
        self.grow_cap = GROW_CAP * score0.length_mm
        todo = list(passes)
        retried = False
        while todo:
            name = todo.pop(0)
            if self._out():
                self.notes.append(f"budget spent before pass '{name}'")
                break
            snap = st.snapshot()
            bank0 = list(self.bank)
            counters = (self.vias_removed, self.widened_mm, dict(self.edits))
            n = self._sweep(name)
            if n:
                lost = [c for c in sorted(self.ok) if not self._connected(c)]
                sc = st.score()
                if lost or self._worse(sc, score0):
                    st.restore(snap)
                    self.bank = bank0
                    self.vias_removed, self.widened_mm, self.edits = counters
                    self._idx.clear()
                    self._cxc.clear()
                    why = f"{len(lost)} connection(s) lost" if lost else "the score got worse"
                    self.notes.append(f"pass '{name}' undone: {why}")
            if not todo and not retried and self.deferred and _lex(self.bank) < 0:
                # clean-up edits that were refused for lack of banked length: once more, now that there is some
                retried = True
                self.deferred = 0
                todo = [p for p in ("entry", "stairs") if p in passes]
        self._sync()
        sc = st.score()
        routed = sum(1 for c in self.conns if st.status(c.idx) == "routed")
        failed = sum(1 for c in self.conns if st.status(c.idx) == "failed")
        total = sum(self.edits.values())
        if self.deferred:
            self.notes.append(f"{self.deferred} clean-up edit(s) not made: they would have cost more length than "
                              "the stage saved")
        return StageReport(
            name="polish", routed=routed, failed=failed, score=sc, work=self.budget.used - self.used0,
            notes=self.notes,
            data={"edits": dict(self.edits), "edits_total": total, "tried": self.tried,
                  "vias_removed": self.vias_removed, "widened_mm": round(self.widened_mm, 4),
                  "deferred": self.deferred, "rejects": dict(sorted(self.rejects.items())),
                  "slack_mm": self.slack, "loose": len(self.loose), "oracle_checks": self.oracle.checks - self.checks0,
                  "delta": {"necked_mm": round(self.bank[0], 6), "vias": self.bank[1],
                            "length_mm": round(self.bank[2], 6), "bends": self.bank[3]},
                  "score_before": score0.key(), "score_after": sc.key()})


def _coupled(ctx: Ctx) -> set:
    """Connections laid as coupled differential-pair lanes (``pairs.coupled_conns``): never re-shaped here."""
    h = ctx.history.get("pairs") if isinstance(getattr(ctx, "history", None), dict) else None
    if not h:
        return set()
    try:
        from . import pairs
        return pairs.coupled_conns(ctx)
    except Exception:  # noqa: BLE001
        return set()


def run(ctx: Ctx, budget: Budget,
        passes: tuple[str, ...] = ("entry", "tight", "stairs", "vias", "widen")) -> StageReport:
    """Polish every routed, connected connection of ``ctx.state`` (differential pairs excepted). Never changes
    connectivity, never returns a worse ``state.score()`` than it was given, never adds illegal copper."""
    return _Polisher(ctx, budget).run(tuple(passes))


# ── the via-frugal re-route (stage 6 integration) ─────────────────────────
REVIA_SCALE = 4.0        # via cost multiplier of the re-route
REVIA_GROW = 1.3         # the re-laid connection may be this much longer ...
REVIA_MM_PER_VIA = 3.0   # ... or this many mm longer per via it saves, whichever allows more
REVIA_CAP = 150_000      # work units one attempt may use (a failed attempt changes nothing)
REVIA_LATERAL_MIN, REVIA_LATERAL_MAX = 3.0, 15.0   # mm of window on every side of the terminals' box
REVIA_ROUNDS = 2         # sweeps (a connection re-laid without vias frees room for the next)


def revia(ctx: Ctx, budget: Budget, only=None) -> StageReport:
    """Re-lay routed connections that carry vias with the window search at ``REVIA_SCALE`` x the via cost and a
    hard limit of one via fewer than they have now (design §4.10 pass 4 asks for a window search at
    ``max_vias = 0`` per span; this is the same idea for the whole connection, with every count from 0 up to
    n - 1 allowed). One transaction per connection, kept only when

    * the connection is routed again with fewer vias, at most ``REVIA_GROW`` x / ``REVIA_MM_PER_VIA`` longer,
    * every connection of the net that was joined before is joined afterwards (other connections tee onto it),
    * ``state.score()`` is better than before (so nothing got necked or lost).

    Not touched: differential pairs, protected pieces (escapes, dog-bones stay where they are; only the
    connection's own copper is re-laid), nets the plan protects. Deterministic: most vias first, then idx."""
    from . import detail
    state = ctx.state
    rep = StageReport(name="vias")
    used0 = budget.used
    pick = None if only is None else set(only)
    saved = tried = 0
    notes = {}
    x = ctx.x

    def protected_net(net: str) -> bool:
        # a poured net's vias are its way to the pour: re-laid as track on a signal layer they take the room pour
        # pads need later (measured on one_air_max: 13 vias saved, 3 pour pads lost)
        n = ctx.board.nets.get(net) if hasattr(ctx.board, "nets") else None
        if n is not None and getattr(n, "has_pour", False):
            return True
        try:
            return bool(getattr(x.net(net), "protect", False))
        except Exception:  # noqa: BLE001
            return False

    def loose(i: int) -> bool:
        return state.connected(i) or state.connected(i, loose=True)

    coupled = _coupled(ctx)
    for _round in range(REVIA_ROUNDS):
        cands = []
        for c in ctx.conns:
            if (pick is not None and c.idx not in pick) or state.status(c.idx) != "routed" or c.role == "pair" \
                    or c.idx in coupled:
                continue
            ps = state.pieces(conn=c.idx)
            nv = sum(1 for p in ps if p.kind == "via" and not p.protected)
            if any(p.stage == "tune" for p in ps):
                continue                    # length-tuned (design §14.6): re-laying it would throw the meanders away
            if nv and not protected_net(c.net):
                cands.append((-nv, c.idx, c))
        cands.sort(key=lambda e: e[:2])
        gained = 0
        for neg, _i, c in cands:
            if budget.left() < CHARGE_BUILD + 6200:
                break
            nv0 = -neg
            ps = [p for p in state.pieces(conn=c.idx) if not p.protected]
            len0 = sum(p.length for p in ps)
            mates = [m.idx for m in ctx.conns if m.net == c.net and m.idx != c.idx and state.status(m.idx) == "routed"
                     and loose(m.idx)]
            if not loose(c.idx):
                continue
            score0 = state.score()
            tried += 1
            tok = state.begin()
            state.rip_conn(c.idx)
            sub = Budget(min(REVIA_CAP, budget.left()), parent=budget)
            try:
                # a path at most REVIA_GROW x as long stays inside an ellipse round the terminals: that much room
                lat = min(REVIA_LATERAL_MAX, max(REVIA_LATERAL_MIN, 0.5 * (REVIA_GROW ** 2 - 1.0) ** 0.5 * len0))
                r = detail.route_conn(ctx, c, sub, via_limit=nv0 - 1, via_scale=REVIA_SCALE, lateral=lat)
            except Exception:  # noqa: BLE001 — a failed attempt must change nothing
                r = {"ok": False}
            ok = bool(r.get("ok"))
            if ok:
                ps1 = [p for p in state.pieces(conn=c.idx) if not p.protected]
                nv1 = sum(1 for p in ps1 if p.kind == "via")
                len1 = sum(p.length for p in ps1)
                ok = (nv1 < nv0 and len1 <= max(REVIA_GROW * len0, len0 + REVIA_MM_PER_VIA * (nv0 - nv1)) + 1e-9
                      and loose(c.idx) and all(loose(m) for m in mates) and state.score() < score0)
            if ok:
                state.commit(tok)
                state.set_status(c.idx, "routed")
                saved += nv0 - nv1
                gained += nv0 - nv1
                notes[c.net] = notes.get(c.net, 0) + nv0 - nv1
            else:
                state.rollback(tok)
                if state.status(c.idx) != "routed":
                    state.set_status(c.idx, "routed")
        if not gained:
            break
    rep.work = budget.used - used0
    rep.routed = sum(1 for c in ctx.conns if state.status(c.idx) == "routed")
    rep.data = {"vias_removed": saved, "tried": tried, "by_net": dict(sorted(notes.items()))}
    if tried:
        rep.notes.append(f"via-frugal re-route: {saved} via(s) saved on {len(notes)} net(s), {tried} connection(s) tried")
    try:
        rep.score = state.score()
    except Exception:  # noqa: BLE001
        pass
    return rep
