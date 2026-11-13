"""STL (ASCII and binary) -> plane groups with the same interface as ``pick.Group`` (pure stdlib).

Triangles are grouped by plane; the boundary of a group is every edge used by exactly one of its
triangles, chained and arc-fitted, so a tessellated board comes back as lines and arcs. STL has no
units: millimetres are assumed unless the caller says otherwise.
"""
from __future__ import annotations

import math
import re
import struct

from . import ir
from . import step as p21
from .pick import _extent, frame, order


def read(path, max_bytes=40 * 1024 * 1024):
    import os
    if os.path.getsize(path) > max_bytes:
        raise p21.TooLarge("%.0f MB" % (os.path.getsize(path) / 1048576.0))
    with open(path, "rb") as f:
        data = f.read()
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if len(data) >= 84 and len(data) == 84 + 50 * n:          # binary (checked by size: some start with "solid")
        tris = []
        for i in range(n):
            fl = struct.unpack_from("<12f", data, 84 + 50 * i)
            tris.append((fl[3:6], fl[6:9], fl[9:12]))
        return tris
    if data[:5].lower() == b"solid" or b"facet" in data[:1000]:
        nums = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        v = [(float(a), float(b), float(c)) for a, b, c in nums]
        return [(v[i], v[i + 1], v[i + 2]) for i in range(0, len(v) - 2, 3)]
    raise ValueError("not an STL file")


class StlGroup:
    def __init__(self, n, d):
        self.n, self.d, self.solid, self.tris = n, d, 0, []
        self.gross = 0.0
        self.min_id = 0
        self.unknown = 0
        self._ext = None
        self._cache = {}

    def loops(self, up=None):
        w = tuple(up or self.n)
        key = tuple(round(v, 6) for v in w)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        ex, ey = frame(w)
        q = lambda p: (round(p21.dot(p, ex), 5), round(-p21.dot(p, ey), 5))
        edges = {}
        for t in self.tris:
            p = [q(v) for v in t]
            for i in range(3):
                a, b = p[i], p[(i + 1) % 3]
                k = (a, b) if a < b else (b, a)
                edges[k] = edges.get(k, 0) + 1
        prims = [("L", a, b) for (a, b), n in edges.items() if n == 1 and a != b]
        loops, opens = ir.chain(prims, tol=2e-4)
        res = ([ir.simplify(lp) for lp in loops], opens)
        self._cache[key] = res
        return res

    def extent(self):
        if self._ext is None:
            self._ext = _extent(self.loops()[0])
            if self._ext[0]:
                self.gross = self._ext[0]
        return self._ext

    def bbox_on(self, up):
        pts = [p for lp in self.loops(up)[0] for p in ir.loop_pts(lp)]
        if not pts:
            return None
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def groups(tris, scale=1.0, ang_q=1e-3, d_q=0.005):
    planes = {}
    for k, (a, b, c) in enumerate(tris):
        if scale != 1.0:
            a, b, c = tuple(v * scale for v in a), tuple(v * scale for v in b), tuple(v * scale for v in c)
        n = p21.cross(p21.sub(b, a), p21.sub(c, a))
        l = math.sqrt(p21.dot(n, n))
        if l < 1e-12:
            continue
        n = (n[0] / l, n[1] / l, n[2] / l)
        d = p21.dot(a, n)
        key = (round(n[0] / ang_q), round(n[1] / ang_q), round(n[2] / ang_q), round(d / d_q))
        g = planes.get(key)
        if g is None:
            g = planes[key] = StlGroup(n, d)
            g.min_id = k
        g.gross += l / 2
        g.tris.append((a, b, c))
    return order(list(planes.values()))
