"""Is a reference designator readable where it sits? One definition, used twice.

Pure stdlib and import-free on purpose: the elegance score (server process) and the silkscreen
tidy pass (pcbnew worker, loaded by file path) must agree on what a "bad" reference is, so both
call ``problems`` on the same data (the worker op ``elegance_extra`` produces it, millimetres):

    refs   [{ref, visible, side, box [x0,y0,x1,y1], h, angle, locked}]
    courts {ref: {box, side}}
    pads   [[x0, y0, x1, y1, mask, ref]]            mask 1 = top, 2 = bottom, 3 = both
    silk   [["s", ref, side, x1, y1, x2, y2, w] | ["c", ref, side, cx, cy, r, w]]
    vias   [[x, y, d]]
    edge   {bbox, polygon, holes}

A reference is bad when it overlaps a pad on its side (``pad``), another reference (``text``),
another footprint's silk artwork (``silk``), has its centre inside another part's courtyard
(``under_part``), leaves the board (``edge``), is under 0.8 mm tall (``small``) or is not at 0/90
degrees (``angle``).
"""
import math

MIN_HEIGHT = 0.8


def hit(a, b, g=0.0):
    return a[0] < b[2] + g and a[2] + g > b[0] and a[1] < b[3] + g and a[3] + g > b[1]


def seg_hits_rect(x1, y1, x2, y2, pad, r):
    """Segment comes within ``pad`` of rect r (Liang-Barsky clip on the grown rect)."""
    rx0, ry0, rx1, ry1 = r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p_, q_ in ((-dx, x1 - rx0), (dx, rx1 - x1), (-dy, y1 - ry0), (dy, ry1 - y1)):
        if abs(p_) < 1e-12:
            if q_ < 0:
                return False
            continue
        t = q_ / p_
        if p_ < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def ring_hits_rect(cx, cy, rad, w, r):
    """The stroke of a circle (radius rad, line width w) touches rect r. A rect wholly inside the ring
    does not count: text inside a large silk circle is readable."""
    nx, ny = min(max(cx, r[0]), r[2]), min(max(cy, r[1]), r[3])
    dmin = math.hypot(nx - cx, ny - cy)
    dmax = max(math.hypot(x - cx, y - cy) for x in (r[0], r[2]) for y in (r[1], r[3]))
    return dmin <= rad + w / 2 and dmax >= rad - w / 2


def pip(poly, x, y):
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            c = not c
    return c


class Scene:
    """Obstacles per side, built once."""

    def __init__(self, extra):
        self.pads = {"top": [], "bottom": []}
        for p in extra.get("pads") or []:
            if p[4] & 1:
                self.pads["top"].append(p)
            if p[4] & 2:
                self.pads["bottom"].append(p)
        self.silk = {"top": [], "bottom": []}
        for s in extra.get("silk") or []:
            self.silk[s[2]].append(s)
        self.courts = extra.get("courts") or {}
        self.texts = {}          # ref -> (side, box) of every visible reference
        for r in extra.get("refs") or []:
            if r.get("visible"):
                self.texts[r["ref"]] = (r["side"], list(r["box"]))
        self.vias = extra.get("vias") or []
        e = extra.get("edge") or {}
        self.bbox = e.get("bbox")
        self.poly = e.get("polygon")
        self.holes = e.get("holes") or []

    def inside_board(self, box, margin=0.0):
        if not self.bbox:
            return True
        b = self.bbox
        if box[0] < b[0] + margin or box[1] < b[1] + margin or box[2] > b[2] - margin or box[3] > b[3] - margin:
            return False
        if self.poly and len(self.poly) > 4:
            for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])):
                if not pip(self.poly, x, y):
                    return False
        for h in self.holes:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if pip(h, cx, cy):
                return False
        return True


def problems(scene, ref, box, side="top", height=1.0, angle=0.0, texts=None):
    """Reasons this reference box is bad (empty list = fine). ``texts`` overrides the other references'
    boxes ({ref: (side, box)}) while a tidy pass is moving them."""
    out = []
    for p in scene.pads[side]:
        if hit(box, p, 0.02):
            out.append("pad")
            break
    for r2, (s2, b2) in (scene.texts if texts is None else texts).items():
        if r2 != ref and s2 == side and hit(box, b2, 0.05):
            out.append("text")
            break
    for s in scene.silk[side]:
        if s[1] == ref:
            continue
        if (s[0] == "s" and seg_hits_rect(s[3], s[4], s[5], s[6], s[7] / 2 + 0.03, box)) or \
                (s[0] == "c" and ring_hits_rect(s[3], s[4], s[5], s[6] + 0.06, box)):
            out.append("silk")
            break
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for r2, c in scene.courts.items():
        if r2 != ref and c["side"] == side and c["box"][0] < cx < c["box"][2] and c["box"][1] < cy < c["box"][3]:
            out.append("under_part")
            break
    if not scene.inside_board(box):
        out.append("edge")
    if height < MIN_HEIGHT - 1e-6:
        out.append("small")
    if abs(((angle + 45) % 90) - 45) > 0.5:
        out.append("angle")
    return out


def own_silk_hits(scene, ref, box, side="top"):
    n = 0
    for s in scene.silk[side]:
        if s[1] != ref:
            continue
        if (s[0] == "s" and seg_hits_rect(s[3], s[4], s[5], s[6], s[7] / 2 + 0.03, box)) or \
                (s[0] == "c" and ring_hits_rect(s[3], s[4], s[5], s[6] + 0.06, box)):
            n += 1
    return n


def via_hits(scene, box):
    return sum(1 for v in scene.vias if hit(box, [v[0] - v[2] / 2, v[1] - v[2] / 2, v[0] + v[2] / 2, v[1] + v[2] / 2]))


def evaluate(extra):
    """-> {'visible': n, 'hidden': n, 'bad': {ref: [reasons]}} over every reference on the board."""
    sc = Scene(extra)
    bad, vis, hid = {}, 0, 0
    for r in extra.get("refs") or []:
        if r.get("board_only"):
            continue          # mounting holes and other board-only items carry no designator by design
        if not r.get("visible"):
            hid += 1
            continue
        vis += 1
        pr = problems(sc, r["ref"], r["box"], r["side"], r.get("h", 1.0), r.get("angle", 0.0))
        if pr:
            bad[r["ref"]] = pr
    return {"visible": vis, "hidden": hid, "bad": bad}
