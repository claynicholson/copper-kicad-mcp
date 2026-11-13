"""Coupled differential pairs (design §14.8): the two nets of a pair routed as ONE object.

    jobs(ctx)                      which connections of the coupled pairs match end for end -> [PairJob]
    job_map(ctx)                   {connection index: PairJob} for both members
    route_pair(ctx, job, budget)   centre line by the ordinary ladder at width 2w + gap, two lanes, fan-ins
    offset(points, d)              an octilinear polyline shifted sideways by d (mitred, never closer than d)
    coupled_conns(ctx)             connections that carry coupled lanes (polish and repair leave them alone)
    with_partners(ctx, ids)        a member that was ripped takes its partner with it (never one lane alone)
    report(ctx)                    {P net: {coupled, why, gap, conns}} of this run, for the constraints block

**How a pair is laid.** At each end the two terminals are *gathered*: each track leaves its terminal on a
diagonal straight to its lane (the fan-in KiCad counts as coupled: design §14.9 "fanin"), the lanes run
``w + gap`` apart to the gather point G, which lies where a rung between the lanes is clear of the pair's own
pads and where a track ``2w + gap`` wide is legal. Between the two gather points the *centre line* is searched by
``detail.route_conn`` exactly like any connection — pattern, corridor windows, wider, wide search — on a virtual
connection ``2w + gap`` wide whose via is as large as the two vias of a layer change side by side. Every path
it finds is handed to ``_accept``: the centre line is offset to both sides, each layer change becomes two vias
across the direction of travel (joined to the lanes by diverging 45-degree pieces), and EVERY resulting segment
and via is checked by the oracle as copper of its own net before both nets are added in one transaction. The
wide search is only an accelerator; a rejected lane closes those cells and the search runs again.

**When it cannot be done** — the pads are swapped at one end, no fan-in is legal, the centre line finds no way —
nothing is added and ``route_pair`` says why; the caller then routes the two connections one after the other as
it always did, and the report carries ``coupled: false`` with that reason. A pair that routed before still routes.

**Gap.** Lanes sit ``max(pair gap, what the oracle asks between the two nets)`` apart: for names KiCad does not
read as a pair (``/USB_DP`` + ``/USB_DM``) that is the ordinary clearance, and the report says so.

Deterministic: canonical orders, no clock, no randomness; coordinates through ``q()``.
Not here: serpentines of a pair as a pair, arcs, blind / buried vias, pairs teed onto a third pad.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field

from ..guided import geom
from .types import Blocker, Budget, ConnV2, Ctx, Terminal, q

SQ = 0.7071067811865476
DIRS8 = ((1.0, 0.0), (SQ, SQ), (0.0, 1.0), (-SQ, SQ), (-1.0, 0.0), (-SQ, -SQ), (0.0, -1.0), (SQ, -SQ))
REACH = 4.0               # mm: P and N terminals further apart than this at one end are not a pair's end
FAT_EXTRA = 0.05          # mm added to 2w + gap for the centre-line search (a mitred corner leaves the envelope)
LEAD_MIN = 0.3            # mm: lanes run at least this far (or one lane spacing) straight before the gather point
GATHER_STEP = 0.1         # mm between gather-point candidates along the exit direction
GATHER_TRIES = 30
RUNG_CLEAR = 0.05         # mm a rung keeps from the pair's own pads / vias (KiCad: a touched rung uncouples the segment)
MIN_SEG = 0.02            # mm: no lane piece is shorter
DIR_TRIES = 4             # exit directions tried per end
EPS = 1e-9


@dataclass
class PairJob:
    pair: object                  # constraints.Pair
    p: int                        # connection index of the P net
    n: int                        # ... of the N net
    swap: bool                    # the N connection's a end is at the P connection's b end
    gap: float                    # the gap the lanes are laid at (>= pair.gap)
    width: float


@dataclass
class Gather:
    layer: int
    u: tuple                      # exit direction (unit, octilinear), away from the pads
    M: tuple                      # mid-point of the two terminals
    side: int                     # +1: P is left of u (normal (-uy, ux)), -1: right
    g0: float                     # along u from M: where both lanes have reached their spacing
    g: float                      # ... the gather point G
    G: tuple
    lanes: dict = field(default_factory=dict)      # 'p' | 'n' -> [(x, y), ...] terminal .. lane point at g0
    ref: str = ""


# ── small geometry ────────────────────────────────────────────────────────
def _left(u):
    return (-u[1], u[0])


def _unit(dx, dy):
    ln = math.hypot(dx, dy)
    return (dx / ln, dy / ln) if ln > EPS else None


def _pt(M, u, n, a, b):
    return (q(M[0] + a * u[0] + b * n[0]), q(M[1] + a * u[1] + b * n[1]))


def _same(a, b, tol=2e-6):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or not _same(out[-1], p):
            out.append(p)
    return out


def offset(points, d: float, keep=()):
    """The polyline ``points`` shifted by ``d`` to the left of its direction of travel (d < 0: to the right),
    corners mitred, so every piece stays parallel to its centre piece at exactly |d|. A centre piece too short
    for its two corners (two turns the same way close together) disappears from the inner lane, which then
    turns in one corner — never nearer than |d| to the centre line. -> [(x, y, kept), ...] where ``kept``
    marks the vertices of ``keep`` (indexes into ``points``: vertices that must stay vertices), or None when
    the line doubles back on itself or its first / last piece cannot hold its corner."""
    pts = _dedupe([(float(x), float(y)) for x, y in points])
    if len(pts) < 2:
        return None
    lines = []                                  # [index of the first centre vertex, point on the line, direction]
    for i in range(len(pts) - 1):
        e = _unit(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        if e is None:
            return None
        n = _left(e)
        lines.append([i, (pts[i][0] + d * n[0], pts[i][1] + d * n[1]), e])
    n0, n1 = _left(lines[0][2]), _left(lines[-1][2])
    start = (pts[0][0] + d * n0[0], pts[0][1] + d * n0[1])
    end = (pts[-1][0] + d * n1[0], pts[-1][1] + d * n1[1])
    keep = set(keep)

    def meet(a, b):
        (_i, pa, ea), (_j, pb, eb) = a, b
        cr = ea[0] * eb[1] - ea[1] * eb[0]
        if abs(cr) < 1e-9:
            if ea[0] * eb[0] + ea[1] * eb[1] < 0:
                return None                      # doubles back
            if abs((pb[0] - pa[0]) * ea[1] - (pb[1] - pa[1]) * ea[0]) > 1e-6:
                return None                      # parallel, not the same line: no corner joins them
            return pb
        t = ((pb[0] - pa[0]) * eb[1] - (pb[1] - pa[1]) * eb[0]) / cr
        return (pa[0] + t * ea[0], pa[1] + t * ea[1])

    for _ in range(len(lines) + 1):
        vs = [start]
        for k in range(len(lines) - 1):
            v = meet(lines[k], lines[k + 1])
            if v is None:
                return None
            vs.append(v)
        vs.append(end)
        bad = None
        for k, (_i, _p, e) in enumerate(lines):
            if (vs[k + 1][0] - vs[k][0]) * e[0] + (vs[k + 1][1] - vs[k][1]) * e[1] < MIN_SEG:
                bad = k
                break
        if bad is None:
            out = []
            for k, v in enumerate(vs):
                lo = lines[k - 1][0] + 1 if k > 0 else 0
                hi = lines[k][0] if k < len(lines) else len(pts) - 1
                out.append((q(v[0]), q(v[1]), any(j in keep for j in range(lo, hi + 1))))
            return out
        if bad == 0 or bad == len(lines) - 1:
            return None
        del lines[bad]
    return None


def _polyline(vs):
    """[(x, y, kept)] -> [(x, y)]: doubled points dropped, collinear vertices merged except the kept ones."""
    pts = []
    for x, y, kept in vs:
        if pts and _same(pts[-1][:2], (x, y)):
            if kept:
                pts[-1] = (pts[-1][0], pts[-1][1], True)
            continue
        pts.append((x, y, bool(kept)))
    if len(pts) < 3:
        return [(x, y) for x, y, _k in pts]
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[i], pts[i + 1]
        cr = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        dt = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
        if abs(cr) < 1e-9 and dt > 0 and not b[2]:
            continue
        out.append(b)
    out.append(pts[-1])
    return [(x, y) for x, y, _k in out]


# ── which connections form a pair ─────────────────────────────────────────
def _closed(t) -> bool:
    return bool(t.layers)


def pair_gap(ctx: Ctx, pair) -> float:
    """The gap the lanes of this pair are laid at: its own, but never below what the oracle asks between the
    two nets (the class's pair gap for names KiCad reads as a pair, else the ordinary clearance)."""
    orc = ctx.oracle
    need = None
    f = getattr(orc, "pair_need", None)
    if callable(f):
        try:
            need = float(f(pair.p, pair.n))
        except Exception:  # noqa: BLE001
            need = None
    if need is None:
        need = max(orc.clearance(pair.p), orc.clearance(pair.n))
    return max(float(pair.gap), need)


def jobs(ctx: Ctx) -> list:
    """Coupled pairs whose connections pair up end for end (both terminals of the P connection next to the two
    of an N connection). Tees, open (pour) ends and a net with no partner connection stay sequential."""
    cons = getattr(ctx.x, "cons", None)
    if cons is None or not hasattr(cons, "coupled_pairs"):
        return []
    by_net = {}
    for c in ctx.conns:
        by_net.setdefault(c.net, []).append(c)
    out = []
    for pair in sorted(cons.coupled_pairs(), key=lambda p: p.p):
        cp = [c for c in by_net.get(pair.p, ()) if _closed(c.a) and _closed(c.b)]
        cn = [c for c in by_net.get(pair.n, ()) if _closed(c.a) and _closed(c.b)]
        cand = []
        for a in cp:
            for b in cn:
                for swap in (False, True):
                    ba, bb = (b.b, b.a) if swap else (b.a, b.b)
                    d1 = math.hypot(a.a.x - ba.x, a.a.y - ba.y)
                    d2 = math.hypot(a.b.x - bb.x, a.b.y - bb.y)
                    if d1 <= REACH and d2 <= REACH and d1 > 1e-6 and d2 > 1e-6:
                        cand.append((round(d1 + d2, 6), a.idx, b.idx, swap))
        used = set()
        gap = None
        for _cost, i, j, swap in sorted(cand):
            if i in used or j in used:
                continue
            used |= {i, j}
            if gap is None:
                gap = pair_gap(ctx, pair)
            out.append(PairJob(pair=pair, p=i, n=j, swap=swap, gap=gap, width=float(pair.width)))
    return out


def job_map(ctx: Ctx) -> dict:
    out = {}
    for j in jobs(ctx):
        out[j.p] = j
        out[j.n] = j
    return out


def _hist(ctx: Ctx) -> dict:
    h = ctx.history.get("pairs")
    if h is None:
        h = ctx.history["pairs"] = {}
    return h


def _record(ctx: Ctx, job: PairJob, coupled: bool, why: str | None, **kw) -> None:
    h = _hist(ctx)
    rec = h.setdefault(job.pair.p, {"conns": {}, "gap": round(job.gap, 6)})
    rec["gap"] = round(job.gap, 6)
    rec["conns"][job.p] = {"partner": job.n, "coupled": bool(coupled), "why": why, **kw}


def coupled_conns(ctx: Ctx) -> set:
    """Connections (of both nets) that were laid as coupled lanes and are still routed."""
    out = set()
    state = ctx.state
    for rec in (ctx.history.get("pairs") or {}).values():
        for i, r in (rec.get("conns") or {}).items():
            if r.get("coupled"):
                j = r.get("partner")
                try:
                    if state.status(i) == "routed" and state.status(j) == "routed":
                        out |= {i, j}
                except Exception:  # noqa: BLE001
                    continue
    return out


def coupled_nets(ctx: Ctx) -> set:
    by = {c.idx: c.net for c in ctx.conns}
    return {by[i] for i in coupled_conns(ctx) if i in by}


def with_partners(ctx: Ctx, ids) -> list:
    """``ids`` (connections about to be routed again) plus the partner of every one that was a coupled lane:
    the partner's lane is taken out too, so the two are laid together again or — when that fails — each on
    its own. One lane of a pair is never left standing beside a re-routed other."""
    ids = list(ids)
    state = ctx.state
    have = set(ids)
    for rec in (ctx.history.get("pairs") or {}).values():
        for i, r in sorted((rec.get("conns") or {}).items()):
            j = r.get("partner")
            if not r.get("coupled") or j is None:
                continue
            for a, b in ((i, j), (j, i)):
                if a in have and b not in have:
                    try:
                        if state.status(b) != "routed":
                            continue
                        state.rip_conn(b)
                    except Exception:  # noqa: BLE001
                        continue
                    ids.append(b)
                    have.add(b)
            if i in have:
                r["coupled"] = False
                r["why"] = "re-routed after KiCad rejected a lane"
    return ids


def report(ctx: Ctx) -> dict:
    """{P net: {'gap': laid gap, 'coupled': connections laid as lanes, 'of': connections looked at, 'why'}}."""
    out = {}
    for p, rec in sorted((ctx.history.get("pairs") or {}).items()):
        cs = rec.get("conns") or {}
        why = next((r.get("why") for _i, r in sorted(cs.items()) if not r.get("coupled") and r.get("why")), None)
        out[p] = {"gap": rec.get("gap"), "coupled": sum(1 for r in cs.values() if r.get("coupled")), "of": len(cs),
                  "why": why}
    return out


# ── the fan-in at one end ─────────────────────────────────────────────────
def _pad_shapes(ctx: Ctx, t, layer: int) -> list:
    if not t.pad:
        return []
    orc = ctx.oracle
    pads = (getattr(orc, "_pads_by_name", None) or {}).get(t.pad)
    if pads is None:
        pads = [p for p in ctx.board.pads if p.name == t.pad]
    out = []
    for p in pads:
        out += list(p.shapes.get(layer) or ())
    return out


def _exit_len(shapes, x, y, u) -> float:
    """How far from (x, y) along u the pad's copper ends (0 when the point is not inside it)."""
    if not shapes or not any(s.dist_pt(x, y) < 0 for s in shapes):
        return 0.0
    t = 0.0
    while t < 4.0:
        t += 0.05
        if all(s.dist_pt(x + t * u[0], y + t * u[1]) >= 0 for s in shapes):
            return t
    return 0.0


def _rides_edge(shapes, p, u) -> bool:
    """Does a lane that starts at ``p`` and runs along ``u`` lie on the outline of one of ``shapes``?"""
    if not shapes:
        return False
    hits = 0
    for i in range(1, 7):
        x, y = p[0] + 0.05 * i * u[0], p[1] + 0.05 * i * u[1]
        if any(abs(s.dist_pt(x, y)) < 0.004 for s in shapes):
            hits += 1
    return hits >= 3


def _legal_chain(orc, layer, pts, w, net, cl) -> bool:
    for a, b in zip(pts, pts[1:]):
        if not orc.seg_legal(layer, a[0], a[1], b[0], b[1], w, net, cl):
            return False
    return True


def _apart(pa, pb, need) -> bool:
    """Do two polylines keep ``need`` (centre line to centre line) from each other?"""
    for a, b in zip(pa, pa[1:]):
        for c, d in zip(pb, pb[1:]):
            if geom.seg_seg_dist(a[0], a[1], b[0], b[1], c[0], c[1], d[0], d[1]) < need - 1e-6:
                return False
    return True


def gather(ctx: Ctx, job: PairJob, tp, tn, layer: int, u, g_fixed: float | None = None):
    """The fan-in of one end on one layer for exit direction ``u``, or None when it is not legal: each track
    from its terminal on a 45-degree diagonal to its lane (after a straight lead out of the pad when the
    diagonal alone does not fit and no rule's max gap forbids the parallel exit), the lanes straight to the
    gather point, which is the first point where the rung between the lanes is clear of the pair's pads and a
    track 2w + gap wide is legal."""
    orc = ctx.oracle
    w, s = job.width, job.width + job.gap
    half = s / 2.0
    fat = 2.0 * w + job.gap + FAT_EXTRA
    cl = _clearance(ctx, job)
    M = ((tp.x + tn.x) / 2.0, (tp.y + tn.y) / 2.0)
    n = _left(u)
    lp = (tp.x - M[0]) * n[0] + (tp.y - M[1]) * n[1]
    if abs(lp) < 0.02:
        return None                                  # the two terminals lie one behind the other in this direction
    k = 1 if lp > 0 else -1
    delta = abs(abs(lp) - half)
    ap = (tp.x - M[0]) * u[0] + (tp.y - M[1]) * u[1]
    shp, shn = _pad_shapes(ctx, tp, layer), _pad_shapes(ctx, tn, layer)
    leads = [0.0]
    hi = getattr(job.pair, "gap_hi", None)
    if hi is None or 2.0 * abs(lp) - w <= hi + 1e-9:
        ex = max(_exit_len(shp, tp.x, tp.y, u), _exit_len(shn, tn.x, tn.y, u))
        if ex > 0:
            leads.append(round(ex + w / 2.0 + 0.05, 3))
    P0, N0 = (q(tp.x), q(tp.y)), (q(tn.x), q(tn.y))
    if len(leads) > 1 and (_rides_edge(shp, _pt(M, u, n, ap + delta, k * half), u)
                           or _rides_edge(shn, _pt(M, u, n, -ap + delta, -k * half), u)):
        # the diagonal would put a lane exactly ON its pad's edge: KiCad then counts the stretch inside the pad
        # either way (design §14.9: a numerical toss-up) and the two nets' lengths stop being comparable. The
        # straight lead out of the pad comes first; the diagonal stays as the fallback
        leads = leads[1:] + leads[:1]
    for t in leads:
        g0 = abs(ap) + t + delta
        lane_p = _dedupe([P0, _pt(P0, u, n, t, 0.0), _pt(M, u, n, ap + t + delta, k * half), _pt(M, u, n, g0, k * half)])
        lane_n = _dedupe([N0, _pt(N0, u, n, t, 0.0), _pt(M, u, n, -ap + t + delta, -k * half),
                          _pt(M, u, n, g0, -k * half)])
        if len(lane_p) > 1 and not _legal_chain(orc, layer, lane_p, w, job.pair.p, cl):
            continue
        if len(lane_n) > 1 and not _legal_chain(orc, layer, lane_n, w, job.pair.n, cl):
            continue
        if len(lane_p) > 1 and len(lane_n) > 1 and not _apart(lane_p, lane_n, s if delta < 1e-6 else min(s, 2 * abs(lp))):
            continue
        lead = max(LEAD_MIN, s)
        tries = [g_fixed] if g_fixed is not None else [g0 + lead + i * GATHER_STEP for i in range(GATHER_TRIES)]
        for g in tries:
            if g < g0 + lead - 1e-6:
                return None
            a, b = _pt(M, u, n, g, k * half), _pt(M, u, n, g, -k * half)
            if any(sh.dist_seg(a[0], a[1], b[0], b[1]) < RUNG_CLEAR for sh in shp + shn):
                continue
            G = _pt(M, u, n, g, 0.0)
            with _Allied(ctx, job):                  # the centre line stands for both nets: neither's pads block it
                fat_ok = orc.seg_legal(layer, G[0], G[1], q(G[0] + 0.05 * u[0]), q(G[1] + 0.05 * u[1]), fat,
                                       job.pair.p, cl)
            if not fat_ok:
                continue
            a0, b0 = lane_p[-1], lane_n[-1]
            if not (orc.seg_legal(layer, a0[0], a0[1], a[0], a[1], w, job.pair.p, cl)
                    and orc.seg_legal(layer, b0[0], b0[1], b[0], b[1], w, job.pair.n, cl)):
                break                                # the lanes run into something before a gather point is found
            ref = (tp.pad or "").rsplit(".", 1)[0]
            return Gather(layer=layer, u=u, M=M, side=k, g0=g0, g=g, G=G, lanes={"p": lane_p, "n": lane_n}, ref=ref)
    return None


class _Allied:
    """``with _Allied(ctx, job):`` the oracle treats the N net's copper as the P net's own (``Oracle.set_ally``);
    ``_Allied(ctx, job, off=True)`` switches that off for the block. The state before is put back on leaving."""

    def __init__(self, ctx: Ctx, job: PairJob, off: bool = False):
        self.orc, self.job, self.off = ctx.oracle, job, off
        self.before = None

    def __enter__(self):
        self.before = dict(getattr(self.orc, "_ally", None) or {})
        f = getattr(self.orc, "set_ally", None)
        if callable(f):
            if self.off:
                f(None)
            else:
                f(self.job.pair.p, self.job.pair.n)
        return self

    def __exit__(self, *exc):
        if hasattr(self.orc, "_ally"):
            self.orc._ally = self.before
        return False


def _clearance(ctx: Ctx, job: PairJob):
    try:
        c = max(float(ctx.state.conn(job.p).clearance or 0.0), float(ctx.state.conn(job.n).clearance or 0.0))
    except Exception:  # noqa: BLE001
        c = 0.0
    return c or None


def _dirs(M, toward, tp, tn) -> list:
    """Exit directions for one end, best first: the one straight out of the pad row (across the line through
    the two terminals) that points towards the other end, then the others by how well they point there."""
    t = _unit(toward[0] - M[0], toward[1] - M[1]) or (1.0, 0.0)
    ax = _unit(tp.x - tn.x, tp.y - tn.y) or (0.0, 1.0)
    out = []
    for k, u in enumerate(DIRS8):
        dot = u[0] * t[0] + u[1] * t[1]
        if dot < -0.01:
            continue
        across = abs(u[0] * ax[0] + u[1] * ax[1]) < 0.05
        out.append((0 if across else 1, -round(dot, 6), k, u))
    return [u for _a, _d, _k, u in sorted(out)][:DIR_TRIES]


def _layers_of(ctx: Ctx, job: PairJob, tp, tn) -> list:
    from . import window as wnd
    cp, cn = ctx.state.conn(job.p), ctx.state.conn(job.n)
    ok = set(wnd.allowed_layers(ctx, cp, corridor=False)) & set(wnd.allowed_layers(ctx, cn, corridor=False))
    return sorted(l for l in tp.layers if l in tn.layers and l in ok)


def _gathers(ctx: Ctx, job: PairJob, tp, tn, toward, cache: dict, end: str) -> list:
    """[(u, {layer: Gather})] for one end, best direction first; every layer of a direction shares one G."""
    out = []
    lays = _layers_of(ctx, job, tp, tn)
    M = ((tp.x + tn.x) / 2.0, (tp.y + tn.y) / 2.0)
    for u in _dirs(M, toward, tp, tn):
        key = (end, u)
        if key not in cache:
            got = {}
            first = None
            for l in lays:
                ga = gather(ctx, job, tp, tn, l, u, None if first is None else first.g)
                if ga is None:
                    continue
                if first is None:
                    first = ga
                got[l] = ga
            cache[key] = got
        if cache[key]:
            out.append((u, cache[key]))
    return out


# ── centre line -> two lanes ──────────────────────────────────────────────
def _runs(specs):
    """Piece specs of a centre line (detail.emit order) -> ([[layer, [points]]], [via xy between runs]) or None
    when the line starts or ends with a via or is not one chain."""
    runs, vias, pend = [], [], None
    for d in specs:
        if d.get("kind") == "via":
            if pend is not None or not runs:
                return None
            pend = (d["x1"], d["y1"])
            continue
        a, b = (d["x1"], d["y1"]), (d["x2"], d["y2"])
        if not runs or pend is not None or d["layer"] != runs[-1][0]:
            if runs and pend is None:
                return None
            if runs:
                if not _same(runs[-1][1][-1], pend, 1e-5) or not _same(a, pend, 1e-5):
                    return None
                vias.append(pend)
            runs.append([d["layer"], [a, b]])
            pend = None
        else:
            if not _same(runs[-1][1][-1], a, 1e-5):
                return None
            runs[-1][1].append(b)
    if pend is not None or not runs:
        return None
    return runs, vias


def _blk(rule, at, layer, net=None, tag=None, piece=None, static=True) -> Blocker:
    return Blocker(rule=rule, net=net, tag=tag, static=static, piece=piece, dist=0.0, need=0.0,
                   at=(float(at[0]), float(at[1])), layer=layer)


def _via_pitch(ctx: Ctx, job: PairJob, via) -> float:
    orc = ctx.oracle
    s = job.width + job.gap
    need = max(float(getattr(job.pair, "via_gap", 0.25) or 0.25), job.gap)
    return max(via[0] + need, via[1] + float(getattr(orc, "h2h", 0.0) or 0.0) + 0.01, s)


def fat_via(ctx: Ctx, job: PairJob, via) -> tuple:
    """(size, drill) of the one via that stands for the two of a layer change in the centre-line search."""
    if not via or via[0] <= 0:
        return (0.0, 0.0)
    pv = _via_pitch(ctx, job, via)
    return (q(pv + via[0]), q(pv + via[1]))


def lanes(ctx: Ctx, job: PairJob, ga: Gather, gb: Gather, runs, vias, via):
    """The copper of both nets for a centre line: ({'p': [spec], 'n': [spec]}, None) or (None, (rule, at, layer,
    kind)) naming the place of the centre line that cannot carry the pair. A spec is a ``RouteState.add`` dict
    without net / conn."""
    w, s = job.width, job.width + job.gap
    half = s / 2.0
    out = {"p": [], "n": []}
    if not _same(runs[0][1][0], ga.G, 1e-5) or not _same(runs[-1][1][-1], gb.G, 1e-5):
        return None, ("ends", ga.G, runs[0][0], "seg")
    if ga.side != -gb.side:
        return None, ("twist", gb.G, runs[-1][0], "seg")
    pv = _via_pitch(ctx, job, via) if vias else 0.0
    chains = []
    for i, (lay, pts) in enumerate(runs):
        pts = _dedupe([(q(x), q(y)) for x, y in pts])
        keep = set()
        if i == 0:
            c0 = _pt(ga.M, ga.u, _left(ga.u), ga.g0, 0.0)
            pts = [c0] + pts
            keep.add(1)
        if i == len(runs) - 1:
            c1 = _pt(gb.M, gb.u, _left(gb.u), gb.g0, 0.0)
            keep.add(len(pts) - 1)
            pts = pts + [c1]
        if len(_dedupe(pts)) != len(pts) or len(pts) < 2:
            return None, ("short", pts[0], lay, "seg")
        chains.append((lay, pts, keep))
    for key, sgn in (("p", ga.side), ("n", -ga.side)):
        o = sgn * half
        polys = []
        for lay, pts, keep in chains:
            vs = offset(pts, o, keep)
            if vs is None:
                mid = pts[len(pts) // 2]
                return None, ("corner", mid, lay, "seg")
            polys.append([lay, vs])
        vpos = []
        for i, C in enumerate(vias):
            pin, pout = chains[i][1], chains[i + 1][1]
            e_in = _unit(pin[-1][0] - pin[-2][0], pin[-1][1] - pin[-2][1])
            e_out = _unit(pout[1][0] - pout[0][0], pout[1][1] - pout[0][1])
            if e_in is None or e_out is None or e_in[0] * e_out[0] + e_in[1] * e_out[1] < -0.01:
                return None, ("via-turn", C, chains[i][0], "via")
            bis = _unit(e_in[0] + e_out[0], e_in[1] + e_out[1])
            m = _left(bis)
            V = (q(C[0] + sgn * pv / 2.0 * m[0]), q(C[1] + sgn * pv / 2.0 * m[1]))
            for e, poly, last in ((e_in, polys[i][1], True), (e_out, polys[i + 1][1], False)):
                nn = _left(e)
                av = (V[0] - C[0]) * e[0] + (V[1] - C[1]) * e[1]
                bv = (V[0] - C[0]) * nn[0] + (V[1] - C[1]) * nn[1]
                jog = abs(bv - o)
                if jog < 1e-6:
                    E = V
                else:
                    t = av - jog if last else av + jog
                    E = (q(C[0] + t * e[0] + o * nn[0]), q(C[1] + t * e[1] + o * nn[1]))
                if last:
                    prev = poly[-2]
                    if (E[0] - prev[0]) * e[0] + (E[1] - prev[1]) * e[1] < MIN_SEG:
                        return None, ("via-room", C, chains[i][0], "via")
                    poly[-1] = (E[0], E[1], True)
                    if not _same(E, V):
                        poly.append((V[0], V[1], True))
                else:
                    nxt = poly[1]
                    if (nxt[0] - E[0]) * e[0] + (nxt[1] - E[1]) * e[1] < MIN_SEG:
                        return None, ("via-room", C, chains[i + 1][0], "via")
                    poly[0] = (E[0], E[1], True)
                    if not _same(E, V):
                        poly.insert(0, (V[0], V[1], True))
            vpos.append(V)
        for i, (lay, vs) in enumerate(polys):
            vs = list(vs)
            if i == 0:
                vs = [(x, y, False) for x, y in ga.lanes[key]] + vs
            if i == len(polys) - 1:
                vs = vs + [(x, y, False) for x, y in reversed(gb.lanes[key])]
            line = _polyline(vs)
            if i > 0:
                V = vpos[i - 1]
                out[key].append({"kind": "via", "layer": -1, "x1": V[0], "y1": V[1], "x2": V[0], "y2": V[1],
                                 "w": via[0], "drill": via[1]})
            for a, b in zip(line, line[1:]):
                if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
                    continue
                out[key].append({"kind": "seg", "layer": lay, "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1], "w": w,
                                 "target_w": w})
    return out, None


def _accept(ctx: Ctx, job: PairJob, GA: dict, GB: dict, via, specs, win=None):
    """detail.route_conn's ``accept``: centre-line specs -> the pieces of both nets (added, connected, committed),
    a Blocker (that place of the centre line cannot carry the pair: the search closes it and runs again) or
    None (nothing can be said about where)."""
    with _Allied(ctx, job, off=True):                # every lane is checked as its own net
        return _accept_lanes(ctx, job, GA, GB, via, specs, win)


def _accept_lanes(ctx: Ctx, job: PairJob, GA: dict, GB: dict, via, specs, win=None):
    orc, state = ctx.oracle, ctx.state
    cl = _clearance(ctx, job)

    def reject(rule, at, layer, kind, seg=None, blk=None):
        if win is not None:
            if kind == "via":
                win.last_reject = ("via", layer, (at[0], at[1]))
            else:
                win.last_reject = ("seg", layer, seg or (at[0], at[1], at[0], at[1]))
        if blk is not None:
            return dataclasses.replace(blk, at=(float(at[0]), float(at[1])), layer=layer)
        return _blk("clearance", at, layer)

    got = _runs(specs)
    if got is None:
        v = next((d for d in specs if d.get("kind") == "via"), None)
        if v is not None:
            return reject("via", (v["x1"], v["y1"]), -1, "via")
        return None
    runs, vias = got
    ga, gb = GA.get(runs[0][0]), GB.get(runs[-1][0])
    if ga is None or gb is None:
        return None
    built, bad = lanes(ctx, job, ga, gb, runs, vias, via)
    if built is None:
        rule, at, layer, kind = bad
        if rule in ("ends", "twist", "short"):
            return None
        seg = None
        if kind == "seg":
            seg = _nearest_centre(runs, layer, at)
            at = geom.pt_seg_proj(at[0], at[1], *seg)
        return reject(rule, at, layer, kind, seg)
    cp, cn = state.conn(job.p), state.conn(job.n)
    tok = state.begin()
    pieces = []
    for key, conn in (("p", cp), ("n", cn)):
        for d in built[key]:
            if d["kind"] == "via":
                b = orc.via_ok(d["x1"], d["y1"], d["w"], d["drill"], conn.net, clearance=cl)
                where, lay = (d["x1"], d["y1"]), runs[0][0]
            else:
                b = orc.seg_ok(d["layer"], d["x1"], d["y1"], d["x2"], d["y2"], d["w"], conn.net, cl)
                where, lay = (b.at if b is not None else (d["x1"], d["y1"])), d["layer"]
            if b is not None:
                state.rollback(tok)
                if d["kind"] == "via":
                    C = min(vias, key=lambda c: math.hypot(c[0] - where[0], c[1] - where[1])) if vias else where
                    return reject(b.rule, C, lay, "via", blk=b)
                seg = _nearest_centre(runs, lay, where)
                if seg is None:
                    return None
                at = geom.pt_seg_proj(where[0], where[1], *seg)
                if _in_fan(ga, gb, at):
                    return None                      # the fan-in itself (checked before): not the centre line's fault
                return reject(b.rule, at, lay, "seg", seg, blk=b)
            pieces.append(state.add(net=conn.net, conn=conn.idx, stage="detail", **d))
    if not (state.connected(job.p) and state.connected(job.n)):
        state.rollback(tok)
        return None
    state.commit(tok)
    state.set_status(job.n, "routed")
    state.set_status(job.p, "routed")
    return pieces


def _in_fan(ga: Gather, gb: Gather, at) -> bool:
    return _same(at, ga.G, 1e-4) or _same(at, gb.G, 1e-4)


def _nearest_centre(runs, layer, at):
    best = None
    for lay, pts in runs:
        if lay != layer:
            continue
        for a, b in zip(pts, pts[1:]):
            d = geom.pt_seg_dist(at[0], at[1], a[0], a[1], b[0], b[1])
            if best is None or d < best[0]:
                best = (d, (a[0], a[1], b[0], b[1]))
    return best[1] if best else None


# ── one pair ──────────────────────────────────────────────────────────────
def _unescape(ctx: Ctx, job: PairJob):
    """Take the fan-out escapes of the pair's own pads out (terminals back on the pad centres): a pair fans in
    from its pads, an escape stub per pad only adds uncoupled length. -> (undo, keep) or None when no pad of
    the pair has an escape that serves these two connections alone."""
    try:
        from . import fanout
        reg = fanout.registry(ctx)
    except Exception:  # noqa: BLE001
        return None
    mine = {job.p, job.n}
    recs = [(k, r) for k, r in sorted(reg["by_pad"].items())
            if r.get("orig") and {ci for ci, _e in r["orig"]} <= mine]
    if not recs:
        return None
    conns = {c.idx: c for c in ctx.conns}
    saved = [(ci, end, getattr(conns[ci], end)) for _k, r in recs for ci, end in sorted(r["orig"]) if ci in conns]
    state = ctx.state
    tok = state.begin()
    for k, r in recs:
        fanout._drop(ctx, r)
        del reg["by_pad"][k]

    def undo():
        state.rollback(tok)
        for k, r in recs:
            reg["by_pad"][k] = r
        for ci, end, term in saved:
            setattr(conns[ci], end, term)

    def keep():
        state.commit(tok)
    return undo, keep


def route_pair(ctx: Ctx, job: PairJob, budget: Budget) -> dict:
    """Lay both connections of ``job`` as coupled lanes. -> {'ok', 'coupled', 'pieces', 'why'}. On failure
    NOTHING was added and the two connections are as they were: the caller routes them one by one.

    A pad of the pair that the fan-out stage gave an escape stub is first tried from its centre (the stub is
    dropped when that works, put back when it does not). A pair with a skew limit gets its skew bumps at once,
    on the shorter lane, while the room beside the pair is still free (``tune.skew_pair``)."""
    state = ctx.state
    cp, cn = state.conn(job.p), state.conn(job.n)
    try:
        from . import tune
        tune.forget(ctx, (cp.net, cn.net))           # laid afresh: bumps of an earlier lay went with its copper
    except Exception:  # noqa: BLE001
        tune = None
    esc = _unescape(ctx, job) if any(t.escape for t in (cp.a, cp.b, cn.a, cn.b)) else None
    out = _lay(ctx, job, budget)
    if esc is not None:
        if out["ok"]:
            esc[1]()
        else:
            esc[0]()
            out = _lay(ctx, job, budget)
    if out["ok"] and tune is not None and job.pair.skew is not None:
        cons = getattr(ctx.x, "cons", None)
        if cons is not None and job.pair in cons.skew_pairs():
            try:
                tune.skew_pair(ctx, tune._Tuner(ctx, budget), job.pair)
            except Exception:  # noqa: BLE001 — the pair is laid; its skew is measured and reported either way
                pass
    return out


def _lay(ctx: Ctx, job: PairJob, budget: Budget) -> dict:
    from . import detail
    state = ctx.state
    out = {"ok": False, "coupled": False, "pieces": [], "why": None}
    cp, cn = state.conn(job.p), state.conn(job.n)

    def fail(why):
        out["why"] = why
        _record(ctx, job, False, why)
        return out

    if state.status(job.p) == "routed" or state.status(job.n) == "routed" or state.connected(job.p) \
            or state.connected(job.n):
        return fail("one of the two nets was routed already: the other is routed beside it on its own")
    if cp.via != cn.via:
        return fail("the two nets use different vias")
    ta, tb = cp.a, cp.b
    na, nb = (cn.b, cn.a) if job.swap else (cn.a, cn.b)
    Ma = ((ta.x + na.x) / 2.0, (ta.y + na.y) / 2.0)
    Mb = ((tb.x + nb.x) / 2.0, (tb.y + nb.y) / 2.0)
    cache = {}
    A = _gathers(ctx, job, ta, na, Mb, cache, "a")
    B = _gathers(ctx, job, tb, nb, Ma, cache, "b")
    budget.charge(40 * (len(A) + len(B) + 2))
    ref_a = (ta.pad or "one end").rsplit(".", 1)[0]
    ref_b = (tb.pad or "the other end").rsplit(".", 1)[0]
    if not A or not B:
        return fail(f"no legal fan-in at {ref_a if not A else ref_b} (the lanes do not fit between the pads there)")
    combos = []
    for i, (ua, GA) in enumerate(A):
        for j, (ub, GB) in enumerate(B):
            sa = next(iter(GA.values())).side
            sb = next(iter(GB.values())).side
            if sa == -sb:
                combos.append((i + j, i, j, GA, GB))
    if not combos:
        return fail(f"the pads of the two nets are swapped between {ref_a} and {ref_b}: lanes would have to cross")
    fat = q(2.0 * job.width + job.gap + FAT_EXTRA)
    via = tuple(cp.via) if cp.via and cp.via[0] > 0 else (0.0, 0.0)
    why = None
    for _rank, _i, _j, GA, GB in sorted(combos, key=lambda c: c[:3])[:2]:
        g1, g2 = next(iter(GA.values())), next(iter(GB.values()))
        dx, dy = abs(g1.G[0] - g2.G[0]), abs(g1.G[1] - g2.G[1])
        vc = dataclasses.replace(cp, a=Terminal(g1.G[0], g1.G[1], tuple(sorted(GA))),
                                 b=Terminal(g2.G[0], g2.G[1], tuple(sorted(GB))), width=fat, neck=fat,
                                 via=fat_via(ctx, job, via), tree_target=False,
                                 est=max(dx, dy) + 0.41421356 * min(dx, dy))
        if dx < 1e-6 and dy < 1e-6:
            why = "the two ends are too close together for a coupled stretch"
            continue
        from . import window as wnd
        wnd.clear_cache(ctx.oracle)                  # windows of the allied search are no one else's, and vice versa
        try:
            with _Allied(ctx, job):
                r = detail.route_conn(ctx, vc, budget, accept=lambda specs, win=None, GA=GA, GB=GB: _accept(
                    ctx, job, GA, GB, via, specs, win))
        finally:
            wnd.clear_cache(ctx.oracle)
        if r.get("ok") and state.connected(job.p) and state.connected(job.n):
            out.update(ok=True, coupled=True, pieces=list(r.get("pieces") or []))
            _record(ctx, job, True, None, vias=sum(1 for p in out["pieces"] if p.kind == "via") // 2)
            return out
        f = r.get("fail") or {}
        where = f.get("where")
        why = "no coupled path at the pair's width: " + str(f.get("reason") or "blocked") + (
            f" (near ({round(where[0], 2)}, {round(where[1], 2)}))" if where else "")
        if budget.spent_out():
            break
    return fail(why or "no coupled path")
