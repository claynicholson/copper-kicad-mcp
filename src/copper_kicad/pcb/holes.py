"""Mounting holes: one definition of "this footprint is a mounting hole", and corner seats for the
holes that come from the schematic (pure Python, no pcbnew).

    is_hole(footprint_id, ref="")     the footprint (or its library) is named MountingHole...
    schematic_holes(footprints)       hole footprints the schematic owns and nobody locked
    seats(n, bbox, inset, polygon)    n positions: the four corners first, then edge midpoints
    seat(snapshot, refs, keep)        move the schematic's holes onto those seats (in the snapshot)

A hole that pcb_setup(holes=...) made is 'board_only' and already sits where it was asked for. A
hole drawn in the schematic arrives with pcb_sync beside the board like every other new part; the
placer treats holes as fixed obstacles, so without a seat it would stay wherever it was dropped.
"""
from __future__ import annotations

import math
import re

# the footprint NAME (or its library) starts with "MountingHole"; a D-sub called
# "..._MountingHolesOffset9.12mm" is a connector, not a hole
_HOLE_RE = re.compile(r"(^|:)\s*mounting_?hole", re.I)


def is_hole(footprint_id, ref: str = "") -> bool:
    return bool(_HOLE_RE.search(footprint_id or ""))


def is_hole_fp(d: dict) -> bool:
    return is_hole(d.get("footprint") or "")


def schematic_holes(footprints, refs=None, keep=None) -> list:
    """Hole footprints auto placement may move: from the schematic (not board_only), not locked, not in
    keep=[...], and inside refs=[...] when that is given."""
    keep = set(keep or ())
    only = set(refs) if refs else None
    return [d for d in footprints if is_hole_fp(d) and "board_only" not in (d.get("flags") or [])
            and not d.get("locked") and d.get("ref") not in keep and (only is None or d.get("ref") in only)]


def _pip(poly, x, y):
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            c = not c
    return c


def _fits(poly, cutouts, x, y, r):
    """The disc (x, y, r) lies inside the outline polygon and outside its cutouts (8 rim samples)."""
    if not poly:
        return True
    pts = [(x, y)] + [(x + r * math.cos(k * math.pi / 4), y + r * math.sin(k * math.pi / 4)) for k in range(8)]
    return all(_pip(poly, px, py) for px, py in pts) and not any(_pip(c, px, py) for c in cutouts or [] for px, py in pts)


def seats(n: int, bbox, inset: float = 3.5, polygon=None, cutouts=None, radius: float = 2.0) -> list:
    """Up to ``n`` hole centres in ``bbox``: corners, then the middle of the long edges, then of the
    short ones. A seat that falls outside ``polygon`` (rounded or slanted outline) is pulled diagonally
    towards the centre in 0.5 mm steps, and dropped when it never fits."""
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    i = min(inset, w / 2, h / 2)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    cand = [(x0 + i, y0 + i), (x1 - i, y0 + i), (x1 - i, y1 - i), (x0 + i, y1 - i)]
    long_mid = [(mx, y0 + i), (mx, y1 - i)] if w >= h else [(x0 + i, my), (x1 - i, my)]
    short_mid = [(x0 + i, my), (x1 - i, my)] if w >= h else [(mx, y0 + i), (mx, y1 - i)]
    cand += long_mid + short_mid
    out = []
    for cx, cy in cand:
        if len(out) >= n:
            break
        x, y = cx, cy
        for _ in range(40):
            if _fits(polygon, cutouts, x, y, radius + 0.25):
                break
            d = math.hypot(mx - x, my - y)
            if d < 0.5:
                x = None
                break
            x, y = x + (mx - x) / d * 0.5, y + (my - y) / d * 0.5
        else:
            x = None
        if x is None or any(math.hypot(x - u, y - v) < 2 * radius + 1.0 for u, v in out):
            continue
        out.append((round(x, 3), round(y, 3)))
    return out


def _radius(d) -> float:
    lb = d.get("lbbox") or d.get("lbody") or [-2, -2, 2, 2]
    return max(lb[2] - lb[0], lb[3] - lb[1], 1.0) / 2


def seat(snapshot: dict, refs=None, keep=None, inset: float | None = None) -> list:
    """Give every movable schematic hole a seat. The snapshot's footprint dicts are replaced by moved
    copies (the caller's dicts are not mutated). -> [{'ref', 'x', 'y', 'rot', 'side'}] for the holes
    that moved; holes that found no seat stay where they are."""
    ol = snapshot.get("outline")
    if not ol or not ol.get("bbox"):
        return []
    fps = snapshot["footprints"]
    mine = schematic_holes(fps, refs, keep)
    if not mine:
        return []
    rad = max(_radius(d) for d in mine)
    ins = inset if inset is not None else max(3.5, math.ceil((rad + 0.5) * 2) / 2)
    taken = [(d["x"], d["y"]) for d in fps if is_hole_fp(d) and d not in mine]
    kept = set(keep or ())
    only = set(refs) if refs else None
    # parts that will not move (locked, keep=[...], outside refs=[...]) block a seat
    stay = [d["bbox"] for d in fps if d.get("bbox") and not is_hole_fp(d)
            and (d.get("locked") or d.get("ref") in kept or (only is not None and d.get("ref") not in only))]

    def clear(p):
        return not any(math.hypot(p[0] - u, p[1] - v) < 2 * rad + 1.0 for u, v in taken) and not any(
            b[0] - rad < p[0] < b[2] + rad and b[1] - rad < p[1] < b[3] + rad for b in stay)
    pos = [p for p in seats(len(mine) + len(taken) + len(stay), ol["bbox"], ins, ol.get("polygon"), ol.get("holes"), rad)
           if clear(p)][:len(mine)]
    # nearest seat first, so a hole that already sits on one does not move
    moves, left = [], list(mine)
    for p in pos:
        d = min(left, key=lambda q: math.hypot(q["x"] - p[0], q["y"] - p[1]))
        left.remove(d)
        if math.hypot(d["x"] - p[0], d["y"] - p[1]) > 1e-6 or d.get("rot"):
            nd = dict(d, x=p[0], y=p[1], rot=0)
            fps[fps.index(d)] = nd
            moves.append({"ref": d["ref"], "x": p[0], "y": p[1], "rot": 0, "side": d.get("side", "top")})
    return moves
