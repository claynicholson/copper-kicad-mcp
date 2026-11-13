"""Window grid: a short-lived ``guided.grid.Grid`` rasterised fresh from the oracle (design §4.7).

A window covers one box of the board for ONE connection's probe (net, track width, clearance, via). Its pitch
comes from the local pad pitch (``choose_pitch``) and its origin is shifted so that the dominant pad-centre
residue falls on cell centres. Nothing is shared between windows: every one is rasterised from
``oracle.near`` / ``oracle.need``, so windows of different pitch and origin never share lattice state and the
oracle stays the only judge (the grid is an accelerator; every emitted segment is still checked exactly).

Cell values (``cm.S`` static, ``cm.occ`` static + routed copper; one block of ``G`` cells per window layer)

    0         free for this window's net
    -1        blocked (foreign copper / hole / edge / keep-out halo, or off the board)
    MASKED    (-2) outside the corridor mask or on the window border: blocked for the search, but the probe
              and the search report having touched it (``open``): a wider corridor could go on there

``cm.SV`` / ``cm.vocc`` are the same for a through via centre (one block of ``G`` cells; a via must be legal
on EVERY board layer, not just the window's). ``owner[state]`` / ``vowner[cell]`` hold the id of the routed
piece whose halo covers a cell (0 = none): hard in a normal search, priced in a soft search.

Inflation of a shape: ``need + half_width - TOL_MM + max(GUARD_MM, raster_guard(pitch, half_width + need))``
— the guard is what makes a straight step between two free neighbouring cells legal past a convex corner
(design Appendix A1; a fixed guard does not).
"""
from __future__ import annotations

import math
import weakref
from array import array
from collections import OrderedDict

from ..guided.grid import ClassMap, Grid
from .oracle import NO_NEED, PLAN_FIRST_AUTO, raster_guard
from .types import GUARD_MM, TOL_MM, ConnV2, Ctx

PITCHES = (0.25, 0.2, 0.15, 0.125, 0.1, 0.075, 0.05)     # the ladder of design §4.7
COARSE = (0.3, 0.4, 0.5)          # only when even 0.25 mm gives more than MAX_STATES states
MAX_STATES = 400_000              # cells x layers of one window
FINE_PITCH = 1.3                  # mm: footprints whose pad pitch is at most this constrain the window pitch
MASKED = -2
SOFT_PRICE = {"thermal": 6.0, "reserve": 6.0}     # cost multiplier inside a soft shape of another net
PAD_REPEL = 1.25                  # cost multiplier within one cell of a foreign pad's halo (0 = off)
VIA_IN_PAD = 0.6                  # a via centre keeps 0.6 x its radius away from an SMD pad of its own net (v1 policy)
TERMINAL_OPEN = 1.0               # mm round both terminals that the corridor mask never closes
LRU_SIZE = 8

_NEG = array("i", [-1])
_ZERO = array("i", [0])


class Window:
    """One rasterised box. ``state = layer_index * G + iy * W + ix`` (layer_index = position in ``layers``)."""
    __slots__ = ("grid", "pitch", "layers", "lidx", "box", "cm", "owner", "vowner", "multi", "vmulti", "cmul", "hist",
                 "net", "net_id", "width", "clearance", "via", "soft", "col_tile", "row_tile", "tile_t", "key",
                 "shapes", "last_reject", "ends", "_undo", "_vundo", "__weakref__")

    def cell(self, x: float, y: float, layer: int) -> int:
        """Search state of the cell nearest to (x, y) on board layer ``layer``."""
        g = self.grid
        ix, iy = g.cell(x, y)
        return self.lidx[layer] * g.G + iy * g.W + ix

    def xy(self, state: int) -> tuple[float, float, int]:
        """-> (x mm, y mm, board layer) of a search state; coordinates are on the 1 nm grid."""
        g = self.grid
        i = state % g.G
        return g.mx(i % g.W), g.my(i // g.W), self.layers[state // g.G]

    def free(self, state: int) -> bool:
        o = self.cm.occ[state]
        return o == 0 or o == self.net_id

    def block(self, cells) -> None:
        """Close search states for every later search in this window (an emitted segment through them failed
        the exact check). Undone by ``reset()``, so a cached window equals a rebuilt one."""
        occ = self.cm.occ
        undo = self._undo
        for c in cells:
            undo.append((c, occ[c]))
            occ[c] = -1

    def block_via(self, cells) -> None:
        """Same for via sites; ``cells`` are 2-D cell indexes (``state % G``)."""
        vocc = self.cm.vocc
        undo = self._vundo
        for c in cells:
            undo.append((c, vocc[c]))
            vocc[c] = -1

    def reset(self) -> None:
        occ, vocc = self.cm.occ, self.cm.vocc
        while self._undo:
            c, v = self._undo.pop()
            occ[c] = v
        while self._vundo:
            c, v = self._vundo.pop()
            vocc[c] = v
        self.last_reject = None
        self.ends = None


# ── board facts ───────────────────────────────────────────────────────────
def _fp_pitch(board) -> dict:
    """ref -> (pad pitch mm, x0, y0, x1, y1): the smallest centre distance between two pads of the footprint."""
    info = getattr(board, "_v2_fp_pitch", None)
    if info is not None:
        return info
    by = {}
    for p in board.pads:
        by.setdefault(p.ref, []).append((p.x, p.y))
    info = {}
    for ref in sorted(by):
        pts = sorted(set(by[ref]))
        if len(pts) < 2:
            continue
        best = math.inf
        n = len(pts)
        for i in range(n):
            x, y = pts[i]
            for j in range(i + 1, n):
                dx = pts[j][0] - x
                if dx >= best:
                    break                      # sorted by x: nothing closer further on
                d = math.hypot(dx, pts[j][1] - y)
                if 1e-6 < d < best:
                    best = d
        if best < math.inf:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            info[ref] = (round(best, 6), min(xs), min(ys), max(xs), max(ys))
    try:
        board._v2_fp_pitch = info
    except AttributeError:
        pass
    return info


def min_pad_pitch(board, box) -> float | None:
    """Smallest pad pitch among the fine-pitch footprints touching ``box`` (None when there is none)."""
    x0, y0, x1, y1 = box
    best = None
    for pitch, fx0, fy0, fx1, fy1 in _fp_pitch(board).values():
        if pitch <= FINE_PITCH and fx0 <= x1 and fx1 >= x0 and fy0 <= y1 and fy1 >= y0:
            if best is None or pitch < best:
                best = pitch
    return best


def _states(box, p, layers) -> int:
    return (int(math.ceil((box[2] - box[0]) / p)) + 4) * (int(math.ceil((box[3] - box[1]) / p)) + 4) * max(1, layers)


def _origin(board, box, p) -> tuple[float, float]:
    """Dominant pad-centre residue (mode of x mod p and of y mod p over the pads in the box; ties: the smaller)."""
    x0, y0, x1, y1 = box
    pn = int(round(p * 1e6))
    cx, cy = {}, {}
    for pad in board.pads:
        if x0 <= pad.x <= x1 and y0 <= pad.y <= y1:
            rx = int(round(pad.x * 1e6)) % pn
            ry = int(round(pad.y * 1e6)) % pn
            cx[rx] = cx.get(rx, 0) + 1
            cy[ry] = cy.get(ry, 0) + 1
    ox = min(cx, key=lambda r: (-cx[r], r)) / 1e6 if cx else 0.0
    oy = min(cy, key=lambda r: (-cy[r], r)) / 1e6 if cy else 0.0
    return ox, oy


def _terminal_origin(board, conn, p):
    """The lattice residue that puts a fine-pitch terminal of the connection on a cell centre (the finer-pitch
    one of the two; an escape-stub end counts as finest), or None: a pad between at-the-rule neighbours can
    only be left along its axis, which needs its centre on the lattice whatever the other pads in the box do."""
    fp = _fp_pitch(board)
    best = None
    for k, t in enumerate((conn.a, conn.b)):
        pitch = 0.0 if t.escape else None
        if pitch is None and t.pad:
            info = fp.get(t.pad.rsplit(".", 1)[0]) or fp.get(t.pad)
            if info is not None and info[0] <= FINE_PITCH:
                pitch = info[0]
        if pitch is not None and (best is None or (pitch, k) < best[0]):
            best = ((pitch, k), t)
    if best is None:
        return None
    pn = int(round(p * 1e6))
    t = best[1]
    return (int(round(t.x * 1e6)) % pn) / 1e6, (int(round(t.y * 1e6)) % pn) / 1e6


def choose_pitch(board, box, w: float, cl: float, layers: int) -> tuple[float, float, float]:
    """-> (p, ox, oy): the window pitch and the lattice origin residue (cell centres are at ox + i*p, oy + j*p).

    p is the largest ladder value with (a) p <= (w + cl) / 2, (b) the smallest pad pitch among the fine-pitch
    footprints touching the box an integer multiple of p within 1 um when such a p exists, else p <= pitch / 4,
    (c) cells x layers <= MAX_STATES — when (c) fails p is coarsened step by step (a, b given up)."""
    cands = [p for p in PITCHES if p <= (w + cl) / 2 + 1e-9] or [PITCHES[-1]]
    mp = min_pad_pitch(board, box)
    if mp:
        div = [p for p in cands if round(mp / p) >= 1 and abs(mp - round(mp / p) * p) <= 0.001 + 1e-9]
        if div:
            cands = div
        else:
            cands = [p for p in cands if p <= mp / 4 + 1e-9] or [cands[-1]]
    p = cands[0]
    if _states(box, p, layers) > MAX_STATES:
        ladder = sorted(set(PITCHES + COARSE))
        fit = [c for c in ladder if c > p and _states(box, c, layers) <= MAX_STATES]
        p = fit[0] if fit else ladder[-1]
    ox, oy = _origin(board, box, p)
    return p, ox, oy


def tile_origin(tiles) -> tuple[float, float, float]:
    """-> (x0, y0, t) of a ``tiles.Tiles``: tile (tx, ty) covers [x0 + tx*t, x0 + (tx+1)*t) x [y0 + ty*t, ...).
    Uses the attributes ``x0`` / ``y0`` when the object has them, else derives them from ``tile_of`` by bisection."""
    got = getattr(tiles, "_v2_origin", None)
    if got is not None:
        return got
    t = float(tiles.t)
    x0, y0 = getattr(tiles, "x0", None), getattr(tiles, "y0", None)
    if x0 is None or y0 is None:
        def edge(axis):
            k0 = tiles.tile_of(0.0, 0.0)[axis]
            hi = t
            for _ in range(20000):
                if tiles.tile_of(*((hi, 0.0) if axis == 0 else (0.0, hi)))[axis] != k0:
                    break
                hi += t
            else:
                return 0.0
            lo = hi - t
            for _ in range(48):
                mid = (lo + hi) / 2
                if tiles.tile_of(*((mid, 0.0) if axis == 0 else (0.0, mid)))[axis] == k0:
                    lo = mid
                else:
                    hi = mid
            return round(hi - (k0 + 1) * t, 6)
        x0, y0 = edge(0), edge(1)
    got = (float(x0), float(y0), t)
    try:
        tiles._v2_origin = got
    except AttributeError:
        pass
    return got


def tiles_box(tiles, mask) -> tuple[float, float, float, float]:
    """Bounding box (mm) of a set of tiles."""
    x0, y0, t = tile_origin(tiles)
    xs = [k[0] for k in mask]
    ys = [k[1] for k in mask]
    return (x0 + min(xs) * t, y0 + min(ys) * t, x0 + (max(xs) + 1) * t, y0 + (max(ys) + 1) * t)


def net_cfg(ctx: Ctx, net: str):
    """The plan's per-net record (guided.plan.NetCfg) or None."""
    nets = getattr(ctx.cfg, "nets", None)
    return nets.get(net) if nets else None


def allowed_layers(ctx: Ctx, conn: ConnV2, corridor: bool = True) -> tuple[int, ...]:
    """Board layers this connection may use: the plan's ``layers_ok``, narrowed by its corridor's layers (the
    layers the global stage planned it on) unless ``corridor`` is False or that would cut a terminal off."""
    nc = net_cfg(ctx, conn.net)
    ok = set(getattr(nc, "layers_ok", None) or range(ctx.board.L))
    cor = (ctx.corridors or {}).get(conn.idx) if corridor else None
    if cor is not None and cor.layers:
        both = ok & set(cor.layers)
        if any(l in both for l in conn.a.layers) and any(l in both for l in conn.b.layers):
            ok = both
    return tuple(sorted(ok))


# ── LRU ───────────────────────────────────────────────────────────────────
_CACHES = weakref.WeakKeyDictionary()      # oracle -> OrderedDict(key -> Window)
STATS = {"builds": 0, "hits": 0}


def clear_cache(oracle=None) -> None:
    """Drop the window LRU (of one oracle, or of all)."""
    if oracle is None:
        _CACHES.clear()
    else:
        _CACHES.pop(oracle, None)


def _reach(ctx, hw_max) -> float:
    o = ctx.oracle
    r = getattr(o, "_reach", None)
    if r is None:
        R = ctx.board.rules
        r = max([ctx.board.default_clr, R["edge_clearance"], R["hole_clearance"], R["hole_to_hole"] + 1.0]
                + [n.clearance for n in ctx.board.nets.values()]) + 0.5
    return r + hw_max


def _rbox_spans(g, b, R):
    """Rows of cells whose centre is within ``R`` (> 0) of a ROTATED rectangle (the inner rectangle of box
    ``b``): the convex hull of four discs, one interval per row. Replaces grid.box_spans' per-cell test."""
    cx, cy, ux, uy, ihw, ihh, _r = b
    s = g.step
    W, H = g.W, g.H
    gcx, gcy = (cx - g.x0) / s, (cy - g.y0) / s
    ihw, ihh, R = ihw / s, ihh / s, R / s
    cs = []
    for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
        cs.append((gcx + sx * ihw * ux - sy * ihh * uy, gcy + sx * ihw * uy + sy * ihh * ux))
    edges = []
    for k in range(4):
        (px, py), (qx, qy) = cs[k], cs[(k + 1) % 4]
        dx, dy = qx - px, qy - py
        L = math.hypot(dx, dy)
        if L < 1e-12:
            continue
        for sg in (1.0, -1.0):                    # both offsets: one is the outer edge, the other lies inside
            nx, ny = -dy / L * R * sg, dx / L * R * sg
            ax, ay, bx, by = px + nx, py + ny, qx + nx, qy + ny
            if abs(ay - by) > 1e-12:
                edges.append((ax, ay, bx, by) if ay < by else (bx, by, ax, ay))
    ys = [c[1] for c in cs]
    r0, r1 = max(1, int(math.ceil(min(ys) - R - 1e-9))), min(H - 2, int(math.floor(max(ys) + R + 1e-9)))
    R2 = R * R
    for iy in range(r0, r1 + 1):
        lo, hi = 1e18, -1e18
        for x, y in cs:
            d = iy - y
            qq = R2 - d * d
            if qq >= 0:
                qq = math.sqrt(qq)
                if x - qq < lo:
                    lo = x - qq
                if x + qq > hi:
                    hi = x + qq
        for px, py, qx, qy in edges:
            if py <= iy <= qy:
                x = px + (iy - py) * (qx - px) / (qy - py)
                if x < lo:
                    lo = x
                if x > hi:
                    hi = x
        i0, i1 = max(1, int(math.ceil(lo - 1e-9))), min(W - 2, int(math.floor(hi + 1e-9)))
        if i0 <= i1:
            yield iy, i0, i1


def _spans(g, s, grow, cache):
    """Rows of cells of grid ``g`` within ``grow`` of shape ``s``: [(iy, i0, i1)], memoised per window.
    (Measured for W4: sharing these lists between windows on a common lattice does not pay — the geometry is
    only a quarter of the build time, and translating cached spans costs as much as it saves.)"""
    k = (id(s), round(grow, 7))
    v = cache.get(k)
    if v is None:
        b = s.g
        if (s.kind == "box" and b[4] > 0 and b[5] > 0 and b[6] + grow > 0
                and not (abs(abs(b[2]) - 1) < 1e-6 or abs(abs(b[3]) - 1) < 1e-6)):
            v = list(_rbox_spans(g, b, b[6] + grow))
        else:
            v = list(g.shape_spans(s, grow))
        cache[k] = v
    return v


def build(ctx: Ctx, conn: ConnV2, tiles_mask: frozenset | None, box, *, width: float, pitch: float | None = None,
          soft: bool = False, layers: tuple | None = None, cache: bool = True) -> Window:
    """Rasterise the window for ``conn`` at track width ``width``.

    tiles_mask  corridor tiles (needs ``ctx.tiles``); cells outside are MASKED. None = the whole box.
    box         (x0, y0, x1, y1) mm; clipped to the board.
    pitch       force the pitch (rescue: half pitch); default ``choose_pitch``.
    soft        also record every piece covering a cell (``multi`` / ``vmulti``) for crossing reports.
    layers      board layers of the window (default ``allowed_layers``).
    cache       use the 8-entry LRU (a hit returns the same window after ``reset()``; equal to a rebuild).
    """
    board, orc = ctx.board, ctx.oracle
    net = conn.net
    nid = orc.net_id(net)
    hw = width / 2
    cl = max(conn.clearance or 0.0, orc.clearance(net))
    vsize, vdrill = conn.via if conn.via and conn.via[0] > 0 else (0.0, 0.0)
    vr = vsize / 2
    lays = tuple(sorted(layers if layers is not None else allowed_layers(ctx, conn)))
    bb = board.bbox
    box = (max(box[0], bb[0] - 0.5), max(box[1], bb[1] - 0.5), min(box[2], bb[2] + 0.5), min(box[3], bb[3] + 0.5))
    if box[2] <= box[0] or box[3] <= box[1]:
        box = (box[0], box[1], box[0] + 1.0, box[1] + 1.0)
    if pitch is None:
        p, ox, oy = choose_pitch(board, box, width, cl, len(lays))
    else:
        p = pitch
        ox, oy = _origin(board, box, p)
    pref = _terminal_origin(board, conn, p)
    if pref is not None:
        ox, oy = pref
    gx0 = math.floor((box[0] - ox) / p + 1e-9) * p + ox
    gy0 = math.floor((box[1] - oy) / p + 1e-9) * p + oy
    use_vias = len(lays) > 1 and vsize > 0

    # ── everything near, per board layer (a via needs all of them) ────────
    grow_max = _reach(ctx, max(hw, vr)) + 0.2 * p + p + GUARD_MM
    near = {}
    for l in (range(board.L) if use_vias else lays):
        near[l] = orc.near(l, box, grow_max)
    softs = {l: orc.near_soft(l, box, hw + p) for l in lays}
    hist_pts = (ctx.history or {}).get("points") or ()
    regs, nc = _plan_fields(ctx, net)

    key = None
    lru = None
    if cache:
        dyn = tuple(sorted({s.tag for l in near for s in near[l] if isinstance(s.tag, int)}))
        hard = tuple(sorted({s.tag[1] for l in near for s in near[l] if s.cls == "soft"}))
        sf = tuple(sorted({s.tag[1] for l in softs for s in softs[l]}))
        key = (tuple(round(v, 6) for v in box), p, round(ox, 6), round(oy, 6), lays, net, round(width, 6),
               round(cl, 6), round(vsize, 6), round(vdrill, 6), tiles_mask, bool(soft),
               (round(conn.a.x, 6), round(conn.a.y, 6), round(conn.b.x, 6), round(conn.b.y, 6)),
               dyn, hard, sf, tuple(tuple(pt) for pt in hist_pts),
               tuple((tuple(r.rect), r.cost, bool(r.forbid), None if r.layers is None else tuple(r.layers),
                      r.idx < PLAN_FIRST_AUTO) for r in regs), orc.mask_openings_hard)
        lru = _CACHES.get(orc)
        if lru is None:
            lru = _CACHES[orc] = OrderedDict()
        win = lru.get(key)
        if win is not None:
            lru.move_to_end(key)
            win.reset()
            STATS["hits"] += 1
            return win
    STATS["builds"] += 1

    g = Grid((gx0, gy0, box[2], box[3]), p, len(lays))
    G, W, H = g.G, g.W, g.H
    nl = len(lays)

    # ── base map: on the board, inside the corridor ───────────────────────
    base = _NEG * G
    for o, holes in board.outline:
        for iy, i0, i1 in g.poly_spans(o):
            a = iy * W + i0
            base[a:a + i1 - i0 + 1] = _ZERO * (i1 - i0 + 1)
        for hp in holes:
            for iy, i0, i1 in g.poly_spans(hp):
                a = iy * W + i0
                base[a:a + i1 - i0 + 1] = _NEG * (i1 - i0 + 1)
    for iy in (0, H - 1):                                   # the border ring: more window would continue here
        base[iy * W:(iy + 1) * W] = array("i", [MASKED]) * W
    for iy in range(H):
        base[iy * W] = MASKED
        base[iy * W + W - 1] = MASKED
    col_tile = row_tile = None
    tile_t = 0.0
    if tiles_mask is not None:
        if ctx.tiles is None:
            raise ValueError("window.build: a tiles_mask needs ctx.tiles for the tile geometry")
        tx0, ty0, tile_t = tile_origin(ctx.tiles)
        col_tile = [int(math.floor((g.x0 + ix * p - tx0) / tile_t + 1e-9)) for ix in range(W)]
        row_tile = [int(math.floor((g.y0 + iy * p - ty0) / tile_t + 1e-9)) for iy in range(H)]
        runs = []                                           # (tx, i0, i1) over the interior columns
        for ix in range(1, W - 1):
            t = col_tile[ix]
            if runs and runs[-1][0] == t:
                runs[-1][2] = ix
            else:
                runs.append([t, ix, ix])
        masked = array("i", [MASKED]) * G
        rows = {}
        for iy in range(1, H - 1):
            ty = row_tile[iy]
            sp = rows.get(ty)
            if sp is None:
                sp = rows[ty] = [(i0, i1) for t, i0, i1 in runs if (t, ty) in tiles_mask]
            a = iy * W
            for i0, i1 in sp:
                masked[a + i0:a + i1 + 1] = base[a + i0:a + i1 + 1]
        for t in (conn.a, conn.b):                          # the terminals are never masked off
            for iy, i0, i1 in g.capsule_spans(t.x, t.y, t.x, t.y, TERMINAL_OPEN):
                a = iy * W
                masked[a + i0:a + i1 + 1] = base[a + i0:a + i1 + 1]
        base = masked

    cm = ClassMap.__new__(ClassMap)
    cm.key = (round(hw, 4), round(cl, 4), round(vr, 4), round(vdrill, 4))
    cm.h, cm.c, cm.vr, cm.vd = cm.key
    S = array("i", base) * nl
    cm.S = S
    SV = array("i", base) if use_vias else array("i", [-1]) * G
    cm.SV = SV

    # ── static shapes ─────────────────────────────────────────────────────
    spc = {}
    need = orc.need
    neg_row = _NEG * W
    dyn_t, dyn_v = [], []            # (state base, spans, piece id) stamped after the static map is frozen
    cmul = None
    rep = array("f", [PAD_REPEL]) * W
    for li, l in enumerate(lays):
        b0 = li * G
        for s in near[l]:
            n = need(net, hw, s, l, "seg", 0.0, cl)
            if n == NO_NEED:
                continue
            grow = n + hw - TOL_MM + max(GUARD_MM, raster_guard(p, hw + n))
            spans = _spans(g, s, grow, spc)
            if isinstance(s.tag, int):
                dyn_t.append((b0, spans, s.tag))
                continue
            for iy, i0, i1 in spans:
                a = b0 + iy * W + i0
                S[a:a + i1 - i0 + 1] = neg_row[:i1 - i0 + 1]
            if PAD_REPEL and s.cls == "cu" and hasattr(s.tag, "name"):
                if cmul is None:
                    cmul = array("f", [1.0]) * (nl * G)
                for iy, i0, i1 in spans:                       # the halo dilated by one cell
                    j0 = i0 - 1 if i0 > 1 else i0
                    n = (i1 + 1 if i1 < W - 2 else i1) - j0 + 1
                    a = b0 + iy * W + j0
                    cmul[a:a + n] = rep[:n]
                    if iy > 1:
                        cmul[a - W:a - W + n] = rep[:n]
                    if iy < H - 2:
                        cmul[a + W:a + W + n] = rep[:n]
    if use_vias:
        seen = set()
        for l in range(board.L):
            for s in near[l]:
                n = need(net, vr, s, l, "via", vdrill, cl)
                own_smd = (s.cls == "cu" and s.net == nid and nid and getattr(s.tag, "kind", None) in ("smd", "conn"))
                if n == NO_NEED:
                    if not own_smd:
                        continue
                    grow = vr * VIA_IN_PAD
                else:
                    grow = n + vr - TOL_MM + GUARD_MM
                    if own_smd and vr * VIA_IN_PAD > grow:
                        grow = vr * VIA_IN_PAD
                k = (id(s), round(grow, 7))
                if k in seen:
                    continue
                seen.add(k)
                spans = _spans(g, s, grow, spc)
                if isinstance(s.tag, int):
                    dyn_v.append((spans, s.tag))
                    continue
                for iy, i0, i1 in spans:
                    a = iy * W + i0
                    SV[a:a + i1 - i0 + 1] = neg_row[:i1 - i0 + 1]

    # ── routed copper: hard in occ / vocc, remembered by piece id ─────────
    occ = array("i", S)
    vocc = array("i", SV)
    cm.occ, cm.vocc = occ, vocc
    owner = array("i", [0]) * (nl * G)
    vowner = array("i", [0]) * G
    multi, vmulti = {}, {}
    for b0, spans, pid in dyn_t:
        _stamp_owner(occ, owner, multi if soft else None, b0, W, spans, pid, neg_row)
    for spans, pid in dyn_v:
        _stamp_owner(vocc, vowner, vmulti if soft else None, 0, W, spans, pid, neg_row)

    # ── prices ────────────────────────────────────────────────────────────
    for li, l in enumerate(lays):
        b0 = li * G
        for s in softs[l]:
            if nid and s.net == nid:
                continue
            price = SOFT_PRICE.get(s.tag[0], 6.0)
            if cmul is None:
                cmul = array("f", [1.0]) * (nl * G)
            for iy, i0, i1 in g.shape_spans(s, hw):
                a = b0 + iy * W
                for i in range(a + i0, a + i1 + 1):
                    v = cmul[i]
                    if 0 <= v < price:
                        cmul[i] = price
    if regs or (nc is not None and (getattr(nc, "avoid", None) or getattr(nc, "corridor", None))):
        if cmul is None:
            cmul = array("f", [1.0]) * (nl * G)
        _plan_prices(g, lays, cmul, regs, nc, board.nets[net].pads if net in board.nets else ())
    hist = None
    if hist_pts:
        for pt in hist_pts:
            x, y, r, cost = pt[0], pt[1], pt[2], pt[3]
            lay = pt[4] if len(pt) > 4 else None
            if x + r < box[0] or x - r > box[2] or y + r < box[1] or y - r > box[3]:
                continue
            if hist is None:
                hist = array("f", [0.0]) * (nl * G)
            sp = list(g.capsule_spans(x, y, x, y, r))
            for li, l in enumerate(lays):
                if lay is not None and lay != l:
                    continue
                b0 = li * G
                for iy, i0, i1 in sp:
                    a = b0 + iy * W
                    for i in range(a + i0, a + i1 + 1):
                        hist[i] += cost / p

    win = Window()
    win.grid, win.pitch, win.layers, win.box, win.cm = g, p, lays, box, cm
    win.lidx = {l: i for i, l in enumerate(lays)}
    win.owner, win.vowner, win.multi, win.vmulti = owner, vowner, multi, vmulti
    win.cmul, win.hist = cmul, hist
    win.net, win.net_id, win.width, win.clearance, win.via = net, nid, width, cl, (vsize, vdrill)
    win.soft = bool(soft)
    win.col_tile, win.row_tile, win.tile_t = col_tile, row_tile, tile_t
    win.key = key
    win.shapes = sum(len(v) for v in near.values())
    win.last_reject = None
    win.ends = None             # (source anchors, target anchors) by state: set by detail before emit
    win._undo, win._vundo = [], []
    if lru is not None:
        lru[key] = win
        while len(lru) > LRU_SIZE:
            lru.popitem(last=False)
    return win


def _stamp_owner(occ, owner, multi, b0, W, spans, pid, neg_row):
    for iy, i0, i1 in spans:
        a = b0 + iy * W + i0
        n = i1 - i0 + 1
        occ[a:a + n] = neg_row[:n]
        if multi is not None and owner[a:a + n].count(0) != n:
            for i in range(a, a + n):
                o = owner[i]
                if o == 0:
                    owner[i] = pid
                elif o != pid:
                    m = multi.get(i)
                    if m is None:
                        multi[i] = [o, pid]
                    elif pid not in m:
                        m.append(pid)
        else:
            owner[a:a + n] = array("i", [pid]) * n


def _plan_fields(ctx, net):
    regs = [r for r in (getattr(ctx.cfg, "regions", None) or ()) if r.applies(net)]
    return regs, net_cfg(ctx, net)


def _grown(g, rect):
    """``rect`` grown by just under one cell (see ``_plan_prices``)."""
    d = g.step * (1.0 - 1e-6)
    return rect[0] - d, rect[1] - d, rect[2] + d, rect[3] + d


def _rect_spans(g, rect):
    x0, y0, x1, y1 = rect
    i0, j0 = max(1, int(math.ceil(g.gx(x0) - 1e-9))), max(1, int(math.ceil(g.gy(y0) - 1e-9)))
    i1, j1 = min(g.W - 2, int(math.floor(g.gx(x1) + 1e-9))), min(g.H - 2, int(math.floor(g.gy(y1) + 1e-9)))
    if i0 <= i1:
        for iy in range(j0, j1 + 1):
            yield iy, i0, i1


def _plan_prices(g, lays, cmul, regs, nc, pads):
    """Plan regions (cost / forbid), per-net avoid rectangles and the plan corridor, as in v1's ``_cmul``."""
    G, W = g.G, g.W
    cor = getattr(nc, "corridor", None) if nc is not None else None
    if cor:
        pts, width, strict, outside = cor["points"], cor["width"], cor["strict"], cor["outside_cost"]
        spans = []
        for i in range(len(pts) - 1):
            spans += list(g.capsule_spans(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], width / 2))
        if len(pts) == 1:
            spans += list(g.capsule_spans(pts[0][0], pts[0][1], pts[0][0], pts[0][1], width / 2))
        for pad in pads:                                   # the net's own pads are always reachable
            spans += list(g.capsule_spans(pad.x, pad.y, pad.x, pad.y, max(width / 2, 1.0)))
        keep = bytearray(G)
        for iy, i0, i1 in spans:
            keep[iy * W + i0:iy * W + i1 + 1] = b"\x01" * (i1 - i0 + 1)
        out = -1.0 if strict else float(outside)
        for li in range(len(lays)):
            b0 = li * G
            for i in range(G):
                if not keep[i]:
                    v = cmul[b0 + i]
                    if v >= 0:
                        cmul[b0 + i] = out if out < 0 else v * out
    for r in regs:
        for li, l in enumerate(lays):
            if r.layers is not None and l not in r.layers:
                continue
            b0 = li * G
            # a forbidden area of the plan is a rule of the oracle (centre lines may not touch it): one more ring
            # of cells, so that no diagonal step between two free cells clips a corner of the rectangle
            for iy, i0, i1 in _rect_spans(g, _grown(g, r.rect) if r.forbid and r.idx < PLAN_FIRST_AUTO else r.rect):
                a = b0 + iy * W
                if r.forbid:
                    cmul[a + i0:a + i1 + 1] = array("f", [-1.0]) * (i1 - i0 + 1)
                else:
                    for i in range(a + i0, a + i1 + 1):
                        if cmul[i] >= 0:
                            cmul[i] *= r.cost
    for rect in (getattr(nc, "avoid", None) or ()) if nc is not None else ():
        for li in range(len(lays)):
            b0 = li * G
            for iy, i0, i1 in _rect_spans(g, _grown(g, rect)):
                a = b0 + iy * W
                cmul[a + i0:a + i1 + 1] = array("f", [-1.0]) * (i1 - i0 + 1)
