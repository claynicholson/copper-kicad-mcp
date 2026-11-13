"""2D outline intermediate representation shared by every CAD reader (pure stdlib).

All values are millimetres in KiCad axes (x right, y DOWN), seen from the component side::

    prim = ("L", p0, p1) | ("A", start, mid, end) | ("C", centre, r)
    loop = [prim, ...]            closed; a full circle is the one-prim loop [("C", c, r)]

Public: chain, simplify, classify, transform, loop_pts, poly_area, pip, circle3, arc_from,
decimate, loop_bbox, prims_to_json / prims_from_json.
"""
from __future__ import annotations

import math

TOL = 0.01

# ───────────────────────── geometry helpers ─────────────────────────
def circle3(a, b, c):
    ax, ay = a; bx, by = b; cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def prim_ends(p):
    if p[0] == "L":
        return p[1], p[2]
    if p[0] == "A":
        return p[1], p[3]
    return None, None


def prim_rev(p):
    if p[0] == "L":
        return ("L", p[2], p[1])
    if p[0] == "A":
        return ("A", p[3], p[2], p[1])
    return p


def prim_pts(p, da=math.pi / 48):
    if p[0] == "L":
        return [p[1], p[2]]
    if p[0] == "C":
        (cx, cy), r = p[1], p[2]
        return [(cx + r * math.cos(2 * math.pi * k / 96), cy + r * math.sin(2 * math.pi * k / 96)) for k in range(96)]
    s, m, e = p[1], p[2], p[3]
    cr = circle3(s, m, e)
    if not cr:
        return [s, e]
    (cx, cy), r = cr
    a0 = math.atan2(s[1] - cy, s[0] - cx); am = math.atan2(m[1] - cy, m[0] - cx); a1 = math.atan2(e[1] - cy, e[0] - cx)
    ccw = ((am - a0) % (2 * math.pi)) < ((a1 - a0) % (2 * math.pi))
    sweep = (a1 - a0) % (2 * math.pi) if ccw else -((a0 - a1) % (2 * math.pi))
    n = max(2, int(abs(sweep) / da) + 1)
    return [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n)) for k in range(n + 1)]


def loop_pts(loop):
    out = []
    for p in loop:
        q = prim_pts(p)
        out += q if p[0] == "C" else q[:-1]
    return out


def poly_area(pts):
    n = len(pts)
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))


def pip(pts, x, y):
    c = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            c = not c
    return c


def arc_from(c, r, a0, a1, ccw=True):
    """centre/radius/angles (rad, math CCW in the prim's own coordinate system) -> ('A', s, m, e)"""
    sweep = (a1 - a0) % (2 * math.pi) if ccw else -((a0 - a1) % (2 * math.pi))
    if abs(sweep) < 1e-9:
        sweep = 2 * math.pi if ccw else -2 * math.pi
    f = lambda t: (c[0] + r * math.cos(t), c[1] + r * math.sin(t))
    return ("A", f(a0), f(a0 + sweep / 2), f(a0 + sweep))


# ───────────────────────── chaining ─────────────────────────
def chain(prims, tol=TOL):
    """Unordered prims -> (closed loops, open chains). Circles are loops by themselves."""
    loops, opens = [], []
    segs = []
    for p in prims:
        if p[0] == "C":
            loops.append([p])
        else:
            s, e = prim_ends(p)
            if math.dist(s, e) > 1e-9 or p[0] == "A":
                segs.append(p)
    q = 1.0 / (tol * 2)
    grid = {}

    def keys(pt):
        ix, iy = int(math.floor(pt[0] * q)), int(math.floor(pt[1] * q))
        return [(ix + dx, iy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
    for i, p in enumerate(segs):
        for end, pt in enumerate(prim_ends(p)):
            grid.setdefault((int(math.floor(pt[0] * q)), int(math.floor(pt[1] * q))), []).append((i, end))
    used = [False] * len(segs)

    def find(pt, skip):
        best = None
        for k in keys(pt):
            for i, end in grid.get(k, ()):
                if used[i] or i == skip:
                    continue
                d = math.dist(prim_ends(segs[i])[end], pt)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, i, end)
        return best
    for i0 in range(len(segs)):
        if used[i0]:
            continue
        used[i0] = True
        ch = [segs[i0]]
        while True:   # extend forward
            tail = prim_ends(ch[-1])[1]
            if len(ch) > 1 and math.dist(tail, prim_ends(ch[0])[0]) <= tol:
                break
            b = find(tail, -1)
            if not b:
                break
            used[b[1]] = True
            ch.append(segs[b[1]] if b[2] == 0 else prim_rev(segs[b[1]]))
        closed = math.dist(prim_ends(ch[-1])[1], prim_ends(ch[0])[0]) <= tol and (len(ch) > 1 or ch[0][0] == "A")
        if not closed:
            while True:   # extend backward
                head = prim_ends(ch[0])[0]
                b = find(head, -1)
                if not b:
                    break
                used[b[1]] = True
                ch.insert(0, segs[b[1]] if b[2] == 1 else prim_rev(segs[b[1]]))
                if math.dist(prim_ends(ch[-1])[1], prim_ends(ch[0])[0]) <= tol:
                    closed = True
                    break
        (loops if closed else opens).append(ch)
    return loops, opens


# ───────────────────────── arc recovery ─────────────────────────
def simplify(loop, tol=0.01, min_arc_pts=4):
    """Merge collinear segments and turn runs of short segments lying on one circle back into arcs.
    Only 'L' runs are touched; existing arcs/circles are kept."""
    if len(loop) == 1 and loop[0][0] == "C":
        return loop
    if any(p[0] != "L" for p in loop) and sum(1 for p in loop if p[0] == "L") < 6:
        return merge_arcs(_merge_collinear(loop))
    # split the loop into maximal L-runs
    n = len(loop)
    start = next((i for i in range(n) if loop[i][0] != "L"), None)
    if start is None:
        pts = [p[1] for p in loop]
        # whole loop a circle?
        cr = _fit(pts + [pts[0]], tol)
        if cr and len(pts) >= 8 and _spans_full(pts, cr[0]):
            return [("C", cr[0], cr[1])]
        # rotate so that no arc run wraps around the start: begin at the longest straight edge when
        # there is a clear one, else at the sharpest corner
        m = len(pts)
        lens = [math.dist(pts[i], pts[(i + 1) % m]) for i in range(m)]
        k = max(range(m), key=lambda i: lens[i])
        if lens[k] < 2.0 * sorted(lens)[m // 2]:
            k = max(range(m), key=lambda i: abs(_turn(pts[i - 1], pts[i], pts[(i + 1) % m])))
        pts = pts[k:] + pts[:k]
        return merge_arcs(_merge_collinear(_fit_run(pts + [pts[0]], tol, min_arc_pts)))
    out = []
    order = [loop[(start + i) % n] for i in range(n)]
    run = []
    for p in order + [None]:
        if p is not None and p[0] == "L":
            if not run:
                run = [p[1]]
            run.append(p[2])
        else:
            if run:
                out += _fit_run(run, tol, min_arc_pts)
                run = []
            if p is not None:
                out.append(p)
    return merge_arcs(_merge_collinear(out))


def _turn(a, b, c):
    return math.atan2((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]),
                      (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1]))


def _spans_full(pts, c):
    angs = sorted(math.atan2(p[1] - c[1], p[0] - c[0]) for p in pts)
    gaps = [angs[i + 1] - angs[i] for i in range(len(angs) - 1)] + [angs[0] + 2 * math.pi - angs[-1]]
    return max(gaps) < math.pi / 3


def _fit(pts, tol):
    """circle through first/mid/last that every point (and every chord midpoint, within sagitta) fits"""
    if len(pts) < 3:
        return None
    cr = circle3(pts[0], pts[len(pts) // 2], pts[-1]) if math.dist(pts[0], pts[-1]) > 1e-6 else \
        circle3(pts[0], pts[len(pts) // 3], pts[2 * len(pts) // 3])
    if not cr or cr[1] > 2000:
        return None
    c, r = cr
    for i, p in enumerate(pts):
        if abs(math.hypot(p[0] - c[0], p[1] - c[1]) - r) > tol:
            return None
        if i:
            ch = math.dist(pts[i - 1], p)
            if ch > 1.2 * r:                       # chord too long: not a tessellated arc (<= ~74 deg per chord)
                return None
            if r - math.sqrt(max(r * r - ch * ch / 4, 0)) > max(4 * tol, 0.05):   # sagitta: a real corner, not a chord
                return None
    return cr


def _fit_run(pts, tol, min_pts):
    out = []
    i, n = 0, len(pts)
    while i < n - 1:
        j = i + min_pts - 1
        best = None
        if j < n:
            sgn = None
            ok = True
            # constant turning direction
            while j < n:
                cr = _fit(pts[i:j + 1], tol)
                if not cr:
                    break
                t = [_turn(pts[k - 1], pts[k], pts[k + 1]) for k in range(i + 1, j)]
                if not (all(v > 1e-6 for v in t) or all(v < -1e-6 for v in t)):
                    break
                best = (j, cr)
                j += 1
        if best:
            j = best[0]
            seg = pts[i:j + 1]
            c, r = best[1]
            # midpoint ON the circle, on the side the run passes
            mid = seg[len(seg) // 2]
            if len(seg) % 2 == 0:
                a, b = seg[len(seg) // 2 - 1], seg[len(seg) // 2]
                mx, my = (a[0] + b[0]) / 2 - c[0], (a[1] + b[1]) / 2 - c[1]
                l = math.hypot(mx, my) or 1
                mid = (c[0] + mx / l * r, c[1] + my / l * r)
            out.append(("A", seg[0], mid, seg[-1]))
            i = j
        else:
            out.append(("L", pts[i], pts[i + 1]))
            i += 1
    return out


def _merge_collinear(loop, tol=1e-4):
    out = []
    for p in loop:
        if out and p[0] == "L" and out[-1][0] == "L":
            a, b, c = out[-1][1], out[-1][2], p[2]
            cr = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            dt = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
            if abs(cr) <= tol * max(math.dist(a, c), 1e-9) and dt > 0:
                out[-1] = ("L", a, c)
                continue
        out.append(p)
    if len(out) > 2 and out[0][0] == "L" and out[-1][0] == "L":
        a, b, c = out[-1][1], out[-1][2], out[0][2]
        cr = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cr) <= tol * max(math.dist(a, c), 1e-9):
            out[0] = ("L", a, c)
            out.pop()
    return out


# ───────────────────────── classification ─────────────────────────
MOUNT_D = {"M2": 2.2, "M2.5": 2.7, "M3": 3.2, "M4": 4.3}
STD_T = (0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0, 2.4, 3.2)


def hole_size(d):
    """Nearest stock mounting-hole size within 0.25 mm, else None."""
    k = min(MOUNT_D, key=lambda n: abs(MOUNT_D[n] - d))
    return k if abs(MOUNT_D[k] - d) <= 0.25 else None


def std_thickness(t):
    """Nearest standard PCB thickness, biased upward (KiCad exports the substrate only: 1.51 -> 1.6)."""
    return min(STD_T, key=lambda s: abs(s - t) + (0.05 if s < t - 0.02 else 0))


def loop_bbox(loop):
    pts = loop_pts(loop)
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


def _narrow(lp, pts, limit):
    """Smallest width of a loop (measured across its own straight edges) is below ``limit``."""
    if len(lp) == 1 and lp[0][0] == "C":
        return 2 * lp[0][2] < limit
    best = None
    for p in lp:
        if p[0] != "L":
            continue
        dx, dy = p[2][0] - p[1][0], p[2][1] - p[1][1]
        l = math.hypot(dx, dy)
        if l < 1e-6:
            continue
        nx, ny = -dy / l, dx / l
        v = [q[0] * nx + q[1] * ny for q in pts]
        w = max(v) - min(v)
        if best is None or w < best:
            best = w
    if best is None:
        b = [min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts)]
        best = min(b[2] - b[0], b[3] - b[1])
    return best < limit


def classify(loops, holes="mounting", min_hole=1.8, max_hole=6.6):
    """loops (KiCad axes) -> outline dict, or None when there is no loop.

    Outer = largest area. Loops inside it: circles in the mounting-hole range -> holes; smaller circles
    -> ignored drills; slots narrower than ``min_hole`` -> ignored (component slots of an exported board);
    everything else -> cutouts. holes='all' keeps drills and narrow slots as cutouts too; holes='none'
    drops mounting holes. Loops outside the outer one (title blocks, a second board) are counted, never
    imported."""
    info = []
    for lp in loops:
        pts = loop_pts(lp)
        if len(pts) >= 3:
            info.append((abs(poly_area(pts)), lp, pts))
    info.sort(key=lambda t: -t[0])
    if not info:
        return None
    area, outer, opts = info[0]
    ign = {"drills": 0, "narrow_slots": 0, "islands": 0, "other_loops": 0, "open_chains": 0}
    out = {"outer": outer, "cutouts": [], "holes": [], "ignored": ign, "area": area, "thickness": None, "enclosure": None}
    for a, lp, pts in info[1:]:
        if not pip(opts, pts[0][0], pts[0][1]):
            ign["other_loops"] += 1
            continue
        circ = len(lp) == 1 and lp[0][0] == "C"
        d = 2 * lp[0][2] if circ else 0.0
        if circ and min_hole <= d <= max_hole and holes != "none" and max_hole > 0:
            out["holes"].append({"x": round(lp[0][1][0], 4), "y": round(lp[0][1][1], 4), "d": round(d, 3), "size": hole_size(d)})
        elif circ and min_hole <= d <= max_hole and max_hole > 0:
            ign["drills"] += 1          # holes='none': the user does not want them
            continue
        elif circ and d < min_hole and holes != "all":
            ign["drills"] += 1
            continue
        elif not circ and holes != "all" and min_hole > 0 and _narrow(lp, pts, min_hole):
            ign["narrow_slots"] += 1
            continue
        else:
            out["cutouts"].append(lp)
        out["area"] -= a
    xs = [p[0] for p in opts]
    ys = [p[1] for p in opts]
    out["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
    return out


def _arc_sweep(p):
    """('A', s, m, e) -> (centre, r, start angle, signed sweep) or None for a degenerate arc."""
    cr = circle3(p[1], p[2], p[3])
    if not cr:
        return None
    (cx, cy), r = cr
    a0 = math.atan2(p[1][1] - cy, p[1][0] - cx)
    am = math.atan2(p[2][1] - cy, p[2][0] - cx)
    a1 = math.atan2(p[3][1] - cy, p[3][0] - cx)
    ccw = ((am - a0) % (2 * math.pi)) < ((a1 - a0) % (2 * math.pi))
    sweep = (a1 - a0) % (2 * math.pi) if ccw else -((a0 - a1) % (2 * math.pi))
    return (cx, cy), r, a0, sweep


def merge_arcs(loop, tol=0.01):
    """Join neighbouring arcs that share centre and radius (one corner split in two by a writer)."""
    if len(loop) < 2:
        return loop
    out = []
    for p in loop:
        if out and p[0] == "A" and out[-1][0] == "A":
            q = out[-1]
            s1, s2 = _arc_sweep(q), _arc_sweep(p)
            if s1 and s2 and math.dist(s1[0], s2[0]) <= tol and abs(s1[1] - s2[1]) <= tol and \
                    math.dist(q[3], p[1]) <= tol and (s1[3] > 0) == (s2[3] > 0) and abs(s1[3] + s2[3]) < 2 * math.pi - 1e-6:
                (cx, cy), r = s1[0], s1[1]
                h = s1[2] + (s1[3] + s2[3]) / 2
                out[-1] = ("A", q[1], (cx + r * math.cos(h), cy + r * math.sin(h)), p[3])
                continue
        out.append(p)
    return out


def decimate(pts, tol):
    """Douglas-Peucker on a closed polygon (list of [x, y])."""
    n = len(pts)
    if n <= 8:
        return list(pts)
    i0 = min(range(n), key=lambda i: (pts[i][0], pts[i][1]))
    ring = list(pts[i0:]) + list(pts[:i0])
    i1 = max(range(n), key=lambda i: math.dist(ring[i], ring[0]))

    def dp(seg):
        keep = [0, len(seg) - 1]
        stack = [(0, len(seg) - 1)]
        while stack:
            a, b = stack.pop()
            ax, ay = seg[a]
            bx, by = seg[b]
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            worst, wi = 0.0, None
            for i in range(a + 1, b):
                px, py = seg[i]
                if l2 < 1e-18:
                    d = math.hypot(px - ax, py - ay)
                else:
                    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                    d = math.hypot(px - ax - t * dx, py - ay - t * dy)
                if d > worst:
                    worst, wi = d, i
            if wi is not None and worst > tol:
                keep.append(wi)
                stack += [(a, wi), (wi, b)]
        return [seg[i] for i in sorted(set(keep))]
    a = dp(ring[:i1 + 1])
    b = dp(ring[i1:] + [ring[0]])
    return a[:-1] + b[:-1]


def prims_to_json(loop):
    """[("L", p0, p1), ...] -> [["L", x0, y0, x1, y1], ["A", sx, sy, mx, my, ex, ey], ["C", cx, cy, r]]"""
    out = []
    for p in loop:
        if p[0] == "L":
            out.append(["L", round(p[1][0], 6), round(p[1][1], 6), round(p[2][0], 6), round(p[2][1], 6)])
        elif p[0] == "A":
            out.append(["A"] + [round(v, 6) for q in p[1:4] for v in q])
        else:
            out.append(["C", round(p[1][0], 6), round(p[1][1], 6), round(p[2], 6)])
    return out


def prims_from_json(lst):
    out = []
    for p in lst:
        if p[0] == "L":
            out.append(("L", (p[1], p[2]), (p[3], p[4])))
        elif p[0] == "A":
            out.append(("A", (p[1], p[2]), (p[3], p[4]), (p[5], p[6])))
        else:
            out.append(("C", (p[1], p[2]), p[3]))
    return out


def kinds(loop):
    d = {"lines": 0, "arcs": 0, "circles": 0}
    for p in loop:
        d[{"L": "lines", "A": "arcs", "C": "circles"}[p[0]]] += 1
    return d


def gaps(chains, limit=6):
    """End points of open chains: [[x, y], ...] (where a drawing fails to close)."""
    out = []
    for ch in chains:
        s, e = prim_ends(ch[0])[0], prim_ends(ch[-1])[1]
        out += [[round(s[0], 3), round(s[1], 3)], [round(e[0], 3), round(e[1], 3)]]
    return out[:limit]


def transform(loop, f):
    out = []
    for p in loop:
        if p[0] == "L":
            out.append(("L", f(p[1]), f(p[2])))
        elif p[0] == "A":
            out.append(("A", f(p[1]), f(p[2]), f(p[3])))
        else:
            c = f(p[1]); e = f((p[1][0] + p[2], p[1][1]))
            out.append(("C", c, math.dist(c, e)))
    return out

