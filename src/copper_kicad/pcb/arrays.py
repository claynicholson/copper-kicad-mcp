"""Identical large parts as one row or matrix (pure Python; a post-pass of the elegant placer).

Four or more parts with the same footprint (potentiometers, switches, relays, sockets, displays ...)
read as a group on a finished board. Connectivity-driven placement scatters them at whatever
rotation happens to be cheapest for each; this pass takes such a group off the board and puts it
back as a regular block at one rotation, as close as possible to where it was. It only commits a
block in which every cell is legal for the placer (keep-in, courtyards, spacing); otherwise the
group stays exactly as it was placed.

    regularise(placer, ...) -> [{'refs': [...], 'cols': c, 'rows': r, 'rot': deg}]   (also in placer.notes['arrays'])
"""
from __future__ import annotations

import math
import time

MIN_COUNT = 4
MIN_AREA = 25.0       # mm2 of courtyard: small passives are aligned by the row pass, not here
SKIP_ROLES = ("connector", "fixed", "mcu", "crystal", "decap", "xtal_cap", "esd")


def groups_of(parts, min_count=MIN_COUNT, min_area=MIN_AREA) -> list:
    g = {}
    for p in parts:
        if p.fixed or not p.placed or p.ref.startswith("#") or p.role in SKIP_ROLES or p.antenna or p.anchor:
            continue
        if len(p.pads) < 3 or p.area() < min_area:
            continue
        g.setdefault((p.fpid, len(p.pads), p.side), []).append(p)
    return [sorted(v, key=lambda p: p.ref) for v in g.values() if len(v) >= min_count]


def _up(v, g):
    return math.ceil(v / g - 1e-9) * g


def _cells(n, cols, rows):
    return [(k % cols, k // cols) for k in range(n)][:n] if cols * rows >= n else []


def _try_shape(pl, g, rot, cols, rows, centre, deadline):
    """First legal origin (nearest to the group's old centre) for a cols x rows block -> [(x, y)] per cell or None."""
    p0 = g[0]
    r0 = p0.rect(0, 0, rot)
    pw, ph = r0[2] - r0[0], r0[3] - r0[1]
    gap = max(pl.spacing, 1.0)
    px, py = _up(pw + gap, pl.grid), _up(ph + gap, pl.grid)
    bw, bh = (cols - 1) * px + pw, (rows - 1) * py + ph
    b = pl.box
    if bw > b[2] - b[0] or bh > b[3] - b[1]:
        return None
    # block top-left corner positions the box allows, nearest to the old centre first
    lo_x, hi_x, lo_y, hi_y = b[0], b[2] - bw, b[1], b[3] - bh
    want_x, want_y = centre[0] - bw / 2, centre[1] - bh / 2
    step = max(pl.grid, 1.0)
    xs = [lo_x + i * step for i in range(int((hi_x - lo_x) / step) + 1)]
    ys = [lo_y + j * step for j in range(int((hi_y - lo_y) / step) + 1)]
    cand = sorted(((abs(x - want_x) + abs(y - want_y), x, y) for x in xs for y in ys))
    cells = _cells(len(g), cols, rows)
    for _, x, y in cand:
        if time.time() > deadline:
            return None
        pos = []
        for cx, cy in cells:
            # part origin so that its courtyard's top-left lands on the cell's corner, on the placement grid
            ox = round((x + cx * px - r0[0]) / pl.grid) * pl.grid
            oy = round((y + cy * py - r0[1]) / pl.grid) * pl.grid
            if not pl.legal(p0, ox, oy, rot):
                pos = None
                break
            pos.append((round(ox, 4), round(oy, 4)))
        if pos:
            return pos
    return None


def _array(pl, g, deadline):
    n = len(g)
    rots = [round(p.rot) % 360 for p in g]
    rot = max(sorted(set(rots)), key=rots.count)
    old = {p.ref: (p.x, p.y, p.rot) for p in g}
    centre = (sum(p.x for p in g) / n, sum(p.y for p in g) / n)
    c0 = sum(pl.cost(p, p.x, p.y, p.rot) for p in g)
    for p in g:
        pl.placed_rects.pop(p.ref, None)
        p.placed = False
    b = pl.box
    board_aspect = (b[2] - b[0]) / max(b[3] - b[1], 1e-6)
    shapes = []
    for rows in range(1, n + 1):
        cols = math.ceil(n / rows)
        if (cols, rows) not in shapes and (cols - 1) * rows < n:        # no empty row or column
            shapes.append((cols, rows))
    r0 = g[0].rect(0, 0, rot)
    cell_aspect = (r0[2] - r0[0]) / max(r0[3] - r0[1], 1e-6)
    # full rectangles first, then the shape closest to the board's proportions
    shapes.sort(key=lambda s: (s[0] * s[1] != n, abs(math.log(max(s[0] * cell_aspect / s[1], 1e-6) / board_aspect))))
    best = None
    for cols, rows in shapes[:6]:
        pos = _try_shape(pl, g, rot, cols, rows, centre, deadline)
        if not pos:
            continue
        # keep the parts' relative order: rows by old y, then by old x inside a row
        by_y = sorted(g, key=lambda p: (old[p.ref][1], old[p.ref][0]))
        order = []
        for r in range(rows):
            order += sorted(by_y[r * cols:(r + 1) * cols], key=lambda p: old[p.ref][0])
        for p, (x, y) in zip(order, pos):
            pl.commit(p, x, y, rot)
        cost = sum(pl.cost(p, p.x, p.y, p.rot) for p in g)
        for p in g:
            pl.placed_rects.pop(p.ref, None)
            p.placed = False
        if best is None or cost < best[0]:
            best = (cost, cols, rows, [(p, xy) for p, xy in zip(order, pos)])
        if best and best[1] * best[2] == n:
            break               # a full rectangle near the old spot: good enough
    # regularity is worth some wire, not any amount of it
    if best is None or best[0] > 1.6 * c0 + 25.0 * n:
        for p in g:
            x, y, r = old[p.ref]
            pl.commit(p, x, y, r)
        return None
    for p, (x, y) in best[3]:
        pl.commit(p, x, y, rot)
    return {"refs": [p.ref for p, _ in best[3]], "cols": best[1], "rows": best[2], "rot": rot}


def regularise(pl, budget_s: float = 3.0) -> list:
    out = []
    deadline = time.time() + budget_s
    for g in sorted(groups_of(pl.parts), key=lambda v: -len(v) * v[0].area()):
        if time.time() > deadline:
            break
        r = _array(pl, g, deadline)
        if r:
            out.append(r)
    pl.notes["arrays"] = out
    return out
