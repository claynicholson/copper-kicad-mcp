"""Pure coordinate math shared by every focus channel.

Everything is in KiCad millimetres, document coordinates (X right, Y down):
schematic sheet mm and board mm — exactly the user units of KiCad's own SVG
export, which is what makes the canvas pixel-true.

``ViewMap`` is the single screen <-> mm transform. ``static/canvas.js`` carries
a line-for-line port (``mmToScreen`` / ``screenToMm``); tests/test_focus.py
round-trips real component positions through this class and the browser test
does the same through the page.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

Box = list  # [x0, y0, x1, y1] mm, x0<=x1, y0<=y1


def norm_box(b) -> list[float]:
    x0, y0, x1, y1 = (float(v) for v in b)
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def box_intersects(a, b) -> bool:
    """Inclusive: boxes that merely touch count (a drag that grazes a part selects it)."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def box_contains(outer, inner) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def point_in_box(p, b) -> bool:
    return b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3]


def box_area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def box_center(b) -> list[float]:
    return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]


def union(boxes) -> list[float] | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def grow(b, m: float) -> list[float]:
    return [b[0] - m, b[1] - m, b[2] + m, b[3] + m]


def dist_point_box(p, b) -> float:
    """0 inside the box, else the distance to its edge."""
    dx = max(b[0] - p[0], 0.0, p[0] - b[2])
    dy = max(b[1] - p[1], 0.0, p[1] - b[3])
    return math.hypot(dx, dy)


def dist_point_seg(p, a, b) -> float:
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 1e-12:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def seg_intersects_box(a, b, box) -> bool:
    """Liang–Barsky: does segment a-b touch the (inclusive) box?"""
    x0, y0, x1, y1 = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    return True


def r3(v: float) -> float:
    """mm rounded to 1 µm — and never '-0.0'."""
    v = round(float(v), 3)
    return 0.0 if v == 0 else v


def rbox(b) -> list[float]:
    return [r3(v) for v in b]


_NAT = re.compile(r"(\d+)")


def natural_key(s: str):
    """'C2' < 'C10', 'U1' < 'U1A'."""
    return [int(t) if t.isdigit() else t.lower() for t in _NAT.split(str(s))]


@dataclass
class ViewMap:
    """Screen px <-> KiCad mm for one document shown in a viewport.

    view   [x, y, w, h] — the SVG viewBox, in mm (KiCad SVG user units ARE mm)
    scale  px per mm
    ox, oy screen px of the image's top-left corner
    mirror True for the PCB bottom view (image flipped left-right about its centre)
    """
    view: tuple
    scale: float = 1.0
    ox: float = 0.0
    oy: float = 0.0
    mirror: bool = False

    def mm_to_screen(self, x: float, y: float) -> tuple[float, float]:
        vx, vy, vw, _vh = self.view
        lx = x - vx
        if self.mirror:
            lx = vw - lx
        return self.ox + lx * self.scale, self.oy + (y - vy) * self.scale

    def screen_to_mm(self, sx: float, sy: float) -> tuple[float, float]:
        vx, vy, vw, _vh = self.view
        lx = (sx - self.ox) / self.scale
        if self.mirror:
            lx = vw - lx
        return vx + lx, vy + (sy - self.oy) / self.scale

    def fit(self, width: float, height: float, margin: float = 24.0) -> "ViewMap":
        """Scale/offset that centres the whole view in a width x height px viewport."""
        _vx, _vy, vw, vh = self.view
        s = max(1e-6, min((width - 2 * margin) / vw, (height - 2 * margin) / vh))
        return ViewMap(self.view, s, (width - vw * s) / 2, (height - vh * s) / 2, self.mirror)

    def zoom_at(self, sx: float, sy: float, factor: float) -> "ViewMap":
        """Zoom keeping the mm point under screen (sx, sy) fixed."""
        s = self.scale * factor
        return ViewMap(self.view, s, sx - (sx - self.ox) * factor, sy - (sy - self.oy) * factor, self.mirror)
