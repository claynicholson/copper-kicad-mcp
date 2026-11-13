"""Minimal ISO 10303-21 (STEP) reader + planar-face extraction, pure stdlib.

Only what an outline needs: entities are indexed as raw text and parsed lazily, planar faces come
back with their boundary loops as 2D prims in the face's own frame. Assembly placements
(CONTEXT_DEPENDENT_SHAPE_REPRESENTATION / ITEM_DEFINED_TRANSFORMATION / MAPPED_ITEM) are resolved
into one 4x4 per solid so faces of different parts are compared in world coordinates.
"""
from __future__ import annotations

import gzip
import io
import math
import re
import zipfile

_TOK = re.compile(r"\s*(?:(#\d+)|('(?:[^']|'')*')|(\.[A-Z0-9_]+\.)|([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|([(),$*]))")


class Ref(int):
    __slots__ = ()


def _parse_args(s, i):
    """s[i] is just after '(' ; returns (list, index after ')')"""
    out = []
    m = _TOK.match
    while True:
        t = m(s, i)
        if not t:
            raise ValueError("bad token at %r" % s[i:i + 30])
        i = t.end()
        g = t.lastindex
        if g == 1:
            out.append(Ref(t.group(1)[1:]))
        elif g == 2:
            out.append(t.group(2)[1:-1])
        elif g == 3:
            out.append(t.group(3))
        elif g == 4:
            out.append(float(t.group(4)))
        elif g == 5:                       # typed parameter NAME(...)
            name = t.group(5)
            t2 = m(s, i)
            if t2 and t2.group(6) == "(":
                sub_, i = _parse_args(s, t2.end())
                out.append((name, sub_))
            else:
                out.append(name)
        else:
            c = t.group(6)
            if c == "(":
                sub_, i = _parse_args(s, i)
                out.append(sub_)
            elif c == ")":
                return out, i
            elif c == ",":
                continue
            else:
                out.append(None if c == "$" else "*")


_ENT = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]*)\s*\(")
_CMT = re.compile(r"/\*.*?\*/", re.S)
_Q = re.compile(r"(')")


MAX_BYTES = 40 * 1024 * 1024
_DATA = re.compile(r"\bDATA\s*(\([^;]*\))?\s*;")


class StepError(ValueError):
    """Not a readable STEP file (message is user-facing)."""


class TooLarge(StepError):
    pass


def read_text(path, max_bytes=MAX_BYTES):
    """File text as latin-1. STEP-Z is read through gzip (KiCad writes .stpz as a gzip stream) or zip."""
    import os
    if os.path.getsize(path) > max_bytes:
        raise TooLarge("%.0f MB" % (os.path.getsize(path) / 1048576.0))
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as z:
            raw = z.read(max_bytes * 4 + 1)
    elif raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            if not names:
                raise StepError("empty STEP-Z archive")
            raw = z.read(names[0])
    if len(raw) > max_bytes * 4:
        raise TooLarge("%.0f MB unpacked" % (len(raw) / 1048576.0))
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    return raw.decode("latin-1")


def _header_end(txt):
    """Index just after the header's ENDSEC; (quote-aware, so DATA; inside a header string is not the data section)."""
    k = txt.find("HEADER")
    if k < 0:
        return 0
    i, n, q = k, len(txt), False
    while i < n:
        c = txt[i]
        if c == "'":
            q = not q
        elif not q and c == "E" and txt.startswith("ENDSEC", i):
            j = txt.find(";", i)
            return (j + 1) if j >= 0 else i + 6
        i += 1
    return 0


def load(path, max_bytes=MAX_BYTES):
    """-> Model. Entities are kept as raw text and parsed on demand (only the ones geometry touches)."""
    txt = read_text(path, max_bytes)
    if "ISO-10303-21" not in txt[:2000]:
        raise StepError("not a STEP Part 21 file (no ISO-10303-21 header)")
    m = _DATA.search(txt, _header_end(txt))
    if not m:
        raise StepError("not a STEP Part 21 file (no DATA section)")
    txt = txt[m.end():]
    if "/*" in txt:
        txt = _strip_comments(txt)
    raw = {}
    for part in _split(txt):
        part = part.lstrip()
        mm = _ENT.match(part)
        if mm:
            raw[int(mm.group(1))] = part
    if not raw:
        raise StepError("the STEP file has no entities")
    return Model(raw)


def _strip_comments(txt):
    if "'" not in txt:
        return _CMT.sub("", txt)
    out, q = [], False
    for chunk in _Q.split(txt):
        if chunk == "'":
            q = not q
            out.append(chunk)
        elif q or "/*" not in chunk:
            out.append(chunk)
        else:
            out.append(_CMT.sub("", chunk))
    return "".join(out)


def _split(txt):
    """Split on ';' that is not inside a 'string'."""
    if "'" not in txt:
        return txt.split(";")
    out, buf, q = [], [], False
    for chunk in _Q.split(txt):
        if chunk == "'":
            q = not q
            buf.append(chunk)
        elif q:
            buf.append(chunk)
        else:
            parts = chunk.split(";")
            for p in parts[:-1]:
                buf.append(p)
                out.append("".join(buf))
                buf = []
            buf.append(parts[-1])
    return out


class Model:
    def __init__(self, raw):
        self.raw = raw
        self.cache = {}
        self.types = {}
        for i, s in raw.items():
            m = _ENT.match(s)
            self.types.setdefault(m.group(2) or "&", []).append(i)

    def get(self, i):
        e = self.cache.get(i)
        if e is None:
            s = self.raw[i]
            m = _ENT.match(s)
            args, _ = _parse_args(s, m.end())
            if m.group(2):
                e = (m.group(2), args)
            else:
                e = ("&", {a[0]: a[1] for a in args if isinstance(a, tuple)})
            self.cache[i] = e
        return e

    def of(self, *names):
        for n in names:
            for i in self.types.get(n, ()):
                yield i


# vector helpers
def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    l = math.sqrt(dot(a, a)) or 1.0
    return (a[0] / l, a[1] / l, a[2] / l)


class Geo:
    def __init__(self, m: Model):
        self.m = m
        self.scale = self._unit_scale()
        self.unresolved = 0          # assembly relationships that could not be turned into a transform

    def _unit_scale(self):
        m = self.m
        for i in m.types.get("&", ()):
            s = m.raw[i]
            if "LENGTH_UNIT" in s:
                u = s.upper().replace(" ", "")
                if "CONVERSION_BASED_UNIT" in u:
                    if "INCH" in u:
                        return 25.4
                    if "FOOT" in u:
                        return 304.8
                if "SI_UNIT" in u:
                    if ".MILLI." in u:
                        return 1.0
                    if ".CENTI." in u:
                        return 10.0
                    if ".MICRO." in u:
                        return 1e-3
                    if "$,.METRE." in u:
                        return 1000.0
        return 1.0

    def pt(self, i):
        c = self.m.get(i)[1][1]
        s = self.scale
        return (c[0] * s, c[1] * s, (c[2] if len(c) > 2 else 0.0) * s)

    def dir(self, i):
        c = self.m.get(i)[1][1]
        return norm((c[0], c[1], c[2] if len(c) > 2 else 0.0))

    def axis(self, i):
        """AXIS2_PLACEMENT_3D -> (origin, z, x)"""
        a = self.m.get(i)[1]
        o = self.pt(a[1])
        z = self.dir(a[2]) if a[2] is not None else (0.0, 0.0, 1.0)
        x = self.dir(a[3]) if len(a) > 3 and a[3] is not None else None
        if x is None:
            x = cross((0, 1, 0), z) if abs(z[1]) < 0.9 else cross((0, 0, 1), z)
        d = dot(x, z)
        x = norm((x[0] - d * z[0], x[1] - d * z[1], x[2] - d * z[2]))
        return o, z, x

    def curve(self, i):
        t, a = self.m.get(i)
        if t in ("SURFACE_CURVE", "SEAM_CURVE", "INTERSECTION_CURVE", "TRIMMED_CURVE"):
            return self.curve(a[1])
        if t == "LINE":
            return ("line",)
        if t == "CIRCLE":
            o, z, x = self.axis(a[1])
            return ("circle", o, z, x, a[2] * self.scale)
        if t == "ELLIPSE":
            o, z, x = self.axis(a[1])
            return ("ellipse", o, z, x, a[2] * self.scale, a[3] * self.scale)
        if t == "B_SPLINE_CURVE_WITH_KNOTS":
            deg = int(a[1])
            cps = [self.pt(r) for r in a[2]]
            kv = [k for k, mu in zip(a[7], a[6]) for _ in range(int(mu))]
            return ("bspline", deg, cps, kv, None)
        if t in ("BEZIER_CURVE", "B_SPLINE_CURVE", "QUASI_UNIFORM_CURVE"):
            return ("bspline", int(a[1]), [self.pt(r) for r in a[2]], None, None)
        if t == "POLYLINE":
            return ("poly", [self.pt(r) for r in a[1]])
        if t == "&" and "B_SPLINE_CURVE" in a:
            bc = a["B_SPLINE_CURVE"]
            deg = int(bc[0])
            cps = [self.pt(r) for r in bc[1]]
            kk = a.get("B_SPLINE_CURVE_WITH_KNOTS")
            kv = [k for k, mu in zip(kk[1], kk[0]) for _ in range(int(mu))] if kk else None
            w = a.get("RATIONAL_B_SPLINE_CURVE")
            return ("bspline", deg, cps, kv, w[0] if w else None)
        return ("unknown", t)


# ── assembly placement ────────────────────────────────────────────────────
IDENT = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0))
_SOLIDS = ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS", "SHELL_BASED_SURFACE_MODEL", "FACETED_BREP")


def mat_mul(a, b):
    """3x4 rigid matrices (rows), a after b."""
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) + (a[i][3] if j == 3 else 0.0) for j in range(4))
                 for i in range(3))


def mat_inv(a):
    r = tuple(tuple(a[j][i] for j in range(3)) for i in range(3))          # transpose of the rotation
    t = tuple(-sum(r[i][k] * a[k][3] for k in range(3)) for i in range(3))
    return tuple(r[i] + (t[i],) for i in range(3))


def mat_pt(a, p):
    return tuple(a[i][0] * p[0] + a[i][1] * p[1] + a[i][2] * p[2] + a[i][3] for i in range(3))


def mat_dir(a, d):
    return tuple(a[i][0] * d[0] + a[i][1] * d[1] + a[i][2] * d[2] for i in range(3))


def _axis_mat(geo, i):
    o, z, x = geo.axis(i)
    y = cross(z, x)
    return ((x[0], y[0], z[0], o[0]), (x[1], y[1], z[1], o[1]), (x[2], y[2], z[2], o[2]))


def solid_transforms(m, geo):
    """solid id -> 3x4 world matrix (missing = identity). Walks the representation relationships once.
    geo.unresolved counts relationships whose transformation could not be read."""
    rep_items, item_rep = {}, {}
    for t, ids in m.types.items():
        if not t.endswith("REPRESENTATION") or t == "CONTEXT_DEPENDENT_SHAPE_REPRESENTATION":
            continue
        for rid in ids:
            try:
                a = m.get(rid)[1]
                items = a[1] if isinstance(a, list) and len(a) > 1 and isinstance(a[1], list) else None
            except Exception:  # noqa: BLE001
                items = None
            if items is None:
                continue
            refs = [int(x) for x in items if isinstance(x, Ref)]
            rep_items[rid] = set(refs)
            for r in refs:
                item_rep.setdefault(r, rid)
    solid_ids = set(m.of(*_SOLIDS))
    parent = {}          # child rep -> [(parent rep, matrix child->parent)]

    def link(child, par, mat):
        if child != par:
            parent.setdefault(child, []).append((par, mat))
    for rid in m.types.get("SHAPE_REPRESENTATION_RELATIONSHIP", ()):
        a = m.get(rid)[1]
        if isinstance(a[2], Ref) and isinstance(a[3], Ref):
            # the brep representation and the product's shape representation share one frame
            r1, r2 = int(a[2]), int(a[3])
            child, par = (r1, r2) if rep_items.get(r1, set()) & solid_ids else (r2, r1)
            link(child, par, IDENT)
    for rid in m.types.get("&", ()):
        if "REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION" not in m.raw[rid]:
            continue
        try:
            a = m.get(rid)[1]
            rr = a["REPRESENTATION_RELATIONSHIP"]
            r1, r2 = int(rr[2]), int(rr[3])
            tr = a["REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION"][0]
            tt, ta = m.get(tr)
            if tt != "ITEM_DEFINED_TRANSFORMATION":
                raise ValueError(tt)
            a1, a2 = int(ta[2]), int(ta[3])
            m1, m2 = _axis_mat(geo, a1), _axis_mat(geo, a2)
            i1, i2 = rep_items.get(r1, set()), rep_items.get(r2, set())
            if a1 in i2 and a2 in i1 and not (a1 in i1 and a2 in i2):
                m1, m2 = m2, m1
            link(r1, r2, mat_mul(m2, mat_inv(m1)))
        except Exception:  # noqa: BLE001
            geo.unresolved += 1
    for mid in m.types.get("MAPPED_ITEM", ()):
        try:
            a = m.get(mid)[1]
            rm = m.get(a[1])[1]                      # REPRESENTATION_MAP(origin, mapped_rep)
            par = item_rep.get(mid)
            if par is None:
                continue
            link(int(rm[1]), par, mat_mul(_axis_mat(geo, int(a[2])), mat_inv(_axis_mat(geo, int(rm[0])))))
        except Exception:  # noqa: BLE001
            geo.unresolved += 1
    cache = {}

    def world(rep, depth=0):
        if rep in cache:
            return cache[rep]
        cache[rep] = IDENT
        ps = parent.get(rep)
        if ps and depth < 40:
            par, mat = ps[0]
            cache[rep] = mat_mul(world(par, depth + 1), mat)
        return cache[rep]
    out = {}
    for sid in solid_ids:
        rep = item_rep.get(sid)
        if rep is not None:
            w = world(rep)
            if any(abs(w[i][j] - IDENT[i][j]) > 1e-9 for i in range(3) for j in range(4)):
                out[sid] = w
    return out


def bspline_points(deg, cps, kv, w, n=None):
    """de Boor sampling over the full knot range."""
    nc = len(cps)
    if kv is None:  # clamped uniform
        kv = [0] * (deg + 1) + list(range(1, nc - deg)) + [max(1, nc - deg)] * (deg + 1)
    n = n or max(8, 6 * nc)
    lo, hi = kv[deg], kv[nc]
    out = []
    P = [(c[0] * (w[i] if w else 1.0), c[1] * (w[i] if w else 1.0), c[2] * (w[i] if w else 1.0), (w[i] if w else 1.0))
         for i, c in enumerate(cps)]
    k = deg
    for s in range(n + 1):
        u = lo + (hi - lo) * s / n
        while k < nc - 1 and kv[k + 1] <= u:
            k += 1
        d = [P[j + k - deg] for j in range(deg + 1)]
        for r in range(1, deg + 1):
            for j in range(deg, r - 1, -1):
                den = kv[j + 1 + k - r] - kv[j + k - deg]
                al = (u - kv[j + k - deg]) / den if den else 0.0
                a_, b_ = d[j - 1], d[j]
                d[j] = ((1 - al) * a_[0] + al * b_[0], (1 - al) * a_[1] + al * b_[1],
                        (1 - al) * a_[2] + al * b_[2], (1 - al) * a_[3] + al * b_[3])
        x = d[deg]
        out.append((x[0] / x[3], x[1] / x[3], x[2] / x[3]))
    return out


class Face:
    __slots__ = ("id", "origin", "n", "u", "v", "loops", "area", "bbox", "unknown", "solid", "d", "eids", "gross")


def planar_faces(m: Model, g: Geo, xf=None):
    """xf: {face id: 3x4 matrix} world placement of the face's solid (assemblies)."""
    """-> [Face]; loop = (is_outer, [prims]) in face 2D coords (u,v):
       ('L',p0,p1) ('A',c,r,p0,p1,ccw) ('C',c,r) ('P',[pts])"""
    faces = []
    for fid in m.of("ADVANCED_FACE", "FACE_SURFACE"):
        a = m.get(fid)[1]
        st, sa = m.get(a[2])
        if st != "PLANE":
            continue
        o, z, x = g.axis(sa[1])
        if a[3] == ".F.":
            z = (-z[0], -z[1], -z[2])
        y = cross(z, x)
        f = Face()
        f.id, f.origin, f.n, f.u, f.v, f.loops, f.unknown = fid, o, z, x, y, [], 0
        f.eids = []
        f.d = dot(o, z)

        def p2(p, o=o, x=x, y=y):
            d = (p[0] - o[0], p[1] - o[1], p[2] - o[2])
            return (dot(d, x), dot(d, y))
        for bid in a[1]:
            bt, ba = m.get(bid)
            lt, la = m.get(ba[1])
            if lt != "EDGE_LOOP":
                continue
            prims = []
            eids = []
            for oe in la[1]:
                oa = m.get(oe)[1]
                ea = m.get(oa[3])[1]
                fwd = (oa[4] == ".T.")
                eids.append(int(oa[3]))
                p_a = g.pt(m.get(ea[1])[1][1])
                p_b = g.pt(m.get(ea[2])[1][1])
                c = g.curve(ea[3])
                same = (ea[4] == ".T.")
                s, e = (p_a, p_b) if fwd else (p_b, p_a)
                if c[0] == "line":
                    prims.append(("L", p2(s), p2(e)))
                elif c[0] == "circle":
                    _, co, cz, cx, r = c
                    ccw3 = (same == fwd)                     # traversal CCW about the circle axis
                    ccw = ccw3 if dot(cz, z) > 0 else not ccw3
                    if ea[1] == ea[2] or math.dist(s, e) < 1e-7:
                        prims.append(("C", p2(co), r))
                    else:
                        prims.append(("A", p2(co), r, p2(s), p2(e), ccw))
                elif c[0] == "bspline":
                    pts = bspline_points(c[1], c[2], c[3], c[4])
                    if same != fwd:
                        pts.reverse()
                    # trim to the edge vertices when the curve is longer than the edge
                    prims.append(("P", [p2(q) for q in _trim(pts, s, e)]))
                elif c[0] == "poly":
                    pts = list(c[1])
                    if same != fwd:
                        pts.reverse()
                    prims.append(("P", [p2(q) for q in pts]))
                elif c[0] == "ellipse":
                    _, co, cz, cx, r1, r2 = c
                    cy = cross(cz, cx)

                    def ang(p):
                        d = sub(p, co)
                        return math.atan2(dot(d, cy) / r2, dot(d, cx) / r1)
                    a0, a1 = (ang(p_a), ang(p_b)) if same else (ang(p_b), ang(p_a))
                    if a1 <= a0 + 1e-12:
                        a1 += 2 * math.pi
                    pts = []
                    for k in range(33):
                        tt = a0 + (a1 - a0) * k / 32
                        ct, st_ = math.cos(tt), math.sin(tt)
                        pts.append((co[0] + r1 * ct * cx[0] + r2 * st_ * cy[0], co[1] + r1 * ct * cx[1] + r2 * st_ * cy[1],
                                    co[2] + r1 * ct * cx[2] + r2 * st_ * cy[2]))
                    if same != fwd:
                        pts.reverse()
                    prims.append(("P", [p2(q) for q in pts]))
                else:
                    f.unknown += 1
                    prims.append(("L", p2(s), p2(e)))
            if ba[2] == ".F.":
                prims = [rev(p) for p in reversed(prims)]
                eids.reverse()
            f.loops.append((bt == "FACE_OUTER_BOUND", prims))
            f.eids.append(eids)
        if not f.loops:
            continue
        ars = [loop_area(pr) for _, pr in f.loops]
        k = max(range(len(ars)), key=lambda i: abs(ars[i]))
        f.loops = [f.loops[k]] + [l for i, l in enumerate(f.loops) if i != k]
        f.eids = [f.eids[k]] + [l for i, l in enumerate(f.eids) if i != k]
        f.gross = abs(ars[k])
        f.area = abs(ars[k]) - sum(abs(v) for i, v in enumerate(ars) if i != k)
        pts = [q for pr in f.loops[0][1] for q in flat(pr)]
        f.bbox = (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))
        w = xf.get(fid) if xf else None
        if w is not None:
            f.origin, f.n, f.u, f.v = mat_pt(w, f.origin), norm(mat_dir(w, f.n)), norm(mat_dir(w, f.u)), norm(mat_dir(w, f.v))
            f.d = dot(f.origin, f.n)
        faces.append(f)
    return faces


def _trim(pts, s, e):
    i0 = min(range(len(pts)), key=lambda i: math.dist(pts[i], s))
    i1 = min(range(len(pts)), key=lambda i: math.dist(pts[i], e))
    if i0 < i1 and (i0 > 0 or i1 < len(pts) - 1):
        return [s] + pts[i0 + 1:i1] + [e]
    return pts


def rev(p):
    if p[0] == "L":
        return ("L", p[2], p[1])
    if p[0] == "A":
        return ("A", p[1], p[2], p[4], p[3], not p[5])
    if p[0] == "P":
        return ("P", p[1][::-1])
    return p


def arc_pts(c, r, s, e, ccw, seg=None):
    a0 = math.atan2(s[1] - c[1], s[0] - c[0])
    a1 = math.atan2(e[1] - c[1], e[0] - c[0])
    if ccw and a1 <= a0:
        a1 += 2 * math.pi
    if not ccw and a1 >= a0:
        a1 -= 2 * math.pi
    n = seg or max(2, int(abs(a1 - a0) / (math.pi / 32)) + 1)
    return [(c[0] + r * math.cos(a0 + (a1 - a0) * k / n), c[1] + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(n + 1)]


def flat(p):
    if p[0] == "L":
        return [p[1], p[2]]
    if p[0] == "A":
        return arc_pts(p[1], p[2], p[3], p[4], p[5])
    if p[0] == "C":
        return [(p[1][0] + p[2] * math.cos(2 * math.pi * k / 64), p[1][1] + p[2] * math.sin(2 * math.pi * k / 64)) for k in range(65)]
    return list(p[1])


def loop_area(prims):
    pts = [q for p in prims for q in flat(p)]
    n = len(pts)
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))


def solids_of(m):
    """face id -> solid id"""
    out = {}
    for sid in m.of("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS"):
        a = m.get(sid)[1]
        shells = [a[1]] + (list(a[2]) if len(a) > 2 and isinstance(a[2], list) else [])
        for sh in shells:
            st, sa = m.get(sh)
            if st == "ORIENTED_CLOSED_SHELL":
                st, sa = m.get(sa[2])
            for f in sa[1]:
                out[f] = sid
    for sid in m.of("SHELL_BASED_SURFACE_MODEL"):
        for sh in m.get(sid)[1][1]:
            for f in m.get(sh)[1][1]:
                out.setdefault(f, sid)
    return out


def candidates(path, max_bytes=MAX_BYTES):
    """-> (model, geo, planar faces with .solid set, in world coordinates)."""
    m = load(path, max_bytes)
    g = Geo(m)
    sol = solids_of(m)
    st = solid_transforms(m, g)
    xf = {f: st[s] for f, s in sol.items() if s in st} if st else None
    faces = planar_faces(m, g, xf)
    for f in faces:
        f.solid = sol.get(f.id)
    return m, g, faces


def vertex_bbox(path, max_bytes=MAX_BYTES):
    """World bounding box of every CARTESIAN_POINT used by a VERTEX_POINT: (min xyz, max xyz) in mm."""
    m = load(path, max_bytes)
    g = Geo(m)
    lo, hi = [1e18] * 3, [-1e18] * 3
    for vid in m.of("VERTEX_POINT"):
        p = g.pt(m.get(vid)[1][1])
        for i in range(3):
            lo[i] = min(lo[i], p[i])
            hi[i] = max(hi[i], p[i])
    return (tuple(lo), tuple(hi)) if lo[0] < 1e17 else None
