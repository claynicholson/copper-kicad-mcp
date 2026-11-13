"""Routing grid: per-"probe class" occupancy maps with exact, clearance-aware
rasterisation.

A *probe class* is ``(half_width, clearance, via_radius, via_drill)`` of the
net being routed. For each class there is one owner map per copper layer and
one via map (through vias occupy every layer):

    0        free: a track centre line / via centre may be here
    n > 0    only net ``n`` may be here (inside the clearance halo of net n's own copper)
    -1       blocked for everyone

A cell is tested at its centre: it is blocked when the centre is closer than
``half_width + clearance`` to foreign copper. Because every halo is at least
2 cells thick, a straight step between two free neighbouring centres can dip
into a halo by only a few micrometres; ``EPS`` covers that, so on-grid tracks
are clearance-correct by construction.

Dynamic (routed) copper is reference-counted with three arrays (count, sum of
net ids, sum of squares) so a rip-up restores the map exactly: all covering
nets are the same net iff ``sumsq * count == sum * sum``.
"""
from __future__ import annotations

import math
from array import array

from .geom import box_dist

EPS = 0.012          # mm added to every separation (grid sagitta + rounding)
BLOCK = 46337        # pseudo net id for "blocks everyone" dynamic marks (never a real net id)


def _ceil(v):
    return int(math.ceil(v - 1e-9))


def _floor(v):
    return int(math.floor(v + 1e-9))


class ClassMap:
    __slots__ = ("key", "h", "c", "vr", "vd", "S", "occ", "SV", "vocc", "cnt", "s1", "s2", "vcnt", "vs1", "vs2")

    def __init__(self, key, n_t, n_v):
        self.key = key
        self.h, self.c, self.vr, self.vd = key
        self.S = array("i", [0]) * n_t        # static owners, tracks (L*G)
        self.SV = array("i", [0]) * n_v       # static owners, vias (G)
        self.occ = None
        self.vocc = None
        self.cnt = array("H", [0]) * n_t
        self.s1 = array("q", [0]) * n_t
        self.s2 = array("q", [0]) * n_t
        self.vcnt = array("H", [0]) * n_v
        self.vs1 = array("q", [0]) * n_v
        self.vs2 = array("q", [0]) * n_v

    def freeze(self):
        self.occ = array("i", self.S)
        self.vocc = array("i", self.SV)

    def mark(self, via, cells, nid):
        if via:
            cnt, s1, s2, occ = self.vcnt, self.vs1, self.vs2, self.vocc
        else:
            cnt, s1, s2, occ = self.cnt, self.s1, self.s2, self.occ
        sq = nid * nid
        own = -1 if nid == BLOCK else nid
        for i in cells:
            cnt[i] += 1
            s1[i] += nid
            s2[i] += sq
            o = occ[i]
            if o == 0:
                occ[i] = own
            elif o != own:
                occ[i] = -1

    def unmark(self, via, cells, nid):
        if via:
            cnt, s1, s2, occ, S = self.vcnt, self.vs1, self.vs2, self.vocc, self.SV
        else:
            cnt, s1, s2, occ, S = self.cnt, self.s1, self.s2, self.occ, self.S
        sq = nid * nid
        for i in cells:
            c = cnt[i] - 1
            cnt[i] = c
            a = s1[i] - nid
            s1[i] = a
            b = s2[i] - sq
            s2[i] = b
            s = S[i]
            if c == 0:
                occ[i] = s
            elif s == -1 or b * c != a * a:
                occ[i] = -1
            else:
                n = a // c
                if n == BLOCK:
                    occ[i] = -1
                elif s == 0 or s == n:
                    occ[i] = n
                else:
                    occ[i] = -1


class Grid:
    def __init__(self, bbox, step, n_layers):
        x0, y0, x1, y1 = bbox
        self.step = step
        # one blocked border cell all round so the search never needs bounds checks
        self.x0 = x0 - step
        self.y0 = y0 - step
        self.W = int(math.ceil((x1 - x0) / step)) + 3
        self.H = int(math.ceil((y1 - y0) / step)) + 3
        self.G = self.W * self.H
        self.L = n_layers
        self.N = self.G * n_layers
        self.classes = {}
        # search scratch arrays (shared by every search)
        self.gs = array("d", [0.0]) * self.N
        self.st = array("I", [0]) * self.N
        self.par = array("i", [0]) * self.N
        self.pd = bytearray(self.N)
        self.sid = 0

    # ── coordinates ───────────────────────────────────────────────────────
    def gx(self, x):
        return (x - self.x0) / self.step

    def gy(self, y):
        return (y - self.y0) / self.step

    def mx(self, ix):
        return round(self.x0 + ix * self.step, 6)

    def my(self, iy):
        return round(self.y0 + iy * self.step, 6)

    def cell(self, x, y):
        ix = int(round((x - self.x0) / self.step))
        iy = int(round((y - self.y0) / self.step))
        return min(max(ix, 0), self.W - 1), min(max(iy, 0), self.H - 1)

    def xy(self, i):
        """flat (single layer) index -> (x mm, y mm)"""
        return self.mx(i % self.W), self.my(i // self.W)

    # ── span generators (grid units, clipped to the interior) ─────────────
    def capsule_spans(self, x1, y1, x2, y2, R):
        """Rows of cells whose centre is within R (mm) of segment (x1,y1)-(x2,y2) (mm)."""
        s = self.step
        ax, ay, bx, by = (x1 - self.x0) / s, (y1 - self.y0) / s, (x2 - self.x0) / s, (y2 - self.y0) / s
        R = R / s
        if R <= 0:
            return
        W, H = self.W, self.H
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        R2 = R * R
        ya, yb = (ay, by) if ay <= by else (by, ay)
        r0, r1 = max(1, _ceil(ya - R)), min(H - 2, _floor(yb + R))
        if L < 1e-9:
            for iy in range(r0, r1 + 1):
                d = iy - ay
                q = R2 - d * d
                if q < 0:
                    continue
                q = math.sqrt(q)
                i0, i1 = max(1, _ceil(ax - q)), min(W - 2, _floor(ax + q))
                if i0 <= i1:
                    yield iy, i0, i1
            return
        nx, ny = -dy / L * R, dx / L * R
        quad = ((ax + nx, ay + ny), (bx + nx, by + ny), (bx - nx, by - ny), (ax - nx, ay - ny))
        edges = []
        for k in range(4):
            p, q = quad[k], quad[(k + 1) % 4]
            if abs(p[1] - q[1]) > 1e-12:
                edges.append((p[0], p[1], q[0], q[1]) if p[1] < q[1] else (q[0], q[1], p[0], p[1]))
        for iy in range(r0, r1 + 1):
            lo, hi = 1e18, -1e18
            d = iy - ay
            q = R2 - d * d
            if q >= 0:
                q = math.sqrt(q)
                lo, hi = ax - q, ax + q
            d = iy - by
            q = R2 - d * d
            if q >= 0:
                q = math.sqrt(q)
                if bx - q < lo:
                    lo = bx - q
                if bx + q > hi:
                    hi = bx + q
            for px, py, qx, qy in edges:
                if py <= iy <= qy:
                    x = px + (iy - py) * (qx - px) / (qy - py)
                    if x < lo:
                        lo = x
                    if x > hi:
                        hi = x
            i0, i1 = max(1, _ceil(lo)), min(W - 2, _floor(hi))
            if i0 <= i1:
                yield iy, i0, i1

    def box_spans(self, b, grow):
        """Rows of cells whose centre is within ``grow`` of the box (grow may be negative = inset)."""
        cx, cy, ux, uy, ihw, ihh, r = b
        s = self.step
        W, H = self.W, self.H
        if ihh == 0 and r + grow > 0:
            # a capsule (track / circle / oval)
            yield from self.capsule_spans(cx - ux * ihw, cy - uy * ihw, cx + ux * ihw, cy + uy * ihw, r + grow)
            return
        if ihw == 0 and r + grow > 0:
            yield from self.capsule_spans(cx + uy * ihh, cy - ux * ihh, cx - uy * ihh, cy + ux * ihh, r + grow)
            return
        aligned = abs(abs(ux) - 1) < 1e-6 or abs(abs(uy) - 1) < 1e-6
        if aligned:
            ex, ey = (ihw, ihh) if abs(ux) > 0.5 else (ihh, ihw)
            rr = r + grow
            if rr < 0:
                ex, ey, rr = ex + rr, ey + rr, 0.0
                if ex < 0 or ey < 0:
                    return
            gcx, gcy = (cx - self.x0) / s, (cy - self.y0) / s
            ex, ey, rr = ex / s, ey / s, rr / s
            r0, r1 = max(1, _ceil(gcy - ey - rr)), min(H - 2, _floor(gcy + ey + rr))
            for iy in range(r0, r1 + 1):
                d = abs(iy - gcy) - ey
                if d <= 0:
                    half = ex + rr
                else:
                    q = rr * rr - d * d
                    if q < 0:
                        continue
                    half = ex + math.sqrt(q)
                i0, i1 = max(1, _ceil(gcx - half)), min(W - 2, _floor(gcx + half))
                if i0 <= i1:
                    yield iy, i0, i1
            return
        # rotated box: test cell centres inside its bounding box
        ext = abs(ux) * ihw + abs(uy) * ihh + r + max(grow, 0)
        eyt = abs(uy) * ihw + abs(ux) * ihh + r + max(grow, 0)
        c0, c1 = max(1, _ceil((cx - ext - self.x0) / s)), min(W - 2, _floor((cx + ext - self.x0) / s))
        r0, r1 = max(1, _ceil((cy - eyt - self.y0) / s)), min(H - 2, _floor((cy + eyt - self.y0) / s))
        x0, y0 = self.x0, self.y0
        for iy in range(r0, r1 + 1):
            y = y0 + iy * s
            start = None
            for ix in range(c0, c1 + 1):
                if box_dist(b, x0 + ix * s, y) <= grow:
                    if start is None:
                        start = ix
                elif start is not None:
                    yield iy, start, ix - 1
                    start = None
            if start is not None:
                yield iy, start, c1

    def poly_spans(self, pts):
        """Interior rows of a polygon (even-odd, sampled at cell centres)."""
        s = self.step
        W, H = self.W, self.H
        g = [((x - self.x0) / s, (y - self.y0) / s) for x, y in pts]
        n = len(g)
        if n < 3:
            return
        ys = [p[1] for p in g]
        r0, r1 = max(1, _ceil(min(ys))), min(H - 2, _floor(max(ys)))
        # bucket edges by row for long polygons (zone fills have thousands of points)
        rows = {}
        for i in range(n):
            x1, y1 = g[i]
            x2, y2 = g[(i + 1) % n]
            if y1 == y2:
                continue
            if y1 > y2:
                x1, y1, x2, y2 = x2, y2, x1, y1
            a, b = max(r0, _ceil(y1)), min(r1, _floor(y2))
            k = (x2 - x1) / (y2 - y1)
            for iy in range(a, b + 1):
                if y1 <= iy < y2:
                    rows.setdefault(iy, []).append(x1 + (iy - y1) * k)
        for iy in sorted(rows):
            xs = sorted(rows[iy])
            for j in range(0, len(xs) - 1, 2):
                i0, i1 = max(1, _ceil(xs[j])), min(W - 2, _floor(xs[j + 1]))
                if i0 <= i1:
                    yield iy, i0, i1

    def shape_spans(self, shape, grow):
        """Spans for a geom.Shape grown by ``grow`` (mm)."""
        if shape.kind == "box":
            yield from self.box_spans(shape.g, grow)
            return
        pts = shape.g
        yield from self.poly_spans(pts)
        if grow > 0:
            n = len(pts)
            for i in range(n):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % n]
                yield from self.capsule_spans(x1, y1, x2, y2, grow)

    def cells(self, spans, base=0):
        W = self.W
        out = []
        for iy, i0, i1 in spans:
            out.extend(range(base + iy * W + i0, base + iy * W + i1 + 1))
        return out

    # ── static stamping ───────────────────────────────────────────────────
    def stamp(self, arr, base, spans, owner):
        W = self.W
        if owner == -1:
            for iy, i0, i1 in spans:
                a = base + iy * W + i0
                n = i1 - i0 + 1
                arr[a:a + n] = array("i", [-1]) * n
            return
        for iy, i0, i1 in spans:
            a = base + iy * W
            for i in range(a + i0, a + i1 + 1):
                o = arr[i]
                if o == 0:
                    arr[i] = owner
                elif o != owner:
                    arr[i] = -1
