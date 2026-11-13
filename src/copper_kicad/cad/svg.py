"""SVG -> outline prims (pure stdlib).

Elements: path (M L H V C S Q T A Z), rect (rx), circle, ellipse, line, polyline, polygon, nested
``transform``. Circular path arcs under a uniform-scale transform become true arcs; Beziers and
elliptical arcs are flattened adaptively (0.005 mm) and re-fitted by ``ir.simplify``.
Scale: ``width`` with a unit plus ``viewBox`` give mm per user unit; otherwise 96 dpi.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from . import ir

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_TOK = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|(" + _NUM + ")")
_UNIT = {"mm": 1.0, "cm": 10.0, "in": 25.4, "px": 25.4 / 96, "": 25.4 / 96, "pt": 25.4 / 72, "pc": 25.4 / 6}
FLAT = 0.005


def _mul(a, b):
    return (a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
            a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5])


def _parse_tf(s):
    mtx = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        v = [float(x) for x in re.findall(_NUM, args)]
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            a = math.radians(v[0])
            c, s_ = math.cos(a), math.sin(a)
            t = (c, s_, -s_, c, 0, 0)
            if len(v) == 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        else:
            continue
        mtx = _mul(mtx, t)
    return mtx


def _uniform(m):
    """Similarity transform (rotation + uniform scale, maybe mirrored)?"""
    sx, sy = math.hypot(m[0], m[1]), math.hypot(m[2], m[3])
    return abs(sx - sy) <= 1e-9 * max(sx, sy, 1e-12) and abs(m[0] * m[2] + m[1] * m[3]) <= 1e-9 * max(sx * sy, 1e-12)


def _bez(p0, p1, p2, p3, tol, out, depth=0):
    """Adaptive cubic flattening: appends points after p0 up to and including p3."""
    dx, dy = p3[0] - p0[0], p3[1] - p0[1]
    l = math.hypot(dx, dy) or 1e-12
    d1 = abs((p1[0] - p0[0]) * dy - (p1[1] - p0[1]) * dx) / l
    d2 = abs((p2[0] - p0[0]) * dy - (p2[1] - p0[1]) * dx) / l
    if depth >= 14 or (max(d1, d2) <= tol and depth >= 2):
        out.append(p3)
        return
    m01 = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
    m12 = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    m23 = ((p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2)
    a = ((m01[0] + m12[0]) / 2, (m01[1] + m12[1]) / 2)
    b = ((m12[0] + m23[0]) / 2, (m12[1] + m23[1]) / 2)
    c = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    _bez(p0, m01, a, c, tol, out, depth + 1)
    _bez(c, b, m23, p3, tol, out, depth + 1)


def _arc_centre(x1, y1, rx, ry, phi, fa, fs, x2, y2):
    """SVG endpoint -> centre parameterisation: (cx, cy, rx, ry, theta1, dtheta)"""
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(num / den, 0.0)) if den else 0.0
    if fa == fs:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx, cy = cp * cxp - sp * cyp + (x1 + x2) / 2, sp * cxp + cp * cyp + (y1 + y2) / 2
    t1 = math.atan2((y1p - cyp) / ry, (x1p - cxp) / rx)
    t2 = math.atan2((-y1p - cyp) / ry, (-x1p - cxp) / rx)
    dt = t2 - t1
    if fs and dt < 0:
        dt += 2 * math.pi
    elif not fs and dt > 0:
        dt -= 2 * math.pi
    return cx, cy, rx, ry, t1, dt


def path_prims(d, ap, uniform, tol_user):
    """Path data -> prims in output coordinates. ap = point transform; tol_user = flatness in user units."""
    toks = _TOK.findall(d or "")
    i, cmd = 0, None
    cx = cy = sx = sy = 0.0
    last_c = last_q = None
    prims = []

    def num():
        nonlocal i
        while i < len(toks) and not toks[i][1]:
            i += 1
        v = float(toks[i][1])
        i += 1
        return v

    def line(a, b):
        if math.dist(a, b) > 1e-12:
            prims.append(("L", ap(a), ap(b)))

    def poly(pts):
        for k in range(len(pts) - 1):
            line(pts[k], pts[k + 1])
    try:
        while i < len(toks):
            if toks[i][0]:
                cmd = toks[i][0]
                i += 1
                if cmd in "Zz":
                    line((cx, cy), (sx, sy))
                    cx, cy = sx, sy
                    last_c = last_q = None
                    continue
            if cmd is None:
                i += 1
                continue
            rel = cmd.islower()
            c = cmd.upper()
            if c == "M":
                x, y = num(), num()
                if rel:
                    x, y = x + cx, y + cy
                cx, cy, sx, sy = x, y, x, y
                cmd = "l" if rel else "L"
                last_c = last_q = None
            elif c == "L":
                x, y = num(), num()
                if rel:
                    x, y = x + cx, y + cy
                line((cx, cy), (x, y))
                cx, cy = x, y
                last_c = last_q = None
            elif c == "H":
                x = num() + (cx if rel else 0)
                line((cx, cy), (x, cy))
                cx = x
                last_c = last_q = None
            elif c == "V":
                y = num() + (cy if rel else 0)
                line((cx, cy), (cx, y))
                cy = y
                last_c = last_q = None
            elif c in "CS":
                if c == "C":
                    x1, y1 = num(), num()
                    if rel:
                        x1, y1 = x1 + cx, y1 + cy
                else:
                    x1, y1 = (2 * cx - last_c[0], 2 * cy - last_c[1]) if last_c else (cx, cy)
                x2, y2, x, y = num(), num(), num(), num()
                if rel:
                    x2, y2, x, y = x2 + cx, y2 + cy, x + cx, y + cy
                pts = [(cx, cy)]
                _bez((cx, cy), (x1, y1), (x2, y2), (x, y), tol_user, pts)
                poly(pts)
                last_c, last_q = (x2, y2), None
                cx, cy = x, y
            elif c in "QT":
                if c == "Q":
                    x1, y1 = num(), num()
                    if rel:
                        x1, y1 = x1 + cx, y1 + cy
                else:
                    x1, y1 = (2 * cx - last_q[0], 2 * cy - last_q[1]) if last_q else (cx, cy)
                x, y = num(), num()
                if rel:
                    x, y = x + cx, y + cy
                c1 = (cx + 2 / 3 * (x1 - cx), cy + 2 / 3 * (y1 - cy))
                c2 = (x + 2 / 3 * (x1 - x), y + 2 / 3 * (y1 - y))
                pts = [(cx, cy)]
                _bez((cx, cy), c1, c2, (x, y), tol_user, pts)
                poly(pts)
                last_q, last_c = (x1, y1), None
                cx, cy = x, y
            elif c == "A":
                rx, ry, rot, fa, fs, x, y = num(), num(), num(), num(), num(), num(), num()
                if rel:
                    x, y = x + cx, y + cy
                if rx == 0 or ry == 0 or math.dist((cx, cy), (x, y)) < 1e-12:
                    line((cx, cy), (x, y))
                else:
                    ox, oy, rx, ry, t1, dt = _arc_centre(cx, cy, rx, ry, math.radians(rot), bool(fa), bool(fs), x, y)
                    if abs(rx - ry) <= 1e-9 * max(rx, ry) and uniform:
                        tm = t1 + dt / 2
                        prims.append(("A", ap((cx, cy)), ap((ox + rx * math.cos(tm), oy + rx * math.sin(tm))), ap((x, y))))
                    else:
                        cr, sr = math.cos(math.radians(rot)), math.sin(math.radians(rot))
                        n = max(8, int(abs(dt) / (2 * math.acos(max(0.0, 1 - tol_user / max(rx, ry))) or 0.05)) + 1)
                        n = min(n, 720)
                        pts = [(cx, cy)]
                        for k in range(1, n):
                            t = t1 + dt * k / n
                            ex, ey = rx * math.cos(t), ry * math.sin(t)
                            pts.append((ox + cr * ex - sr * ey, oy + sr * ex + cr * ey))
                        pts.append((x, y))
                        poly(pts)
                cx, cy = x, y
                last_c = last_q = None
            else:
                i += 1
    except IndexError:
        pass
    return prims


def read(path, layer=None, units=None):
    """-> {'prims', 'scale' (mm per user unit), 'units_assumed', 'ids': [...]}.
    layer: only the element (group) with this id or inkscape:label, and its children."""
    root = ET.parse(path).getroot()
    vb = [float(v) for v in re.findall(_NUM, root.get("viewBox", ""))] or None
    mu = re.match(r"\s*(" + _NUM + r")\s*([a-z%]*)", root.get("width", ""))
    sc, assumed = 25.4 / 96, True
    if mu and vb and len(vb) == 4 and vb[2] and mu.group(2) in _UNIT:
        sc = float(mu.group(1)) * _UNIT[mu.group(2)] / vb[2]
        assumed = mu.group(2) in ("", "px")
    elif mu and mu.group(2) in ("mm", "cm", "in") and not vb:
        sc, assumed = _UNIT[mu.group(2)], False        # no viewBox: user units are the width's unit
    if units in ("mm", "inch", "cm", "m"):
        sc, assumed = {"mm": 1.0, "inch": 25.4, "cm": 10.0, "m": 1000.0}[units], False
    prims, ids, approx = [], [], [False]

    def fnum(el, k, d=0.0):
        m = re.match(r"\s*(" + _NUM + ")", el.get(k, "") or "")
        return float(m.group(1)) if m else d

    def walk(el, mtx, active):
        tag = el.tag.split("}")[-1] if isinstance(el.tag, str) else ""
        if tag in ("defs", "clipPath", "mask", "symbol", "pattern", "marker", "text", "metadata", "title", "desc"):
            return
        mtx = _mul(mtx, _parse_tf(el.get("transform")))
        eid = el.get("id")
        label = next((v for k, v in el.attrib.items() if k.endswith("}label")), None)
        if tag == "g" and (eid or label):
            ids.append(label or eid)
        if layer is not None and not active:
            active = layer in (eid, label)
        uni = _uniform(mtx)
        det = abs(mtx[0] * mtx[3] - mtx[1] * mtx[2]) ** 0.5 or 1.0
        tol_user = FLAT / (det * sc)

        def ap(p):
            return ((mtx[0] * p[0] + mtx[2] * p[1] + mtx[4]) * sc, (mtx[1] * p[0] + mtx[3] * p[1] + mtx[5]) * sc)
        if active or layer is None:
            if tag == "path":
                d = el.get("d", "")
                if re.search(r"[CcSsQqTt]", d):
                    approx[0] = True
                prims.extend(path_prims(d, ap, uni, tol_user))
            elif tag == "circle" or (tag == "ellipse" and abs(fnum(el, "rx") - fnum(el, "ry")) < 1e-12):
                r = fnum(el, "r") if tag == "circle" else fnum(el, "rx")
                c = (fnum(el, "cx"), fnum(el, "cy"))
                if r > 0 and uni:
                    prims.append(("C", ap(c), math.dist(ap(c), ap((c[0] + r, c[1])))))
                elif r > 0:
                    pts = [ap((c[0] + r * math.cos(2 * math.pi * k / 180), c[1] + r * math.sin(2 * math.pi * k / 180))) for k in range(181)]
                    prims.extend(("L", pts[k], pts[k + 1]) for k in range(180))
            elif tag == "ellipse":
                c, rx, ry = (fnum(el, "cx"), fnum(el, "cy")), fnum(el, "rx"), fnum(el, "ry")
                if rx > 0 and ry > 0:
                    pts = [ap((c[0] + rx * math.cos(2 * math.pi * k / 180), c[1] + ry * math.sin(2 * math.pi * k / 180))) for k in range(181)]
                    prims.extend(("L", pts[k], pts[k + 1]) for k in range(180))
                    approx[0] = True
            elif tag == "line":
                prims.append(("L", ap((fnum(el, "x1"), fnum(el, "y1"))), ap((fnum(el, "x2"), fnum(el, "y2")))))
            elif tag in ("polyline", "polygon"):
                v = [float(x) for x in re.findall(_NUM, el.get("points", ""))]
                pts = [ap((v[k], v[k + 1])) for k in range(0, len(v) - 1, 2)]
                if tag == "polygon" and pts:
                    pts.append(pts[0])
                prims.extend(("L", pts[k], pts[k + 1]) for k in range(len(pts) - 1) if math.dist(pts[k], pts[k + 1]) > 1e-12)
            elif tag == "rect":
                x, y, w_, h_ = fnum(el, "x"), fnum(el, "y"), fnum(el, "width"), fnum(el, "height")
                rx, ry = el.get("rx"), el.get("ry")
                r = fnum(el, "rx") if rx is not None else (fnum(el, "ry") if ry is not None else 0.0)
                r = min(r, w_ / 2, h_ / 2)
                if w_ > 0 and h_ > 0:
                    if r <= 0 or not uni:
                        c = [ap(p) for p in ((x, y), (x + w_, y), (x + w_, y + h_), (x, y + h_))]
                        prims.extend(("L", c[k], c[(k + 1) % 4]) for k in range(4))
                    else:
                        k = r * (1 - math.sqrt(0.5))
                        L = lambda a, b: prims.append(("L", ap(a), ap(b))) if math.dist(a, b) > 1e-12 else None
                        A = lambda a, m_, b: prims.append(("A", ap(a), ap(m_), ap(b)))
                        L((x + r, y), (x + w_ - r, y))
                        A((x + w_ - r, y), (x + w_ - k, y + k), (x + w_, y + r))
                        L((x + w_, y + r), (x + w_, y + h_ - r))
                        A((x + w_, y + h_ - r), (x + w_ - k, y + h_ - k), (x + w_ - r, y + h_))
                        L((x + w_ - r, y + h_), (x + r, y + h_))
                        A((x + r, y + h_), (x + k, y + h_ - k), (x, y + h_ - r))
                        L((x, y + h_ - r), (x, y + r))
                        A((x, y + r), (x + k, y + k), (x + r, y))
        for ch in el:
            walk(ch, mtx, active)
    walk(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), False)
    if layer is not None and not prims:
        raise ValueError("no element with id or label '%s' holds outline geometry; groups in the file: %s"
                         % (layer, ", ".join(ids[:20]) or "none"))
    return {"prims": prims, "scale": sc, "units_assumed": assumed, "ids": ids[:30], "approx": approx[0]}


def dedupe(prims, tol=1e-4):
    """Drop exact duplicate prims (a plotted outline is often drawn twice: fill + stroke)."""
    seen, out = set(), []
    for p in prims:
        if p[0] == "C":
            k = ("C", round(p[1][0] / tol), round(p[1][1] / tol), round(p[2] / tol))
        else:
            e = sorted([(round(p[1][0] / tol), round(p[1][1] / tol)), (round(p[-1][0] / tol), round(p[-1][1] / tol))])
            mid = (round(p[2][0] / tol), round(p[2][1] / tol)) if p[0] == "A" else None
            k = (p[0], e[0], e[1], mid)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out
