"""S4 detail: pattern routes, then window A* inside the corridor, then bounded widening (design §4.8).

Per connection, in ``rank`` order (most constrained corridor first within equal plan rank):

    1 pattern     the <= 5 direct shapes (straight, two L postures with a 45 degree leg, two Z postures) on a
                  layer both terminals share, judged by the oracle only; the first legal one wins
    2 probe       a 2 000-expansion flood from the target side in the step-3 window: walled in by copper ->
                  fail fast with ``pad ... is boxed in`` (the source side shows up as a search that runs dry)
    3 window A*   in the corridor at full width, on the layers the global stage planned
    4 the same at neck width (pieces keep ``target_w`` = full width for polish)
    5 corridor + 2 rings, both widths, on every layer the plan allows
    6 bounded wide search: the box of both terminals grown by 25 % + 5 mm, at most 40 000 x effort expansions
    7 the best failure record goes back to the caller (repair)

Geometry: cell path -> 45 degree polyline, collinear points merged; each end is joined to its exact anchor (pad
centre / escape point / tee point on same-net copper) by an axis-aligned or 45 degree piece where one is legal;
every coordinate is quantised to 1 nm and EVERY segment and via is checked by the oracle before it is added.
A rejected segment closes its cells in the window and the search runs again (at most 3 times).

Nothing here looks at a clock for a decision; ``time.perf_counter`` only feeds the report's timing figures.
"""
from __future__ import annotations

import dataclasses
import math
import time

from ..guided import astar, geom
from . import search as srch
from . import window as wnd
from .types import (CHARGE_PATTERN, CHARGE_SEARCH, CHARGE_WINDOW, ORACLE_CHECKS_PER_UNIT, Blocker, Budget, ConnV2, Ctx,
                    StageReport, Terminal, q)
from .window import Window

PROBE_LIMIT = 2000            # expansions of the target-side flood
PROBE_MAX_TARGETS = 64        # a tree with more target cells than this is not probed
RESEARCH = 3                  # re-searches after an emitted segment failed the exact check
SEARCH_BASE, SEARCH_PER_STEP, SEARCH_MAX = 20_000, 1500, 120_000
WIDE_CAP = 40_000             # expansions of the bounded wide search (x effort)
WIDE_GROW, WIDE_PAD = 0.25, 5.0
BOX_MARGIN = 1.0              # mm added round the corridor tiles
RING_MM = 1.0                 # one "ring" when there is no tile graph
STUB_REACH = 1.6              # mm beyond the pad a start / end stub may reach for a free cell
TEE_RADIUS = 0.75             # x pitch: cells this close to same-net copper are tee targets
TOUCH = 2e-6                  # mm: two points this close are the same point
BEND_STATE_CELLS = 0          # windows with at most this many states search with bend_state (0 = never)
SQ = 0.7071067811865476
import os as _os
# price of running against the global stage's automatic layer direction when the plan gives none (as
# plan.global.direction_bias; 0 = off, the default: measured at 0.3 / 0.5 / 1.0 on the corpus it moves every hard
# board by +-5 connections with no trend and costs one_air_max its last 1 - 2 connections; COPPER_V2_AUTODIR sets it)
AUTO_DIR_BIAS = float(_os.environ.get("COPPER_V2_AUTODIR", "0") or 0)
PAIR_BUDGET = _os.environ.get("COPPER_V2_PAIR_BUDGET") or "own"     # 'own' | 'shared' (measurement switch)
PAIR_CAP = 300_000            # work units one coupled-pair attempt may use (its own allowance, 1.5 s of budget)
BRIDGE_MARGIN = 0.1           # mm: a bridge end / stitch via keeps this far inside a pour island's outline
BRIDGE_GROW = (2.0, 5.0)      # mm round the nearest pair of the two sides: the bridge windows (then: the whole side)
BRIDGE_SPAN = 30.0            # mm: largest side of a bridge window
STITCH_TRIES = 400            # exact via checks per bridge window for a one-via bridge (stitch via)

REASON_EFFORT = "search effort limit reached before a path was found (raise global.effort or time_budget_s)"
REASON_BUDGET = "time budget used up before a path was found (raise time_budget_s)"
REASON_WALLED = "no path: walled in by pads / keep-outs / board edge / forbidden regions"


# ── small helpers ─────────────────────────────────────────────────────────
def _stats(ctx: Ctx) -> dict:
    st = ctx.history.get("detail")
    if st is None:
        st = ctx.history["detail"] = {
            "pattern_tries": 0, "pattern_hits": 0, "windows": 0, "searches": 0, "expansions": 0, "probes": 0,
            "emit_rejects": 0, "window_cells": 0, "window_shapes": 0, "necked": 0, "soft_plans": 0,
            "build_s": 0.0, "search_s": 0.0, "emit_s": 0.0, "pattern_s": 0.0, "search_sizes": [], "joins": {}}
    return st


def _cfg(ctx: Ctx, name: str, default):
    return getattr(ctx.cfg, name, default) if ctx.cfg is not None else default


def _charge_checks(ctx: Ctx, budget: Budget, before: int) -> None:
    budget.charge((ctx.oracle.checks - before) // ORACLE_CHECKS_PER_UNIT)


def _simplify(pts):
    """Drop repeated and collinear points."""
    out = []
    for p in pts:
        if out and abs(out[-1][0] - p[0]) < 1e-6 and abs(out[-1][1] - p[1]) < 1e-6:
            continue
        while len(out) >= 2:
            ax, ay = out[-2]
            bx, by = out[-1]
            cross = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
            dot = (bx - ax) * (p[0] - bx) + (by - ay) * (p[1] - by)
            if abs(cross) < 1e-9 and dot > 0:
                out.pop()
            else:
                break
        out.append(p)
    return out


def _aligned(dx, dy) -> bool:
    return abs(dx) < 1e-6 or abs(dy) < 1e-6 or abs(abs(dx) - abs(dy)) < 1e-6


def _pad_extent(ctx: Ctx, name: str | None) -> float:
    if not name:
        return 0.0
    pm = getattr(ctx.board, "_v2_padmap", None)
    if pm is None:
        pm = {}
        for p in ctx.board.pads:
            pm.setdefault(p.name, []).append(p)
        try:
            ctx.board._v2_padmap = pm
        except AttributeError:
            pass
    ext = 0.0
    for p in pm.get(name, ()):
        for shs in p.shapes.values():
            for s in shs:
                ext = max(ext, s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1])
    return ext


def _net_conns(ctx: Ctx, net: str) -> list:
    c = ctx.history.get("_detail_bynet")
    if c is None or c[0] != len(ctx.conns):
        by = {}
        for cn in ctx.conns:
            by.setdefault(cn.net, []).append(cn)
        c = ctx.history["_detail_bynet"] = (len(ctx.conns), by)
    return c[1].get(net, ())


def _tkey(t):
    return ("c", t.cluster) if t.cluster else ("t", t.pad or (round(t.x, 6), round(t.y, 6)))


def _touches(p, x, y, layers) -> bool:
    """Does piece ``p`` pass through / end at the point (on one of ``layers``; None = any)?"""
    if p.kind == "via":
        return abs(p.x1 - x) <= TOUCH and abs(p.y1 - y) <= TOUCH
    if layers is not None and p.layer not in layers:
        return False
    return geom.pt_seg_dist(x, y, p.x1, p.y1, p.x2, p.y2) <= TOUCH


def _pieces_touch(a, b) -> bool:
    if a.kind == "via" and b.kind == "via":
        return abs(a.x1 - b.x1) <= TOUCH and abs(a.y1 - b.y1) <= TOUCH
    if a.kind == "via":
        return _touches(b, a.x1, a.y1, None)
    if b.kind == "via":
        return _touches(a, b.x1, b.y1, None)
    if a.layer != b.layer:
        return False
    return (_touches(b, a.x1, a.y1, None) or _touches(b, a.x2, a.y2, None)
            or _touches(a, b.x1, b.y1, None) or _touches(a, b.x2, b.y2, None))


def _sides(ctx: Ctx, conn: ConnV2):
    """-> (pieces on terminal a's side, pieces on terminal b's side, cluster ids on b's side).

    Same-net copper that belongs to a routed connection is on the side of the terminal its connection is
    joined to (union-find over routed connections of the net); loose pieces (reservations, escape stubs, this
    connection's own leftovers) are on a side when they touch its terminal point, directly or through each
    other."""
    state = ctx.state
    conns = _net_conns(ctx, conn.net)
    parent = {}

    def find(k):
        r = k
        while parent.setdefault(r, r) != r:
            r = parent[r]
        while parent[k] != r:
            parent[k], k = r, parent[k]
        return r
    routed = {}
    for c in conns:
        if c.idx != conn.idx and state.status(c.idx) == "routed":
            routed[c.idx] = c
            ra, rb = find(_tkey(c.a)), find(_tkey(c.b))
            if ra != rb:
                parent[max(ra, rb, key=repr)] = min(ra, rb, key=repr)
    ka, kb = find(_tkey(conn.a)), find(_tkey(conn.b))
    side_a, side_b, loose = [], [], []
    for p in state.pieces(net=conn.net):
        c = routed.get(p.conn)
        if c is None:
            loose.append(p)
            continue
        r = find(_tkey(c.a))
        if r == kb and kb != ka:
            side_b.append(p)
        elif r == ka:
            side_a.append(p)
    if loose:
        la = [p for p in loose if _touches(p, conn.a.x, conn.a.y, conn.a.layers)]
        lb = [p for p in loose if p not in la and _touches(p, conn.b.x, conn.b.y, conn.b.layers)]
        rest = [p for p in loose if p not in la and p not in lb]
        grew = True
        while grew and rest:
            grew = False
            for p in list(rest):
                if any(_pieces_touch(p, o) for o in la):
                    la.append(p)
                elif any(_pieces_touch(p, o) for o in lb):
                    lb.append(p)
                else:
                    continue
                rest.remove(p)
                grew = True
        side_a += la
        side_b += lb
    clusters = {k[1] for k in list(parent) if k[0] == "c" and find(k) == kb} if kb != ka else set()
    if conn.b.cluster:
        clusters.add(conn.b.cluster)
    if conn.a.cluster and conn.a.cluster != conn.b.cluster:
        clusters.discard(conn.a.cluster)
    return side_a, side_b, clusters


# ── 1: pattern ────────────────────────────────────────────────────────────
def _pattern_shapes(ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    adx, ady = abs(dx), abs(dy)
    if adx < 1e-6 and ady < 1e-6:
        return []
    if _aligned(dx, dy):
        return [[(ax, ay), (bx, by)]]
    sx = 1.0 if dx > 0 else -1.0
    sy = 1.0 if dy > 0 else -1.0
    m = min(adx, ady)
    out = [[(ax, ay), (q(ax + sx * m), q(ay + sy * m)), (bx, by)],            # diagonal, then straight
           [(ax, ay), (q(bx - sx * m), q(by - sy * m)), (bx, by)]]            # straight, then diagonal
    if adx > ady:
        s = (adx - ady) / 2
        out.append([(ax, ay), (q(ax + sx * s), ay), (q(bx - sx * s), by), (bx, by)])       # straight-diagonal-straight
    else:
        s = (ady - adx) / 2
        out.append([(ax, ay), (ax, q(ay + sy * s)), (bx, q(by - sy * s)), (bx, by)])
    h = m / 2
    out.append([(ax, ay), (q(ax + sx * h), q(ay + sy * h)), (q(bx - sx * h), q(by - sy * h)), (bx, by)])   # diag-straight-diag
    return out


def _plan_blocks_pattern(ctx: Ctx, conn: ConnV2) -> bool:
    nc = wnd.net_cfg(ctx, conn.net)
    if nc is not None and (getattr(nc, "avoid", None) or getattr(nc, "corridor", None)):
        return True
    x0, x1 = min(conn.a.x, conn.b.x), max(conn.a.x, conn.b.x)
    y0, y1 = min(conn.a.y, conn.b.y), max(conn.a.y, conn.b.y)
    for r in _cfg(ctx, "regions", None) or ():
        if r.applies(conn.net) and r.rect[0] <= x1 and r.rect[2] >= x0 and r.rect[1] <= y1 and r.rect[3] >= y0:
            return True
    return False


def _in_corridor(ctx: Ctx, conn: ConnV2, pts) -> bool:
    cor = (ctx.corridors or {}).get(conn.idx)
    if cor is None or not cor.tiles or ctx.tiles is None:
        return True
    tiles, tile_of = cor.tiles, ctx.tiles.tile_of
    step = float(ctx.tiles.t) / 2
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        n = max(1, int(math.ceil(math.hypot(x2 - x1, y2 - y1) / step)))
        for k in range(n + 1):
            t = k / n
            if tuple(tile_of(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)) not in tiles:
                return False
    return True


def _tee_points(ctx: Ctx, conn: ConnV2, lays) -> list:
    """Points on the routed tree of terminal b's side that are clearly nearer to terminal a than b itself:
    [(x, y, layer)], nearest first, at most 3 per layer: where a 45 degree ray from a meets a tree segment
    (one straight piece), else the foot of the perpendicular."""
    if not conn.tree_target:
        return []
    _a, side_b, _c = _sides(ctx, conn)
    if not side_b:
        return []
    ax, ay = q(conn.a.x), q(conn.a.y)
    limit = math.hypot(conn.b.x - ax, conn.b.y - ay) - 0.5
    out = []
    for p in side_b:
        if p.kind != "seg" or p.layer not in lays:
            continue
        hits = _ray_hits(ax, ay, p.x1, p.y1, p.x2, p.y2)
        fx, fy = geom.pt_seg_proj(ax, ay, p.x1, p.y1, p.x2, p.y2)
        hits.append((q(fx), q(fy)))
        for x, y in hits:
            d = math.hypot(x - ax, y - ay)
            if 1e-6 < d < limit:
                out.append((round(d, 6), p.layer, x, y))
    out.sort()
    res, per = [], {}
    for _d, l, x, y in out:
        if per.get(l, 0) < 3 and (x, y, l) not in res:
            per[l] = per.get(l, 0) + 1
            res.append((x, y, l))
    return res


def pattern(ctx: Ctx, conn: ConnV2) -> list[tuple] | None:
    """A direct route from terminal a, or None: ``[(x, y, layer), ...]`` (one layer, no vias), full width.

    Tries the straight line, else two L postures (one 45 degree leg) and two Z postures, on every layer both
    terminals share, and takes the first one every segment of which is oracle-legal, crosses no thermal /
    reserve shape of another net and stays inside the connection's corridor. A ``tree_target`` connection first
    tries the same shapes to at most three tee points per layer on its net's routed tree that are nearer than terminal b."""
    st = _stats(ctx)
    orc = ctx.oracle
    ok = wnd.allowed_layers(ctx, conn)
    if _plan_blocks_pattern(ctx, conn):
        return None
    nc = wnd.net_cfg(ctx, conn.net)
    lc = getattr(nc, "layer_cost", None) or {}
    ax, ay, bx, by = q(conn.a.x), q(conn.a.y), q(conn.b.x), q(conn.b.y)
    w, net, cl = conn.width, conn.net, conn.clearance or None
    goals = [(x, y, [l]) for x, y, l in _tee_points(ctx, conn, [l for l in ok if l in conn.a.layers])]
    lays = sorted((l for l in ok if l in conn.a.layers and l in conn.b.layers), key=lambda l: (lc.get(l, 1.0), l))
    if lays:
        goals.append((bx, by, lays))
    for gx, gy, glays in goals:
        shapes = _pattern_shapes(ax, ay, gx, gy)
        corridor_ok = {}
        for l in glays:
            for k, pts in enumerate(shapes):
                st["pattern_tries"] += 1
                good = True
                for i in range(len(pts) - 1):
                    (x1, y1), (x2, y2) = pts[i], pts[i + 1]
                    if not orc.seg_legal(l, x1, y1, x2, y2, w, net, cl) or orc.soft_hits(l, x1, y1, x2, y2, w, net):
                        good = False
                        break
                if not good:
                    continue
                if k not in corridor_ok:
                    corridor_ok[k] = _in_corridor(ctx, conn, pts)
                if corridor_ok[k]:
                    st["pattern_hits"] += 1
                    return [(x, y, l) for x, y in _simplify(pts)]
    return None


# ── sources and targets ───────────────────────────────────────────────────
def _term_cells(ctx: Ctx, conn: ConnV2, win: Window, term, width: float, lays, soft: bool, victims=None) -> dict:
    """{state: (g0 steps, anchor)}: lattice cells from which the terminal point is reached by one legal stub.
    The cell under the point when that works, else the first free cell along each of 8 rays."""
    g, p = win.grid, win.pitch
    G, W = g.G, g.W
    arr = win.cm.S if soft else win.cm.occ
    cmul, nid, orc = win.cmul, win.net_id, ctx.oracle
    net, cl = conn.net, conn.clearance or None
    px, py = q(term.x), q(term.y)
    out = {}
    if not (win.box[0] - p <= px <= win.box[2] + p and win.box[1] - p <= py <= win.box[3] + p):
        return out
    anchor = ("pt", px, py)
    tmax = STUB_REACH + _pad_extent(ctx, term.pad) / 2

    def free(s):
        o = arr[s]
        return not (o and o != nid) and (cmul is None or cmul[s] >= 0)

    def legal(l, x, y):
        if not soft:
            return orc.seg_legal(l, px, py, x, y, width, net, cl)
        b = orc.seg_ok(l, px, py, x, y, width, net, cl)
        if b is None:
            return True
        if b.static or b.piece is None:
            return False
        if victims is not None:
            victims.add(b.piece)
        return True
    for l in term.layers:
        if l not in lays or l not in win.lidx:
            continue
        base = win.lidx[l] * G
        ix, iy = g.cell(px, py)
        s = base + iy * W + ix
        x, y = g.mx(ix), g.my(iy)
        d = math.hypot(x - px, y - py)
        if free(s) and (d < 1e-6 or legal(l, x, y)):
            out[s] = (d / p, anchor)
            continue
        for dx, dy in astar.DIRS:
            k = 1.0 if (dx == 0 or dy == 0) else SQ
            t = p
            tries = 0
            last = s
            while t <= tmax:
                ix, iy = g.cell(px + dx * k * t, py + dy * k * t)
                c = base + iy * W + ix
                if c != last and free(c):
                    x, y = g.mx(ix), g.my(iy)
                    if legal(l, x, y):
                        g0 = math.hypot(x - px, y - py) / p + 0.5
                        if c not in out or g0 < out[c][0]:
                            out[c] = (g0, anchor)
                        break
                    tries += 1
                    if tries >= 3:
                        break
                last = c
                t += p
    return out


def _tee_cells(win: Window, lays, soft: bool, segs, vias, out: dict) -> None:
    """Add the free cells along same-net copper as tee targets: ``out[state] = anchor``."""
    g, p = win.grid, win.pitch
    G, W = g.G, g.W
    arr = win.cm.S if soft else win.cm.occ
    cmul, nid = win.cmul, win.net_id
    R = TEE_RADIUS * p
    for l, x1, y1, x2, y2 in segs:
        if l not in lays or l not in win.lidx:
            continue
        base = win.lidx[l] * G
        anchor = ("seg", x1, y1, x2, y2)
        for iy, i0, i1 in g.capsule_spans(x1, y1, x2, y2, R):
            a = base + iy * W
            for s in range(a + i0, a + i1 + 1):
                o = arr[s]
                if not (o and o != nid) and s not in out and (cmul is None or cmul[s] >= 0):
                    out[s] = anchor
    for x, y in vias:
        if not (win.box[0] <= x <= win.box[2] and win.box[1] <= y <= win.box[3]):
            continue
        ix, iy = g.cell(x, y)
        if math.hypot(g.mx(ix) - x, g.my(iy) - y) > R:
            continue
        for l in lays:
            if l in win.lidx:
                s = win.lidx[l] * G + iy * W + ix
                o = arr[s]
                if not (o and o != nid) and s not in out and (cmul is None or cmul[s] >= 0):
                    out[s] = ("pt", q(x), q(y))


def _island_cells(win: Window, lays, soft: bool, islands, out: dict) -> None:
    """Add the free cells well inside pour islands ``[(layer, points)]`` to ``out[state] = (0.0, ('pt', x, y))``:
    a track that ends there / a via there is joined to the island (the fill is the net's own copper)."""
    g = win.grid
    G = g.G
    arr = win.cm.S if soft else win.cm.occ
    cmul, nid = win.cmul, win.net_id
    bx0, by0, bx1, by1 = win.box
    m = BRIDGE_MARGIN
    for l, pts in islands:
        if l not in lays or l not in win.lidx or len(pts) < 3:
            continue
        xs = [x for x, _y in pts]
        ys = [y for _x, y in pts]
        if max(xs) < bx0 or min(xs) > bx1 or max(ys) < by0 or min(ys) > by1:
            continue
        inner = set(g.cells(g.poly_spans(pts)))
        if not inner:
            continue
        n = len(pts)
        for k in range(n):
            x1, y1 = pts[k]
            x2, y2 = pts[(k + 1) % n]
            if max(x1, x2) < bx0 - m or min(x1, x2) > bx1 + m or max(y1, y2) < by0 - m or min(y1, y2) > by1 + m:
                continue
            inner.difference_update(g.cells(g.capsule_spans(x1, y1, x2, y2, m)))
        base = win.lidx[l] * G
        for i in sorted(inner):
            s = base + i
            o = arr[s]
            if not (o and o != nid) and s not in out and (cmul is None or cmul[s] >= 0):
                x, y = g.xy(i)
                out[s] = (0.0, ("pt", x, y))


def _bridge_side(ctx: Ctx, conn: ConnV2, win: Window, width: float, lays, soft: bool, spec: dict, victims) -> dict:
    """{state: (g0, anchor)} for one side of a bridge: every pad, track, via and pour island of ``spec``."""
    out = {}
    for t in spec.get("pads") or ():
        for s, v in _term_cells(ctx, conn, win, t, width, lays, soft, victims).items():
            if s not in out or v[0] < out[s][0]:
                out[s] = v
    tee = {}
    _tee_cells(win, lays, soft, spec.get("segs") or (), spec.get("vias") or (), tee)
    for s in sorted(tee):
        if s not in out:
            out[s] = (0.0, tee[s])
    _island_cells(win, lays, soft, spec.get("islands") or (), out)
    return out


def _ends(ctx: Ctx, conn: ConnV2, win: Window, width: float, lays, soft: bool, strict: bool, bridge=None):
    """-> (sources {state: g0}, source anchors, target anchors, stub victims)."""
    victims = set()
    if bridge is not None:
        src = _bridge_side(ctx, conn, win, width, lays, soft, bridge["src"], victims)
        tg = _bridge_side(ctx, conn, win, width, lays, soft, bridge["tgt"], victims)
        tgt = {s: v[1] for s, v in tg.items()}
        return ({s: v[0] for s, v in src.items() if s not in tgt}, {s: v[1] for s, v in src.items()}, tgt, victims)
    src = _term_cells(ctx, conn, win, conn.a, width, lays, soft, victims)
    tgt = {s: v[1] for s, v in _term_cells(ctx, conn, win, conn.b, width, lays, soft, victims).items()}
    if conn.tree_target and not strict:
        _a, side_b, clusters = _sides(ctx, conn)
        segs = [(p.layer, p.x1, p.y1, p.x2, p.y2) for p in side_b if p.kind == "seg"]
        vias = [(p.x1, p.y1) for p in side_b if p.kind == "via"]
        cl = (getattr(ctx.board, "clusters", None) or {}).get(conn.net) or {}
        layer_of = {n: i for i, n in enumerate(ctx.board.layers)}
        for cid in sorted(clusters):
            c = cl.get(cid)
            if not c:
                continue
            for t in c["tracks"]:
                l = t[1] if isinstance(t[1], int) else layer_of.get(t[1], -1)
                segs.append((l, t[2], t[3], t[4], t[5]))
            for v in c["vias"]:
                vias.append((v[1], v[2]))
        _tee_cells(win, lays, soft, segs, vias, tgt)
    sources = {s: v[0] for s, v in src.items() if s not in tgt}
    return sources, {s: v[1] for s, v in src.items()}, tgt, victims


# ── geometry ──────────────────────────────────────────────────────────────
_RAYS = ((1.0, 0.0), (0.0, 1.0), (SQ, SQ), (SQ, -SQ))


def _ray_hits(px, py, ax, ay, bx, by):
    """Points X on segment AB such that P->X is horizontal, vertical or at 45 degrees; nearest to P first."""
    vx, vy = bx - ax, by - ay
    out = []
    for ux, uy in _RAYS:
        den = vx * uy - vy * ux
        if abs(den) < 1e-12:
            continue
        s = ((px - ax) * uy - (py - ay) * ux) / den
        if -1e-9 <= s <= 1 + 1e-9:
            x, y = q(ax + s * vx), q(ay + s * vy)
            out.append((math.hypot(x - px, y - py), x, y))
    out.sort()
    return [(x, y) for _d, x, y in out]


def _join_cands(anchor, pts, p):
    """Ways to reach ``anchor`` from the polyline end ``pts[0]``: [(new polyline, leading segments to check, label)],
    best first; the last one is the plain stub."""
    ax, ay = pts[0]
    if anchor[0] == "pt":
        px, py = anchor[1], anchor[2]
        if abs(px - ax) < 1e-6 and abs(py - ay) < 1e-6:
            return [([(px, py)] + pts[1:], 0, "on")]
        out = []
        if len(pts) >= 3:
            # slide the first run sideways so that it passes through the anchor: no stub at all
            (bx, by), (cx, cy) = pts[1], pts[2]
            ux, uy = ax - bx, ay - by
            vx, vy = bx - cx, by - cy
            den = vx * uy - vy * ux
            L = math.hypot(vx, vy)
            if abs(den) > 1e-12 and L > 1e-9:
                s = ((px - cx) * uy - (py - cy) * ux) / den
                x, y = q(cx + s * vx), q(cy + s * vy)
                if s > 1e-9 and (s - 1) * L <= p and (px - x) * ux + (py - y) * uy > 0:
                    out.append(([(px, py), (x, y)] + pts[2:], 2, "slide"))
        if len(pts) >= 2:
            bx, by = pts[1]
            for x, y in _ray_hits(px, py, ax, ay, bx, by):
                if (x - px) * (bx - x) + (y - py) * (by - y) < -1e-12:
                    continue                              # would double back
                if abs(x - px) < 1e-6 and abs(y - py) < 1e-6:
                    out.append(([(px, py)] + pts[1:], 1, "on"))
                elif abs(x - bx) < 1e-6 and abs(y - by) < 1e-6:
                    out.append(([(px, py)] + pts[1:], 1, "ray"))
                else:
                    out.append(([(px, py), (x, y)] + pts[1:], 2, "ray"))
        dx, dy = ax - px, ay - py
        if not _aligned(dx, dy):
            m = min(abs(dx), abs(dy))
            sx = 1.0 if dx > 0 else -1.0
            sy = 1.0 if dy > 0 else -1.0
            out.append(([(px, py), (q(px + sx * m), q(py + sy * m))] + pts, 2, "dogleg"))
            out.append(([(px, py), (q(ax - sx * m), q(ay - sy * m))] + pts, 2, "dogleg"))
        out.append(([(px, py)] + pts, 1, "stub"))
        return out
    _k, x1, y1, x2, y2 = anchor
    if geom.pt_seg_dist(ax, ay, x1, y1, x2, y2) <= 1e-6:
        return [(pts, 0, "on")]
    out = []
    if len(pts) >= 2:
        dx, dy = ax - pts[1][0], ay - pts[1][1]
        L = math.hypot(dx, dy)
        if L > 1e-9:
            hit = _line_hit(ax, ay, dx / L, dy / L, x1, y1, x2, y2, 3 * p)
            if hit:
                out.append(([hit] + pts, 1, "tee_ext"))
    for x, y in _ray_hits(ax, ay, x1, y1, x2, y2):
        if math.hypot(x - ax, y - ay) <= 2 * p:
            out.append(([(x, y)] + pts, 1, "tee_ray"))
    x, y = geom.pt_seg_proj(ax, ay, x1, y1, x2, y2)
    out.append(([(q(x), q(y))] + pts, 1, "tee_proj"))
    return out


def _line_hit(px, py, ux, uy, x1, y1, x2, y2, reach):
    vx, vy = x2 - x1, y2 - y1
    den = vx * uy - vy * ux
    if abs(den) < 1e-12:
        return None
    s = ((px - x1) * uy - (py - y1) * ux) / den
    if not (-1e-9 <= s <= 1 + 1e-9):
        return None
    x, y = x1 + s * vx, y1 + s * vy
    t = (x - px) * ux + (y - py) * uy
    if t < 0 or t > reach:
        return None
    return q(x), q(y)


def emit(ctx: Ctx, conn: ConnV2, win: Window, path: list[int], width: float) -> list[dict] | Blocker:
    """Cell path -> piece fields for ``RouteState.add`` (segments in path order, a via at each layer change),
    or the Blocker of the first segment / via that fails the oracle (``win.last_reject`` then says what failed).

    The ends are joined to the anchors in ``win.ends`` (set by the caller: ``({state: anchor}, {state: anchor})``
    for the source and the target side; an anchor is ``('pt', x, y)`` or ``('seg', x1, y1, x2, y2)`` for a tee)."""
    orc = ctx.oracle
    g = win.grid
    G, W = g.G, g.W
    net, cl = conn.net, conn.clearance or None
    vs, vd = win.via
    runs = []
    vias = []
    cur = None
    for s in path:
        li = s // G
        i = s - li * G
        xy = (g.mx(i % W), g.my(i // W))
        if li != cur:
            if cur is not None and (not vias or vias[-1][1] != xy):
                vias.append((len(runs), xy))
            runs.append([win.layers[li], [xy]])
            cur = li
        else:
            runs[-1][1].append(xy)
    win.last_reject = None
    src, tgt = win.ends or ({}, {})
    for which, anchor in ((0, src.get(path[0])), (-1, tgt.get(path[-1]))):
        if anchor is None:
            continue
        lay = runs[which][0]
        pts = _simplify(runs[which][1])
        if which:
            pts.reverse()
        chosen = None
        cands = _join_cands(anchor, pts, win.pitch)
        for cand, k, label in cands:
            good = True
            for j in range(min(k, len(cand) - 1)):
                if not orc.seg_legal(lay, cand[j][0], cand[j][1], cand[j + 1][0], cand[j + 1][1], width, net, cl):
                    good = False
                    break
            if good:
                chosen = list(cand)
                joins = _stats(ctx)["joins"]
                joins[label] = joins.get(label, 0) + 1
                break
        if chosen is None:
            cand = cands[-1][0]
            b = orc.seg_ok(lay, cand[0][0], cand[0][1], cand[1][0], cand[1][1], width, net, cl)
            win.last_reject = ("end", lay, path[0] if which == 0 else path[-1])
            return b or _generic_blocker(lay, cand[1])
        if which:
            chosen.reverse()
        runs[which][1] = chosen
    out = []
    via_at = {k: xy for k, xy in vias}
    tw = max(conn.width, width)
    for k, (lay, pts) in enumerate(runs):
        if k in via_at:
            x, y = via_at[k]
            if not orc.via_legal(x, y, vs, vd, net, clearance=cl):
                win.last_reject = ("via", lay, (x, y))
                return orc.via_ok(x, y, vs, vd, net, clearance=cl) or _generic_blocker(lay, (x, y))
            out.append({"kind": "via", "net": net, "conn": conn.idx, "layer": -1, "x1": x, "y1": y, "x2": x, "y2": y,
                        "w": vs, "drill": vd, "stage": "detail"})
        pts = _simplify([(q(x), q(y)) for x, y in pts])
        for j in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[j], pts[j + 1]
            if not orc.seg_legal(lay, x1, y1, x2, y2, width, net, cl):
                win.last_reject = ("seg", lay, (x1, y1, x2, y2))
                return orc.seg_ok(lay, x1, y1, x2, y2, width, net, cl) or _generic_blocker(lay, (x1, y1))
            wl = layer_width(ctx, conn, lay, width)
            out.append({"kind": "seg", "net": net, "conn": conn.idx, "layer": lay, "x1": x1, "y1": y1, "x2": x2,
                        "y2": y2, "w": wl, "stage": "detail", "target_w": tw if wl == width else wl})
    return out


def layer_width(ctx: Ctx, conn: ConnV2, layer: int, width: float) -> float:
    """The width a segment of ``conn`` is laid at on ``layer``: ``width``, unless the net carries a per-layer
    width rule (plan ``nets[..].widths`` or a KiCad ``track_width`` rule with a layer: design §14.8 "width
    classes") and ``width`` is the connection's full width — the search ran at the widest of the layers, so the
    narrower layer width is legal wherever the path is."""
    cons = getattr(ctx.x, "cons", None)
    if cons is None or abs(width - conn.width) > 1e-9 or not hasattr(cons, "lay_width"):
        return width
    wl = cons.lay_width(conn.net, layer, conn.width)
    return width if wl == conn.width else q(wl)


def _generic_blocker(layer, at) -> Blocker:
    return Blocker(rule="clearance", net=None, tag=None, static=True, piece=None, dist=0.0, need=0.0,
                   at=(at[0], at[1]), layer=layer)


def _block_reject(win: Window, path, blk: Blocker, sources: dict, tgt: dict) -> None:
    """Close the cells of the rejected piece so the next search goes another way."""
    g = win.grid
    G, W = g.G, g.W
    kind, lay, what = win.last_reject or ("seg", blk.layer, None)
    if kind == "via":
        ix, iy = g.cell(what[0], what[1])
        win.block_via([iy * W + ix])
        return
    if kind == "end":
        cells = [what]
    else:
        li = win.lidx.get(lay, -1)
        ax, ay = blk.at
        if not (math.isfinite(ax) and math.isfinite(ay)):
            ax, ay = (what[0] + what[2]) / 2, (what[1] + what[3]) / 2
        near = []
        for s in path:
            if s // G != li:
                continue
            i = s % G
            near.append((math.hypot(g.mx(i % W) - ax, g.my(i // W) - ay), s))
        near.sort()
        r = 1.5 * win.pitch
        cells = [s for d, s in near if d <= r] or [s for _d, s in near[:1]]
        x1, y1, x2, y2 = what
        cells = [s for s in cells
                 if geom.pt_seg_dist(g.mx((s % G) % W), g.my((s % G) // W), x1, y1, x2, y2) <= win.pitch] or cells[:1]
    win.block(cells)
    for s in cells:
        sources.pop(s, None)
        tgt.pop(s, None)


def _stitch(ctx: Ctx, conn: ConnV2, win: Window, sources: dict, sanc: dict, tgt: dict, budget: Budget):
    """A bridge that is one via: a lattice point that lies in the source side's copper on one layer and in the
    target side's on another (two pour islands of the net over each other). The window's via map is a raster with
    a guard band; here every candidate is asked of the exact checker, deepest inside both sides first.
    -> [Piece] (added, connected, committed) | None."""
    g = win.grid
    G, W = g.G, g.W
    orc, state = ctx.oracle, ctx.state
    vs, vd = win.via
    if vs <= 0:
        return None
    by2d = {}
    for t, anc in tgt.items():
        if anc[0] == "pt":
            by2d.setdefault(t % G, []).append(t)
    cands = set()
    for s in sources:
        anc = sanc.get(s)
        i = s % G
        if anc is None or anc[0] != "pt" or i not in by2d:
            continue
        x, y = g.xy(i)
        if abs(anc[1] - x) > 1e-6 or abs(anc[2] - y) > 1e-6:
            continue                        # a pad's stub cell, not copper under the point itself
        if any(t // G != s // G and abs(tgt[t][1] - x) <= 1e-6 and abs(tgt[t][2] - y) <= 1e-6 for t in by2d[i]):
            cands.add(i)
    if not cands:
        return None

    def depth(i):
        n = 0
        for d in (-W - 1, -W, -W + 1, -1, 1, W - 1, W, W + 1):
            if i + d in cands:
                n += 1
        return n
    order = sorted(cands, key=lambda i: (-depth(i), i))[:STITCH_TRIES]
    cl = conn.clearance or None
    checks = orc.checks
    try:
        for i in order:
            x, y = g.xy(i)
            if not orc.via_legal(x, y, vs, vd, conn.net, clearance=cl):
                continue
            tok = state.begin()
            pc = state.add(kind="via", net=conn.net, conn=conn.idx, layer=-1, x1=x, y1=y, x2=x, y2=y, w=vs, drill=vd,
                           stage="detail")
            if state.connected(conn.idx):
                state.commit(tok)
                return [pc]
            state.rollback(tok)
    finally:
        _charge_checks(ctx, budget, checks)
    return None


# ── one window ────────────────────────────────────────────────────────────
def _moves(ctx: Ctx, conn: ConnV2, win: Window, lays) -> tuple:
    nc = wnd.net_cfg(ctx, conn.net)
    g = win.grid
    bias = 0.0 if getattr(nc, "keep_short", False) else _cfg(ctx, "direction_bias", 0.5)
    lc = getattr(nc, "layer_cost", None) or {}
    ld = getattr(nc, "layer_dir", None) or {}
    if not ld and AUTO_DIR_BIAS > 0 and not getattr(nc, "keep_short", False):
        # no plan directions: the global stage's automatic ones (design §4.6), as a soft price
        st = ctx.history.get("global_state") if isinstance(getattr(ctx, "history", None), dict) else None
        ld = getattr(st, "dirs", None) or {}
        if ld:
            bias = AUTO_DIR_BIAS
    out = []
    for li, l in enumerate(win.layers):
        if l not in lays:
            out.append(None)
            continue
        mult = lc.get(l, 1.0)
        d = ld.get(l, "any")
        ch = mult * (1 + bias if d == "v" else 1.0)
        cv = mult * (1 + bias if d == "h" else 1.0)
        cd = mult * (1 + bias / 2 if d in ("h", "v") else 1.0)
        mv = astar.layer_moves(g.W, g.G, li, ch, cv, cd)
        if _cfg(ctx, "style", "45") == "90":
            mv = tuple(m for m in mv if not (m[1] and m[2]))
        out.append(mv)
    return tuple(out)


def _tile_dist(win: Window, mask, tgt) -> dict | None:
    """Reverse Dijkstra over the corridor's tiles from the tiles holding target cells: {(tx, ty): mm}."""
    if mask is None or win.col_tile is None:
        return None
    g = win.grid
    G, W = g.G, g.W
    T = win.tile_t
    seeds = set()
    for s in tgt:
        i = s % G
        seeds.add((win.col_tile[i % W], win.row_tile[i // W]))
    seeds = sorted(k for k in seeds if k in mask)
    if not seeds:
        return None
    from heapq import heappop, heappush
    dist = {k: 0.0 for k in seeds}
    heap = [(0.0, k) for k in seeds]
    while heap:
        d, (tx, ty) = heappop(heap)
        if d > dist[(tx, ty)]:
            continue
        for dx, dy in astar.DIRS:
            k = (tx + dx, ty + dy)
            if k not in mask:
                continue
            nd = d + (T if (dx == 0 or dy == 0) else T * 1.4142135623730951)
            if nd < dist.get(k, 1e18):
                dist[k] = nd
                heappush(heap, (nd, k))
    return dist


def _soft_table(ctx: Ctx, conn: ConnV2, win: Window, soft_cost) -> dict:
    """Per-piece price (steps per halo cell) from a per-connection price in mm: a number, {conn idx: mm} or a
    callable(Piece) -> mm | None. Crossing a piece's halo costs about its connection's price in total."""
    out = {}
    p = win.pitch
    for pc in ctx.state.pieces():
        if pc.net == conn.net:
            continue
        if callable(soft_cost):
            mm = soft_cost(pc)
        elif isinstance(soft_cost, dict):
            mm = soft_cost.get(pc.conn, soft_cost.get(None))
        else:
            mm = float(soft_cost)
        if mm is None:
            continue
        halo = max(1.0, (pc.w + win.width + 2 * win.clearance) / p)
        out[pc.id] = mm / p / halo
    return out


def _route_window(ctx: Ctx, conn: ConnV2, budget: Budget, *, mask, box, width: float, limit: int, pitch_scale: float,
                  extra_vias: int, soft_cost, lays, strict: bool, bridge=None, via_limit: int | None = None,
                  via_scale: float = 1.0, accept=None) -> dict:
    """One rung of the ladder: build the window, find a path, emit it. -> {ok, ...} | {fail...} | {plan...}"""
    st = _stats(ctx)
    orc, state = ctx.oracle, ctx.state
    soft = soft_cost is not None
    pitch = None
    if pitch_scale != 1.0:
        cl = max(conn.clearance or 0.0, orc.clearance(conn.net))
        pitch = max(0.025, round(wnd.choose_pitch(ctx.board, box, width, cl, len(lays))[0] * pitch_scale, 4))
    t0 = time.perf_counter()
    win = wnd.build(ctx, conn, mask, box, width=width, pitch=pitch, soft=soft, layers=lays)
    budget.charge(CHARGE_WINDOW)
    st["windows"] += 1
    st["window_cells"] += win.grid.N
    st["window_shapes"] += win.shapes
    st["build_s"] += time.perf_counter() - t0
    p = win.pitch
    nid = win.net_id
    checks = orc.checks
    sources, sanc, tgt, stub_victims = _ends(ctx, conn, win, width, lays, soft, strict, bridge)
    _charge_checks(ctx, budget, checks)
    res = {"ok": False, "win": win, "sources": dict(sources), "width": width, "open": True}
    if not sources or not tgt:
        end = "a" if not sources else "b"
        res.update(reason="no_start" if not sources else "no_target", enclosed=end, expanded=0, open=False,
                   where=[conn.a.x, conn.a.y] if not sources else [conn.b.x, conn.b.y])
        return res
    if not soft and bridge is None and len(tgt) <= PROBE_MAX_TARGETS:
        st["probes"] += 1
        pr = srch.probe(win, nid, tgt, PROBE_LIMIT, budget, layers_ok=lays, vias=conn.via[0] > 0)
        if pr["enclosed"]:
            res.update(reason="enclosed", enclosed="b", expanded=pr["reached"], open=False, probe=pr,
                       where=[conn.b.x, conn.b.y])
            return res
    nc = wnd.net_cfg(ctx, conn.net)
    max_vias = getattr(nc, "max_vias", None)
    if max_vias is not None:
        max_vias += extra_vias
    if via_limit is not None:                 # the via-frugal re-route (polish.revia): fewer vias than before, or nothing
        max_vias = via_limit if max_vias is None else min(max_vias, via_limit)
    via_mm = getattr(nc, "via_cost", None)
    if via_mm is None:
        via_mm = _cfg(ctx, "via_cost", 5.0)
    via_mm *= via_scale
    via_cost = via_mm / p if (max_vias != 0 and len(lays) > 1 and conn.via[0] > 0) else None
    if bridge is not None and via_cost is not None and not soft:
        got = _stitch(ctx, conn, win, sources, sanc, tgt, budget)
        if got is not None:
            res.update(ok=True, pieces=got, expanded=0, vias=1)
            return res
    turn = astar.turn_table(_cfg(ctx, "bend_cost", 0.3) / p)
    moves = _moves(ctx, conn, win, lays)
    sc = _soft_table(ctx, conn, win, soft_cost) if soft else None
    win.ends = (sanc, tgt)
    bend = 0 < win.grid.N <= BEND_STATE_CELLS
    via_mult = 1.0
    last = None
    for _attempt in range(RESEARCH + 1):
        if not sources or not tgt:
            break
        # a bridge joins two regions that usually lie side by side: uniform-cost search (no aim at one target)
        h = srch.heuristic(win, tgt, _tile_dist(win, mask, tgt),
                           _cfg(ctx, "hweight", 1.15) if bridge is None else 0.0)
        t0 = time.perf_counter()
        path, info = srch.search(win, nid, sources, tgt, h=h, turn=turn,
                                 via_cost=None if via_cost is None else via_cost * via_mult, layers_ok=lays,
                                 cmul=win.cmul, hist=win.hist, soft_cost=sc, bend_state=bend, limit=limit,
                                 budget=budget, moves=moves)
        st["search_s"] += time.perf_counter() - t0
        st["searches"] += 1
        st["expansions"] += info["expanded"]
        st["search_sizes"].append(info["expanded"])
        if path is None:
            best = info["best"]
            where = list(win.xy(best)[:2]) if best >= 0 else [conn.a.x, conn.a.y]
            reason = info["reason"]
            enclosed = None
            if reason == "exhausted" and not info["open"] and info["expanded"] <= PROBE_LIMIT:
                reason, enclosed = "enclosed", "a"           # the start is walled in close by: "boxed in"
            res.update(reason=reason, enclosed=enclosed, expanded=info["expanded"], open=info["open"], where=where)
            return res if last is None else last
        if soft:
            cr = dict(info["crossed"])
            for pid in sorted(stub_victims):
                cr.setdefault(pid, 1)
            if cr:
                res.update(plan=True, crossed=cr, path=path)
                return res
        nv = sum(1 for i in range(1, len(path)) if path[i] // win.grid.G != path[i - 1] // win.grid.G)
        if max_vias is not None and nv > max_vias:
            last = dict(res, reason="max_vias", vias=nv, max_vias=max_vias, expanded=info["expanded"],
                        where=list(win.xy(path[0])[:2]))
            if via_mult < 16:
                via_mult *= 4.0
                continue
            return last
        checks = orc.checks
        t0 = time.perf_counter()
        out = emit(ctx, conn, win, path, width)
        st["emit_s"] += time.perf_counter() - t0
        _charge_checks(ctx, budget, checks)
        if isinstance(out, Blocker):
            st["emit_rejects"] += 1
            last = dict(res, reason="rejected", blocker=out, expanded=info["expanded"], where=list(out.at))
            _block_reject(win, path, out, sources, tgt)
            continue
        if accept is not None:
            # the path is a stand-in for other copper (a pair's centre line: pairs._accept lays the two lanes)
            checks = orc.checks
            got = accept(out, win)
            _charge_checks(ctx, budget, checks)
            if isinstance(got, Blocker):
                st["emit_rejects"] += 1
                last = dict(res, reason="rejected", blocker=got, expanded=info["expanded"], where=list(got.at))
                _block_reject(win, path, got, sources, tgt)
                continue
            if got:
                res.update(ok=True, pieces=list(got), expanded=info["expanded"], vias=nv)
                return res
            return dict(res, reason="not_connected", expanded=info["expanded"], where=list(win.xy(path[-1])[:2]))
        tok = state.begin()
        pieces = [state.add(**d) for d in out]
        if state.connected(conn.idx):
            state.commit(tok)
            res.update(ok=True, pieces=pieces, expanded=info["expanded"], vias=nv)
            return res
        state.rollback(tok)
        last = dict(res, reason="not_connected", expanded=info["expanded"], where=list(win.xy(path[-1])[:2]))
        return last
    return last or dict(res, reason="rejected", expanded=0, where=[conn.a.x, conn.a.y])


# ── the ladder ────────────────────────────────────────────────────────────
def _dilate(ctx: Ctx, tiles: frozenset, rings: int) -> frozenset:
    if rings <= 0:
        return tiles
    nx, ny = getattr(ctx.tiles, "nx", None), getattr(ctx.tiles, "ny", None)
    cur = set(tiles)
    for _ in range(rings):
        add = set()
        for tx, ty in cur:
            for dx, dy in astar.DIRS:
                k = (tx + dx, ty + dy)
                if k not in cur and (nx is None or 0 <= k[0] < nx) and (ny is None or 0 <= k[1] < ny):
                    add.add(k)
        cur |= add
    return frozenset(cur)


def _terminal_box(conn: ConnV2, grow: float, pad: float):
    x0, x1 = min(conn.a.x, conn.b.x), max(conn.a.x, conn.b.x)
    y0, y1 = min(conn.a.y, conn.b.y), max(conn.a.y, conn.b.y)
    gx, gy = (x1 - x0) * grow + pad, (y1 - y0) * grow + pad
    return (x0 - gx, y0 - gy, x1 + gx, y1 + gy)


def _corridor_box(ctx: Ctx, conn: ConnV2, rings: int):
    """-> (tiles mask | None, box) of the connection's corridor widened by ``rings`` rings."""
    cor = (ctx.corridors or {}).get(conn.idx)
    if cor is not None and cor.tiles and ctx.tiles is not None:
        mask = _dilate(ctx, frozenset(cor.tiles), rings)
        b = wnd.tiles_box(ctx.tiles, mask)
        box = (b[0] - BOX_MARGIN, b[1] - BOX_MARGIN, b[2] + BOX_MARGIN, b[3] + BOX_MARGIN)
        tb = _terminal_box(conn, 0.0, BOX_MARGIN)
        return mask, (min(box[0], tb[0]), min(box[1], tb[1]), max(box[2], tb[2]), max(box[3], tb[3]))
    return None, _terminal_box(conn, 0.1, 1.5 + rings * RING_MM)


def _limit(ctx: Ctx, conn: ConnV2, p: float) -> int:
    est = conn.est or (max(abs(conn.a.x - conn.b.x), abs(conn.a.y - conn.b.y))
                       + 0.41421356 * min(abs(conn.a.x - conn.b.x), abs(conn.a.y - conn.b.y)))
    return int(min(SEARCH_MAX, SEARCH_BASE + SEARCH_PER_STEP * est / p) * _cfg(ctx, "effort", 1.0))


def _blocked_by(ctx: Ctx, conn: ConnV2, rec: dict, budget: Budget):
    """-> (blockers ranked, static_share, frontier points) for a failure record, from a flood in its window."""
    win, sources, width = rec.get("win"), rec.get("sources"), rec.get("width") or conn.width
    if win is None:
        return [], 1.0, []
    orc = ctx.oracle
    if not sources or rec.get("reason") in ("no_start", "no_target"):
        return _stub_blockers(ctx, conn, conn.b if rec.get("enclosed") == "b" else conn.a, width, budget)
    pr = rec.get("probe")
    if pr is None or rec.get("enclosed") != "b":
        # a search that ran dry is re-flooded in full, so the copper that closes the region off is seen even
        # when it is far from the start; after a 'limit' failure the flood (and static_share) is local
        lim = PROBE_LIMIT
        if rec.get("reason") in ("exhausted", "enclosed"):
            lim = min(SEARCH_MAX, int(rec.get("expanded") or 0) + PROBE_LIMIT)
        pr = srch.probe(win, win.net_id, sources, lim, budget, layers_ok=win.layers, vias=conn.via[0] > 0)
    by_id = None
    tally = {}
    for key, n in pr["frontier_blockers"]:
        if key == "static":
            continue
        if by_id is None:
            by_id = {p.id: p for p in ctx.state.pieces()}
        pc = by_id.get(key)
        k = (pc.net if pc else None, "via" if pc and pc.kind == "via" else "track", False, pc.conn if pc else None)
        tally[k] = tally.get(k, 0) + n
    static_n = dict(pr["frontier_blockers"]).get("static", 0)
    names = {}
    checks = orc.checks
    for s, n in pr["frontier"][:8]:
        x1, y1, l = win.xy(s)
        x2, y2, _l = win.xy(n)
        x2, y2 = x1 + 3 * (x2 - x1), y1 + 3 * (y2 - y1)      # well into the halo (its rim is only the raster guard)
        for b in orc.blockers(l, x1, y1, x2, y2, width, conn.net, limit=3, clearance=conn.clearance or None):
            if b.rule in ("thermal", "reserve") or not b.static:
                continue
            k = (b.net, b.tag, True, None)
            names[k] = names.get(k, 0) + 1
    _charge_checks(ctx, budget, checks)
    tot = sum(names.values())
    for k, n in names.items():
        tally[k] = tally.get(k, 0) + (max(1, round(static_n * n / tot)) if static_n else n)
    if static_n and not names:
        tally[(None, "static", True, None)] = static_n
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], repr(kv[0])))
    out = [{"net": k[0], "tag": k[1], "static": k[2], "conn": k[3], "count": n} for k, n in ranked[:8]]
    return out, pr["static_share"], [list(win.xy(s)[:2]) for s, _n in pr["frontier"]]


def _stub_blockers(ctx: Ctx, conn: ConnV2, term, width: float, budget: Budget):
    """What stands in the way of a short stub out of a terminal, in 8 directions: (ranked, static share, None)."""
    orc = ctx.oracle
    reach = _pad_extent(ctx, term.pad) / 2 + 0.6
    tally = {}
    checks = orc.checks
    by_id = None
    for l in term.layers:
        for dx, dy in astar.DIRS:
            k = 1.0 if (dx == 0 or dy == 0) else SQ
            for b in orc.blockers(l, term.x, term.y, term.x + dx * k * reach, term.y + dy * k * reach, width,
                                  conn.net, limit=4, clearance=conn.clearance or None):
                if b.rule in ("thermal", "reserve"):
                    continue
                cn = None
                if b.piece is not None:
                    if by_id is None:
                        by_id = {p.id: p for p in ctx.state.pieces()}
                    cn = by_id[b.piece].conn if b.piece in by_id else None
                key = (b.net, b.tag, b.static, cn)
                tally[key] = tally.get(key, 0) + 1
    _charge_checks(ctx, budget, checks)
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], repr(kv[0])))
    tot = sum(tally.values())
    st = sum(n for k, n in tally.items() if k[2])
    out = [{"net": k[0], "tag": k[1], "static": k[2], "conn": k[3], "count": n} for k, n in ranked[:8]]
    return out, (round(st / tot, 3) if tot else 1.0), [[term.x, term.y]]


def _fail(ctx: Ctx, conn: ConnV2, rec: dict | None, tried: list, budget: Budget) -> dict:
    rec = rec or {}
    r = rec.get("reason") or "budget"
    blockers, share, frontier = _blocked_by(ctx, conn, rec, budget) if r != "budget" else ([], 1.0, [])
    blocked_by = list(dict.fromkeys(b["net"] for b in blockers if not b["static"] and b["net"]))
    width = rec.get("width") or conn.width
    cl = conn.clearance or ctx.oracle.clearance(conn.net)
    enclosed = rec.get("enclosed") or "none"
    if r in ("no_start", "enclosed", "no_target") and enclosed in ("a", "b"):
        t = conn.a if enclosed == "a" else conn.b
        name = t.pad or f"({round(t.x, 3)}, {round(t.y, 3)})"
        text = (f"pad {name} is boxed in: no way out for a {width} mm track (clearance {round(cl, 4)} mm) on the "
                f"allowed layers")
    elif r == "limit":
        text = REASON_EFFORT
    elif r == "budget":
        text = REASON_BUDGET
    elif r == "max_vias":
        text = f"needs {rec.get('vias')} vias but max_vias={rec.get('max_vias')}"
    elif r == "rejected":
        b = rec.get("blocker")
        text = ("every path found fails the exact clearance check"
                + (f" ({b.rule} to {b.tag or b.net or 'copper'})" if b is not None else ""))
    elif r == "not_connected":
        text = "the routed copper does not join the two ends (tee point not recognised)"
    else:
        text = ("blocked by routed nets: " + ", ".join(blocked_by[:6])) if blocked_by and share < 0.999 \
            else REASON_WALLED
    return {"reason": text, "code": r, "where": [round(v, 4) for v in (rec.get("where") or [conn.a.x, conn.a.y])],
            "blocked_by": blocked_by, "blockers": blockers, "static_share": share, "tried": list(tried),
            "probe": {"enclosed": enclosed}, "frontier": frontier, "width": width}


_RANK = {"budget": 0, "no_start": 1, "no_target": 1, "enclosed": 2, "exhausted": 3, "not_connected": 4,
         "max_vias": 5, "rejected": 6, "limit": 7}


def route_conn(ctx: Ctx, conn: ConnV2, budget: Budget, *, widen: int = 0, pitch_scale: float = 1.0,
               extra_vias: int = 0, soft_cost=None, bridge: dict | None = None, via_limit: int | None = None,
               via_scale: float = 1.0, rungs_max: int | None = None, lateral: float | None = None,
               accept=None) -> dict:
    """Route one connection (design §4.8 steps 1-6).

    accept       None = normal. Else ``accept(specs, win) -> [Piece] | Blocker | None``: the path found (piece
                 fields in path order, as ``emit`` returns them) is NOT added; the callback lays whatever it
                 stands for (a differential pair's two lanes: ``pairs._accept``) and returns the pieces it
                 added, a Blocker (that place cannot be used: its cells are closed and the search runs again) or
                 None (refused for a reason no place explains).

    bridge       None = normal. Else {'src': side, 'tgt': side, 'box': (x0, y0, x1, y1), 'all': box | None} with
                 side = {'pads': [Terminal], 'segs': [(layer, x1, y1, x2, y2)], 'vias': [(x, y)], 'islands':
                 [(layer, points)]}: the search starts from ALL copper of the source side and ends on ANY copper
                 of the target side (pads, tracks, vias, the inside of pour islands), in windows round ``box``
                 (then ``all``) — a stitch via between two overlapping islands is the shortest such path.
                 ``conn.b`` may be an open terminal; no pattern, no corridor.

    widen        rings added to the corridor from the first rung on (repair: 2)
    pitch_scale  window pitch multiplier (rescue: 0.5)
    extra_vias   added to the plan's ``max_vias``
    via_limit    a path with more vias than this is no result (on top of the plan's ``max_vias``)
    via_scale    multiplier of the via cost (polish.revia: the via-frugal re-route)
    rungs_max    at most this many rungs of the ladder (None = all)
    lateral      None = the ladder. Else one rung only: a window without corridor mask round both terminals, grown
                 by this many mm on every side (polish.revia: room for the detour a human takes instead of a via)
    soft_cost    None = normal. Else a rip price in mm per crossed connection (a number, {conn idx: mm, None:
                 default} or callable(Piece) -> mm | None): routed copper of other nets may be crossed at that
                 price. When the cheapest path crosses something NOTHING is added and the result is
                 ``{ok: False, plan: True, crossed: {conn idx: pieces crossed}, crossed_pieces: {piece id: cells}}``.

    -> {ok, pieces: [Piece], fail: {...} | None, crossed: {conn: n}, how: 'pattern'|'window'|'already', width, step}
       fail = {reason (text, v1 phrases), code ('enclosed'|'no_start'|'no_target'|'exhausted'|'limit'|'budget'|
               'max_vias'|'rejected'|'not_connected'|'outside'|'layer_pad'|'plan'), where [x, y],
               blocked_by [net names of ROUTED copper in the way, ranked], blockers [{net, tag, static, conn, count}],
               static_share (1.0 = no rip-up can help), tried [rungs], probe {'enclosed': 'a'|'b'|'none'},
               frontier [[x, y], ...], width}
    On success the pieces are in ``ctx.state`` (one committed transaction) and the connection's status is
    'routed'. On failure the state is untouched and the status is left to the caller.
    """
    st = _stats(ctx)
    state, orc = ctx.state, ctx.oracle
    out = {"ok": False, "pieces": [], "fail": None, "crossed": {}, "how": None, "width": conn.width, "step": None}
    tried = []
    if state.connected(conn.idx):
        state.set_status(conn.idx, "routed")
        out.update(ok=True, how="already")
        return out
    for t in ((conn.a,) if bridge is not None else (conn.a, conn.b)):
        if not orc.inside(q(t.x), q(t.y)):
            name = t.pad or "terminal"
            out["fail"] = {"reason": f"pad {name} at ({round(t.x, 2)}, {round(t.y, 2)}) is outside the board outline "
                                     f"— place the part on the board first (pcb_place)", "code": "outside",
                           "where": [t.x, t.y], "blocked_by": [], "blockers": [], "static_share": 1.0, "tried": tried,
                           "probe": {"enclosed": "none"}, "frontier": [], "width": conn.width}
            return out
    lays = wnd.allowed_layers(ctx, conn, corridor=False)     # every layer the plan allows
    lays_cor = wnd.allowed_layers(ctx, conn)                 # ... narrowed to the layers the global stage planned
    for end, t in ((("a", conn.a),) if bridge is not None else (("a", conn.a), ("b", conn.b))):
        if not any(l in lays for l in t.layers):
            why = (f"pad {t.pad or 'terminal'} is not on any layer this net may use "
                   f"({', '.join(str(l) for l in lays)}) — widen nets[...].layers / global.layers") if t.layers else \
                "this end has no copper terminal (a pour connection): left to the pours pass"
            out["fail"] = {"reason": why,
                           "code": "layer_pad", "where": [t.x, t.y], "blocked_by": [], "blockers": [],
                           "static_share": 1.0, "tried": tried, "probe": {"enclosed": end}, "frontier": [],
                           "width": conn.width}
            return out
    # 1: pattern
    if soft_cost is None and pitch_scale == 1.0 and bridge is None:
        tried.append("pattern")
        if budget.left() >= CHARGE_PATTERN:
            n0, c0 = st["pattern_tries"], orc.checks
            t0 = time.perf_counter()
            poly = pattern(ctx, conn)
            st["pattern_s"] += time.perf_counter() - t0
            budget.charge(CHARGE_PATTERN * (st["pattern_tries"] - n0))
            _charge_checks(ctx, budget, c0)
            if poly and accept is not None:
                got = accept([{"kind": "seg", "net": conn.net, "conn": conn.idx, "layer": poly[i][2],
                               "x1": poly[i][0], "y1": poly[i][1], "x2": poly[i + 1][0], "y2": poly[i + 1][1],
                               "w": conn.width, "stage": "detail", "target_w": conn.width}
                              for i in range(len(poly) - 1)], None)
                _charge_checks(ctx, budget, c0)
                if got and not isinstance(got, Blocker):
                    state.set_status(conn.idx, "routed")
                    out.update(ok=True, pieces=list(got), how="pattern", step="pattern")
                    return out
            elif poly:
                tok = state.begin()
                pieces = []
                for i in range(len(poly) - 1):
                    (x1, y1, l), (x2, y2, _l) = poly[i], poly[i + 1]
                    wl = layer_width(ctx, conn, l, conn.width)
                    pieces.append(state.add(kind="seg", net=conn.net, conn=conn.idx, layer=l, x1=x1, y1=y1, x2=x2,
                                            y2=y2, w=wl, stage="detail", target_w=wl))
                if state.connected(conn.idx):
                    state.commit(tok)
                    state.set_status(conn.idx, "routed")
                    out.update(ok=True, pieces=pieces, how="pattern", step="pattern")
                    return out
                state.rollback(tok)
    # 3-6: the ladder
    widths = [conn.width]
    if conn.neck and conn.neck < conn.width - 1e-6:
        widths.append(conn.neck)
    effort = _cfg(ctx, "effort", 1.0)
    rungs = [("corridor", widen, w) for w in widths] + [("corridor+2", widen + 2, w) for w in widths] \
        + [("wide", None, w) for w in widths]
    if bridge is not None:
        rungs = [(f"bridge{k}", k, w) for k in range(len(BRIDGE_GROW) + (1 if bridge.get("all") else 0))
                 for w in widths]
    if lateral is not None and bridge is None:
        rungs = [("lateral", "lat", w) for w in widths]
    if rungs_max is not None:
        rungs = rungs[:rungs_max]
    best = None
    dead = set()          # widths for which a terminal is walled in whatever the corridor
    for name, rings, width in rungs:
        if width in dead:
            continue
        if budget.left() < CHARGE_WINDOW + 4000 + 1:
            if best is None:
                best = {"reason": "budget"}
            break
        if bridge is not None:
            bb = bridge["box"] if rings < len(BRIDGE_GROW) else bridge["all"]
            gr = BRIDGE_GROW[rings] if rings < len(BRIDGE_GROW) else BRIDGE_GROW[0]
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            hx = min(BRIDGE_SPAN / 2, (bb[2] - bb[0]) / 2 + gr)
            hy = min(BRIDGE_SPAN / 2, (bb[3] - bb[1]) / 2 + gr)
            mask, box = None, (cx - hx, cy - hy, cx + hx, cy + hy)
        elif rings == "lat":
            mask, box = None, _terminal_box(conn, 0.0, float(lateral))
            rings = None
        elif rings is None:
            mask, box = None, _terminal_box(conn, WIDE_GROW, WIDE_PAD)
        else:
            mask, box = _corridor_box(ctx, conn, rings)
        rl = lays_cor if (rings == 0 and bridge is None) else lays   # planned layers bind only the un-widened corridor
        cl = max(conn.clearance or 0.0, orc.clearance(conn.net))
        p = wnd.choose_pitch(ctx.board, box, width, cl, len(rl))[0] * pitch_scale
        limit = int(WIDE_CAP * effort) if (rings is None or bridge is not None) else _limit(ctx, conn, p)
        label = f"{name}@{width:g}"
        tried.append(label)
        strict = False
        r = _route_window(ctx, conn, budget, mask=mask, box=box, width=width, limit=limit, pitch_scale=pitch_scale,
                          extra_vias=extra_vias, soft_cost=soft_cost, lays=rl, strict=strict, bridge=bridge,
                          via_limit=via_limit, via_scale=via_scale, accept=accept)
        if not r["ok"] and r.get("reason") == "not_connected" and conn.tree_target and bridge is None:
            tried.append(label + "/strict")
            r = _route_window(ctx, conn, budget, mask=mask, box=box, width=width, limit=limit,
                              pitch_scale=pitch_scale, extra_vias=extra_vias, soft_cost=soft_cost, lays=rl,
                              strict=True, via_limit=via_limit, via_scale=via_scale)
        if r["ok"]:
            state.set_status(conn.idx, "routed")
            if width < conn.width - 1e-6:
                st["necked"] += 1
            out.update(ok=True, pieces=r["pieces"], how="window", width=width, step=label)
            return out
        if r.get("plan"):
            st["soft_plans"] += 1
            by_id = {pc.id: pc for pc in state.pieces()}
            cr = {}
            for pid in r["crossed"]:
                pc = by_id.get(pid)
                k = pc.conn if pc is not None else -1
                cr[k] = cr.get(k, 0) + 1
            out.update(plan=True, crossed=dict(sorted(cr.items())), crossed_pieces=r["crossed"], width=width,
                       step=label)
            out["fail"] = {"reason": "blocked by routed copper (soft plan)", "code": "plan",
                           "where": [conn.a.x, conn.a.y], "blocked_by": [], "blockers": [], "static_share": 0.0,
                           "tried": tried, "probe": {"enclosed": "none"}, "frontier": [], "width": width,
                           "crossed": dict(sorted(cr.items()))}
            return out
        if best is None or _RANK.get(r.get("reason"), 3) >= _RANK.get(best.get("reason"), 3):
            best = r
        if bridge is None and r.get("reason") in ("enclosed", "no_start", "no_target", "exhausted") \
                and not r.get("open"):
            dead.add(width)               # the search ran dry without ever touching the corridor mask or the
            #                               window border: a wider corridor cannot help this width
        if r.get("reason") == "budget":
            break
    out["fail"] = _fail(ctx, conn, best, tried, budget)
    return out


def route_span(ctx: Ctx, conn: ConnV2, a, b, layer: int, budget: Budget, *, width: float | None = None,
               margin: float = 1.5) -> list[tuple] | None:
    """A single-layer route for ``conn``'s net between two points (no vias): ``[(x, y), ...]`` from ``a`` to ``b``,
    every segment oracle-legal at ``width`` (default the connection's), or None. NOTHING is added to the state —
    this is the window search as a service for polish (e.g. re-laying the span between two vias on one layer).
    The window is the box of both points grown by 10 % + ``margin`` mm (3 x ``margin`` once more when the search
    ran into the window border); the connection's own copper is no obstacle (same net)."""
    w = width or conn.width
    ta = Terminal(q(a[0]), q(a[1]), (layer,))
    tb = Terminal(q(b[0]), q(b[1]), (layer,))
    dx, dy = abs(ta.x - tb.x), abs(ta.y - tb.y)
    c = dataclasses.replace(conn, a=ta, b=tb, tree_target=False, est=max(dx, dy) + 0.41421356 * min(dx, dy))
    if dx < 1e-6 and dy < 1e-6:
        return [(ta.x, ta.y)]
    orc = ctx.oracle
    for grow in (margin, 3 * margin):                  # one wider try when the first window was too small
        if budget.left() < CHARGE_WINDOW + CHARGE_SEARCH + 1:
            return None
        win = wnd.build(ctx, c, None, _terminal_box(c, 0.1, grow), width=w, layers=(layer,))
        budget.charge(CHARGE_WINDOW)
        checks = orc.checks
        src = _term_cells(ctx, c, win, ta, w, (layer,), False)
        tgt = {s: v[1] for s, v in _term_cells(ctx, c, win, tb, w, (layer,), False).items()}
        _charge_checks(ctx, budget, checks)
        sources = {s: v[0] for s, v in src.items()}
        if not sources or not tgt:
            return None
        win.ends = ({s: v[1] for s, v in src.items()}, tgt)
        p = win.pitch
        turn = astar.turn_table(_cfg(ctx, "bend_cost", 0.3) / p)
        moves = _moves(ctx, c, win, (layer,))
        wider = False
        for _attempt in range(RESEARCH + 1):
            if not sources or not tgt:
                break
            path, info = srch.search(win, win.net_id, sources, tgt,
                                     h=srch.heuristic(win, tgt, None, _cfg(ctx, "hweight", 1.15)), turn=turn,
                                     via_cost=None, layers_ok=(layer,), cmul=win.cmul, hist=win.hist,
                                     limit=_limit(ctx, c, p), budget=budget, moves=moves)
            if path is None:
                wider = info["reason"] == "exhausted" and info["open"]
                break
            checks = orc.checks
            out = emit(ctx, c, win, path, w)
            _charge_checks(ctx, budget, checks)
            if isinstance(out, Blocker):
                _block_reject(win, path, out, sources, tgt)
                continue
            if not out:
                return None
            return [(out[0]["x1"], out[0]["y1"])] + [(d["x2"], d["y2"]) for d in out]
        if not wider:
            return None
    return None


# ── the stage ─────────────────────────────────────────────────────────────
def _pair_jobs(ctx: Ctx) -> dict:
    """{connection index: pairs.PairJob} of the coupled differential pairs; {} without constraints (every board
    that names no pair, ``global.constraints: off | report``) and when ``pairs.py`` is not in the build."""
    global _pairs
    cons = getattr(ctx.x, "cons", None)
    if cons is None or not hasattr(cons, "coupled_pairs") or not cons.coupled_pairs():
        return {}
    if _pairs is None:
        try:
            from . import pairs as _p
        except Exception:  # noqa: BLE001
            return {}
        _pairs = _p
    try:
        return _pairs.job_map(ctx)
    except Exception:  # noqa: BLE001 — a pair that cannot be set up is two nets, as before
        return {}


_pairs = None


def order(ctx: Ctx, only=None) -> list:
    """Connections in detail order: plan rank, then most corridor overflow first, then ``rank``."""
    pick = None if only is None else set(only)
    cs = [c for c in ctx.conns if pick is None or c.idx in pick]
    cor = ctx.corridors or {}

    def key(c):
        ov = cor[c.idx].overflow if c.idx in cor else 0.0
        return (c.rank[:1] if c.rank else (), -ov, c.rank, c.idx)
    return sorted(cs, key=key)


def run(ctx: Ctx, budget: Budget, only: list[int] | None = None, max_failed: int | None = None) -> StageReport:
    """Route every connection that is not routed yet (``only``: these connection indexes).
    ``max_failed``: stop (``data['aborted']``) once more pad-to-pad connections than this have failed for a
    reason other than budget — the engine's via-frugal trial gives up early on a board that is not easy."""
    st = _stats(ctx)
    state = ctx.state
    snap = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in st.items()}
    used0 = budget.used
    b0, h0 = wnd.STATS["builds"], wnd.STATS["hits"]
    rep = StageReport(name="detail")
    todo = [c for c in order(ctx, only) if state.status(c.idx) != "routed"]
    how = {"pattern": 0, "window": 0, "already": 0}
    codes = {}
    failed = []
    hard = 0
    aborted = False
    pairs_n = [0, 0]
    pairs_work = 0
    pairs_tried = set()
    pair_jobs = _pair_jobs(ctx)
    for k, conn in enumerate(todo):
        if budget.left() < CHARGE_PATTERN:
            r = {"ok": False, "fail": _fail(ctx, conn, {"reason": "budget"}, [], budget)}
        else:
            job = pair_jobs.get(conn.idx)
            if job is not None and state.status(conn.idx) != "routed" and (job.p, job.n) not in pairs_tried:
                pairs_tried.add((job.p, job.n))
                # a coupled differential pair: both nets as one object (design §14.8). When that is refused
                # nothing was added, and this connection and its partner are routed one by one as before
                pb = budget
                if PAIR_BUDGET == "own":
                    # the attempt has its own allowance: a pair that cannot be coupled must not cost the other
                    # connections their search budget (measured on cm5_minima: interface notes, step C)
                    pb = Budget(PAIR_CAP)
                pr = _pairs.route_pair(ctx, job, pb)
                pairs_work += pb.used if pb is not budget else 0
                pairs_n[0 if pr["ok"] else 1] += 1
            r = route_conn(ctx, conn, budget)
        if r["ok"]:
            rep.routed += 1
            how[r["how"]] = how.get(r["how"], 0) + 1
        else:
            rep.failed += 1
            codes[r["fail"]["code"]] = codes.get(r["fail"]["code"], 0) + 1
            state.set_status(conn.idx, "failed", r["fail"])
            f = r["fail"]
            if f["code"] not in ("budget", "layer_pad", "outside") and (conn.b.pad or conn.b.cluster or conn.b.layers):
                hard += 1
            failed.append({"conn": conn.idx, "net": conn.net, "from": conn.a.pad, "to": conn.b.pad,
                           "reason": f["reason"], "code": f["code"], "where": f["where"],
                           "blocked_by": f["blocked_by"], "static_share": f["static_share"], "tried": f["tried"],
                           "probe": f["probe"]})
            if max_failed is not None and hard > max_failed:
                aborted = True
                break
        if ctx.progress is not None:
            try:
                ctx.progress((k + 1) / len(todo), f"detail {k + 1}/{len(todo)}: {conn.net}")
            except Exception:  # noqa: BLE001  (a progress callback must never break routing)
                pass
    rep.work = budget.used - used0
    try:
        rep.score = state.score()
    except Exception:  # noqa: BLE001
        pass
    d = {k: (st[k] - snap[k]) for k in st if isinstance(st[k], (int, float))}
    sizes = sorted(st["search_sizes"][len(snap["search_sizes"]):])
    d["median_expansions"] = sizes[len(sizes) // 2] if sizes else 0
    d["window_builds"] = wnd.STATS["builds"] - b0
    d["window_cache_hits"] = wnd.STATS["hits"] - h0
    tot = d["build_s"] + d["search_s"] + d["emit_s"] + d["pattern_s"]
    d["build_share"] = round(d["build_s"] / tot, 3) if tot > 0 else 0.0
    for k in ("build_s", "search_s", "emit_s", "pattern_s"):
        d[k] = round(d[k], 3)
    d["joins"] = {k: v - snap["joins"].get(k, 0) for k, v in sorted(st["joins"].items())}
    d["failed"] = failed
    d["hard_failed"] = hard
    if aborted:
        d["aborted"] = True
    d["how"] = how
    d["fail_codes"] = dict(sorted(codes.items()))
    rep.data = d
    rep.notes.append(f"{how['pattern']} by pattern, {how['window']} by window search, {rep.failed} failed; "
                     f"{d['windows']} windows, {d['searches']} searches, median {d['median_expansions']} expansions")
    if pairs_n[0] or pairs_n[1]:
        d["pairs"] = {"coupled": pairs_n[0], "sequential": pairs_n[1]}
        if pairs_work:
            d["pairs"]["work"] = pairs_work
        rep.notes.append(f"{pairs_n[0]} differential pair connection(s) laid as coupled lanes"
                         + (f", {pairs_n[1]} routed as two nets (the constraints block says why)" if pairs_n[1] else ""))
    if codes.get("budget"):
        rep.notes.append(f"{codes['budget']} connection(s) not finished for lack of budget")
    if d["necked"]:
        rep.notes.append(f"{d['necked']} connection(s) routed at neck width (target_w keeps the full width)")
    if budget.spent_out():
        rep.notes.append("detail budget used up")
    return rep
