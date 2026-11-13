"""Tile graph with capacities from exact geometry (design §4.6).

The board is cut into uniform square tiles of ``t`` mm. A node of the global-routing graph is
``(layer, tx, ty)``; neighbouring tiles of one layer share a *border*, and a border has a capacity in
millimetres of track pitch that fits through it:

    for every static shape near the border:   blocked interval = border ∩ (shape inflated by its need)
    free intervals  len_i                    = (border ∩ inside the outline) − blocked intervals
    tracks                                   = Σ floor((len_i + cl_ref) / p),  p = ref_w + ref_cl (reference class)
    raw_cap_mm                               = tracks × p                       (the design's formula)
    cap_mm                                   = (raw_cap_mm + slack) × cal × scale − booked

``slack`` is the largest remainder ``(len_i + cl_ref) mod p`` of the border's free intervals (0 when no reference
track fits at all). It never admits one more *reference* track (slack < p), but it stops a single track that is
wider than the reference class from overflowing a border it physically fits through: tracks of widths w_j fit
a free interval iff Σ (w_j + cl) <= len + cl, and flooring that to the reference pitch reported ~200 false
cuts per board for power nets (measured, design Appendix A3).

``need`` comes from ``Oracle.need`` (net-class / pad-override clearance, hole, edge, keep-out, mask), so the
capacity model and the legality test share one rule set. A connection puts ``width + clearance`` of demand on
every border it crosses (globalroute.py).

Sides: ``'E'`` is the border between ``(tx, ty)`` and ``(tx + 1, ty)``; ``'N'`` the border between ``(tx, ty)``
and ``(tx, ty + 1)`` (towards +y of the board file, i.e. *down* on screen). ``'W'`` / ``'S'`` are accepted and
mean the E / N border of the neighbour.

Only static geometry counts (pads, kept tracks / vias, holes, keep-outs, mask art, the outline and hardened
soft shapes). Copper that v2 itself placed (``Oracle`` dynamic pieces) is *not* subtracted: reservations are
booked as demand with ``book`` / ``book_via`` and routed connections are the negotiated demand.

Via capacity of a column = number of vias that fit in the tile through every layer (see ``via_cap``).

Flat indexing used by globalroute.py: ``s = (layer × ny + ty) × nx + tx``; ``capE[s]`` / ``capN[s]`` are the
effective capacities, ``hardE[s]`` / ``hardN[s]`` are 1 where a border lies wholly outside the board.
"""
from __future__ import annotations

import math

from ..guided import geom
from .oracle import NO_NEED, Oracle
from .types import Cut

INF = float("inf")
_EPS = 1e-9

# ── calibration (measured on the 12 human-routed corpus references, design Appendix A3;
#    rerun: bench/route/tiles_cal.py) ─────────────────────────────────────────────────
CAL = 1.0                     # the single calibration constant: cap_mm × CAL (measured: 1.0 passes the gate)
CAL_GATE = 0.995              # share of borders on which cap × CAL >= observed human demand
CORRIDORS_ADVISORY = False    # the gate is met at CAL = 1.0 (A3), so corridors may be used as masks; True = cost only


# ── intervals ─────────────────────────────────────────────────────────────
def _merge(iv):
    """Sorted, merged copy of a list of (lo, hi)."""
    if len(iv) < 2:
        return [(a, b) for a, b in iv if b > a]
    out = []
    for a, b in sorted(iv):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _subtract(base, cuts):
    """base − cuts; both sorted and merged."""
    if not cuts:
        return list(base)
    out = []
    for a, b in base:
        x = a
        for c, d in cuts:
            if d <= x:
                continue
            if c >= b:
                break
            if c > x:
                out.append((x, c))
            if d > x:
                x = d
            if x >= b:
                break
        if x < b:
            out.append((x, b))
    return out


def _clip(iv, lo, hi):
    out = []
    for a, b in iv:
        if b <= lo or a >= hi:
            continue
        out.append((a if a > lo else lo, b if b < hi else hi))
    return out


# ── exact line / inflated-shape intersections ─────────────────────────────
def _slab(A, p, W):
    """t with |A + p t| <= W  ->  (lo, hi) | None  (p == 0: all or nothing)."""
    if p == 0.0:
        return (-INF, INF) if abs(A) <= W else None
    a, b = (-W - A) / p, (W - A) / p
    return (a, b) if a <= b else (b, a)


def box_span(g, grow, axis, c):
    """Interval of the axis-parallel line (axis 0: x = c, parameter y; axis 1: y = c, parameter x) that lies
    inside the box ``g`` (rounded rotated rectangle, geom.py) inflated by ``grow``. None when they miss.
    Exact: the inflated box is the Minkowski sum of the inner rectangle and a disc."""
    cx, cy, ux, uy, ihw, ihh, r = g
    R = r + grow
    if R < 0.0:
        R = 0.0
    if axis == 0:
        off, t0 = c - cx, cy
        A, B, p, qv = off * ux, -off * uy, uy, ux
    else:
        off, t0 = c - cy, cx
        A, B, p, qv = off * uy, off * ux, ux, -uy
    lo, hi = INF, -INF
    if ihw > 0.0 or ihh > 0.0:
        for wx, wy in ((ihw + R, ihh), (ihw, ihh + R)):
            s1 = _slab(A, p, wx)
            if s1 is None:
                continue
            s2 = _slab(B, qv, wy)
            if s2 is None:
                continue
            a = s1[0] if s1[0] > s2[0] else s2[0]
            b = s1[1] if s1[1] < s2[1] else s2[1]
            if a <= b:
                if a < lo:
                    lo = a
                if b > hi:
                    hi = b
        corners = ((ihw, ihh), (ihw, -ihh), (-ihw, ihh), (-ihw, -ihh)) if R > 0.0 else ()
    else:
        corners = ((0.0, 0.0),)
    R2 = R * R
    for sx, sy in corners:
        ax, ay = A - sx, B - sy
        tm = -(ax * p + ay * qv)
        perp2 = ax * ax + ay * ay - tm * tm
        if perp2 <= R2:
            h = math.sqrt(R2 - perp2) if R2 > perp2 else 0.0
            if tm - h < lo:
                lo = tm - h
            if tm + h > hi:
                hi = tm + h
    if hi < lo:
        return None
    return (lo + t0, hi + t0)


def poly_spans(pts, grow, axis, c):
    """Intervals of the axis-parallel line inside the polygon inflated by ``grow`` (merged)."""
    out = []
    xs = []
    n = len(pts)
    ia, ib = (0, 1) if axis == 0 else (1, 0)
    p1 = pts[n - 1]
    for i in range(n):
        p2 = pts[i]
        u1, u2 = p1[ia], p2[ia]
        if (u1 > c) != (u2 > c):
            xs.append(p1[ib] + (c - u1) * (p2[ib] - p1[ib]) / (u2 - u1))
        if grow > 0.0 and not ((u1 < c - grow and u2 < c - grow) or (u1 > c + grow and u2 > c + grow)):
            sp = box_span(geom.box_from_seg(p1[0], p1[1], p2[0], p2[1], 0.0), grow, axis, c)
            if sp is not None:
                out.append(sp)
        p1 = p2
    xs.sort()
    for i in range(0, len(xs) - 1, 2):
        out.append((xs[i], xs[i + 1]))
    return _merge(out)


def shape_spans(s, grow, axis, c):
    """Intervals of the line inside ``s`` inflated by ``grow``."""
    if s.kind == "box":
        sp = box_span(s.g, grow, axis, c)
        return [sp] if sp is not None else []
    return poly_spans(s.g, grow, axis, c)


def min_pad_pitch(board) -> float:
    """Smallest centre distance between two pads of one footprint with >= 3 pads (inf when there is none)."""
    by = {}
    for p in board.pads:
        by.setdefault(p.ref, []).append((p.x, p.y))
    best = INF
    for ref in sorted(by):
        pts = sorted(set(by[ref]))
        n = len(pts)
        if n < 3:
            continue
        for i in range(n):
            x, y = pts[i]
            for j in range(i + 1, n):
                dx = pts[j][0] - x
                if dx >= best:
                    break
                d = math.hypot(dx, pts[j][1] - y)
                if 1e-6 < d < best:
                    best = d
    return best


def choose_tile_mm(board, max_nodes: int = 60_000) -> float:
    """Design §4.6: 1.0 mm; 0.5 mm when the board has pad pitch <= 0.5 mm; doubled until tiles × layers fit."""
    t = 0.5 if min_pad_pitch(board) <= 0.5 + 1e-6 else 1.0
    w, h = board.bbox[2] - board.bbox[0], board.bbox[3] - board.bbox[1]
    while max(1, math.ceil(w / t - 1e-9)) * max(1, math.ceil(h / t - 1e-9)) * board.L > max_nodes:
        t *= 2.0
    return t


def reference_class(board, conns=None) -> tuple[float, float]:
    """(ref_w, ref_cl): the track width and clearance tile capacities are counted in. With connections: the
    most common (width, clearance) among them (ties: the finer one); else the board's default class."""
    if conns:
        cnt = {}
        for c in conns:
            if c.width > 0:
                k = (round(c.width, 4), round(c.clearance, 4))
                cnt[k] = cnt.get(k, 0) + 1
        if cnt:
            w, cl = min(cnt, key=lambda k: (-cnt[k], k))
            return w, max(cl, board.rules["clearance"])
    df = board.rules.get("default") or {}
    return max(df.get("width") or 0.2, board.rules["track_width"]), board.default_clr


def default_via(board) -> tuple[float, float]:
    """(size, drill) of the default class, floored by the board minimums (as guided.model.Net does)."""
    df = board.rules.get("default") or {}
    R = board.rules
    via = max(df.get("via") or 0.6, R["via"])
    drill = max(df.get("drill") or 0.3, R["drill"])
    if via < drill + 2 * R["annular"]:
        via = round(drill + 2 * R["annular"], 4)
    return via, drill


class Tiles:
    """The tile graph of one board. ``cal`` multiplies every border capacity (calibration constant)."""

    def __init__(self, board, oracle: "Oracle", tile_mm: float, ref_w: float, ref_cl: float, cal: float = 1.0, *,
                 via: tuple[float, float] | None = None):
        self.board, self.oracle = board, oracle
        self.t = float(tile_mm)
        self.ref_w, self.ref_cl = float(ref_w), float(ref_cl)
        self.pitch = self.ref_w + self.ref_cl
        self.cal = float(cal)
        self.advisory = CORRIDORS_ADVISORY
        self.x0, self.y0 = board.bbox[0], board.bbox[1]
        T = self.t
        self.nx = max(1, int(math.ceil((board.bbox[2] - self.x0) / T - 1e-9)))
        self.ny = max(1, int(math.ceil((board.bbox[3] - self.y0) / T - 1e-9)))
        self.nl = board.L
        self.G = self.nx * self.ny
        self.N = self.G * self.nl
        self.via_size, self.via_drill = via if via else default_via(board)
        self.version = 0                     # bumps on book / book_via / scale (globalroute re-reads capacities)
        self.reservations_booked = False     # set by whoever booked the reservations (power / fanout copper, thermal
        #                                      spokes): globalroute.run books them once when nobody has
        self.regions_scaled = False          # region.capacity_scale applied (globalroute.run, once)
        self.full = math.floor((T + self.ref_cl) / self.pitch + _EPS) * self.pitch     # an empty border
        N = self.N
        self.slack0 = (T + self.ref_cl - self.full) if self.full > 0 else 0.0
        self._rawE = [self.full] * N
        self._rawN = [self.full] * N
        self._slkE = [self.slack0] * N
        self._slkN = [self.slack0] * N
        self._sclE = [1.0] * N
        self._sclN = [1.0] * N
        self._bokE = [0.0] * N
        self._bokN = [0.0] * N
        self.capE = [0.0] * N
        self.capN = [0.0] * N
        self.hardE = bytearray(N)
        self.hardN = bytearray(N)
        self._blk = {}                       # code = s*2 + (0 E | 1 N) -> [(lo, hi, net id, tag)]
        self._free = {}                      # code -> free intervals (only borders that are not simply full)
        self._shadow = {}                    # pad name -> set of codes whose border its inflated copper touches
        self._tshapes = {}                   # column -> [(shape, via need)]
        self._vsites = [-1] * self.G
        self._whole = bytearray(2 * self.G)  # [side * G + column]: that border lies wholly inside the outline
        self._vbook = [0] * self.G
        self.useE = self.useN = self.useV = None      # set_usage(): the last global-routing result (for diag)
        self._pads = {}
        for p in board.pads:
            self._pads.setdefault(p.name, []).append(p)
        self._build()

    @property
    def _reservations_booked(self) -> bool:      # the name diag.py reads
        return self.reservations_booked

    # ── construction ──────────────────────────────────────────────────────
    def _tagname(self, s) -> str:
        t = s.tag
        if t is not None and hasattr(t, "name"):
            return t.name
        f = getattr(self.oracle, "_tag", None)
        if f is not None:
            try:
                return f(s, False) or s.cls
            except Exception:  # noqa: BLE001  (naming only)
                pass
        return {"edge": "edge", "hole": "hole", "kt": "keepout", "kv": "keepout"}.get(s.cls, "copper")

    def _inside_lines(self):
        """Per grid line the intervals inside the outline: ({k: iv} vertical lines x = x0 + kT, {k: iv} horizontal)."""
        T = self.t
        out = ({}, {})
        for axis, n, o in ((0, self.nx, self.x0), (1, self.ny, self.y0)):
            for k in range(1, n):
                c = o + k * T
                iv = []
                for outer, holes in self.board.outline:
                    a = poly_spans(outer, 0.0, axis, c)
                    for h in holes:
                        a = _subtract(a, poly_spans(h, 0.0, axis, c))
                    iv += a
                out[axis][k] = _merge(iv)
        return out

    def _build(self):
        b, o, T = self.board, self.oracle, self.t
        nx, ny, G = self.nx, self.ny, self.G
        x0, y0 = self.x0, self.y0
        hw = self.ref_w / 2
        vhw = self.via_size / 2
        blk, shadow = self._blk, self._shadow
        vneed = {}                                   # id(shape) -> [shape, via need]
        bb = (x0, y0, x0 + nx * T, y0 + ny * T)
        for l in range(self.nl):
            base = l * G
            for s in o.near(l, bb, 5.0):
                if isinstance(s.tag, int) and not isinstance(s.tag, bool):
                    continue                         # v2's own (dynamic) copper: demand, not capacity
                nv = o.need(None, vhw, s, l, "via", self.via_drill, self.ref_cl)
                if nv != NO_NEED:
                    e = vneed.get(id(s))
                    if e is None:
                        vneed[id(s)] = [s, nv]
                    elif nv > e[1]:
                        e[1] = nv
                n = o.need(None, hw, s, l, "seg", 0.0, self.ref_cl)
                if n == NO_NEED:
                    continue
                sb = s.bbox
                nid = s.net if s.cls in ("cu", "hole") else 0
                tag = s.tag if (s.tag is not None and hasattr(s.tag, "name")) else s
                pad = tag.name if tag is not s else None
                # vertical grid lines (E borders)
                k0 = max(1, int(math.ceil((sb[0] - n - x0) / T - 1e-9)))
                k1 = min(nx - 1, int(math.floor((sb[2] + n - x0) / T + 1e-9)))
                for k in range(k0, k1 + 1):
                    for lo, hi in shape_spans(s, n, 0, x0 + k * T):
                        if hi <= lo:
                            continue
                        j0 = max(0, int(math.floor((lo - y0) / T)))
                        j1 = min(ny - 1, int(math.floor((hi - y0) / T)))
                        for j in range(j0, j1 + 1):
                            code = (base + j * nx + k - 1) * 2
                            blk.setdefault(code, []).append((lo, hi, nid, tag))
                            if pad is not None:
                                shadow.setdefault(pad, set()).add(code)
                # horizontal grid lines (N borders)
                k0 = max(1, int(math.ceil((sb[1] - n - y0) / T - 1e-9)))
                k1 = min(ny - 1, int(math.floor((sb[3] + n - y0) / T + 1e-9)))
                for k in range(k0, k1 + 1):
                    for lo, hi in shape_spans(s, n, 1, y0 + k * T):
                        if hi <= lo:
                            continue
                        i0 = max(0, int(math.floor((lo - x0) / T)))
                        i1 = min(nx - 1, int(math.floor((hi - x0) / T)))
                        for i in range(i0, i1 + 1):
                            code = (base + (k - 1) * nx + i) * 2 + 1
                            blk.setdefault(code, []).append((lo, hi, nid, tag))
                            if pad is not None:
                                shadow.setdefault(pad, set()).add(code)
        # shapes per column for the via sites (every layer: vias are through)
        ts = self._tshapes
        for s, nv in vneed.values():
            g = nv + vhw
            sb = s.bbox
            i0 = max(0, int(math.floor((sb[0] - g - x0) / T)))
            i1 = min(nx - 1, int(math.floor((sb[2] + g - x0) / T)))
            j0 = max(0, int(math.floor((sb[1] - g - y0) / T)))
            j1 = min(ny - 1, int(math.floor((sb[3] + g - y0) / T)))
            for j in range(j0, j1 + 1):
                for i in range(i0, i1 + 1):
                    ts.setdefault(j * nx + i, []).append((s, nv))
        # borders: inside the outline, minus the blocked intervals
        vin, hin = self._inside_lines()
        rawE, rawN, hardE, hardN = self._rawE, self._rawN, self.hardE, self.hardN
        cl, p = self.ref_cl, self.pitch
        for side, raw, hard, lines in ((0, rawE, hardE, vin), (1, rawN, hardN, hin)):
            for ty in range(ny):
                for tx in range(nx):
                    r = ty * nx + tx
                    if side == 0:
                        if tx == nx - 1:
                            ins = None
                        else:
                            lo = y0 + ty * T
                            ins = _clip(lines[tx + 1], lo, lo + T)
                    else:
                        if ty == ny - 1:
                            ins = None
                        else:
                            lo = x0 + tx * T
                            ins = _clip(lines[ty + 1], lo, lo + T)
                    whole = bool(ins) and len(ins) == 1 and ins[0][0] <= lo + 1e-9 and ins[0][1] >= lo + T - 1e-9
                    if whole:
                        self._whole[side * G + r] = 1
                    for l in range(self.nl):
                        s = l * G + r
                        if not ins:
                            raw[s] = 0.0
                            hard[s] = 1
                            continue
                        code = s * 2 + side
                        items = blk.get(code)
                        if items is None and whole:
                            continue
                        self._free[code] = _subtract(ins, _merge([(a, c) for a, c, _n, _t in items])) if items else ins
        # capacities of the borders that are not simply full; a free interval cut by a tile corner is counted
        # together with its continuation in the next border (see _count)
        for code in self._free:
            s, side = code >> 1, code & 1
            n, slack = self._count(code)
            (rawN if side else rawE)[s] = n * p
            (self._slkN if side else self._slkE)[s] = slack
        for hard, slk in ((hardE, self._slkE), (hardN, self._slkN)):
            for s in range(self.N):
                if hard[s]:
                    slk[s] = 0.0
        cal = self.cal
        self.capE = [(v + self._slkE[s]) * cal for s, v in enumerate(rawE)]
        self.capN = [(v + self._slkN[s]) * cal for s, v in enumerate(rawN)]

    def _span(self, code):
        """(lo, hi) of border ``code`` along its grid line."""
        r = (code >> 1) % self.G
        lo = (self.x0 + (r % self.nx) * self.t) if code & 1 else (self.y0 + (r // self.nx) * self.t)
        return lo, lo + self.t

    def _piece(self, code, end):
        """The free interval of border ``code`` that touches its low (end 0) / high (end 1) corner:
        (length, spans the whole border) or None. ``code`` must be a border on the grid."""
        s, side = code >> 1, code & 1
        if (self.hardN if side else self.hardE)[s]:
            return None
        lo, hi = self._span(code)
        f = self._free.get(code)
        if f is None:
            return self.t, True
        if not f:
            return None
        a, c = f[-1] if end else f[0]
        if (c < hi - 1e-9) if end else (a > lo + 1e-9):
            return None
        return c - a, (a <= lo + 1e-9 and c >= hi - 1e-9)

    def _count(self, code) -> tuple[int, float]:
        """(reference tracks, slack mm) of border ``code``: sum of floor((len_i + cl) / p) over its free
        intervals, and the largest remainder (len_i + cl) mod p (0.0 when no track fits at all). A
        free interval that is cut by a tile corner continues in the next border of the same grid line; the two
        pieces together carry floor((len_a + len_b + cl) / p) tracks, of which the larger piece (a piece that
        spans its whole border always counts as the larger) keeps its own floor and the smaller gets the rest.
        Without this a gap that happens to straddle a corner would be counted as closed."""
        cl, p = self.ref_cl, self.pitch
        s, side = code >> 1, code & 1
        r = s % self.G
        tx, ty = r % self.nx, r // self.nx
        lo, hi = self._span(code)
        step = (1 if side else self.nx) * 2            # next border along the same grid line
        last = (tx == self.nx - 1) if side else (ty == self.ny - 1)
        first = (tx == 0) if side else (ty == 0)
        n = 0
        slack = 0.0
        f = self._free.get(code)
        if f is None:
            f = [(lo, hi)]
        for a, c in f:
            ln = c - a
            own = int(math.floor((ln + cl) / p + _EPS))
            rem = ln + cl - own * p
            at_lo, at_hi = a <= lo + 1e-9, c >= hi - 1e-9
            if at_lo != at_hi:                          # cut by exactly one corner
                other = None
                later = False
                if at_lo and not first:
                    other = self._piece(code - step, 1)
                    later = True
                elif at_hi and not last:
                    other = self._piece(code + step, 0)
                if other is not None:
                    oln, whole = other
                    if whole or oln > ln + 1e-9 or (abs(oln - ln) <= 1e-9 and later):
                        joint = int(math.floor((ln + oln + cl) / p + _EPS))
                        own = max(0, joint - int(math.floor((oln + cl) / p + _EPS)))
                        rem = 0.0
            n += own
            if own > 0 and rem > slack:
                slack = rem
        return n, (slack if n > 0 and slack > 1e-9 else 0.0)

    # ── queries ───────────────────────────────────────────────────────────
    def tile_of(self, x, y) -> tuple[int, int]:
        tx = int(math.floor((x - self.x0) / self.t))
        ty = int(math.floor((y - self.y0) / self.t))
        return (0 if tx < 0 else (self.nx - 1 if tx >= self.nx else tx),
                0 if ty < 0 else (self.ny - 1 if ty >= self.ny else ty))

    def center(self, tx: int, ty: int) -> tuple[float, float]:
        return self.x0 + (tx + 0.5) * self.t, self.y0 + (ty + 0.5) * self.t

    def rect(self, tx: int, ty: int) -> tuple[float, float, float, float]:
        return (self.x0 + tx * self.t, self.y0 + ty * self.t, self.x0 + (tx + 1) * self.t, self.y0 + (ty + 1) * self.t)

    def _edge(self, layer, tx, ty, side):
        """-> (flat index, 0 E | 1 N) or None when there is no such border."""
        if side == "W":
            tx, side = tx - 1, "E"
        elif side == "S":
            ty, side = ty - 1, "N"
        if not (0 <= layer < self.nl and 0 <= tx < self.nx and 0 <= ty < self.ny):
            return None
        if side == "E":
            if tx >= self.nx - 1:
                return None
            return (layer * self.ny + ty) * self.nx + tx, 0
        if side == "N":
            if ty >= self.ny - 1:
                return None
            return (layer * self.ny + ty) * self.nx + tx, 1
        raise ValueError(f"side must be 'E', 'N', 'W' or 'S', not {side!r}")

    def edge_of(self, code: int) -> tuple[int, int, int, str]:
        """Inverse of the flat border code ``s*2 + side`` -> (layer, tx, ty, 'E' | 'N')."""
        s, side = code >> 1, code & 1
        l, r = divmod(s, self.G)
        ty, tx = divmod(r, self.nx)
        return l, tx, ty, "N" if side else "E"

    def cap(self, layer: int, tx: int, ty: int, side: str) -> float:
        """Effective capacity of a border in mm: (raw_cap + slack) × cal × scale − booked, never negative; the
        number demand (Σ width + clearance) is compared with. 0.0 off the grid."""
        e = self._edge(layer, tx, ty, side)
        if e is None:
            return 0.0
        return (self.capN if e[1] else self.capE)[e[0]]

    def raw_cap(self, layer: int, tx: int, ty: int, side: str) -> float:
        """The design's formula, from geometry alone: Σ floor((len_i + cl) / p) × p (no slack, cal, scale, bookings)."""
        e = self._edge(layer, tx, ty, side)
        if e is None:
            return 0.0
        return (self._rawN if e[1] else self._rawE)[e[0]]

    def tracks(self, layer: int, tx: int, ty: int, side: str) -> int:
        """Reference-class tracks that fit through the border by geometry (raw_cap / pitch)."""
        return int(round(self.raw_cap(layer, tx, ty, side) / self.pitch))

    def slack(self, layer: int, tx: int, ty: int, side: str) -> float:
        e = self._edge(layer, tx, ty, side)
        if e is None:
            return 0.0
        return (self._slkN if e[1] else self._slkE)[e[0]]

    def passable(self, layer: int, tx: int, ty: int, side: str) -> bool:
        """False for a border that lies wholly outside the board outline (or off the grid)."""
        e = self._edge(layer, tx, ty, side)
        return e is not None and not (self.hardN if e[1] else self.hardE)[e[0]]

    def free_intervals(self, layer, tx, ty, side) -> list:
        """Free intervals of a border as (lo, hi) in board mm along the border (y for E, x for N)."""
        e = self._edge(layer, tx, ty, side)
        if e is None or (self.hardN if e[1] else self.hardE)[e[0]]:
            return []
        f = self._free.get(e[0] * 2 + e[1])
        if f is not None:
            return list(f)
        r = e[0] % self.G
        lo = (self.x0 + (r % self.nx) * self.t) if e[1] else (self.y0 + (r // self.nx) * self.t)
        return [(lo, lo + self.t)]

    def blocked(self, layer, tx, ty, side) -> list:
        """What blocks a border: [(lo, hi, tag)] sorted along the border; tag = 'U3.4', 'edge', 'hole', ..."""
        e = self._edge(layer, tx, ty, side)
        if e is None:
            return []
        out = []
        for lo, hi, _nid, tag in self._blk.get(e[0] * 2 + e[1], ()):
            out.append((lo, hi, tag.name if hasattr(tag, "name") else self._tagname(tag)))
        out.sort()
        return out

    def pad_edges(self, pad: str | None) -> frozenset:
        """Border codes (``s*2 + side``) touched by the inflated copper / hole of pad 'U3.4': a connection of
        that pad crosses them inside its own pad's shadow and puts no demand there."""
        return frozenset(self._shadow.get(pad, ())) if pad else frozenset()

    def own_shadow(self, code: int, pos: float, net_id: int, half: float = 0.0) -> bool:
        """Is position ``pos`` along border ``code`` inside the shadow of static copper of net ``net_id``?"""
        if not net_id:
            return False
        for lo, hi, nid, _t in self._blk.get(code, ()):
            if nid == net_id and lo - half <= pos <= hi + half:
                return True
        return False

    # ── vias ──────────────────────────────────────────────────────────────
    def _via_sites(self, col: int) -> int:
        """Vias that fit in the tile through every layer. A k × k sample lattice (spacing <= half the via
        pitch) anchored on the tile gives the legal share of the tile; capacity = share × T² / pitch²,
        rounded down with a quarter of slack, but at least 1 when any sample is legal."""
        T = self.t
        pv = self.via_size + self.ref_cl
        ty, tx = divmod(col, self.nx)
        k = max(2, int(math.ceil(T / (pv / 2) - 1e-9)))
        shapes = self._tshapes.get(col, ())
        vhw = self.via_size / 2
        inside = self.oracle.inside
        # a tile whose four borders lie inside the outline and that holds no outline segment is on the board:
        # no point-in-polygon test per sample (it dominated the global stage on boards with detailed outlines)
        G, nx, W = self.G, self.nx, self._whole
        if 0 < tx < nx - 1 and 0 < ty < self.ny - 1 and W[col] and W[col - 1] and W[G + col] and W[G + col - nx] \
                and not any(sh.cls == "edge" for sh, _nv in shapes):
            inside = None
        legal = 0
        bx, by = self.x0 + tx * T, self.y0 + ty * T
        for j in range(k):
            y = by + (j + 0.5) * T / k
            for i in range(k):
                x = bx + (i + 0.5) * T / k
                if inside is not None and not inside(x, y):
                    continue
                for s, nv in shapes:
                    sb = s.bbox
                    g = nv + vhw
                    if sb[0] - g > x or sb[2] + g < x or sb[1] - g > y or sb[3] + g < y:
                        continue
                    if s.dist_pt(x, y) - vhw < nv - 1e-9:
                        break
                else:
                    legal += 1
        if not legal:
            return 0
        return max(1, int(math.floor(legal / (k * k) * (T * T) / (pv * pv) + 0.25)))

    def via_cap(self, tx: int, ty: int) -> int:
        """Through vias (default class size + reference clearance) that still fit in this column."""
        if not (0 <= tx < self.nx and 0 <= ty < self.ny):
            return 0
        col = ty * self.nx + tx
        v = self._vsites[col]
        if v < 0:
            v = self._vsites[col] = self._via_sites(col)
        v -= self._vbook[col]
        return v if v > 0 else 0

    # ── bookings ──────────────────────────────────────────────────────────
    def _refresh(self, s, side):
        if side:
            v = (self._rawN[s] + self._slkN[s]) * self.cal * self._sclN[s] - self._bokN[s]
            self.capN[s] = v if v > 0 else 0.0
        else:
            v = (self._rawE[s] + self._slkE[s]) * self.cal * self._sclE[s] - self._bokE[s]
            self.capE[s] = v if v > 0 else 0.0
        self.version += 1

    def book(self, layer, tx, ty, side, mm: float) -> None:
        """Take ``mm`` of capacity off a border (thermal spokes, reservations, repair feedback). Negative gives back."""
        e = self._edge(layer, tx, ty, side)
        if e is None:
            return
        (self._bokN if e[1] else self._bokE)[e[0]] += mm
        self._refresh(*e)

    def book_via(self, tx, ty, n: int = 1) -> None:
        if 0 <= tx < self.nx and 0 <= ty < self.ny:
            self._vbook[ty * self.nx + tx] += n
            self.version += 1

    def book_shape(self, layer: int, shape, grow: float = 0.0) -> int:
        """Book a reserved area (a thermal-spoke corridor, a keep-clear box) as demand: every border of ``layer``
        the shape (a geom.Shape, inflated by ``grow``) crosses loses the crossed length + the reference
        clearance. Returns the number of borders booked."""
        T, x0, y0, nx, ny = self.t, self.x0, self.y0, self.nx, self.ny
        sb = shape.bbox
        n = 0
        for axis, lo_b, hi_b, org, cnt in ((0, sb[0], sb[2], x0, nx), (1, sb[1], sb[3], y0, ny)):
            k0 = max(1, int(math.ceil((lo_b - grow - org) / T - 1e-9)))
            k1 = min(cnt - 1, int(math.floor((hi_b + grow - org) / T + 1e-9)))
            for k in range(k0, k1 + 1):
                for lo, hi in shape_spans(shape, grow, axis, org + k * T):
                    if hi <= lo:
                        continue
                    o2 = y0 if axis == 0 else x0
                    lim = ny if axis == 0 else nx
                    j0 = max(0, int(math.floor((lo - o2) / T)))
                    j1 = min(lim - 1, int(math.floor((hi - o2) / T - 1e-12)))
                    for j in range(j0, j1 + 1):
                        a, b = o2 + j * T, o2 + (j + 1) * T
                        ln = (hi if hi < b else b) - (lo if lo > a else a)
                        if ln <= 0:
                            continue
                        if axis == 0:
                            self.book(layer, k - 1, j, "E", ln + self.ref_cl)
                        else:
                            self.book(layer, j, k - 1, "N", ln + self.ref_cl)
                        n += 1
        return n

    def scale(self, layer, tx, ty, side, factor: float) -> None:
        """Multiply a border's capacity (repair feedback, region.capacity_scale). Factors accumulate."""
        e = self._edge(layer, tx, ty, side)
        if e is None:
            return
        (self._sclN if e[1] else self._sclE)[e[0]] *= factor
        self._refresh(*e)

    def scale_rect(self, rect, factor: float, layers=None) -> int:
        """``scale`` every border whose midpoint lies in rect [x0, y0, x1, y1]; returns how many."""
        x0, y0, x1, y1 = rect
        T = self.t
        n = 0
        for l in (range(self.nl) if layers is None else layers):
            for ty in range(self.ny):
                for tx in range(self.nx):
                    ex, ey = self.x0 + (tx + 1) * T, self.y0 + (ty + 0.5) * T
                    if x0 <= ex <= x1 and y0 <= ey <= y1 and tx < self.nx - 1:
                        self.scale(l, tx, ty, "E", factor)
                        n += 1
                    ex, ey = self.x0 + (tx + 0.5) * T, self.y0 + (ty + 1) * T
                    if x0 <= ex <= x1 and y0 <= ey <= y1 and ty < self.ny - 1:
                        self.scale(l, tx, ty, "N", factor)
                        n += 1
        return n

    # ── cuts ──────────────────────────────────────────────────────────────
    def pad_cut(self, pad_a: str, pad_b: str, layer: int, w: float, cl: float) -> Cut:
        """The cut between two pads ('U3.4', 'U3.5') on ``layer`` for tracks of width ``w`` and clearance ``cl``:
        ``tracks = floor((gap − cl) / (w + cl))`` with the exact edge-to-edge gap (which is the vertex gap for
        diagonal neighbours); a pad that asks for more than ``cl`` (local override, mask rule) narrows the gap
        by the difference. ``capacity_mm = tracks × (w + cl)``; 0 when the pads touch or have no copper there."""
        o = self.oracle
        gap = o.pad_gap(pad_a, pad_b, layer)
        cap = 0.0
        if gap != INF and gap > 0:
            extra = 0.0
            for name in (pad_a, pad_b):
                n = cl
                for pad in self._pads.get(name, ()):
                    for s in pad.shapes.get(layer, ()):
                        v = o.need(None, w / 2, s, layer, "seg", 0.0, cl)
                        if v != NO_NEED and v > n:
                            n = v
                extra += n - cl
            tracks = int(math.floor((gap - extra - cl) / (w + cl) + _EPS))
            cap = max(0, tracks) * (w + cl)
        lname = self.board.layers[layer] if 0 <= layer < self.nl else str(layer)
        return Cut(a=pad_a, b=pad_b, layer=lname, capacity_mm=round(cap, 6), demand_mm=0.0, nets=[])

    def neighbour_cuts(self, ref: str, layer: int, w: float, cl: float, max_gap: float = 3.0) -> list[Cut]:
        """Pad-pair cuts around footprint ``ref``: each of its pads against its nearest neighbours in the same
        footprint (side and diagonal) and against facing pads of other footprints, gap <= ``max_gap``. Sorted by
        pad names; every pair once."""
        mine = [p for p in self.board.pads if p.ref == ref and p.shapes.get(layer)]
        if not mine:
            return []
        xs = [p.x for p in mine]
        ys = [p.y for p in mine]
        box = (min(xs) - max_gap - 5, min(ys) - max_gap - 5, max(xs) + max_gap + 5, max(ys) + max_gap + 5)
        others = [p for p in self.board.pads if p.ref != ref and p.shapes.get(layer)
                  and box[0] <= p.x <= box[2] and box[1] <= p.y <= box[3]]
        pairs = {}
        for p in mine:
            near = sorted(((math.hypot(q.x - p.x, q.y - p.y), q.name) for q in mine if q is not p and q.name != p.name))
            for _d, name in near[:8]:
                pairs[tuple(sorted((p.name, name)))] = True
            for q in others:
                if math.hypot(q.x - p.x, q.y - p.y) <= max_gap + 6:
                    pairs[(p.name, q.name)] = True
        out = []
        for a, b in sorted(pairs):
            gap = self.oracle.pad_gap(a, b, layer)
            if 0 < gap <= max_gap:
                out.append(self.pad_cut(a, b, layer, w, cl))
        return out

    def cut_for_edge(self, layer, tx, ty, side, demand: list[tuple[str, float]]) -> Cut:
        """The named cut of a tile border: ``a`` / ``b`` are the obstacles flanking its widest free interval
        (pad names, 'edge', 'hole', ...), or 'tile(tx,ty)|E' when nothing bounds it on that side."""
        e = self._edge(layer, tx, ty, side)
        lname = self.board.layers[layer] if 0 <= layer < self.nl else str(layer)
        here = f"tile({tx},{ty})|{side}"
        nets = sorted({n for n, _mm in demand if n})
        dem = round(sum(mm for _n, mm in demand), 6)
        if e is None:
            return Cut(a=here, b=here, layer=lname, capacity_mm=0.0, demand_mm=dem, nets=nets)
        side = "N" if e[1] else "E"
        r = e[0] % self.G
        tx, ty = r % self.nx, r // self.nx
        here = f"tile({tx},{ty})|{side}"
        blk = self.blocked(layer, tx, ty, side)
        free = self.free_intervals(layer, tx, ty, side)
        a = b = here
        if blk:
            if free:
                lo, hi = max(free, key=lambda f: (f[1] - f[0], -f[0]))
            else:
                lo = hi = (blk[0][0] + blk[-1][1]) / 2
            below = [x for x in blk if x[1] <= lo + 1e-6]
            above = [x for x in blk if x[0] >= hi - 1e-6]
            if below:
                a = max(below, key=lambda x: (x[1], x[2]))[2]
            if above:
                b = min(above, key=lambda x: (x[0], x[2]))[2]
            if not free and a == here and b == here:
                a = b = max(blk, key=lambda x: (x[1] - x[0], x[2]))[2]
        return Cut(a=a, b=b, layer=lname, capacity_mm=round(self.cap(layer, tx, ty, side), 6), demand_mm=dem, nets=nets)

    # ── the last global-routing result, for diagnostics ───────────────────
    def set_usage(self, useE, useN, useV) -> None:
        self.useE, self.useN, self.useV = useE, useN, useV

    def utilisation(self, layer: int) -> list[list[int]]:
        """Per tile (rows = ty) the worst use / capacity of its four borders in percent (0 when nothing was
        routed; 999 = demand on a border without capacity). Empty before a global route."""
        if self.useE is None:
            return []
        nx, ny, G = self.nx, self.ny, self.G
        base = layer * G
        out = []
        for ty in range(ny):
            row = []
            for tx in range(nx):
                s = base + ty * nx + tx
                worst = 0.0
                for use, cap, i, ok in ((self.useE, self.capE, s, tx < nx - 1), (self.useN, self.capN, s, ty < ny - 1),
                                        (self.useE, self.capE, s - 1, tx > 0), (self.useN, self.capN, s - nx, ty > 0)):
                    if ok and use[i] > 1e-9:
                        u = use[i] / cap[i] if cap[i] > 1e-9 else 9.99
                        if u > worst:
                            worst = u
                row.append(int(round(min(worst, 9.99) * 100)))
            out.append(row)
        return out


def make(board, oracle: "Oracle", conns=None, tile_mm: float | None = None, cal: float | None = None) -> Tiles:
    """The tile graph the engine should build: tile size by ``choose_tile_mm`` (or the plan's ``tile_mm``),
    reference class from the connections, calibration constant ``CAL``."""
    w, cl = reference_class(board, conns)
    return Tiles(board, oracle, tile_mm or choose_tile_mm(board), w, cl, CAL if cal is None else cal)


# ── calibration against human routing ─────────────────────────────────────
def _crossings(tiles: Tiles, x1, y1, x2, y2):
    """Tile borders a segment crosses: [(tx, ty, side 0 E | 1 N, position along the border)]."""
    T, x0, y0, nx, ny = tiles.t, tiles.x0, tiles.y0, tiles.nx, tiles.ny
    out = []
    if x1 != x2:
        a, b = (x1, x2) if x1 < x2 else (x2, x1)
        for k in range(max(1, int(math.floor((a - x0) / T)) + 1), min(nx - 1, int(math.ceil((b - x0) / T)) - 1) + 1):
            c = x0 + k * T
            if not (a < c < b):
                continue
            y = y1 + (c - x1) * (y2 - y1) / (x2 - x1)
            ty = int(math.floor((y - y0) / T))
            if 0 <= ty < ny:
                out.append((k - 1, ty, 0, y))
    if y1 != y2:
        a, b = (y1, y2) if y1 < y2 else (y2, y1)
        for k in range(max(1, int(math.floor((a - y0) / T)) + 1), min(ny - 1, int(math.ceil((b - y0) / T)) - 1) + 1):
            c = y0 + k * T
            if not (a < c < b):
                continue
            x = x1 + (c - y1) * (x2 - x1) / (y2 - y1)
            tx = int(math.floor((x - x0) / T))
            if 0 <= tx < nx:
                out.append((tx, k - 1, 1, x))
    return out


def reference_demand(tiles: Tiles, tracks: list, own: bool = True, by: str = "count") -> dict:
    """Calibration helper: the demand human-routed tracks put on the tile borders.

    ``tracks``: [net, layer (index or name), x1, y1, x2, y2, width, ...]. Every border crossing adds to
    ``{(layer, tx, ty, side): mm}``: one reference pitch with ``by='count'`` (the design's reading: count the
    tracks), or ``width + clearance(net)`` with ``by='width'`` (what a connection of that width would demand).
    With ``own`` (default) a crossing inside the shadow of static copper of the track's own net (it is leaving
    its own pad) adds nothing: the same exemption the global router gives a connection at its terminal pads."""
    b, o = tiles.board, tiles.oracle
    lay = {n: i for i, n in enumerate(b.layers)}
    out = {}
    G, nx = tiles.G, tiles.nx
    for t in tracks:
        net, l, x1, y1, x2, y2, w = t[:7]
        if not isinstance(l, int):
            l = lay.get(l, -1)
        if not (0 <= l < tiles.nl):
            continue
        d = tiles.pitch if by == "count" else w + o.clearance(net)
        nid = o.net_id(net)
        for tx, ty, side, pos in _crossings(tiles, x1, y1, x2, y2):
            if own and tiles.own_shadow((l * G + ty * nx + tx) * 2 + side, pos, nid, w / 2):
                continue
            k = (l, tx, ty, "N" if side else "E")
            out[k] = out.get(k, 0.0) + d
    return out


def calibration(tiles: Tiles, tracks: list, cals=(0.8, 0.9, 1.0, 1.1, 1.2, 1.3), by: str = "count") -> dict:
    """Compare capacities with human demand (design §4.6, calibration gate).

    -> {'borders': passable borders, 'used': borders a reference track crosses, 'demand_mm', 'by_cal':
        {cal: {'short': borders with demand > cap × cal, 'ok_all': share of all borders, 'ok_used': share of
        used borders}}, 'ratio_hist': demand / capacity of used borders in bins, 'zero_cap_used': used borders
        with no capacity at all, 'worst': the ten largest shortfalls at cal 1.0}"""
    dem = reference_demand(tiles, tracks, by=by)
    total = 2 * tiles.N - sum(tiles.hardE) - sum(tiles.hardN)      # borders that exist and are on the board
    base = tiles.cal or 1.0
    by = {}
    rows = []
    for k, d in sorted(dem.items()):
        cap = tiles.cap(*k) / base
        rows.append((d, cap, k))
    for c in cals:
        short = sum(1 for d, cap, _k in rows if d > cap * c + 1e-6)
        by[c] = {"short": short, "ok_all": round(1 - short / total, 5) if total else 1.0,
                 "ok_used": round(1 - short / len(rows), 5) if rows else 1.0}
    bins = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
    hist = {f"<={x}": 0 for x in bins}
    hist[">3"] = 0
    zero = 0
    for d, cap, _k in rows:
        if cap <= 1e-9:
            zero += 1
            hist[">3"] += 1
            continue
        r = d / cap
        for x in bins:
            if r <= x + 1e-9:
                hist[f"<={x}"] += 1
                break
        else:
            hist[">3"] += 1
    worst = sorted(rows, key=lambda r: (-(r[0] - r[1]), r[2]))[:10]
    return {"borders": total, "used": len(rows), "demand_mm": round(sum(r[0] for r in rows), 3), "by_cal": by,
            "ratio_hist": hist, "zero_cap_used": zero,
            "worst": [{"edge": list(k), "demand_mm": round(d, 4), "cap_mm": round(cap, 4)} for d, cap, k in worst]}
