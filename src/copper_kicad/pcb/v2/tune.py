"""S7t length tuning: serpentines on the straight runs of nets that are too short for their length group
(design §14.7).

    run(ctx, budget, only=None, cal=None) -> StageReport     tune every member of ``cons.tuned()`` groups
    untune(ctx, nets, avoid=None) -> int                     put the routed shape of these nets back
    capacity(ctx, net) -> float                              mm the free runs of the net could still add
    gain(a, c) / shallow_gain(h)                             what one bump adds, exactly

It runs after KiCad has judged the routed board (``judge.tune_pass``): the longest member of a group is known
only then, and KiCad's verdict on the tuned board is compared with the board it accepted before — a meander that
would cost a DRC error is taken out again, net by net. Nothing here opens a connection: an edit replaces ONE
straight segment of a routed connection by a polyline with the same two end points, inside one ``RouteState``
transaction, and is kept only when every connection of the net is still connected.

**Shape.** All pieces are axial or at 45 degrees to the run, so the board's octilinear style holds on diagonal
runs as well. A *U bump* leaves the run through a 45-degree chamfer ``c``, runs perpendicular to it, crosses a
top and comes back the same way: it takes ``S + 2c`` of the run and adds ``2a - 4c(2 - sqrt 2)`` mm for the
amplitude ``a`` (KiCad's own length figure agrees to 0.1 um: design §14.9). A *shallow bump* (45 degrees up by
``h``, a flat, 45 degrees down) adds ``2h(sqrt 2 - 1)``: the fine adjustment below one U bump.

    leg spacing S   max(project tuning spacing or 0.6, 3 x width, width + clearance)   (centre to centre)
    chamfer c       clamp((S - 0.2) / 2, 0.15, 0.5): every straight piece is at least 0.2 mm long
    amplitude a     2c + 0.2 ... groups[].amp, else the project's max_amplitude, else 1.0 mm; when the plan
                    names no amplitude and that one leaves a member short, the net is laid again with twice and
                    four times as much (at most AMP_AUTO_MAX) and the attempt that ends longest is kept
    keep-off        max(2 x width, c + 0.2) from both ends of the run

**Legality.** Every new segment passes ``oracle.seg_legal`` — all other copper at the net's clearance, holes,
keep-out rule areas, the board edge, and the plan's forbid regions / avoid rectangles / strict corridors — and
crosses no thermal-spoke or reserved shape the run did not cross. The oracle has no rule between copper of ONE
net, so the tuner adds it: a bump keeps ``S - width`` from every other pad, via and track of its own net (a
meander must not short-cut itself). Rectangles KiCad rejected a meander in (``untune(avoid=...)``) stay closed.

**Which run.** The straight segments of this run's copper that are full width, not protected, not teed onto,
axial or diagonal. The run that covers the whole need with the fewest bumps wins (ties: the longest, then
canonical order); when none can, the one with the largest capacity goes first and the next takes the rest.

**Honest failure.** A net never ends above its window (it would become the group's new longest); when the free
runs cannot hold the missing length the record says how many mm are missing and how many could be added.

Deterministic: canonical orders, no clock, no randomness; coordinates through ``q()``. Budget: ``CHARGE_TRY``
per candidate shape, ``CHARGE_EDIT`` per transaction.

**Intra-pair skew** (design §14.8, step C). A differential pair with a skew limit whose two nets differ by more
is brought inside it on the SHORTER net with shallow bumps only (height at most ``SKEW_H``, and never so high
that the bump's flat would leave a KiCad ``diff_pair_gap`` rule's window), on the runs nearest that net's pads
first. Beside a coupled lane the oracle leaves only the outer side: the bump can never close on the partner.

Not here (design §14.13): meanders on copper that was on the board before the run, on arcs, serpentines of a
coupled pair as a pair (a pair that is too short for its length GROUP is reported, not tuned).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import constraints
from .types import Budget, Ctx, StageReport, q

STAGE = "tune"
SPACING = 0.6             # mm: KiCad's default tuning-pattern spacing (leg centre to leg centre)
AMP_MAX = 1.0             # mm: KiCad's default largest amplitude
AMP_AUTO = (2.0, 4.0)     # multiples of that tried in turn when a member stays short and the plan gave no groups[].amp
AMP_AUTO_MAX = 4.0        # mm: ... and never above this
MIN_STRAIGHT = 0.2        # mm: no straight piece of a bump is shorter (the benchmark counts shorter ones as stubs)
H_MIN = 0.08              # mm: lowest shallow bump (adds 0.066 mm)
GUARD = 0.002             # mm: a tuned net stays this far inside its window (KiCad prints 0.1 um; q() is 1 nm)
AIM_BACK = 0.05           # mm below the group's longest member a tuned net aims at (or tol / 4 when that is less)
STEP = 0.1                # mm: how far the survey slides along a run when a bump does not fit
MAX_RUNS = 16             # candidate runs looked at per net (longest first)
MAX_BUMPS = 400           # bumps on one run
MAX_PASSES = 24           # runs used per net
SKEW_H = 0.3              # mm: highest shallow bump of intra-pair skew compensation (design §14.8: min(3 x gap, 0.3))
SKEW_BUMPS = 8            # shallow bumps on one run
PAIR_GROUP = "pair:"      # record['group'] of a net tuned for its pair's skew: 'pair:<P net>'
SKEW_LADDER = (1.0, 0.6, 0.0)   # skew-bump heights tried, as a share of the highest (0 = the lowest bump there is)
HOP_MAX = 2               # stretches of one net that may be moved to another layer for their meanders
HOP_STEP = 0.5            # mm a hop slides along its run looking for two legal via sites
HOP_SLIDES = 8            # positions tried per stretch length
HOP_BUMPS = 24            # most U bumps on one hop stretch
OWN_TOL = 1e-6            # mm: slack of the own-net spacing test
CHARGE_TRY = 6            # work units per candidate shape (polish's scale)
CHARGE_EDIT = 60          # work units per transaction
_K = 2.0 - math.sqrt(2.0)
_T = 2.0 * (math.sqrt(2.0) - 1.0)
_PIECE = ("net", "conn", "kind", "layer", "x1", "y1", "x2", "y2", "w", "drill", "stage", "protected", "target_w")


def gain(a: float, c: float) -> float:
    """mm one U bump of amplitude ``a`` with chamfer ``c`` adds to its run."""
    return 2.0 * a - 4.0 * c * _K


def amp_for(g: float, c: float) -> float:
    """The amplitude whose U bump adds ``g`` mm."""
    return (g + 4.0 * c * _K) / 2.0


def shallow_gain(h: float) -> float:
    """mm one shallow bump (45 degrees up by ``h``, a flat, 45 degrees down) adds."""
    return _T * h


@dataclass(frozen=True)
class Params:
    w: float              # track width
    clr: float            # clearance the net asks for
    S: float              # leg spacing, centre to centre
    c: float              # chamfer
    a_min: float
    a_max: float
    keep: float           # keep-off from the ends of a run

    @property
    def foot(self) -> float:          # what a U bump takes of the run
        return self.S + 2.0 * self.c

    @property
    def gap(self) -> float:           # straight run between two bumps (their nearest legs are then S apart)
        return self.S - 2.0 * self.c

    @property
    def ladder(self) -> tuple:
        """Amplitudes tried at a slot, tallest first: a_max, 3/4, then halves down to a_min."""
        out = [self.a_max]
        for f in (0.75, 0.5, 0.375, 0.25, 0.1875, 0.125):
            if self.a_max * f > self.a_min + 1e-9:
                out.append(self.a_max * f)
        if out[-1] > self.a_min + 1e-9:
            out.append(self.a_min)
        return tuple(out)


def params(w: float, clr: float, spacing: float | None = None, amp: float | None = None) -> Params:
    S = max(float(spacing or SPACING), 3.0 * w, w + clr)
    c = min(0.5, max(0.15, (S - MIN_STRAIGHT) / 2.0))
    a_min = 2.0 * c + MIN_STRAIGHT
    return Params(w=w, clr=clr, S=S, c=c, a_min=a_min, a_max=max(a_min, float(amp or AMP_MAX)),
                  keep=max(2.0 * w, c + MIN_STRAIGHT))


def bump_points(p0, u, side: int, s: float, a: float, P: Params) -> list:
    """The eight points of a U bump that leaves the run (start ``p0``, unit direction ``u``) ``s`` mm from its
    start, on side +1 (left of the direction of travel, y up) or -1."""
    c, S = P.c, P.S
    frame = ((s, 0.0), (s + c, c), (s + c, a - c), (s + 2 * c, a), (s + S, a), (s + S + c, a - c), (s + S + c, c),
             (s + S + 2 * c, 0.0))
    return _world(p0, u, side, frame)


def shallow_points(p0, u, side: int, s: float, h: float) -> list:
    return _world(p0, u, side, ((s, 0.0), (s + h, h), (s + h + MIN_STRAIGHT, h), (s + 2 * h + MIN_STRAIGHT, 0.0)))


def _world(p0, u, side, frame) -> list:
    nx, ny = -u[1] * side, u[0] * side
    return [(q(p0[0] + t * u[0] + h * nx), q(p0[1] + t * u[1] + h * ny)) for t, h in frame]


def allocate(caps: list, gmin: float, need: float) -> tuple[list, float]:
    """Which slots get a U bump, and how much each adds. ``caps``: the largest gain of each slot (mm); ``gmin``:
    the smallest gain a U bump can have; ``need``: mm to add. -> (gain per slot, 0 = unused; rest). The gains sum
    to ``need - rest``: ``rest`` is what U bumps cannot give — less than ``gmin`` (shallow bumps take it) or the
    capacity that is missing. The fewest bumps are used: tallest slots first, ties by position."""
    out = [0.0] * len(caps)
    used, tot = [], 0.0
    for i in sorted(range(len(caps)), key=lambda i: (-caps[i], i)):
        if tot >= need - 1e-9:
            break
        if caps[i] < gmin - 1e-9:
            continue
        out[i] = caps[i]
        tot += caps[i]
        used.append(i)
    if tot < need - 1e-9 or not used:
        return out, max(0.0, need - tot)
    x = tot - need
    for i in reversed(used):
        d = min(x, out[i] - gmin)
        if d > 0:
            out[i] -= d
            x -= d
    if x > 1e-9:
        # even with every bump at its lowest it is too much: one bump fewer, shallow bumps take the difference
        for i in used:
            out[i] = caps[i]
        out[used[-1]] = 0.0
        return out, need - (tot - caps[used[-1]])
    return out, 0.0


def _hist(ctx: Ctx) -> dict:
    """ctx.history['tune']:
      edits     {net: [{'old': piece fields of the replaced run, 'new': [piece ids of the polyline]}]}
      avoid     {net: [(x0, y0, x1, y1)]}   places KiCad rejected a meander of this net in
      banned    {net: why}                  nets the judge gave up tuning (KiCad rejected every attempt)
      nets      {net: record}               the last record of every net the tuner looked at and could not leave alone
      free      {net: {piece ids}}          what a hop left of its run on the run's own layer: still candidate runs
      nohop     {net: why}                  nets the judge took the stretch on another layer from: none is laid again
    """
    h = ctx.history.get("tune")
    if h is None:
        h = ctx.history["tune"] = {"edits": {}, "avoid": {}, "banned": {}, "nets": {}}
    return h


def tuned_conns(ctx: Ctx) -> set:
    """Connections that carry meanders: polish and the via pass leave them exactly as they are."""
    return {p.conn for p in ctx.state.pieces() if p.stage == STAGE}


class _Run:
    """One candidate run: a straight state segment and what the tuner needs to know about it."""
    __slots__ = ("piece", "p0", "u", "len", "P", "soft0", "net", "layer")

    def __init__(self, piece, P, soft0):
        self.piece, self.P, self.soft0 = piece, P, soft0
        self.net, self.layer = piece.net, piece.layer
        self.len = piece.length
        self.p0 = (piece.x1, piece.y1)
        self.u = ((piece.x2 - piece.x1) / self.len, (piece.y2 - piece.y1) / self.len)


class _Stretch:
    """What ``_Run`` needs of a piece, for a stretch that is not in the state yet (a hop's run on its new layer)."""
    __slots__ = ("id", "net", "conn", "layer", "x1", "y1", "x2", "y2", "w", "length")

    def __init__(self, net, conn, layer, a, b, w):
        self.id, self.net, self.conn, self.layer, self.w = None, net, conn, layer, w
        self.x1, self.y1, self.x2, self.y2 = a[0], a[1], b[0], b[1]
        self.length = math.hypot(b[0] - a[0], b[1] - a[1])


class _Tuner:
    def __init__(self, ctx: Ctx, budget: Budget, cal: dict | None = None):
        self.ctx, self.budget, self.cal = ctx, budget, dict(cal or {})
        self.state, self.oracle, self.board = ctx.state, ctx.oracle, ctx.board
        self.h = _hist(ctx)
        env = getattr(ctx.x, "cons_env", None) or {}
        tun = (env.get("tuning") or {}).get("single") or {}
        self.spacing = tun.get("spacing") if isinstance(tun.get("spacing"), (int, float)) else None
        self.proj_amp = tun.get("max_amplitude") if isinstance(tun.get("max_amplitude"), (int, float)) else None
        self.tried = 0
        self.hop_why = ""

    # ── measurement ───────────────────────────────────────────────────────
    def length(self, net: str) -> float:
        return constraints.net_length(self.ctx, net).total + float(self.cal.get(net, 0.0))

    def routed(self, net: str) -> bool | None:
        """True: every connection of the net is routed; False: one is open; None: the run made no connection
        for it (its copper, if any, was on the board before)."""
        cs = [c for c in self.ctx.conns if c.net == net]
        if not cs:
            return None
        return all(self.state.status(c.idx) == "routed" for c in cs)

    # ── candidate runs ────────────────────────────────────────────────────
    def _params(self, p, amp) -> Params:
        clr = self.oracle.clearance(p.net)
        try:
            clr = max(clr, float(self.state.conn(p.conn).clearance or 0.0))
        except (KeyError, AttributeError):
            pass
        return params(p.w, clr, self.spacing, amp or self.proj_amp)

    def runs(self, net: str, amp: float | None = None, skip=()) -> list:
        state = self.state
        pieces = state.pieces(net=net)
        pts = []                                    # (x, y, piece id | None): where other copper attaches
        for p in pieces:
            pts.append((p.x1, p.y1, p.id))
            if p.kind == "seg":
                pts.append((p.x2, p.y2, p.id))
        for t in getattr(self.board, "kept_tracks", None) or ():
            if t[0] == net:
                pts += [(t[2], t[3], None), (t[4], t[5], None)]
        for v in getattr(self.board, "kept_vias", None) or ():
            if v[0] == net:
                pts.append((v[1], v[2], None))
        n = self.board.nets.get(net)
        for pad in (n.pads if n is not None else ()):
            pts.append((pad.x, pad.y, None))
        out = []
        free = (self.h.get("free") or {}).get(net) or ()
        for p in pieces:
            if p.kind != "seg" or (p.stage == STAGE and p.id not in free) or p.protected or p.conn < 0 or p.id in skip:
                continue
            if abs(p.w - p.target_w) > 1e-9 or state.status(p.conn) != "routed":
                continue
            dx, dy = p.x2 - p.x1, p.y2 - p.y1
            ln = math.hypot(dx, dy)
            adx, ady = abs(dx), abs(dy)
            if not (adx <= 2e-6 or ady <= 2e-6 or abs(adx - ady) <= 2e-6):
                continue                            # an odd-angle segment: a bump on it would not be octilinear
            P = self._params(p, amp)
            if ln < 2 * P.keep + 2 * H_MIN + MIN_STRAIGHT:
                continue
            ux, uy = dx / ln, dy / ln
            teed = False
            for x, y, pid in pts:
                if pid == p.id:
                    continue
                t = (x - p.x1) * ux + (y - p.y1) * uy
                if 1e-4 < t < ln - 1e-4 and abs((x - p.x1) * uy - (y - p.y1) * ux) < 1e-4:
                    teed = True
                    break
            if teed:
                continue
            soft0 = {b.tag for b in self.oracle.soft_hits(p.layer, p.x1, p.y1, p.x2, p.y2, p.w, net)}
            out.append(_Run(p, P, soft0))
        out.sort(key=lambda r: (-round(r.len, 6), r.layer, r.p0[0], r.p0[1], r.piece.id))
        return out[:MAX_RUNS]

    # ── legality of one bump ──────────────────────────────────────────────
    def _own(self, R: _Run, bb, grow):
        """Other copper of the run's own net near ``bb``: (shape, 'pad' | 'via' | 'track')."""
        o = self.oracle
        nid = o.net_id(R.net)
        for s in o._static.near(R.layer, bb, grow):
            if s.cls != "cu" or s.net != nid or s.kind == "poly":
                continue
            if hasattr(s.tag, "name"):
                yield s, "pad"
            else:
                g = s.g
                yield s, ("via" if g[4] == 0.0 and g[5] == 0.0 else "track")
        for s in o._dyn.near(R.layer, bb, grow):
            if s.cls != "cu" or s.net != nid or s.tag == R.piece.id:
                continue
            e = o._pieces.get(s.tag)
            yield s, ("via" if e is not None and e[0].kind == "via" else "track")

    def ok(self, R: _Run, pts) -> bool:
        """May this bump replace its stretch of the run?"""
        self.budget.charge(CHARGE_TRY)
        self.tried += 1
        o, P, w = self.oracle, R.P, R.P.w
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        bb = (min(xs), min(ys), max(xs), max(ys))
        hw = w / 2.0
        for x0, y0, x1, y1 in self.h["avoid"].get(R.net, ()):
            if bb[0] - hw <= x1 and bb[2] + hw >= x0 and bb[1] - hw <= y1 and bb[3] + hw >= y0:
                return False
        segs = [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1)]
        for x1, y1, x2, y2 in segs:
            if not o.seg_legal(R.layer, x1, y1, x2, y2, w, R.net, P.clr):
                return False
            if o.plan_on and not self._plan_clear(R, x1, y1, x2, y2, hw):
                return False
            for b in o.soft_hits(R.layer, x1, y1, x2, y2, w, R.net):
                if b.tag not in R.soft0:
                    return False
        need = P.S - w - OWN_TOL
        last = len(segs) - 1
        for s, kind in self._own(R, bb, P.S + w):
            for i, (x1, y1, x2, y2) in enumerate(segs):
                if kind == "track" and i in (0, last):
                    continue                        # the two feet stand on the run: its neighbours are their own chain
                if s.dist_seg(x1, y1, x2, y2) - hw < need:
                    return False
        return True

    def _plan_clear(self, R: _Run, x1, y1, x2, y2, hw: float) -> bool:
        """The oracle keeps a track's CENTRE LINE out of the plan's forbidden areas (``regions[].forbid``,
        ``nets[..].avoid``); copper the tuner adds stays out with its whole width: both edges of the segment
        (the half width past its ends included) are asked as well."""
        ln = math.hypot(x2 - x1, y2 - y1)
        if ln < 1e-9:
            return True
        ux, uy = (x2 - x1) / ln * hw, (y2 - y1) / ln * hw
        for sgn in (1.0, -1.0):
            ox, oy = -uy * sgn, ux * sgn
            if self.oracle.plan_hit(R.net, R.layer, x1 - ux + ox, y1 - uy + oy, x2 + ux + ox, y2 + uy + oy) is not None:
                return False
        return True

    def max_amp(self, R: _Run, s: float, side: int) -> float | None:
        for a in R.P.ladder:
            if self.ok(R, bump_points(R.p0, R.u, side, s, a, R.P)):
                return a
        return None

    def slots(self, R: _Run) -> list:
        """U-bump slots along the run, first fit: [(s, side, largest amplitude)]."""
        P = R.P
        out, s, end, prev = [], P.keep, R.len - P.keep, 1
        while s + P.foot <= end + 1e-9 and len(out) < MAX_BUMPS and not self.budget.spent_out():
            best = None
            for side in (prev, -prev):
                a = self.max_amp(R, s, side)
                if a is not None and (best is None or a > best[0] + 1e-9):
                    best = (a, side)
            if best is None:
                s += STEP
                continue
            out.append((s, best[1], best[0]))
            prev = best[1]
            s += P.foot + P.gap
        return out

    def shallow(self, R: _Run, h: float, taken: list) -> tuple | None:
        """First place a shallow bump of height ``h`` fits on the run outside the ``taken`` intervals: (s, side)."""
        P = R.P
        foot = 2 * h + MIN_STRAIGHT
        s, end = P.keep, R.len - P.keep
        while s + foot <= end + 1e-9 and not self.budget.spent_out():
            clash = next((b for a, b in taken if s < b + P.gap - 1e-9 and s + foot > a - P.gap + 1e-9), None)
            if clash is not None:
                s = clash + P.gap
                continue
            for side in (1, -1):
                if self.ok(R, shallow_points(R.p0, R.u, side, s, h)):
                    return s, side
            s += STEP
        return None

    # ── one edit ──────────────────────────────────────────────────────────
    def _joined(self, net: str) -> dict:
        out = {}
        for c in self.ctx.conns:
            if c.net == net and self.state.status(c.idx) == "routed":
                strict = self.state.connected(c.idx)
                out[c.idx] = (strict, strict or self.state.connected(c.idx, loose=True))
        return out

    def place(self, R: _Run, shapes: list, hop: tuple | None = None) -> list:
        """Replace the run by run + bumps (``shapes``: point lists, any order). -> the new piece ids ([] = the
        edit was refused and undone). ``hop`` = (stretch run on the other layer, via size, drill): the run is
        cut at the stretch's two ends, a via goes on each, and the stretch — with ``shapes`` on it — is laid on
        the stretch's layer."""
        state, p = self.state, R.piece
        before = self._joined(R.net)
        t_of = lambda pt: (pt[0] - R.p0[0]) * R.u[0] + (pt[1] - R.p0[1]) * R.u[1]   # noqa: E731
        V = hop[0] if hop is not None else None
        pts = [V.p0 if V is not None else R.p0]
        for sh in sorted(shapes, key=lambda sh: t_of(sh[0])):
            pts += sh
        pts.append((V.piece.x2, V.piece.y2) if V is not None else (p.x2, p.y2))
        self.budget.charge(CHARGE_EDIT)
        tok = state.begin()
        state.remove(p.id)
        new = []

        def seg(a, b, layer, w):
            if abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6:
                return
            new.append(state.add(net=p.net, conn=p.conn, kind="seg", layer=layer, x1=a[0], y1=a[1], x2=b[0], y2=b[1],
                                 w=w, stage=STAGE, protected=False, target_w=w).id)

        def via(at):
            new.append(state.add(net=p.net, conn=p.conn, kind="via", layer=-1, x1=at[0], y1=at[1], x2=at[0], y2=at[1],
                                 w=hop[1], drill=hop[2], stage=STAGE, protected=False).id)
        if V is not None:
            seg(R.p0, pts[0], p.layer, p.w)
            via(pts[0])
        for a, b in zip(pts, pts[1:]):
            seg(a, b, V.layer if V is not None else p.layer, V.P.w if V is not None else p.w)
        free = []
        if V is not None:
            via(pts[-1])
            seg(pts[-1], (p.x2, p.y2), p.layer, p.w)
            # what is left of the run on its own layer, before the first via and after the second, is still
            # straight copper beside which there may be room: it stays a candidate run (``runs``)
            free = [i for i in (new[0], new[-1]) if state.piece(i).kind == "seg" and state.piece(i).layer == p.layer]
        after = self._joined(R.net)
        if any(k not in after or (v[0] and not after[k][0]) or (v[1] and not after[k][1]) for k, v in before.items()):
            state.rollback(tok)
            return []
        state.commit(tok)
        self.h["edits"].setdefault(R.net, []).append({"old": {k: getattr(p, k) for k in _PIECE}, "old_id": p.id,
                                                      "new": new})
        mine = self.h.setdefault("free", {}).setdefault(R.net, set())
        mine.discard(p.id)
        mine.update(free)
        return new

    # ── one net ───────────────────────────────────────────────────────────
    def tune_net(self, net: str, aim: float, lo: float, hi: float, amp: float | None) -> dict:
        """Add copper until the net is ``aim`` long (never above ``hi - GUARD``). -> the record. With no
        ``amp`` from the plan, an attempt that leaves the net short of its window is repeated from the routed
        shape with a larger amplitude (``AMP_AUTO``); the attempt that ends longest stays."""
        base = float(amp or self.proj_amp or AMP_MAX)
        amps = [base]
        if amp is None:
            for f in AMP_AUTO:
                a = min(AMP_AUTO_MAX, base * f)
                if a > amps[-1] + 1e-9:
                    amps.append(a)
        # every amplitude on the net's own layers first; only when none of them closes the gap, the same
        # amplitudes again with a stretch of the net moved to another layer (two vias) for its meanders
        levels = [(a, False) for a in amps] + [(a, True) for a in amps]
        best, hop_note = None, ""
        for k, (a, hop) in enumerate(levels):
            if hop and self.via_room(net) < 2:
                hop_note = (self.h.get("nohop") or {}).get(net) or \
                    "; a stretch on another layer needs two vias, and max_vias / a via_count rule leaves none"
                break
            rec = self._tune_at(net, aim, lo, hi, a, hop)
            if hop:
                hop_note = self.hop_why or "; no room on another layer either"
            if a > base + 1e-9:
                rec["amp"] = round(a, 3)
            if best is None or rec["after"] > best[2]["after"] + 1e-6:
                best = (a, hop, rec)
            if "short_by" not in rec or self.budget.spent_out() or (hop and not rec.get("hops")):
                break
            if k + 1 < len(levels):
                untune(self.ctx, [net])
        if best[2] is not rec:                      # a later attempt ended shorter: the best one is laid again
            untune(self.ctx, [net])
            rec = self._tune_at(net, aim, lo, hi, best[0], best[1])
            if best[0] > base + 1e-9:
                rec["amp"] = round(best[0], 3)
        if "short_by" in rec and rec.get("why") and hop_note and not best[1] and hop_note not in rec["why"]:
            rec["why"] += hop_note                  # why the other layers did not help either
        return rec

    def _tune_at(self, net: str, aim: float, lo: float, hi: float, amp: float, hop: bool = False) -> dict:
        L0 = self.length(net)
        rec = {"before": round(L0, 4), "aim": round(aim, 4), "bumps": 0, "runs": 0}
        bad, last_cap, last_need = set(), None, None
        hops, want_hop, again = 0, hop, -1
        self.hop_why = ""
        for _ in range(MAX_PASSES):
            L = self.length(net)
            need = aim - L
            room = hi - GUARD - L                   # what may be added at most
            if need < 1e-4 or room < shallow_gain(H_MIN):
                break
            if need < shallow_gain(H_MIN) and L >= lo + GUARD:
                break
            if self.budget.spent_out():
                rec["why"] = "out of budget while tuning (raise global.time_budget_s)"
                break
            if want_hop:
                # the stretch on the other layer goes in first, while every run is still straight; the runs it
                # leaves alone then take what is still missing
                want_hop = False
                lay = self._hop(net, need, room, amp, rec, bad) if hops < HOP_MAX and self.via_room(net) >= 2 else None
                if lay is not None:
                    hops += 1
                    rec["hops"] = hops
                    names = getattr(self.board, "layers", None) or []
                    rec["hop_layers"] = sorted(set(rec.get("hop_layers") or [])
                                               | {names[lay] if 0 <= lay < len(names) else str(lay)})
                    continue
            runs = self.runs(net, amp, bad)
            placed = self._one_run(runs, need, room, rec) if runs else None
            if placed is None:
                if hop and 0 < hops < HOP_MAX and again != hops:
                    again = hops                    # the own-layer runs are used up: one more stretch elsewhere
                    want_hop = True
                    continue
                break
            R, ids, cap = placed
            last_cap, last_need = cap, need
            if not ids:
                bad.add(R.piece.id)
                continue
            rec["runs"] += 1
        L1 = self.length(net)
        if L1 > hi - GUARD + 1e-9 and L1 > L0 + 1e-9:
            # must not happen (every bump's gain is known before it is laid): the routed shape goes back
            untune(self.ctx, [net])
            L1 = self.length(net)
            rec.update(bumps=0, runs=0, why="the meanders would have made the net longer than its window: undone")
            rec.pop("hops", None)
            rec.pop("hop_layers", None)
        rec["after"] = round(L1, 4)
        rec["added"] = round(L1 - L0, 4)
        if L1 < lo - 1e-9:
            rec["short_by"] = round(lo - L1, 4)
            if "why" not in rec:
                left = sum(gain(a, R.P.c) for R in self.runs(net, amp, bad) for _s, _d, a in self.slots(R)) \
                    if not self.budget.spent_out() else 0.0
                rec["capacity"] = round((L1 - L0) + left, 3)
                rec["why"] = (f"no free straight run: {round(L1 - L0, 2)} mm of the {round(aim - L0, 2)} mm needed "
                              "could be added" if L1 > L0 + 1e-6 else
                              f"no free straight run to add the {round(aim - L0, 2)} mm needed")
                if hop:
                    rec["why"] += self.hop_why or "; no room on another layer either"
        return rec

    # ── a stretch on another layer ("hop") ────────────────────────────────
    def via_room(self, net: str) -> int:
        """How many more vias the net may take: the plan's ``nets[..].max_vias`` and a KiCad ``via_count`` rule
        (99 = no limit). A hop needs two."""
        if net in (self.h.get("nohop") or {}):
            return 0                                # KiCad rejected the board with this net's hop (judge.tune_pass)
        lim = None
        nets = getattr(self.ctx.cfg, "nets", None)
        nc = nets.get(net) if nets else None
        cons = getattr(self.ctx.x, "cons", None)
        for v in (getattr(nc, "max_vias", None), (getattr(cons, "max_vias", None) or {}).get(net)):
            if v is not None:
                lim = int(v) if lim is None else min(lim, int(v))
        if lim is None:
            return 99
        have = sum(1 for p in self.state.pieces(net=net) if p.kind == "via") \
            + sum(1 for v in getattr(self.board, "kept_vias", None) or () if v[0] == net)
        return lim - have

    def _hop_layers(self, R: _Run) -> list:
        """[(layer, mm the two vias add, via size, drill)] the run's net may go to, farthest layer first."""
        try:
            conn = self.state.conn(R.piece.conn)
        except (KeyError, AttributeError):
            return []
        n = self.board.nets.get(R.net)
        size, drill = conn.via if conn.via and conn.via[0] > 0 else (getattr(n, "via", 0.0), getattr(n, "drill", 0.0))
        if not size or size <= 0 or getattr(self.board, "L", 1) < 2:
            return []
        nets = getattr(self.ctx.cfg, "nets", None)
        nc = nets.get(R.net) if nets else None
        ok = set(getattr(nc, "layers_ok", None) or range(self.board.L))
        env = constraints.env_of(self.ctx)
        out = []
        for lay in sorted(ok - {R.layer}, key=lambda l: (-abs(l - R.layer), l)):
            h = 2.0 * env["stack"].height(min(lay, R.layer), max(lay, R.layer)) if env["use_height"] else 0.0
            out.append((lay, h, float(size), float(drill)))
        return out

    def _hop_width(self, R: _Run, lay: int) -> float:
        """The width of the run's net on ``lay``: a per-layer width rule's, else the run's own."""
        cons = getattr(self.ctx.x, "cons", None)
        if cons is not None and hasattr(cons, "lay_width"):
            try:
                full = self.state.conn(R.piece.conn).width
            except (KeyError, AttributeError):
                full = R.P.w
            if abs(cons.lay_width(R.net, R.layer, full) - R.P.w) <= 1e-9:
                return q(cons.lay_width(R.net, lay, full))
        return R.P.w

    def _hop(self, net: str, need: float, room: float, amp: float, rec: dict, bad: set):
        """Move a stretch of one straight run to another layer, through a via at each end, and lay there the
        meanders that have no room beside the run (its neighbours on the run's own layer do not exist on the
        other one). The two vias add their height to the net (KiCad counts it), the meanders the rest of
        ``need``; a hop whose vias alone would overshoot is not laid. -> the layer, or None (nothing changed).

        The first run (longest first) and layer (farthest first) whose stretch holds everything that is
        missing is taken, with the shortest such stretch; when none does, the one that adds most."""
        self.hop_why = "; no other layer this net may use"
        best = None
        seen_layer = False
        for R in self.runs(net, amp, bad):
            for lay, hg, size, drill in self._hop_layers(R):
                if hg > need + 1e-9 or hg > room + 1e-9:
                    if not seen_layer:
                        self.hop_why = (f"; the two vias of a stretch on another layer would add {round(hg, 2)} mm, "
                                        "more than is missing")
                    continue
                seen_layer = True
                c = self._hop_on(R, lay, size, drill, need - hg, room - hg, amp)
                if c is None:
                    continue
                got = hg + c[2]
                if c[3]:                            # covers what is missing
                    best = (got, R, lay, size, drill, c)
                    break
                if best is None or got > best[0] + 1e-9:
                    best = (got, R, lay, size, drill, c)
            if (best is not None and best[5][3]) or self.budget.spent_out():
                break
        if best is None:
            if seen_layer:
                self.hop_why = "; no two legal via sites on a straight run for a stretch on another layer"
            return None
        _got, R, lay, size, drill, (V, shapes, _g, _cov) = best
        ids = self.place(R, shapes, hop=(V, size, drill))
        if not ids:
            bad.add(R.piece.id)
            self.hop_why = "; a stretch on another layer would have cut the net's own connection"
            return None
        self.hop_why = ""
        rec["bumps"] += len(shapes)
        rec["runs"] += 1
        return lay

    def _hop_on(self, R: _Run, lay: int, size: float, drill: float, want: float, room: float, amp: float):
        """A stretch of ``R`` on ``lay`` that holds ``want`` mm of extra length (as much of it as possible
        otherwise): a straight stretch with bumps on it, or — where the run is too short for that, or its
        line is taken on the other layer — two vias close together with one loop between them.
        -> (stretch run, shapes, mm they add, covers) | None."""
        sites = self._via_sites(R, size, drill)
        if len(sites) < 2:
            return None
        line = self._hop_line(R, lay, size, sites, want, room, amp)
        if line is not None and line[3]:
            return line
        loop = self._hop_loop(R, lay, size, drill, sites, want, room, amp)
        if loop is not None and (line is None or loop[3] or loop[2] > line[2] + 1e-9):
            return loop
        return line

    def _via_sites(self, R: _Run, size: float, drill: float) -> list:
        """Distances along the run (every HOP_STEP / 2) where it may be cut with a via: legal for the oracle on
        every layer, on no reserved site, with no copper of the via inside a forbidden area of the plan (the
        oracle tests a via's centre: here its whole pad stays out of ``regions[].forbid``, ``nets[..].avoid``
        and ``regions[].no_vias``), and not in or against a pad of its own net (the oracle has no same-net
        rule; the clearance is kept from it)."""
        o, net, clr = self.oracle, R.net, R.P.clr
        nid, vr = o.net_id(net), size / 2.0
        k = vr * math.sqrt(0.5)
        spokes = ((vr, 0.0), (0.0, vr), (k, k), (k, -k))
        no_vias = getattr(self.ctx.x, "no_vias", None)
        closed = self.h["avoid"].get(net, ())
        vk = max(R.P.keep, vr + MIN_STRAIGHT)       # the via centres stay this far from the run's ends
        out, t = [], vk
        while t <= R.len - vk + 1e-9 and not self.budget.spent_out():
            x, y = q(R.p0[0] + R.u[0] * t), q(R.p0[1] + R.u[1] * t)
            self.budget.charge(CHARGE_TRY)
            self.tried += 1
            good = o.via_legal(x, y, size, drill, net, clearance=clr) and not any(
                o.soft_hits(l, x, y, x, y, size, net) for l in range(self.board.L))
            if good and any(x - vr <= x1 and x + vr >= x0 and y - vr <= y1 and y + vr >= y0 for x0, y0, x1, y1 in closed):
                good = False                        # where KiCad rejected a meander of this net before
            if good and o.plan_on:
                good = all(o.plan_hit(net, None, x - dx, y - dy, x + dx, y + dy) is None for dx, dy in spokes)
            if good and callable(no_vias):
                good = not any(no_vias(x + sx * dx, y + sx * dy, net) for dx, dy in spokes + ((0.0, 0.0),)
                               for sx in (1.0, -1.0))
            if good:
                for l in range(self.board.L):
                    if any(sh.cls == "cu" and sh.net == nid and hasattr(sh.tag, "name")
                           and sh.dist_seg(x, y, x, y) - vr < clr - OWN_TOL
                           for sh in o._static.near(l, (x, y, x, y), vr + clr + 0.01)):
                        good = False
                        break
            if good:
                out.append(t)
            t += HOP_STEP / 2.0
        return out

    def _at(self, R: _Run, t: float) -> tuple:
        return (q(R.p0[0] + R.u[0] * t), q(R.p0[1] + R.u[1] * t))

    def _hop_loop(self, R: _Run, lay: int, size: float, drill: float, sites: list, want: float, room: float,
                  amp: float):
        """Two vias ``d`` apart on the run and ONE loop between them on ``lay``: straight out of each via, at
        right angles to the run, for the amplitude ``A``, and a chamfered top. It adds ``2A - 2c(2 - sqrt 2)``
        (the legs leave the via centres radially, so KiCad's in-via shortcut takes nothing off) and needs only
        ``d`` of the run. -> (stretch run, [loop points], mm, covers) | None."""
        net = R.net
        w = self._hop_width(R, lay)
        P = params(w, R.P.clr, self.spacing, amp)
        vr = size / 2.0
        rules = getattr(self.board, "rules", None) or {}
        step = HOP_STEP / 2.0
        d = max(P.S, size + P.clr, drill + float(rules.get("hole_to_hole") or 0.25) + 0.05)
        d = q(math.ceil(d / step - 1e-9) * step)    # a whole number of site steps
        c = min(P.c, d / 2.0 - 0.1) if d / 2.0 - 0.1 >= 0.1 else 0.0
        a_min = vr + c + MIN_STRAIGHT
        a_max = max(a_min, P.a_max)
        loop_gain = lambda A: 2.0 * A - 2.0 * c * _K   # noqa: E731
        if 1e-4 < want < loop_gain(a_min) - 1e-9 or room < -1e-9:
            return None                             # even the lowest loop would overshoot
        ladder = [a_max] + [a_max * f for f in (0.75, 0.5, 0.375, 0.25, 0.1875, 0.125) if a_max * f > a_min + 1e-9]
        if ladder[-1] > a_min + 1e-9:
            ladder.append(a_min)
        Pv = Params(w=P.w, clr=P.clr, S=P.S, c=P.c, a_min=P.a_min, a_max=P.a_max, keep=0.0)

        def points(a, b, side, A):
            nx, ny = -R.u[1] * side, R.u[0] * side
            fr = ((0.0, A - c), (c, A), (d - c, A), (d, A - c))
            mid = [(q(a[0] + t * R.u[0] + h * nx), q(a[1] + t * R.u[1] + h * ny)) for t, h in fr]
            return [a] + mid + [b]

        best, tries = None, 0
        for t in sites:
            if tries >= 2 * HOP_SLIDES or self.budget.spent_out():
                break
            if not any(abs(t2 - (t + d)) < 1e-6 for t2 in sites):
                continue
            tries += 1
            a, b = self._at(R, t), self._at(R, t + d)
            V = _Run(_Stretch(net, R.piece.conn, lay, a, b, w), Pv, set())
            if want <= 1e-4:
                if self.ok(V, [a, b]):
                    return V, [], 0.0, True         # the two vias alone are what is missing
                continue
            for side in (1, -1):
                top = next((A for A in ladder if self.ok(V, points(a, b, side, A))), None)
                if top is None:
                    continue
                A = min(top, (min(want, room) + 2.0 * c * _K) / 2.0)
                if A < a_min - 1e-9:
                    continue
                pts = points(a, b, side, A)
                if A < top - 1e-9 and not self.ok(V, pts):
                    continue
                got = loop_gain(A)
                cov = want - got < shallow_gain(H_MIN) - 1e-9
                if best is None or (cov, got) > (best[3], best[2] + 1e-9):
                    best = (V, [pts[1:-1]], got, cov)
                if cov:
                    return best
        return best

    def _hop_line(self, R: _Run, lay: int, size: float, sites: list, want: float, room: float, amp: float):
        """The shortest stretch of ``R`` that, laid on ``lay`` between two vias, holds ``want`` mm of meanders
        (as much of it as possible otherwise). -> (stretch run, shapes, mm they add, covers) | None."""
        o, net = self.oracle, R.net
        w = self._hop_width(R, lay)
        clr = R.P.clr
        P = params(w, clr, self.spacing, amp)
        vr = size / 2.0
        # bumps keep clear of the stretch's own two vias (the oracle has no same-net rule, and they are not
        # in it yet): the first leg stands S - w from the via's edge, and no corner lies inside the via's pad
        kv = max(vr + P.S - w / 2.0 - P.c, vr + MIN_STRAIGHT, P.keep)
        P = Params(w=P.w, clr=P.clr, S=P.S, c=P.c, a_min=P.a_min, a_max=P.a_max, keep=kv)
        span = sites[-1] - sites[0]
        gmin, gmax = gain(P.a_min, P.c), gain(P.a_max, P.c)
        shal = 2 * (2 * P.a_min + MIN_STRAIGHT) + P.gap          # room for the two shallow bumps of a remainder
        if span < 2 * kv + MIN_STRAIGHT:
            return None
        n_fit = int((span - 2 * kv + P.gap) // (P.foot + P.gap)) if span >= 2 * kv + P.foot else 0
        n_min = 0 if want < gmin - 1e-9 else int(math.ceil(want / gmax - 1e-9))
        counts = sorted({n for n in (n_min, n_min + 1, n_min + 2, 2 * n_min, n_fit) if 0 <= n <= min(n_fit, HOP_BUMPS)})
        if not counts:
            counts = [min(n_fit, HOP_BUMPS)]
        best, seen = None, set()
        # per bump count the stretch that just holds the bumps first, then one with room for the shallow bumps
        # of a remainder as well: the copper moved to the other layer is no longer than it has to be
        for n, extra in [(n, e) for n in counts for e in ((0.0, shal) if n else (shal,))]:
            Lh = 2 * kv + (n * P.foot + (n - 1) * P.gap if n else MIN_STRAIGHT) + extra
            tries = 0
            for ta in sites:
                if tries >= HOP_SLIDES or self.budget.spent_out():
                    break
                # the first legal site at least Lh on; the whole of what the sites span when that is shorter
                tb = next((t for t in sites if t >= ta + Lh - 1e-9), sites[-1] if ta == sites[0] else None)
                if tb is None or tb - ta < 2 * kv + MIN_STRAIGHT or (ta, tb) in seen:
                    continue
                seen.add((ta, tb))
                a, b = self._at(R, ta), self._at(R, tb)
                self.budget.charge(CHARGE_TRY)
                self.tried += 1
                V = _Run(_Stretch(net, R.piece.conn, lay, a, b, w), P, set())
                if not self.ok(V, [a, b]):          # the straight stretch itself: other copper, the plan, closed places
                    continue
                bb = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
                if any(sh.dist_seg(a[0], a[1], b[0], b[1]) - w / 2.0 < P.S - w - OWN_TOL
                       for sh, _kind in self._own(V, bb, P.S + w)):
                    continue                        # other copper of the net on that layer: it would be short-cut
                tries += 1
                shapes, got = self._fill(V, want, room)
                cov = want - got < shallow_gain(H_MIN) - 1e-9
                if best is None or (cov, got) > (best[3], best[2] + 1e-9):
                    best = (V, shapes, got, cov)
                if cov:
                    return best
        return best

    def _fill(self, V: _Run, want: float, room: float) -> tuple:
        """Bumps on a free stretch that add ``want`` mm (never more than ``room``). -> (shapes, mm they add)."""
        if want < 1e-4:
            return [], 0.0
        P = V.P
        gmin = gain(P.a_min, P.c)
        shapes, taken, got = [], [], 0.0
        rest = want
        if want >= gmin - 1e-9:
            sl = self.slots(V)
            caps = [gain(a, P.c) for _s, _d, a in sl]
            gains, rest = allocate(caps, gmin, want)
            for (s, side, _a), g in zip(sl, gains):
                if g <= 0:
                    continue
                pts = bump_points(V.p0, V.u, side, s, amp_for(g, P.c), P)
                if not self.ok(V, pts):
                    rest += g
                    continue
                shapes.append(pts)
                taken.append((s, s + P.foot))
                got += g
        if shallow_gain(H_MIN) - 1e-9 <= rest < gmin - 1e-9 and rest <= room - got + 1e-9:
            more = self._shallows(V, rest, room - got, taken)
            if len(more) == (1 if rest <= shallow_gain(P.a_min) + 1e-9 else 2):
                shapes += more
                got += rest
        return shapes, got

    def _one_run(self, runs: list, need: float, room: float, rec: dict):
        """Tune on the run that costs least. -> (run, new piece ids, capacity of that run) | None (nothing fits)."""
        if any(need >= gain(R.P.a_min, R.P.c) - 1e-9 for R in runs):
            best = None
            for k, R in enumerate(runs):
                sl = self.slots(R)
                caps = [gain(a, R.P.c) for _s, _d, a in sl]
                cap = sum(caps)
                if cap <= 0:
                    continue
                gains, rest = allocate(caps, gain(R.P.a_min, R.P.c), need)
                n = sum(1 for g in gains if g > 0)
                covers = rest < gain(R.P.a_min, R.P.c) - 1e-9 and n > 0
                key = (0, n, k) if covers else (1, -round(cap, 6), k)
                if best is None or key < best[0]:
                    best = (key, R, sl, gains, rest, cap)
            if best is not None:
                _key, R, sl, gains, rest, cap = best
                shapes, taken = [], []
                for (s, side, _a), g in zip(sl, gains):
                    if g <= 0:
                        continue
                    pts = bump_points(R.p0, R.u, side, s, amp_for(g, R.P.c), R.P)
                    if not self.ok(R, pts):
                        continue                    # (a lower bump inside a legal taller one: cannot fail by geometry)
                    shapes.append(pts)
                    taken.append((s, s + R.P.foot))
                if 0 < rest < gain(R.P.a_min, R.P.c):
                    shapes += self._shallows(R, rest, room - (need - rest), taken)
                if not shapes:
                    return R, [], cap
                ids = self.place(R, shapes)
                if ids:
                    rec["bumps"] += len(shapes)
                return R, ids, cap
        # less than one U bump is missing: shallow bumps on the longest run that has room for them
        for R in runs:
            shapes = self._shallows(R, need, room, [])
            if shapes:
                ids = self.place(R, shapes)
                if ids:
                    rec["bumps"] += len(shapes)
                return R, ids, 0.0
        return None

    def _shallows(self, R: _Run, rest: float, room: float, taken: list) -> list:
        """One or two shallow bumps that add ``rest`` (at least the lowest bump, when ``room`` allows that)."""
        cap = shallow_gain(R.P.a_min)
        n = 1 if rest <= cap + 1e-9 else 2
        each = rest / n
        if each < shallow_gain(H_MIN):
            if shallow_gain(H_MIN) > room + 1e-9:
                return []
            each = shallow_gain(H_MIN)
        h = min(each / _T, R.P.a_min)
        out, taken = [], list(taken)
        for _ in range(n):
            at = self.shallow(R, h, taken)
            if at is None:
                break
            out.append(shallow_points(R.p0, R.u, at[1], at[0], h))
            taken.append((at[0], at[0] + 2 * h + MIN_STRAIGHT))
        return out


    # ── intra-pair skew: shallow bumps on the shorter net of a pair ───────
    def skew_net(self, net: str, aim: float, lo: float, hi: float, hmax: float) -> dict:
        """Add shallow bumps (height <= ``hmax``) to ``net`` until it is ``aim`` long, never above ``hi``.
        Runs nearest the net's pads first. -> the record (as ``tune_net``)."""
        L0 = self.length(net)
        rec = {"before": round(L0, 4), "aim": round(aim, 4), "bumps": 0, "runs": 0}
        n = self.board.nets.get(net)
        pads = [(p.x, p.y) for p in (n.pads if n is not None else ())]

        def near(R):
            mx, my = R.p0[0] + R.u[0] * R.len / 2.0, R.p0[1] + R.u[1] * R.len / 2.0
            d = min((math.hypot(mx - x, my - y) for x, y in pads), default=0.0)
            return (round(d, 3), R.layer, R.p0[0], R.p0[1], R.piece.id)
        bad = set()
        gmin = shallow_gain(H_MIN)
        level = 0                                   # index into SKEW_LADDER: the bump height in use
        for _ in range(MAX_PASSES):
            L = self.length(net)
            need, room = aim - L, hi - GUARD - L
            if need < 1e-4 or room < gmin or (need < gmin and L >= lo + GUARD):
                break
            if self.budget.spent_out():
                rec["why"] = "out of budget while tuning (raise global.time_budget_s)"
                break
            placed = False
            while not placed and level < len(SKEW_LADDER):
                # as few bumps as possible first (the highest), then lower ones: a lower bump fits where a
                # neighbour stands too near for a high one, and more of them add the same length
                hcap = max(H_MIN, hmax * SKEW_LADDER[level])
                gmax = shallow_gain(hcap)
                stop = False
                for R in sorted(self.runs(net, None, bad), key=near):
                    k = min(SKEW_BUMPS, max(1, int(math.ceil(need / gmax - 1e-9))))
                    each = min(need, k * gmax) / k
                    if each < gmin:
                        if gmin > room + 1e-9:
                            stop = True
                            break
                        each = gmin
                    h = min(each / _T, hcap)
                    shapes, taken = [], []
                    for _k in range(k):
                        at = self.shallow(R, h, taken)
                        if at is None:
                            break
                        shapes.append(shallow_points(R.p0, R.u, at[1], at[0], h))
                        taken.append((at[0], at[0] + 2 * h + MIN_STRAIGHT))
                    ids = self.place(R, shapes) if shapes else []
                    if ids:
                        rec["bumps"] += len(shapes)
                        rec["runs"] += 1
                        placed = True
                        break
                    bad.add(R.piece.id)
                if stop:
                    break
                if not placed:
                    level += 1
                    bad = set()
            if not placed:
                break
        L1 = self.length(net)
        if L1 > hi - GUARD + 1e-9 and L1 > L0 + 1e-9:
            untune(self.ctx, [net])
            L1 = self.length(net)
            rec.update(bumps=0, runs=0, why="the bumps would have made the net the longer one of its pair: undone")
        rec["after"] = round(L1, 4)
        rec["added"] = round(L1 - L0, 4)
        if L1 < lo - 1e-9:
            rec["short_by"] = round(lo - L1, 4)
            rec.setdefault("why", (f"no free straight run beside the pair: {round(L1 - L0, 2)} mm of the "
                                   f"{round(aim - L0, 2)} mm needed could be added") if L1 > L0 + 1e-6 else
                           f"no free straight run beside the pair to add the {round(aim - L0, 2)} mm needed")
        return rec


def skew_height(pair, gap: float | None = None) -> float:
    """Highest shallow bump for this pair's skew compensation: min(3 x gap, SKEW_H), and inside a KiCad
    ``diff_pair_gap (max ..)`` window when a rule gives one (the bump's flat runs parallel to the partner)."""
    g = float(gap if gap is not None else pair.gap)
    h = min(max(3.0 * g, 0.2), SKEW_H)
    hi = getattr(pair, "gap_hi", None)
    if hi is not None:
        h = min(h, hi - g - 0.02)
    return h


def skew_pair(ctx: Ctx, T: "_Tuner", pair, only=None, changed: set | None = None) -> dict | None:
    """Bring one pair inside its skew limit: shallow bumps on the shorter net, ADDED to whatever bumps it
    already carries (nothing is taken out here: the bumps laid right after the pair was routed, while the room
    beside it was still free, stay). -> the shorter net's record, or None when there was nothing to do."""
    h = _hist(ctx)
    nets = (pair.p, pair.n)
    if pair.skew is None or any(T.routed(n) is not True for n in nets):
        return None                                 # open, or copper from before the run: measured and reported only
    L = {n: T.length(n) for n in nets}
    if min(L.values()) <= 0:
        return None
    short, long_ = sorted(nets, key=lambda n: (L[n], n))
    lo, hi = L[long_] - float(pair.skew), L[long_]
    mine = lambda n: (h["nets"].get(n) or {}).get("group", "").startswith(PAIR_GROUP)   # noqa: E731
    if mine(long_) and not h["edits"].get(long_):
        h["nets"].pop(long_, None)                  # a stale "too short" record of what is now the longer net
    prev = h["nets"].get(short) if mine(short) else None
    if L[short] >= lo + min(GUARD, float(pair.skew) / 4.0) - 1e-9:
        if prev is not None:
            prev.update(after=round(L[short], 4), added=round(L[short] - float(prev.get("before", L[short])), 4))
            for k in ("short_by", "why"):
                prev.pop(k, None)
            if not prev.get("bumps"):
                h["nets"].pop(short, None)
        return None
    if only is not None and short not in only:
        return None
    before = float(prev["before"]) if prev and prev.get("bumps") else L[short]
    base = {"group": PAIR_GROUP + pair.p, "before": round(before, 4), "after": round(L[short], 4),
            "added": round(L[short] - before, 4), "bumps": int((prev or {}).get("bumps") or 0),
            "runs": int((prev or {}).get("runs") or 0), "short_by": round(lo - L[short], 4)}
    if short in h["banned"]:
        h["nets"][short] = dict(base, why=h["banned"][short])
        return h["nets"][short]
    laid = (ctx.history.get("pairs") or {}) if isinstance(ctx.history, dict) else {}
    hmax = skew_height(pair, (laid.get(pair.p) or {}).get("gap") or pair.gap)
    if hmax < H_MIN:
        h["nets"][short] = dict(base, why="the pair's gap rule leaves no room for a skew bump (max gap too near the gap)")
        return h["nets"][short]
    # the middle of the limit (at most AIM_BACK below the longer net), and never nearer to the longer net than
    # the tuner's guard: a limit tighter than that is missed by a few um and reported, with the bumps kept —
    # the pair is as close as shallow bumps bring it, which no other rule of the board can be worse off for
    aim = hi - max(1.5 * GUARD, min(float(pair.skew) / 2.0, AIM_BACK))
    rec = T.skew_net(short, aim, lo, hi, hmax)
    laid_now = rec["bumps"]
    rec.update(group=PAIR_GROUP + pair.p, before=round(before, 4), added=round(rec["after"] - before, 4),
               bumps=rec["bumps"] + base["bumps"], runs=rec["runs"] + base["runs"])
    h["nets"][short] = rec
    if laid_now and changed is not None:
        changed.add(short)
    return rec


def forget(ctx: Ctx, nets) -> None:
    """Drop the tuner's book for nets whose copper is being laid afresh (their meanders went with it)."""
    h = ctx.history.get("tune") if isinstance(ctx.history, dict) else None
    if not h:
        return
    for n in nets:
        h["edits"].pop(n, None)
        h["nets"].pop(n, None)
        (h.get("free") or {}).pop(n, None)


def _skew_pairs(ctx: Ctx, T: "_Tuner", h: dict, only, grouped: set, changed: set) -> None:
    """Bring every pair with a skew limit inside it (the shorter net gets the bumps)."""
    cons = getattr(ctx.x, "cons", None)
    pairs = sorted(cons.skew_pairs(), key=lambda p: p.p) if cons is not None and hasattr(cons, "skew_pairs") else []
    for pair in pairs:
        if any(n in grouped for n in (pair.p, pair.n)):
            continue                                # its nets belong to a tuned length group: their length is set there
        skew_pair(ctx, T, pair, only, changed)


# ── the stage ─────────────────────────────────────────────────────────────
def _aim(g, lo: float, hi: float, ref) -> float | None:
    """The length a short member is brought to."""
    if g.tol > 0 and g.target is not None:
        aim = g.target
    elif g.tol > 0 and ref is not None:
        aim = ref - min(g.tol / 4.0, AIM_BACK)
    elif g.target is not None:
        aim = g.target
    elif math.isfinite(lo):
        aim = lo + (min((hi - lo) / 4.0, AIM_BACK) if math.isfinite(hi) else AIM_BACK)
    else:
        return None
    if math.isfinite(hi):
        aim = min(aim, hi - 2 * GUARD)
    if math.isfinite(lo):
        aim = max(aim, min(lo + 2 * GUARD, hi - 2 * GUARD) if math.isfinite(hi) else lo + 2 * GUARD)
    return aim


def run(ctx: Ctx, budget: Budget, only=None, cal: dict | None = None) -> StageReport:
    """Bring every member of the tuned length groups into its window. ``only``: tune just these nets (the others
    keep the copper they have); ``cal``: {net: KiCad's length - the router's figure}, from an earlier judged
    check. A net that already carries meanders of this run is put back to its routed shape first and tuned
    afresh, so the result does not depend on how often the pass ran.

    ``StageReport.data``: {'changed': [nets whose copper differs from before the call],
                           'nets': {net: {'group', 'before', 'after', 'aim', 'added', 'bumps', 'runs',
                                          'short_by'?, 'long_by'?, 'capacity'?, 'why'?}},
                           'groups': {name: {'target', 'tuned', 'added'}}}"""
    rep = StageReport(name=STAGE)
    used0 = budget.used
    cons = getattr(ctx.x, "cons", None)
    groups = sorted(cons.tuned(), key=lambda g: g.name) if cons is not None and hasattr(cons, "tuned") else []
    h = _hist(ctx)
    changed, gdata = set(), {}
    T = _Tuner(ctx, budget, cal)
    only = None if only is None else set(only)
    for g in groups:
        for n in sorted(g.nets):
            if (only is None or n in only) and h["edits"].get(n):
                untune(ctx, [n])
                changed.add(n)
        lengths, state_of = {}, {}
        for n in sorted(g.nets):
            r = T.routed(n)
            state_of[n] = r
            if r is False:
                continue
            L = T.length(n)
            if L > 0:
                lengths[n] = L
        lo, hi, ref = constraints.window(g, lengths)
        aim = _aim(g, lo, hi, ref)
        tuned = added = 0
        for n in sorted(lengths):
            if only is not None and n not in only:
                continue
            L = lengths[n]
            h["nets"].pop(n, None)
            if L > hi + 1e-9:
                h["nets"][n] = {"group": g.name, "before": round(L, 4), "after": round(L, 4), "added": 0.0, "bumps": 0,
                                "runs": 0, "long_by": round(L - hi, 4),
                                "why": "longer than its window: copper cannot be taken away by tuning (route it "
                                       "earlier or shorter, or raise groups[].len)"}
                continue
            if aim is None or L >= lo + GUARD - 1e-9:
                continue
            base = {"group": g.name, "before": round(L, 4), "after": round(L, 4), "added": 0.0, "bumps": 0, "runs": 0,
                    "short_by": round(lo - L, 4)}
            if n in h["banned"]:
                h["nets"][n] = dict(base, why=h["banned"][n])
                continue
            pair = cons.pair_of(n) if hasattr(cons, "pair_of") else None
            if pair is not None and getattr(pair, "coupled", False):
                h["nets"][n] = dict(base, why="one net of a coupled differential pair: a pair is not tuned net by "
                                              "net (meanders of a pair as a pair are not in this build)")
                continue
            if state_of[n] is None:
                h["nets"][n] = dict(base, why="its copper was on the board before this run: existing copper is "
                                              "measured, never altered (ripup: 'nets' lets the router re-lay it)")
                continue
            rec = T.tune_net(n, aim, lo, hi, g.amp)
            rec["group"] = g.name
            h["nets"][n] = rec
            if rec["bumps"]:
                changed.add(n)
                tuned += 1
                added += rec["added"]
        gdata[g.name] = {"target": None if ref is None else round(ref, 4), "tuned": tuned, "added": round(added, 4)}
    _skew_pairs(ctx, T, h, only, {n for g in groups for n in g.nets}, changed)
    rep.data = {"changed": sorted(changed), "nets": {n: dict(r) for n, r in sorted(h["nets"].items())},
                "groups": gdata, "tried": T.tried}
    rep.routed = sum(1 for r in h["nets"].values() if r.get("bumps") and "short_by" not in r)
    rep.failed = sum(1 for r in h["nets"].values() if "short_by" in r or "long_by" in r)
    rep.score = ctx.state.score()
    rep.work = budget.used - used0
    return rep


def untune(ctx: Ctx, nets, avoid: list | None = None) -> int:
    """Put the routed shape of these nets back (piece for piece: the same fields, new ids). ``avoid``:
    rectangles (x0, y0, x1, y1) no later meander of these nets may touch. -> runs restored."""
    h = _hist(ctx)
    state = ctx.state
    done = 0
    for net in sorted(set(nets)):
        if avoid:
            h["avoid"].setdefault(net, []).extend(tuple(float(v) for v in r) for r in avoid)
        remap = {}                                  # a piece an undone edit put back: its old id -> its new one
        for e in reversed(h["edits"].pop(net, [])):
            new = [remap.get(i, i) for i in e["new"]]
            if not all(state.piece(i) is not None for i in new):
                continue                            # the state was restored to before this edit
            tok = state.begin()
            for i in new:
                state.remove(i)
            back = state.add(**e["old"])
            state.commit(tok)
            if e.get("old_id") is not None:
                remap[e["old_id"]] = back.id        # (an edit on the left-over part of a hopped run)
            done += 1
        (h.get("free") or {}).pop(net, None)
        h["nets"].pop(net, None)
    return done


def capacity(ctx: Ctx, net: str) -> float:
    """mm the free straight runs of the net could add now, every slot at its largest amplitude (a report figure:
    nothing is changed)."""
    cons = getattr(ctx.x, "cons", None)
    g = cons.group_of(net) if cons is not None and hasattr(cons, "group_of") else None
    T = _Tuner(ctx, Budget(10 ** 9))
    return sum(gain(a, R.P.c) for R in T.runs(net, getattr(g, "amp", None)) for _s, _d, a in T.slots(R))
