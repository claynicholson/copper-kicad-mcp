"""Exact 2-D geometry for the guided router (pure Python, millimetres).

Two primitives cover every copper shape on a board:

* ``box``  — a rounded, rotated rectangle ``(cx, cy, ux, uy, ihw, ihh, r)``:
  the inner rectangle (half extents ``ihw`` × ``ihh`` along the unit axis
  ``(ux, uy)`` and its normal) grown by ``r``. Circles (ihw = ihh = 0), track
  segments (ihh = 0), ovals, rectangles and round-rects are all boxes.
* ``poly`` — a simple polygon (list of points), used for custom pads, rule
  areas, the board outline and zone fills.

``ShapeIndex`` is a bucketed spatial index answering "how close does this
track segment / via come to copper of another net".
"""
from __future__ import annotations

import math

INF = float("inf")


def box_from_pad(g):
    """Worker pad box [cx, cy, ux, uy, half_w, half_h, corner_r] -> box tuple."""
    cx, cy, ux, uy, hw, hh, r = g
    r = max(0.0, min(r, hw, hh))
    return (cx, cy, ux, uy, hw - r, hh - r, r)


def box_from_seg(x1, y1, x2, y2, w):
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return ((x1 + x2) / 2, (y1 + y2) / 2, 1.0, 0.0, 0.0, 0.0, w / 2)
    return ((x1 + x2) / 2, (y1 + y2) / 2, dx / L, dy / L, L / 2, 0.0, w / 2)


def box_circle(x, y, r):
    return (x, y, 1.0, 0.0, 0.0, 0.0, r)


def box_bbox(b, grow=0.0):
    cx, cy, ux, uy, ihw, ihh, r = b
    ex = abs(ux) * ihw + abs(uy) * ihh + r + grow
    ey = abs(uy) * ihw + abs(ux) * ihh + r + grow
    return (cx - ex, cy - ey, cx + ex, cy + ey)


def box_dist(b, px, py):
    """Signed distance from a point to the box outline (negative inside)."""
    cx, cy, ux, uy, ihw, ihh, r = b
    dx, dy = px - cx, py - cy
    lx = abs(dx * ux + dy * uy) - ihw
    ly = abs(dy * ux - dx * uy) - ihh
    if lx <= 0 and ly <= 0:
        return (lx if lx > ly else ly) - r
    if lx < 0:
        lx = 0.0
    if ly < 0:
        ly = 0.0
    return math.hypot(lx, ly) - r


def pt_seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    if L <= 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L
    if t < 0:
        t = 0.0
    elif t > 1:
        t = 1.0
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def pt_seg_proj(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    if L <= 0:
        return ax, ay
    t = ((px - ax) * dx + (py - ay) * dy) / L
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return ax + t * dx, ay + t * dy


def _orient(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segs_cross(a, b, c, d):
    o1 = _orient(a[0], a[1], b[0], b[1], c[0], c[1])
    o2 = _orient(a[0], a[1], b[0], b[1], d[0], d[1])
    o3 = _orient(c[0], c[1], d[0], d[1], a[0], a[1])
    o4 = _orient(c[0], c[1], d[0], d[1], b[0], b[1])
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0) and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0


def seg_seg_dist(ax, ay, bx, by, cx, cy, dx, dy):
    if segs_cross((ax, ay), (bx, by), (cx, cy), (dx, dy)):
        return 0.0
    return min(pt_seg_dist(ax, ay, cx, cy, dx, dy), pt_seg_dist(bx, by, cx, cy, dx, dy),
               pt_seg_dist(cx, cy, ax, ay, bx, by), pt_seg_dist(dx, dy, ax, ay, bx, by))


def seg_rect_dist(ax, ay, bx, by, hw, hh):
    """Distance from segment AB to the axis-aligned rectangle [-hw,hw]x[-hh,hh] (0 if they touch)."""
    # Liang–Barsky: does the segment enter the rectangle?
    t0, t1 = 0.0, 1.0
    dx, dy = bx - ax, by - ay
    inside = True
    for p, q in ((-dx, ax + hw), (dx, hw - ax), (-dy, ay + hh), (dy, hh - ay)):
        if p == 0:
            if q < 0:
                inside = False
                break
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    inside = False
                    break
                if t > t0:
                    t0 = t
            else:
                if t < t0:
                    inside = False
                    break
                if t < t1:
                    t1 = t
    if inside:
        return 0.0
    best = INF
    for px, py in ((ax, ay), (bx, by)):
        ex = abs(px) - hw
        ey = abs(py) - hh
        d = math.hypot(ex if ex > 0 else 0.0, ey if ey > 0 else 0.0)
        if d < best:
            best = d
    for cx, cy in ((hw, hh), (hw, -hh), (-hw, hh), (-hw, -hh)):
        d = pt_seg_dist(cx, cy, ax, ay, bx, by)
        if d < best:
            best = d
    return best


def seg_box_dist(x1, y1, x2, y2, b):
    """Distance from the segment (a centre line) to the box outline; <= 0 when they overlap."""
    cx, cy, ux, uy, ihw, ihh, r = b
    ax, ay = x1 - cx, y1 - cy
    bx, by = x2 - cx, y2 - cy
    lax, lay = ax * ux + ay * uy, ay * ux - ax * uy
    lbx, lby = bx * ux + by * uy, by * ux - bx * uy
    return seg_rect_dist(lax, lay, lbx, lby, ihw, ihh) - r


def pt_in_poly(pts, x, y):
    c = False
    n = len(pts)
    x1, y1 = pts[n - 1]
    for i in range(n):
        x2, y2 = pts[i]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
        x1, y1 = x2, y2
    return c


def seg_poly_dist(x1, y1, x2, y2, pts):
    if pt_in_poly(pts, x1, y1):
        return 0.0
    best = INF
    n = len(pts)
    px, py = pts[n - 1]
    for i in range(n):
        qx, qy = pts[i]
        d = seg_seg_dist(x1, y1, x2, y2, px, py, qx, qy)
        if d < best:
            best = d
            if d <= 0:
                return 0.0
        px, py = qx, qy
    return best


def pt_poly_dist(pts, x, y):
    """Signed distance to a polygon outline (negative inside)."""
    best = INF
    n = len(pts)
    px, py = pts[n - 1]
    for i in range(n):
        qx, qy = pts[i]
        d = pt_seg_dist(x, y, px, py, qx, qy)
        if d < best:
            best = d
        px, py = qx, qy
    return -best if pt_in_poly(pts, x, y) else best


def poly_bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def poly_area(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


class Shape:
    """A piece of copper (or a hole / edge / keep-out) the router must respect.

    kind   'box' | 'poly'
    cls    'cu' copper, 'hole' drilled hole, 'edge' board edge, 'kt' no-track area, 'kv' no-via area
    net    net id (0 = no net: blocks everyone)
    clr    the shape's own clearance (its net class / local override), mm
    layers tuple of layer indexes
    """
    __slots__ = ("kind", "g", "bbox", "cls", "net", "clr", "layers", "tag", "alive")

    def __init__(self, kind, g, cls, net, clr, layers, tag=None):
        self.kind, self.g, self.cls, self.net, self.clr = kind, g, cls, net, clr
        self.layers = tuple(layers)
        self.bbox = box_bbox(g) if kind == "box" else poly_bbox(g)
        self.tag = tag
        self.alive = True

    def dist_seg(self, x1, y1, x2, y2):
        if self.kind == "box":
            return seg_box_dist(x1, y1, x2, y2, self.g)
        return seg_poly_dist(x1, y1, x2, y2, self.g)

    def dist_pt(self, x, y):
        if self.kind == "box":
            return box_dist(self.g, x, y)
        return pt_poly_dist(self.g, x, y)


class ShapeIndex:
    """Bucketed index of shapes per layer for exact clearance queries."""

    def __init__(self, cell=1.0):
        self.cell = cell
        self.b = {}

    def _keys(self, bbox, grow=0.0):
        c = self.cell
        x0 = int(math.floor((bbox[0] - grow) / c))
        x1 = int(math.floor((bbox[2] + grow) / c))
        y0 = int(math.floor((bbox[1] - grow) / c))
        y1 = int(math.floor((bbox[3] + grow) / c))
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                yield x, y

    def add(self, s: Shape):
        b = self.b
        for lay in s.layers:
            for x, y in self._keys(s.bbox):
                b.setdefault((lay, x, y), []).append(s)

    def remove(self, s: Shape):
        s.alive = False
        b = self.b
        for lay in s.layers:
            for x, y in self._keys(s.bbox):
                lst = b.get((lay, x, y))
                if lst:
                    try:
                        lst.remove(s)
                    except ValueError:
                        pass

    def near(self, lay, bbox, grow):
        seen = set()
        b = self.b
        for x, y in self._keys(bbox, grow):
            for s in b.get((lay, x, y), ()):
                i = id(s)
                if i not in seen:
                    seen.add(i)
                    yield s
