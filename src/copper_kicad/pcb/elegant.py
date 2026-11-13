"""Placement that is scored for elegance, not only for wirelength (pure Python, no pcbnew).

    auto_size(snapshot, ...)   -> the board size the parts need (analytic start, verified by placing)
    auto_place(snapshot, ...)  -> several structured placement candidates, the best one by
                                  ``review.elegance.placement_score`` (ratsnest as tie-break)

``ElegantPlacer`` extends ``place.Placer``:
  * keep-in test on an occupancy bitmap when the outline has arcs, slanted edges or cutouts;
  * side-entry connectors (USB, JST-SH ...) sit with their body flush with the board edge (or
    overhanging by a given amount), clamped so pad copper and holes keep the edge clearance;
  * decoupling caps must end within 4 mm of the pin they serve: neighbours are evicted and re-placed
    when needed, and what still cannot be done is reported (``decap_far``);
  * two-pad parts that almost line up are snapped into rows / columns.
"""
from __future__ import annotations

import math
import time

from . import holes as holesmod
from . import place as placer
from .place import Part, Placer, snap, tf

DECAP_MAX = 4.0


def _box_of(p, box, x=None, y=None, rot=None):
    """Board rect of a local box of part p."""
    x = p.x if x is None else x
    y = p.y if y is None else y
    rot = p.rot if rot is None else rot
    pts = [p.ltf(cx, cy, rot) for cx, cy in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]
    return (x + min(q[0] for q in pts), y + min(q[1] for q in pts), x + max(q[0] for q in pts), y + max(q[1] for q in pts))


class Bitmap:
    """Free-cell map of an outline: inside the polygon, outside every cutout, ``edge`` away from both."""

    def __init__(self, poly, holes, bbox, edge, cell=0.25):
        self.x0, self.y0 = bbox[0], bbox[1]
        self.g = cell
        self.nx = max(1, int(math.ceil((bbox[2] - bbox[0]) / cell)))
        self.ny = max(1, int(math.ceil((bbox[3] - bbox[1]) / cell)))
        blocked = [bytearray(b"\x01" * self.nx) for _ in range(self.ny)]
        self._fill(blocked, poly, 0)
        for h in holes or []:
            self._fill(blocked, h, 1)
        for ring in [poly] + list(holes or []):
            self._ring(blocked, ring, edge)
        # summed-area table of blocked cells
        sat = [[0] * (self.nx + 1) for _ in range(self.ny + 1)]
        for j in range(self.ny):
            row, acc, above, cur = blocked[j], 0, sat[j], sat[j + 1]
            for i in range(self.nx):
                acc += row[i]
                cur[i + 1] = above[i + 1] + acc
        self.sat = sat

    def _fill(self, blocked, poly, value):
        n = len(poly)
        for j in range(self.ny):
            y = self.y0 + (j + 0.5) * self.g
            xs = []
            for i in range(n):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % n]
                if (y1 > y) != (y2 > y):
                    xs.append(x1 + (x2 - x1) * (y - y1) / (y2 - y1))
            xs.sort()
            row = blocked[j]
            for k in range(0, len(xs) - 1, 2):
                i0 = max(0, int(math.ceil((xs[k] - self.x0) / self.g - 0.5)))
                i1 = min(self.nx, int(math.floor((xs[k + 1] - self.x0) / self.g - 0.5)) + 1)
                for i in range(i0, i1):
                    row[i] = value

    def _ring(self, blocked, poly, rad):
        if rad <= 0:
            return
        n = len(poly)
        g = self.g
        k = int(math.ceil(rad / g))
        r2 = rad * rad
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            steps = max(1, int(math.hypot(x2 - x1, y2 - y1) / (g / 2)))
            for s in range(steps + 1):
                px, py = x1 + (x2 - x1) * s / steps, y1 + (y2 - y1) * s / steps
                ci, cj = int((px - self.x0) / g), int((py - self.y0) / g)
                for j in range(max(0, cj - k), min(self.ny, cj + k + 1)):
                    cy = self.y0 + (j + 0.5) * g
                    row = blocked[j]
                    for ii in range(max(0, ci - k), min(self.nx, ci + k + 1)):
                        cx = self.x0 + (ii + 0.5) * g
                        if (cx - px) ** 2 + (cy - py) ** 2 < r2:
                            row[ii] = 1

    def free(self, r) -> bool:
        """Every cell whose centre lies in rect r is free (a rect outside the map is not)."""
        g = self.g
        i0 = int(math.ceil((r[0] - self.x0) / g - 0.5 - 1e-9))
        i1 = int(math.floor((r[2] - self.x0) / g - 0.5 + 1e-9)) + 1
        j0 = int(math.ceil((r[1] - self.y0) / g - 0.5 - 1e-9))
        j1 = int(math.floor((r[3] - self.y0) / g - 0.5 + 1e-9)) + 1
        if i0 < 0 or j0 < 0 or i1 > self.nx or j1 > self.ny:
            return False
        if i1 <= i0 or j1 <= j0:
            return True
        s = self.sat
        return s[j1][i1] - s[j0][i1] - s[j1][i0] + s[j0][i0] == 0


class ElegantPlacer(Placer):
    def __init__(self, parts, outline, spacing=None, edge=0.5, grid=0.5, tracks=None, main_edge="left", mcu_rot=None,
                 mcu_pull=0.0, overhang=None, copper_edge=0.3, hole_edge=0.3, flush=True):
        Placer.__init__(self, parts, outline, spacing=spacing, edge=edge, grid=grid, tracks=tracks)
        self.main_edge = main_edge
        self.mcu_rot = mcu_rot
        self.mcu_pull = mcu_pull
        self.overhang_mm = dict(overhang or {})
        self.copper_edge, self.hole_edge = copper_edge, hole_edge
        self.flush = flush
        self.notes = {"clamped": {}, "decap_far": [], "aligned": 0, "flush": []}
        holes = outline.get("holes") or []
        self.bitmap = None
        if self.poly and (holes or not self.rect_outline):
            self.bitmap = Bitmap(self.poly, holes, outline["bbox"], edge)

    # ── keep-in ────────────────────────────────────────────────────────
    def _inside(self, r):
        if self.bitmap is None:
            return Placer._inside(self, r)
        return self.bitmap.free(r)

    def legal(self, p, x, y, rot, ignore=None):
        if self.bitmap is not None and p.overhang:
            # the clipped-rect trick of the base class only knows the bounding box: test the part of the
            # courtyard that is on the board against the bitmap instead
            r = p.rect(x, y, rot)
            ob = self.ol["bbox"]
            k = {"left": 0, "top": 1, "right": 2, "bottom": 3}[p.overhang]
            r2 = list(r)
            r2[k] = max(r2[k], ob[k] + self.edge) if k < 2 else min(r2[k], ob[k] - self.edge)
            if r2[0] >= r2[2] or r2[1] >= r2[3] or not self.bitmap.free(tuple(r2)):
                return False
            saved, p.overhang = p.overhang, None
            saved_bm, self.bitmap = self.bitmap, _AlwaysFree
            try:
                return Placer.legal(self, p, x, y, rot, ignore)
            finally:
                p.overhang, self.bitmap = saved, saved_bm
        return Placer.legal(self, p, x, y, rot, ignore)

    # ── connectors ─────────────────────────────────────────────────────
    def _side_entry(self, p) -> bool:
        from ..review.elegance import side_entry
        return side_entry(p)

    def _flush_coord(self, p, e, rot):
        """Coordinate (x for left/right, y for top/bottom) that puts the connector body flush with edge e
        (+ requested overhang), clamped so pad copper / holes keep their edge clearance. None = not applicable."""
        fab = p.d.get("lfab")
        if not self.flush or p.edge_hint or not fab or not self._side_entry(p):
            return None
        ob = self.ol["bbox"]
        fr = _box_of(p, fab, 0.0, 0.0, rot)
        want = float(self.overhang_mm.get(p.ref, 0.0))
        k = {"left": 0, "top": 1, "right": 2, "bottom": 3}[e]
        sgn = -1.0 if k < 2 else 1.0                 # outward direction along the axis
        c = ob[k] - fr[k] + sgn * want               # body front at the edge, pushed out by `want`
        # clamp: copper and holes must stay inside by their clearance
        worst = 0.0
        for pd in p.pads:
            dx, dy = p.ltf(pd["lx"], pd["ly"], rot)
            w, h = pd.get("w", 0.0), pd.get("h", 0.0)
            if int(round(rot)) % 180 == 90:
                w, h = h, w
            half = (w if k in (0, 2) else h) / 2
            centre = c + (dx if k in (0, 2) else dy)
            ext = centre + sgn * half               # outermost copper / hole wall toward the edge
            clr = self.hole_edge if pd.get("type") == "npth" else self.copper_edge
            inside_by = (ext - ob[k]) * -sgn        # > 0 = inside the board
            worst = max(worst, clr - inside_by)
        if worst > 1e-9:
            c -= sgn * worst
            self.notes["clamped"][p.ref] = round(worst, 3)
        return round(c, 4)

    def place_connectors(self, conns, power_in):
        b = self.box
        order = sorted(conns, key=lambda p: (0 if p in power_in else 1, -len(p.pads), p.ref))
        edges = ["left", "right", "top", "bottom"] if self.main_edge == "left" else \
            ["top", "bottom", "left", "right"] if self.main_edge == "top" else \
            ["right", "left", "bottom", "top"] if self.main_edge == "right" else ["bottom", "top", "right", "left"]
        slots = {e: [] for e in edges}
        for i, p in enumerate(order):
            e = edges[0] if p in power_in else edges[i % 4]
            if p not in power_in and e == edges[0] and power_in:
                e = edges[1]
            slots[e].append(p)
        g = self.grid
        for e, lst in slots.items():
            n = len(lst)
            for k, p in enumerate(lst):
                if not self._on_edge(p, e, (k + 1) / (n + 1)):
                    done = False
                    if not p.edge_hint:
                        for e2 in [x for x in ("top", "bottom", "right", "left") if x != e]:
                            if self._on_edge(p, e2, 0.5):
                                done = True
                                break
                    if not done:
                        p.overhang = None
                        r = self.search(p)
                        if r:
                            self.commit(p, r[0], r[1], r[2])
                        else:
                            self.pin_in_place(p)
        # RF modules next (antenna on an edge), then the main IC with this candidate's rotation / pull
        for p in sorted([q for q in self.parts if q.antenna and not q.placed and not q.fixed], key=lambda q: -q.area()):
            self._place_antenna(p)
        mcu = self.mcu
        if mcu is not None and not mcu.fixed and not mcu.placed and (self.mcu_rot is not None or self.mcu_pull):
            self._place_mcu(mcu, [c for c in order if c.placed][:1])

    def _on_edge(self, p, e, frac) -> bool:
        """Put connector p on edge e at fraction ``frac`` along it, sliding until legal."""
        b, g = self.box, self.grid
        ob = self.ol["bbox"]
        rot = self._edge_rot(p, e)
        r0 = p.rect(0, 0, rot)
        w, h = r0[2] - r0[0], r0[3] - r0[1]
        horiz = e in ("left", "right")
        if e == "left":
            x, y = b[0] - r0[0], b[1] + frac * (b[3] - b[1]) - (r0[1] + h / 2)
        elif e == "right":
            x, y = b[2] - r0[2], b[1] + frac * (b[3] - b[1]) - (r0[1] + h / 2)
        elif e == "top":
            x, y = b[0] + frac * (b[2] - b[0]) - (r0[0] + w / 2), b[1] - r0[1]
        else:
            x, y = b[0] + frac * (b[2] - b[0]) - (r0[0] + w / 2), b[3] - r0[3]
        exact = False
        p.overhang = None
        if p.edge_hint:
            hx, hy = tf(p.edge_hint[0], p.edge_hint[1], rot)
            if horiz:
                x = (ob[0] if e == "left" else ob[2]) - hx
            else:
                y = (ob[1] if e == "top" else ob[3]) - hy
            exact = True
            p.overhang = e
        else:
            fc = self._flush_coord(p, e, rot)
            if fc is not None:
                if horiz:
                    x = fc
                else:
                    y = fc
                exact = True
                p.overhang = e
        if horiz:
            if not exact:
                x = math.ceil(x / g - 1e-9) * g if e == "left" else math.floor(x / g + 1e-9) * g
            y = snap(y, g)
        else:
            if not exact:
                y = math.ceil(y / g - 1e-9) * g if e == "top" else math.floor(y / g + 1e-9) * g
            x = snap(x, g)
        for off in placer._offsets(max(b[2] - b[0], b[3] - b[1]), g):
            xx, yy = (x, y + off) if horiz else (x + off, y)
            if self.legal(p, xx, yy, rot):
                self.commit(p, round(xx, 4), round(yy, 4), rot)
                p.edge = e
                if exact and not p.edge_hint:
                    self.notes["flush"].append(p.ref)
                return True
        p.overhang = None
        return False

    def _place_mcu(self, mcu, main):
        b = self.box
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if main and self.mcu_pull:
            cx += (main[0].x - cx) * self.mcu_pull
            cy += (main[0].y - cy) * self.mcu_pull
        best = None
        for rot in ((self.mcu_rot,) if self.mcu_rot is not None else (0, 90)):
            r0 = mcu.rect(0, 0, rot)
            for off in placer._offsets(max(b[2] - b[0], b[3] - b[1]) / 2, self.grid):
                for dx, dy in ((off, 0), (0, off)):
                    x = snap(cx - (r0[0] + r0[2]) / 2 + dx)
                    y = snap(cy - (r0[1] + r0[3]) / 2 + dy)
                    if self.legal(mcu, x, y, rot):
                        c = self.cost(mcu, x, y, rot) + abs(dx) + abs(dy)
                        if best is None or c < best[0]:
                            best = (c, x, y, rot)
                if best is not None and abs(off) > 6:
                    break
        if best:
            self.commit(mcu, best[1], best[2], best[3])

    # ── post passes ────────────────────────────────────────────────────
    def run(self):
        Placer.run(self)
        self.fix_decaps()
        self.align_rows()
        from . import arrays
        arrays.regularise(self)         # four or more identical large parts become one row / matrix
        return sorted(set(self.unplaced_refs))

    def _decap_dist(self, p, x=None, y=None, rot=None):
        q, qp = p.anchor
        qx, qy = q.pad_pos(qp)
        mine = [pd for pd in p.pads if pd.get("net") and pd.get("net") == qp.get("net")]
        px, py = p.pad_pos(mine[0], x, y, rot) if mine else ((p.x if x is None else x), (p.y if y is None else y))
        return math.hypot(qx - px, qy - py)

    def _near_spot(self, p, limit):
        """Best legal spot for decap p with its supply pad within ``limit`` of the anchor pin, or None."""
        q, qp = p.anchor
        qx, qy = q.pad_pos(qp)
        g = self.grid
        best = None
        span = limit + 2.5
        n = int(span / g)
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                x, y = snap(qx + i * g, g), snap(qy + j * g, g)
                for rot in (0, 90, 180, 270):
                    d = self._decap_dist(p, x, y, rot)
                    if d > limit or not self.legal(p, x, y, rot):
                        continue
                    c = self.cost(p, x, y, rot)
                    if best is None or c < best[0]:
                        best = (c, x, y, rot)
        return best

    def fix_decaps(self, limit=DECAP_MAX):
        decaps = [p for p in self.parts if p.role == "decap" and p.anchor and p.anchor[0].placed and p.placed
                  and not p.fixed and not getattr(p, "p_unplaced", False)]
        for p in sorted(decaps, key=lambda q: -self._decap_dist(q)):
            if self._decap_dist(p) <= limit:
                continue
            old = (p.x, p.y, p.rot)
            del self.placed_rects[p.ref]
            p.placed = False
            spot = self._near_spot(p, limit)
            if spot:
                self.commit(p, spot[1], spot[2], spot[3])
                continue
            # evict up to two lower-priority neighbours of the pin, seat the cap, then re-place them
            q, qp = p.anchor
            qx, qy = q.pad_pos(qp)
            near = [o for o in self.parts if o is not p and o.placed and not o.fixed and o.ref in self.placed_rects
                    and o.role in ("attached", "other", "switch", "xtal_cap") and not o.antenna
                    and math.hypot(o.x - qx, o.y - qy) < limit + 3.0]
            near.sort(key=lambda o: math.hypot(o.x - qx, o.y - qy))
            done = False
            for group in [[o] for o in near[:4]] + [near[:2]] * (len(near) >= 2):
                saved = [(o, o.x, o.y, o.rot) for o in group]
                for o in group:
                    del self.placed_rects[o.ref]
                    o.placed = False
                spot = self._near_spot(p, limit)
                ok = False
                if spot:
                    self.commit(p, spot[1], spot[2], spot[3])
                    ok = True
                    for o in group:
                        r = self.search(o)
                        if r:
                            self.commit(o, r[0], r[1], r[2])
                        else:
                            ok = False
                            break
                if ok:
                    done = True
                    break
                # roll back
                for o in group:
                    self.placed_rects.pop(o.ref, None)
                    o.placed = False
                self.placed_rects.pop(p.ref, None)
                p.placed = False
                for o, x, y, rot in saved:
                    self.commit(o, x, y, rot)
            if not done:
                self.commit(p, *old)
                self.notes["decap_far"].append([p.ref, f"{q.ref}.{qp.get('num')}", round(self._decap_dist(p), 2)])

    def align_rows(self, reach=6.0, tol=1.0, max_cost=0.5):
        small = [p for p in self.parts if len(p.pads) == 2 and p.placed and not p.fixed and p.role != "connector"
                 and not getattr(p, "p_unplaced", False)]
        n = 0
        for a in small:
            for bq in small:
                if bq is a or math.hypot(a.x - bq.x, a.y - bq.y) > reach:
                    continue
                for axis in (0, 1):
                    da = (bq.x - a.x) if axis == 0 else (bq.y - a.y)
                    if not (0.02 < abs(da) < tol):
                        continue
                    nx, ny = (a.x, bq.y) if axis == 0 else (bq.x, a.y)
                    c0 = self.cost(bq, bq.x, bq.y, bq.rot)
                    old = self.placed_rects.pop(bq.ref)
                    bq.placed = False
                    ok = self.legal(bq, nx, ny, bq.rot) and self.cost(bq, nx, ny, bq.rot) <= c0 + max_cost
                    if ok and bq.role == "decap" and bq.anchor and self._decap_dist(bq, nx, ny, bq.rot) > DECAP_MAX:
                        ok = False
                    if ok:
                        self.commit(bq, nx, ny, bq.rot)
                        n += 1
                    else:
                        bq.placed = True
                        self.placed_rects[bq.ref] = old
        self.notes["aligned"] = n


class _AlwaysFree:
    @staticmethod
    def free(r):
        return True


# ── one placement run ─────────────────────────────────────────────────────
def _parts_of(snapshot, refs=None, keep=None):
    parts = [Part(d) for d in snapshot["footprints"]]
    for i, o in enumerate(snapshot.get("obstacles") or []):
        x0, y0, x1, y1 = o["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        ob = Part({"ref": f"#OBST{i + 1}", "x": cx, "y": cy, "rot": 0, "locked": True, "pad_list": [],
                   "lbbox": [x0 - cx, y0 - cy, x1 - cx, y1 - cy], "footprint": o.get("kind", "")})
        ob.role = "fixed"
        parts.append(ob)
    keep = set(keep or [])
    if refs:
        keep |= {p.ref for p in parts if p.ref not in set(refs)}
    for p in parts:
        if p.ref in keep:
            p.fixed = True
        if p.side == "bottom" and not p.fixed:
            p.fixed = True   # bottom-side parts are left alone by the auto placer
        p.was_fixed = p.fixed
    return parts


def _run_once(snapshot, refs, keep, spacing, edge, grid, variant, rules, overhang):
    ol = snapshot["outline"]
    parts = _parts_of(snapshot, refs, keep)
    before = placer.ratsnest(parts)
    v = dict(variant or {})
    sp = spacing
    pl = ElegantPlacer(parts, ol, spacing=sp, edge=edge, grid=grid, tracks=snapshot.get("track_segs") if refs else None,
                       main_edge=v.get("main_edge", "left"), mcu_rot=v.get("mcu_rot"), mcu_pull=v.get("mcu_pull", 0.0),
                       overhang=overhang, copper_edge=(rules or {}).get("copper_edge", 0.3),
                       hole_edge=(rules or {}).get("hole_clearance", 0.3))
    if v.get("spacing_step") and spacing is None:
        ladder = [0.3, 0.6, 1.0, 1.5]
        i = min(range(4), key=lambda k: abs(ladder[k] - pl.spacing))
        pl.spacing = ladder[max(0, min(3, i + v["spacing_step"]))]
    pl.run()
    return parts, pl, before


def _result(parts, pl, before):
    unplaced = sorted(set(pl.unplaced_refs))
    moves = [{"ref": p.ref, "x": p.x, "y": p.y, "rot": p.rot % 360, "side": "top"}
             for p in parts if not p.fixed and p.placed and not p.ref.startswith("#OBST")]
    roles = {}
    for p in parts:
        if p.ref.startswith("#OBST"):
            continue
        roles.setdefault(p.role, []).append(p.ref + (f"→{p.anchor[0].ref}.{p.anchor[1]['num']}" if p.anchor else ""))
    return {"moves": moves, "score_before": before, "score_after": placer.ratsnest(parts), "unplaced": unplaced,
            "spacing": pl.spacing, "fill_ratio": round(pl.fill, 3), "roles": roles, "notes": pl.notes}


VARIANTS = [("baseline", {}), ("main connector on the long edge", {"main_edge": "top"}), ("tighter spacing", {"spacing_step": -1}),
            ("looser spacing", {"spacing_step": 1}), ("main IC rotated", {"mcu_rot": 90}),
            ("main IC toward the connector", {"mcu_pull": 0.33})]


def auto_place(snapshot: dict, refs=None, keep=None, spacing=None, edge_clearance: float = 0.5, grid: float = 0.5,
               candidates: int = 6, budget_s: float = 20.0, rules=None, overhang=None, src=None) -> dict:
    """Best of up to ``candidates`` structured placements (time-budgeted; one when refs=[...] is given).
    Returns the same dict as ``place.auto_place`` plus 'tried' [{variant, score, ratsnest}], 'placement_score'
    and 'notes' (flush connectors, clamped overhang, decap_far)."""
    from ..review import elegance
    ol = snapshot.get("outline")
    if not ol:
        raise ValueError("no outline")
    src = src if src is not None else ol.get("src")
    x0, y0, x1, y1 = ol["bbox"]
    diag = math.hypot(x1 - x0, y1 - y0) or 1.0
    # mounting holes that come from the schematic get their corner seats before anything is placed
    # around them (the placer treats every hole as a fixed obstacle)
    snapshot = dict(snapshot, footprints=list(snapshot["footprints"]))
    seated = holesmod.seat(snapshot, refs, keep)
    variants = list(VARIANTS)
    if refs or candidates <= 1:
        variants = variants[:1]
    elif x1 - x0 < y1 - y0:        # portrait board: the long edge is the left one
        variants[1] = ("main connector on the short edge", {"main_edge": "top"})
    runs, tried = [], []
    base_spacing, mcu_free = (None, 0), True
    t0 = time.time()
    n = 1
    for i, (name, v) in enumerate(variants):
        if i >= min(n, len(variants)) or (i and time.time() - t0 > budget_s):
            break
        if v.get("spacing_step") and (spacing is not None or base_spacing in ((0.3, -1), (1.5, 1))
                                      or (base_spacing[0], v["spacing_step"]) in ((0.3, -1), (1.5, 1))):
            n += 1            # nothing to vary (spacing fixed by the caller or already at the end of the ladder)
            continue
        if ("mcu_rot" in v or "mcu_pull" in v) and not mcu_free:
            n += 1            # the main IC is fixed, absent, or an RF module that belongs on an edge
            continue
        t1 = time.time()
        parts, pl, before = _run_once(snapshot, refs, keep, spacing, edge_clearance, grid, v, rules, overhang)
        dt = time.time() - t1
        if i == 0:
            n = max(1, min(candidates, len(variants), int(budget_s / max(dt, 1e-3))))
            base_spacing = (pl.spacing, 0)
            mcu_free = pl.mcu is not None and not pl.mcu.antenna and not getattr(pl.mcu, "was_fixed", False)
        res = _result(parts, pl, before)
        ps = elegance.placement_score([p for p in parts if not p.ref.startswith("#OBST")], ol, src=src, classified=True)
        ov = _overlap_count(parts)
        tried.append({"variant": name, "score": ps["score"], "ratsnest": res["score_after"],
                      **({"unplaced": len(res["unplaced"])} if res["unplaced"] else {})})
        runs.append((ps["score"] - 25.0 * len(res["unplaced"]) - 25.0 * ov, res, ps, name))
    # rank by placement score; wirelength breaks near-ties (10 % more ratsnest costs one point)
    short = min(r[1]["score_after"] for r in runs) or 1.0
    best = None
    for sc, res, ps, name in runs:
        rank = sc - 10.0 * (res["score_after"] / short - 1.0)
        if best is None or rank > best[0] + 1e-9:
            best = (rank, res, ps, name)
    _, res, ps, name = best
    if seated:
        res["moves"] = seated + res["moves"]
        res["notes"] = dict(res.get("notes") or {}, holes_seated=[m["ref"] for m in seated])
    res["tried"] = tried
    res["variant"] = name
    res["placement_score"] = ps["score"]
    res["seconds"] = round(time.time() - t0, 2)
    return res


def _overlap_count(parts) -> int:
    n = 0
    live = [p for p in parts if not p.ref.startswith("#OBST")]
    rects = [p.rect() for p in live]
    for i in range(len(live)):
        a = rects[i]
        for j in range(i + 1, len(live)):
            b = rects[j]
            if live[i].side != live[j].side and not (live[i].fixed or live[j].fixed):
                continue
            if a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]:
                n += 1
    return n


# ── board size ────────────────────────────────────────────────────────────
def _is_hole(d) -> bool:
    return holesmod.is_hole_fp(d)


def _is_connector(d) -> bool:
    import re
    pre = re.sub(r"\d.*$", "", d.get("ref") or "").upper()
    fpl = (d.get("footprint") or "").lower()
    return pre in ("J", "CN", "USB", "P", "X") and len(d.get("pad_list") or []) >= 2 and         (pre in ("J", "CN", "USB") or any(k in fpl for k in ("conn", "usb", "header", "jst", "terminal")))


def auto_size(snapshot: dict, aspects=(1.25, 1.6), margin: float = 0.05, hole_inset: float = 3.5, origin=None,
              width=None, height=None, budget_s: float = 20.0, edge_clearance: float = 0.5, util: float = 0.52,
              new_holes: float | None = None) -> dict:
    """Smallest comfortable rectangle for the parts: start where the part courtyards fill ``util`` of
    the board (mounting holes are obstacles, not part of that sum), verify with one placement per
    aspect, grow 8 % when something does not fit (at most 3 times per aspect), then add ``margin``
    per side and round up to whole millimetres (about 0.45 utilisation in the end).

    -> {'width', 'height', 'origin', 'tried': [...], 'seconds'} or {'failed': reason, 'tried': [...]}.
    Mounting holes already on the board are assumed to move to the corners (``hole_inset``).
    A fixed ``width`` or ``height`` makes the search one-dimensional."""
    t0 = time.time()
    fps = snapshot["footprints"]
    comps = [d for d in fps if not _is_hole(d)]
    sch_holes = holesmod.schematic_holes(fps)
    holes = [d for d in fps if _is_hole(d) and ("board_only" in (d.get("flags") or []) or d in sch_holes)]
    if not comps:
        return {"failed": "the board has no parts to size for", "tried": []}

    def area_of(d):
        lb = d.get("lbbox") or d.get("lbody") or [-1, -1, 1, 1]
        return max(lb[2] - lb[0], 0.5) * max(lb[3] - lb[1], 0.5)

    def dims(d):
        lb = d.get("lbbox") or d.get("lbody") or [-1, -1, 1, 1]
        return lb[2] - lb[0], lb[3] - lb[1]
    keep = (new_holes + 3.2) if (new_holes and not holes) else 0.0      # side of the keep-out square of a hole to be added
    a_parts = sum(area_of(d) for d in comps)
    longest = max(max(dims(d)) for d in comps)
    second = max(min(dims(d)) for d in comps)
    # a long connector must fit along an edge between the corner holes (else it ends up mid-board)
    hole_keep = max((max(dims(d)) for d in holes), default=keep)
    conn = [max(dims(d)) for d in comps if _is_connector(d)]
    edge_need = (max(conn) + 2 * (hole_inset + hole_keep / 2 + 1.0)) if conn and (holes or keep) else (max(conn) + 2.0 if conn else 0.0)
    longest = max(longest, edge_need - 2)
    a0 = a_parts / util
    hole_area = sum(area_of(d) for d in holes) + 4 * keep * keep
    ox, oy = origin or (snapshot["outline"]["bbox"][:2] if snapshot.get("outline") else (100.0, 100.0))
    tried, fits = [], []
    aspects = list(aspects)
    if width or height:
        aspects = [None]
    first_dt = None
    for asp in aspects:
        area = a0
        for attempt in range(4 if asp is not None else 8):     # one free side: keep growing it
            if time.time() - t0 >= budget_s:
                break
            if asp is None:
                w = float(width) if width else max(_up(area / float(height)), _up(longest + 2))
                h = float(height) if height else max(_up(area / float(width)), _up(second + 2))
            else:
                h = max(_up(math.sqrt(area / asp)), _up(second + 2))
                w = max(_up(math.sqrt(area / asp) * asp), _up(longest + 2))
            if 2 * hole_inset + 4 > min(w, h) and holes:
                h = max(h, 2 * hole_inset + 4)
                w = max(w, 2 * hole_inset + 4)
            if keep:
                h = max(h, 2 * hole_inset + 4)
                w = max(w, 2 * hole_inset + 4)
            trial = _trial_snapshot(snapshot, ox, oy, w, h, holes, hole_inset, keep)
            t1 = time.time()
            parts, pl, _ = _run_once(trial, None, None, None, edge_clearance, 0.5, {}, None, None)
            dt = time.time() - t1
            first_dt = first_dt or dt
            res_unplaced = sorted(set(pl.unplaced_refs))
            ov = _overlap_count(parts)
            rs = placer.ratsnest(parts)
            row = {"size": [w, h], "ratsnest": rs, "seconds": round(dt, 2)}
            off_edge = [p.ref for p in parts if p.role == "connector" and not p.fixed and not getattr(p, "edge", None)]
            if off_edge and attempt < 2:
                row["connectors_off_edge"] = off_edge
            if res_unplaced or ov or (off_edge and attempt < 2):
                row["fits"] = False
                tried.append(row)
                # first retry: the corner holes get their own area on top; after that grow by 8 %
                area = (area + hole_area) if (attempt == 0 and hole_area) else area * (1.08 if asp is not None else 1.15)
                if width and height:
                    break
                continue
            row["fits"] = True
            tried.append(row)
            from ..review import elegance
            ps = elegance.placement_score([p for p in parts if not p.ref.startswith("#OBST")], trial["outline"], src="auto",
                                          classified=True)["score"]
            row["score"] = ps
            fits.append((w * h * (1 + rs / 1000.0), w, h, ps))
            break
        if first_dt and first_dt > 8.0:
            break            # slow board: one aspect is all the budget allows
    if not fits:
        return {"failed": "no rectangle up to %.0f x %.0f mm could place every part" % (
            max((r["size"][0] for r in tried), default=0), max((r["size"][1] for r in tried), default=0)),
            "tried": tried, "seconds": round(time.time() - t0, 2)}
    # smallest area x wirelength wins, unless another aspect uses its area clearly more evenly
    # (one placement-score point is worth 1 % of area)
    best_cost = min(f[0] for f in fits)
    _, w, h, _ = min(fits, key=lambda f: f[0] / best_cost - f[3] / 100.0)
    if not width:
        w = math.ceil(w * (1 + margin) - 1e-9)
    if not height:
        h = math.ceil(h * (1 + margin) - 1e-9)
    return {"width": float(w), "height": float(h), "origin": [ox, oy], "tried": tried, "parts_area": round(a_parts, 1),
            "utilisation": round(a_parts / (w * h), 3), "seconds": round(time.time() - t0, 2)}


def _up(v, g=0.5):
    return math.ceil(v / g - 1e-9) * g


def corner_holes(ox, oy, w, h, inset):
    return [[ox + inset, oy + inset], [ox + w - inset, oy + inset], [ox + w - inset, oy + h - inset], [ox + inset, oy + h - inset]]


def _trial_snapshot(snapshot, ox, oy, w, h, holes, inset, keep=0.0):
    """A copy of the snapshot with a w x h rectangle outline and the board's mounting holes at its corners."""
    pos = corner_holes(ox, oy, w, h, inset)
    if len(holes) > 4:           # more holes than corners: edge midpoints next
        pos = [list(p) for p in holesmod.seats(len(holes), [ox, oy, ox + w, oy + h], inset)]
    fps = []
    k = 0
    for d in snapshot["footprints"]:
        if d in holes and k < len(pos):
            d = dict(d, x=pos[k][0], y=pos[k][1], rot=0)
            k += 1
        fps.append(d)
    ol = {"bbox": [ox, oy, ox + w, oy + h], "width": w, "height": h, "closed": True, "area": w * h,
          "polygon": [[ox, oy], [ox + w, oy], [ox + w, oy + h], [ox, oy + h]]}
    obst = [{"kind": "hole", "bbox": [x - keep / 2, y - keep / 2, x + keep / 2, y + keep / 2]} for x, y in pos] if keep else []
    return {"footprints": fps, "outline": ol, "obstacles": obst, "track_segs": []}
