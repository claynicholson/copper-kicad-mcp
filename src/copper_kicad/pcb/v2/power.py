"""S1 power: pour dog-bone via sites, thermal-spoke capacity, decoupling stubs, local-only necking (design §4.4).

Runs before any signal is routed and reserves what power needs:

* **Thermal spokes** — every pad of a poured net that sits inside its pour on a layer gets 4 spoke corridors
  as *soft* oracle shapes (rule ``'thermal'``): copper of other nets may cross them, at a price the search sets.
* **Decoupling** — the capacitor-pad -> IC-pin connections S0 found (``ctx.history['classify']['decaps']``)
  are routed first with a pattern router (straight / L / Z), at power width.
* **Dog-bones** — every SMD pad of a poured net that its pour does not reach gets a short stub and a through
  via inside the pour, as ``protected`` pieces (``stage='power'``, ``conn=-1``) — when the via reaches the pour
  on a layer signals do not route on (a plane layer). Where the pour shares its layer with signals the site is
  only reserved *softly* (a ``'reserve'`` disc): copper placed there now could wall signals in, or be cut off
  from the pour by the tracks still to come; the pours pass of S7 places it once the signals are in.
  Candidates: 8 directions x 3 stub lengths (+ plan ``reserve`` sites); score = stub length + 2 x congestion
  estimate + escape-lane / pad-pair-cut penalty + pour-boundary penalty (+ soft shapes of other nets, + a pour
  that is no plane); the cheapest candidate that the oracle accepts wins. A site that would close the exit
  channel of a neighbouring pad of another net is not used (reservations must not wall signals in); such a pad
  gets a stub to a neighbouring dog-bone via of its net when one is near. A pad without a usable site is
  reported as ``power_unreserved`` with the reason (the pours pass of S7 retries it, after the signals).

**Local-only necking** (``neck_split``): a segment that does not fit at its full width is cut into spans; only
the spans that are illegal at full width are narrowed (to the widest legal width, never below the neck width),
and carry ``target_w`` = the full width. A net is never narrowed over its whole length because one spot is tight.

Everything is judged by the oracle; nothing here uses a grid, a clock or randomness.
"""
from __future__ import annotations

import math

from ..guided import geom
from ..guided.geom import Shape
from . import classify
from .types import CHARGE_PATTERN, ORACLE_CHECKS_PER_UNIT, Budget, ConnV2, Ctx, Piece, StageReport, q

INF = float("inf")
_S2 = math.sqrt(2.0)

# the 8 stub directions in canonical order (axial first), and the 3 stub lengths
DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))
STUB_EXTRA = (0.0, 0.3, 0.8)          # mm added to "pad edge + via radius + clearance"
RESERVE_REACH = 6.0                   # mm: a plan reserve site serves the nearest pad of its net within this distance
SHARE_REACH = 5.0                     # mm of stub: a pad without a site of its own may use a neighbour's dog-bone via

LANE_LENGTH = 2.0                     # mm: how far the exit lane of a pad reaches beyond the pad edge
LANE_TEST = 0.45                      # mm beyond the pad edge the straight way out must stay legal (open_lanes)
RISK_DOGBONES = True                  # S1: dog-bones also for pour pads in fine-pitch rows reached only on a signal layer
DECAP_LANE_CHECK = False              # S1: refuse a decoupling track that shuts a neighbouring pad's way out (measured: no gain)
LANE_SLACK = 0.2                      # mm of free track-centre positions an exit channel must keep to count as open
LANE_PENALTY = 3.0                    # mm-equivalent: the via would sit in another pad's exit lane
CUT_PENALTY = 3.0                     # ... or in the gap between two neighbouring pads
SOFT_PENALTY = 2.0                    # ... or on a thermal spoke / reserved site of another net (per hit, max 2)
ISLAND_PENALTY = 1.0                  # ... or on the boundary of the pour
SHARED_LAYER_PENALTY = 2.0            # ... or reach the pour only on layers that signals route on as well
SOFT_ON_SHARED_LAYERS = True          # S1 reserves such sites as soft discs instead of copper (False: copper at once)
ISLAND_MARGIN = 0.2                   # mm of pour wanted around the via copper
CONGESTION_MM = 0.1                   # mm per pad in the 1 mm cell of the site (half that for the 8 cells around)

SPOKE_WIDTH = 0.5                     # mm, KiCad's default thermal spoke width (the extract carries no zone value)
SPOKE_GAP = 0.5                       # mm, KiCad's default thermal relief gap
SPOKE_REACH = 0.3                     # mm the corridor continues into the pour beyond the gap

NECK_STEP = 0.01                      # mm: resolution of the widest-legal-width search
NECK_MIN_SPAN = 0.4                   # mm: spans are not bisected below this (leaf spans are 0.2 .. 0.4 mm)


# ── per-board caches ──────────────────────────────────────────────────────
def _cache(board) -> dict:
    c = board.__dict__.get("_v2_power")
    if c is None:
        c = board.__dict__["_v2_power"] = {}
    return c


class _Rings:
    """Even-odd point-in-polygon and distance-to-boundary over a set of rings, with bucketed edges so that a
    query costs the edges near the point, not the whole (possibly 20 000-vertex) fill."""
    CELL = 1.0

    def __init__(self, rings):
        self.rows = {}
        self.cells = {}
        self.edges = 0
        fl = math.floor
        c = self.CELL
        for pts in rings:
            n = len(pts)
            if n < 3:
                continue
            x1, y1 = pts[n - 1]
            for x2, y2 in pts:
                e = (x1, y1, x2, y2)
                self.edges += 1
                r0, r1 = int(fl(min(y1, y2) / c)), int(fl(max(y1, y2) / c))
                c0, c1 = int(fl(min(x1, x2) / c)), int(fl(max(x1, x2) / c))
                if y1 != y2:
                    for r in range(r0, r1 + 1):
                        self.rows.setdefault(r, []).append(e)
                if (r1 - r0 + 1) * (c1 - c0 + 1) <= 4:
                    for r in range(r0, r1 + 1):
                        for k in range(c0, c1 + 1):
                            self.cells.setdefault((k, r), []).append(e)
                else:                       # a long edge: only the cells it really passes
                    steps = max(r1 - r0, c1 - c0) * 2 + 1
                    seen = set()
                    for i in range(steps + 1):
                        t = i / steps
                        k = (int(fl((x1 + (x2 - x1) * t) / c)), int(fl((y1 + (y2 - y1) * t) / c)))
                        for kk in ((k[0], k[1]), (k[0] + 1, k[1]), (k[0] - 1, k[1]), (k[0], k[1] + 1), (k[0], k[1] - 1)):
                            if kk not in seen:
                                seen.add(kk)
                                self.cells.setdefault(kk, []).append(e)
                x1, y1 = x2, y2

    def contains(self, x: float, y: float) -> bool:
        inside = False
        for x1, y1, x2, y2 in self.rows.get(int(math.floor(y / self.CELL)), ()):
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
        return inside

    def edge_dist(self, x: float, y: float, reach: float) -> float:
        """Distance to the nearest boundary edge; exact when it is <= reach, else any value > reach (or inf)."""
        c = self.CELL
        fl = math.floor
        best = INF
        for r in range(int(fl((y - reach) / c)), int(fl((y + reach) / c)) + 1):
            for k in range(int(fl((x - reach) / c)), int(fl((x + reach) / c)) + 1):
                for x1, y1, x2, y2 in self.cells.get((k, r), ()):
                    d = geom.pt_seg_dist(x, y, x1, y1, x2, y2)
                    if d < best:
                        best = d
        return best


def _pours(board) -> dict:
    """{net name: {layer: [{'outline': _Rings, 'fill': _Rings | None, 'gap': mm, 'spoke': mm}]}}.
    ``fill`` is None when the zone is not filled in the extract (then the outline is all that is known)."""
    c = _cache(board)
    if "pours" not in c:
        d = {}
        for z in (getattr(board, "ex", None) or {}).get("zones") or []:
            if z.get("rule_area") or z.get("teardrop") or not z.get("net") or z["net"] not in board.nets:
                continue
            rings = []
            for p in z.get("polys") or []:
                if len(p.get("o") or []) >= 3:
                    rings.append([tuple(v) for v in p["o"]])
                    rings += [[tuple(v) for v in h] for h in p.get("h") or [] if len(h) >= 3]
            if not rings:
                continue
            outline = _Rings(rings)
            fills = z.get("fills") or {}
            for l in z["layers"]:
                fill = None
                if z.get("filled") or fills:
                    fr = []
                    for p in fills.get(str(l)) or []:
                        if len(p.get("o") or []) >= 3:
                            fr.append([tuple(v) for v in p["o"]])
                            fr += [[tuple(v) for v in h] for h in p.get("h") or [] if len(h) >= 3]
                    fill = _Rings(fr)
                d.setdefault(z["net"], {}).setdefault(l, []).append({
                    "outline": outline, "fill": fill,
                    "gap": float(z.get("thermal_gap") or SPOKE_GAP),
                    "spoke": float(z.get("thermal_width") or z.get("spoke_width") or SPOKE_WIDTH)})
        c["pours"] = d
    return c["pours"]


def _zone_at(board, net_name: str, layer: int, x: float, y: float):
    for z in _pours(board).get(net_name, {}).get(layer, ()):
        if z["outline"].contains(x, y):
            return z
    return None


def pour_reaches(board, pad) -> bool:
    """Does the pour of the pad's net reach the pad on one of the pad's layers? Filled zones: the pad's
    connectivity cluster owns a fill island there. Unfilled zones: the pad centre is inside the zone outline."""
    net = board.net_by_id.get(pad.net)
    if net is None or not net.has_pour:
        return False
    cl = board.clusters.get(net.name, {}).get(pad.cluster)
    for l in pad.layers:
        for z in _pours(board).get(net.name, {}).get(l, ()):
            if not z["outline"].contains(pad.x, pad.y):
                continue
            if z["fill"] is None:
                return True
            if cl is not None and any(il == l for il, _pts in cl["islands"]):
                return True
            if z["fill"].contains(pad.x, pad.y):
                return True
    return False


def _cluster_reached(board, net, cid: int) -> bool:
    cl = board.clusters.get(net.name, {}).get(cid)
    if cl is None:
        return False
    if cl["islands"]:
        return True
    if any(pour_reaches(board, p) for p in cl["pads"]):
        return True
    pours = _pours(board).get(net.name, {})
    for v in cl["vias"]:                       # kept via inside a pour that is not filled yet
        for l in range(v[5], v[6] + 1):
            for z in pours.get(l, ()):
                if z["fill"] is None and z["outline"].contains(v[1], v[2]):
                    return True
    return False


def needs_dogbone(board, pad) -> bool:
    """An SMD pad of a poured net whose cluster the pour does not reach."""
    net = board.net_by_id.get(pad.net)
    if net is None or not net.has_pour or pad.kind not in ("smd", "conn") or not pad.layers:
        return False
    return not _cluster_reached(board, net, pad.cluster)


def pour_at_risk(ctx: Ctx, pad) -> bool:
    """A pad the pour reaches today, on a layer signals route on, in a fine-pitch row — while its net also has
    a plane (a pour on a layer signals do not use). The escapes of its neighbours will go through that pour
    contact, and afterwards there is no room left at the pad to reach the plane (measured: the open ground pads
    of a 0.4 mm board-to-board connector). Such a pad gets its dog-bone in S1 like a pad the pour misses.
    Fine pitch = two tracks of the board's default class do not fit between neighbouring pads of the row."""
    board = ctx.board
    net = board.net_by_id.get(pad.net)
    if net is None or not net.has_pour or pad.kind not in ("smd", "conn") or not pad.layers:
        return False
    sig = signal_layers(ctx)
    pours = _pours(board).get(net.name, {})
    if not any(l not in sig for l in pours) or any(l not in sig for l in pad.layers):
        return False
    if not pour_reaches(board, pad):
        return False
    c = _cache(board)
    rows = c.setdefault("risk_rows", {})
    key = (pad.ref, pad.name, q(pad.x), q(pad.y))
    if pad.ref not in rows:
        from . import fanout
        w = max(board.rules["default"].get("width") or 0.2, board.rules["track_width"])
        cl = board.default_clr
        fine = set()
        if not getattr(fanout, "STUB", False):
            for r in fanout.footprint_rows(board, pad.ref):
                if not r.grid and r.pitch < 2 * (w + cl) + r.pad_w - 1e-9:
                    fine.update((p.ref, p.name, q(p.x), q(p.y)) for p in r.pads)
        rows[pad.ref] = fine
    return key in rows[pad.ref]


# ── pad geometry ──────────────────────────────────────────────────────────
def _exit(pad, layer: int, ux: float, uy: float) -> float:
    """Distance from the pad centre to the pad's copper edge along the unit direction (ux, uy), mm."""
    best = 0.0
    for s in pad.shapes.get(layer, ()):
        if s.dist_pt(pad.x, pad.y) > 0:
            continue
        if s.kind == "box" and abs(s.g[0] - pad.x) < 1e-9 and abs(s.g[1] - pad.y) < 1e-9:
            _cx, _cy, ax, ay, ihw, ihh, r = s.g
            if ihw == 0 and ihh == 0:                    # a circle
                best = max(best, r)
                continue
            lx, ly = abs(ux * ax + uy * ay), abs(uy * ax - ux * ay)
            hi = min((ihw + r) / lx if lx > 1e-12 else INF, (ihh + r) / ly if ly > 1e-12 else INF)
            if r <= 0 or lx < 1e-9 or ly < 1e-9:         # exact for a rectangle and along the axes
                best = max(best, hi)
                continue
        else:
            bb = s.bbox
            hi = math.hypot(bb[2] - bb[0], bb[3] - bb[1]) + 0.01
        lo = 0.0
        for _ in range(40):
            mid = (lo + hi) / 2
            if s.dist_pt(pad.x + ux * mid, pad.y + uy * mid) <= 0:
                lo = mid
            else:
                hi = mid
        best = max(best, hi)
    return best


def _clear_dist(pad, layer: int, ux: float, uy: float, need: float) -> float:
    """Smallest distance from the pad centre along (ux, uy) at which a point is ``need`` away from the pad's
    copper (true distance, not along the ray): pad edge + need for a direction square to a straight edge."""
    shapes = pad.shapes.get(layer, ())
    lo = _exit(pad, layer, ux, uy)
    if not shapes or need <= 0:
        return lo
    if len(shapes) == 1 and shapes[0].kind == "box":
        _cx, _cy, ax, ay, ihw, ihh, _r = g = shapes[0].g
        if abs(g[0] - pad.x) < 1e-9 and abs(g[1] - pad.y) < 1e-9 and (
                (ihw == 0 and ihh == 0) or abs(ux * ax + uy * ay) < 1e-9 or abs(uy * ax - ux * ay) < 1e-9):
            return lo + need                 # a circle, or square to a flat side: exact without a search
        dist = shapes[0].dist_pt
    else:
        def dist(x, y):
            return min(s.dist_pt(x, y) for s in shapes)
    hi = lo + need
    for _ in range(8):
        if dist(pad.x + ux * hi, pad.y + uy * hi) >= need:
            break
        hi += need
    for _ in range(26):                      # 2 mm / 2^26: far below the 1 nm the result is rounded up to
        mid = (lo + hi) / 2
        if dist(pad.x + ux * mid, pad.y + uy * mid) >= need:
            hi = mid
        else:
            lo = mid
    return hi


def pad_key(board, pad) -> str:
    """A key that is unique per pad: the pad name ('U3.4'), or 'name@x,y' when the footprint has several pads of
    that number (thermal-pad segments, shield tabs)."""
    c = _cache(board)
    dup = c.get("dup")
    if dup is None:
        seen = set()
        dup = c["dup"] = set()
        for p in board.pads:
            (dup if p.name in seen else seen).add(p.name)
    return f"{pad.name}@{pad.x:.4f},{pad.y:.4f}" if pad.name in dup else pad.name


def pad_by_key(board, key: str):
    for p in board.pads:
        if p.name == key or (("@" in key) and pad_key(board, p) == key):
            return p
    return None


def _layer_of(pad) -> int:
    for l in pad.layers:
        if pad.shapes.get(l):
            return l
    return pad.layers[0] if pad.layers else 0


def _axes(pad, layer: int):
    """(ux, uy, half extent along u, half extent across) of the pad's main shape; world axes for a polygon."""
    best = None
    for s in pad.shapes.get(layer, ()):
        if s.kind == "box":
            _cx, _cy, ax, ay, ihw, ihh, r = s.g
            e = (ax, ay, ihw + r, ihh + r, ihw == 0 and ihh == 0)
        else:
            bb = s.bbox
            e = (1.0, 0.0, (bb[2] - bb[0]) / 2, (bb[3] - bb[1]) / 2, False)
        if best is None or e[2] * e[3] > best[2] * best[3]:
            best = e
    return best or (1.0, 0.0, 0.0, 0.0, True)


# ── candidates ────────────────────────────────────────────────────────────
def dogbone_candidates(pad, via: tuple[float, float], clearance: float) -> list[tuple]:
    """[(stub_end_xy, via_xy)] in canonical order: 3 stub lengths (pad edge + via radius + clearance, +0.3 mm,
    +0.8 mm; shortest first) x 8 directions (+x, +y, -x, -y, then the diagonals ++, -+, --, +- in board
    coordinates). "Pad edge + via radius + clearance" is the nearest point along the direction where the via
    copper is ``clearance`` away from the pad's copper (true distance: no via in or against its own pad). The
    stub runs from the pad centre straight to the via centre (``stub_end_xy == via_xy``), exactly axial or 45
    degrees; all coordinates are quantised to 1 nm."""
    layer = _layer_of(pad)
    r = via[0] / 2.0
    px, py = q(pad.x), q(pad.y)
    out, seen = [], set()
    for extra in STUB_EXTRA:
        for dx, dy in DIRS:
            if dx and dy:
                L = _clear_dist(pad, layer, dx / _S2, dy / _S2, r + clearance) + extra
                c = math.ceil(L / _S2 * 1e6 - 1e-6) / 1e6
                v = (q(px + dx * c), q(py + dy * c))
            else:
                L = _clear_dist(pad, layer, float(dx), float(dy), r + clearance) + extra
                c = math.ceil(L * 1e6 - 1e-6) / 1e6
                v = (q(px + dx * c), q(py + dy * c))
            if v not in seen:
                seen.add(v)
                out.append((v, v))
    return out


def _reserve_sites(ctx: Ctx, net_name: str) -> list[tuple]:
    return [at for name, at in _plan_reserve(ctx) if name == net_name]


def _plan_reserve(ctx: Ctx) -> list[tuple]:
    """Plan ``reserve`` sites as [(net name, (x, y))] (planx: {'at': (x, y), 'net': name}; raw plan: 'for')."""
    out = []
    for d in classify._hint(ctx.x, "reserve") or []:
        try:
            name = d.get("net") or d.get("for")
            out.append((name, (q(float(d["at"][0])), q(float(d["at"][1])))))
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            continue
    return out


# ── escape lanes, pad-pair cuts, congestion ───────────────────────────────
def _lanes(board) -> dict:
    """Bucketed exit lanes and pad-pair cuts of every footprint.
    {'lanes': {(cx, cy): [(box, net id, bbox, (pad name, px, py, ux, uy, exit, channel half width))]},
     'cuts': {(cx, cy): [(x1, y1, x2, y2, net a, net b)]}}
    The lane of a pad runs from its edge LANE_LENGTH outwards, as wide as the pad; its *channel* (what a track
    leaving the pad can really use) reaches half-way to the next pad of the footprint on the same layer. A part
    whose pads all sit on one line (an edge connector) has lanes on both sides."""
    c = _cache(board)
    if "lanes" in c:
        return c["lanes"]
    lanes, cuts = {}, {}
    cell = 2.0
    fl = math.floor
    by_ref = {}
    for p in board.pads:
        if p.layers:
            by_ref.setdefault(p.ref, []).append(p)

    def put(d, bb, item):
        for iy in range(int(fl(bb[1] / cell)), int(fl(bb[3] / cell)) + 1):
            for ix in range(int(fl(bb[0] / cell)), int(fl(bb[2] / cell)) + 1):
                d.setdefault((ix, iy), []).append(item)

    for ref in sorted(by_ref):
        pads = by_ref[ref]
        n = len(pads)
        mx = sum(p.x for p in pads) / n
        my = sum(p.y for p in pads) / n
        for i, p in enumerate(pads):
            net = board.net_by_id.get(p.net)
            if net is None or len(net.pads) < 2:
                continue
            layer = _layer_of(p)
            ax, ay, hu, hv, _round = _axes(p, layer)
            if n == 2:
                o = pads[1 - i]
                vx, vy = p.x - o.x, p.y - o.y
                L = math.hypot(vx, vy)
                if L < 1e-6:
                    continue
                dirs = [(vx / L, vy / L)]
            else:
                vx, vy = p.x - mx, p.y - my
                if max(hu, hv) >= 1.3 * min(hu, hv) and min(hu, hv) > 0:
                    ux, uy = (ax, ay) if hu > hv else (-ay, ax)       # along the pad's long axis ...
                    dot = ux * vx + uy * vy
                    if abs(dot) < 1e-6:                               # ... both ways when the part is one row
                        dirs = [(ux, uy), (-ux, -uy)]
                    else:                                             # ... away from the footprint centre
                        dirs = [(ux, uy) if dot > 0 else (-ux, -uy)]
                else:
                    if abs(vx) < 1e-6 and abs(vy) < 1e-6:
                        continue
                    dirs = [(math.copysign(1.0, vx), 0.0) if abs(vx) >= abs(vy) else (0.0, math.copysign(1.0, vy))]
            near = [math.hypot(o.x - p.x, o.y - p.y) for o in pads if o is not p and set(o.layers) & set(p.layers)]
            near = [d for d in near if d > 1e-6]
            for ux, uy in dirs:
                e = _exit(p, layer, ux, uy)
                half = _exit(p, layer, -uy, ux)
                if half <= 0:
                    continue
                chan = max(half, min(min(near) / 2, half + 1.0)) if near else half + 1.0
                g = (p.x + ux * (e + LANE_LENGTH / 2), p.y + uy * (e + LANE_LENGTH / 2), ux, uy, LANE_LENGTH / 2, half, 0.0)
                bb = geom.box_bbox(g, 3.0)
                put(lanes, bb, (g, p.net, bb, (p.name, p.x, p.y, ux, uy, e, chan)))
        if n >= 2:
            nb = classify._row_neighbours(pads)
            for i in range(n):
                for j in nb[i]:
                    if j > i:
                        a, b = pads[i], pads[j]
                        bb = (min(a.x, b.x) - 1.0, min(a.y, b.y) - 1.0, max(a.x, b.x) + 1.0, max(a.y, b.y) + 1.0)
                        put(cuts, bb, (a.x, a.y, b.x, b.y, a.net, b.net))
    c["lanes"] = {"lanes": lanes, "cuts": cuts, "cell": cell}
    return c["lanes"]


def lane_penalty(board, pad, x: float, y: float, reach: float) -> float:
    """Penalty (mm-equivalent) of a via whose copper + clearance (radius ``reach``) around (x, y) lies in the
    exit lane of a pad of another net, or in the gap between two neighbouring pads of a footprint."""
    L = _lanes(board)
    key = (int(math.floor(x / L["cell"])), int(math.floor(y / L["cell"])))
    pen = 0.0
    for g, nid, bb, _lane in L["lanes"].get(key, ()):
        if nid == pad.net or x < bb[0] or x > bb[2] or y < bb[1] or y > bb[3]:
            continue
        if geom.box_dist(g, x, y) < reach:
            pen += LANE_PENALTY
            break
    for x1, y1, x2, y2, na, nb in L["cuts"].get(key, ()):
        if na == pad.net and nb == pad.net:
            continue
        if geom.pt_seg_dist(x, y, x1, y1, x2, y2) < reach:
            pen += CUT_PENALTY
            break
    return pen


def lane_blocked(board, pad, x: float, y: float, radius: float, others=()) -> str | None:
    """Would a via of ``radius`` at (x, y) close the exit channel of a pad of another net? -> that pad's name.

    Across the channel of a foreign pad (see ``_lanes``) a track centre must stay ``radius + clearance + width / 2``
    (clearance and width of that pad's net) away from the via and from every via in ``others`` ([(x, y, radius)],
    the dog-bones already reserved) that stands at about the same depth of the lane. When less than LANE_SLACK
    (0.2 mm) of positions is left the channel is closed in practice: the reservation would wall that pad in."""
    L = _lanes(board)
    key = (int(math.floor(x / L["cell"])), int(math.floor(y / L["cell"])))
    for _g, nid, bb, (name, px, py, ux, uy, e, chan) in L["lanes"].get(key, ()):
        if nid == pad.net or x < bb[0] or x > bb[2] or y < bb[1] or y > bb[3]:
            continue
        net = board.net_by_id.get(nid)
        if net is None:
            continue
        margin = net.clearance + net.width / 2
        a = (x - px) * ux + (y - py) * uy
        c = (y - py) * ux - (x - px) * uy
        if not (e - radius - margin < a < e + LANE_LENGTH + radius + margin) or abs(c) - radius - margin >= chan:
            continue
        cut = [(c - radius - margin, c + radius + margin)]
        for ox, oy, orad in others:
            a2 = (ox - px) * ux + (oy - py) * uy
            c2 = (oy - py) * ux - (ox - px) * uy
            if abs(a2 - a) < radius + orad + 2 * margin and e - orad - margin < a2 < e + LANE_LENGTH + orad + margin:
                cut.append((c2 - orad - margin, c2 + orad + margin))
        cut.sort()
        pos = -chan
        free = 0.0                          # the widest free stretch of track-centre positions across the channel
        for lo, hi in cut:
            if min(lo, chan) - pos > free:
                free = min(lo, chan) - pos
            if hi > pos:
                pos = hi
        if chan - pos > free:
            free = chan - pos
        if free < LANE_SLACK:
            return name
    return None


def congestion(ctx: Ctx, x: float, y: float) -> float:
    """Coarse congestion estimate at a via site, mm-equivalent: pads in the 1 mm cell (and half weight for the
    8 cells around), plus a flat 3 mm when the tile graph says the column has no via capacity left."""
    c = _cache(ctx.board)
    grid = c.get("density")
    if grid is None:
        grid = c["density"] = {}
        for p in ctx.board.pads:
            if p.net and p.layers:
                k = (int(math.floor(p.x)), int(math.floor(p.y)))
                grid[k] = grid.get(k, 0) + 1
    ix, iy = int(math.floor(x)), int(math.floor(y))
    n = grid.get((ix, iy), 0) * 2
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx or dy:
                n += grid.get((ix + dx, iy + dy), 0)
    est = CONGESTION_MM * n / 2.0
    tiles = ctx.tiles
    if tiles is not None:
        try:
            tx, ty = tiles.tile_of(x, y)
            if tiles.via_cap(tx, ty) <= 0:
                est += 3.0
        except (NotImplementedError, IndexError, KeyError, AttributeError):
            pass
    return est


def pour_penalty(board, net_name: str, x: float, y: float, radius: float) -> float | None:
    """None: a via at (x, y) is in no pour of the net (useless as a dog-bone). Else 0.0, or ISLAND_PENALTY when
    on every pour layer the via would sit on the pour's boundary (outline or fill island edge, or outside the
    present fill)."""
    best = None
    reach = radius + ISLAND_MARGIN
    for l, zones in sorted(_pours(board).get(net_name, {}).items()):
        for z in zones:
            if not z["outline"].contains(x, y):
                continue
            pen = 0.0
            if z["outline"].edge_dist(x, y, reach) < reach:
                pen = ISLAND_PENALTY
            elif z["fill"] is not None and (not z["fill"].contains(x, y) or z["fill"].edge_dist(x, y, reach) < reach):
                pen = ISLAND_PENALTY
            if best is None or pen < best:
                best = pen
            if best == 0.0:
                return 0.0
    return best


# ── local-only necking ────────────────────────────────────────────────────
def _mid(x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0:
        return x1, q(y1 + dy / 2)
    if dy == 0:
        return q(x1 + dx / 2), y1
    if abs(abs(dx) - abs(dy)) < 5e-7:
        m = q(abs(dx) / 2)
        return q(x1 + math.copysign(m, dx)), q(y1 + math.copysign(m, dy))
    return q(x1 + dx / 2), q(y1 + dy / 2)


def neck_split(oracle, layer: int, x1, y1, x2, y2, width: float, neck: float, net: str,
               clearance: float | None = None, ignore: frozenset = frozenset(),
               min_span: float | None = None) -> list[tuple] | None:
    """Local-only necking of one segment. -> [(x1, y1, x2, y2, w), ...] from start to end, or None when the
    segment is illegal even at ``neck`` width.

    The whole segment at ``width`` if that is legal. Otherwise it is bisected: every span that is legal at full
    width keeps it; a span of at most ``min_span`` (default max(0.4 mm, 2 x width)) that is not gets the widest
    legal width in [neck, width) on a 0.01 mm ladder. Neighbouring spans of equal width are merged, neighbouring
    narrow spans share their smallest width. Every returned span is oracle-legal as returned."""
    x1, y1, x2, y2 = q(x1), q(y1), q(x2), q(y2)
    if oracle.seg_legal(layer, x1, y1, x2, y2, width, net, clearance, ignore):
        return [(x1, y1, x2, y2, width)]
    if neck >= width - 1e-9 or not oracle.seg_legal(layer, x1, y1, x2, y2, neck, net, clearance, ignore):
        return None
    lim = max(NECK_MIN_SPAN, 2.0 * width) if min_span is None else min_span
    spans = []

    def widest(a, b, c, d):
        n = int(math.floor((width - neck) / NECK_STEP - 1e-9))
        lo, hi = 0, n + 1                      # neck + lo*step is legal, neck + hi*step (>= width) is not
        while hi - lo > 1:
            m = (lo + hi) // 2
            if oracle.seg_legal(layer, a, b, c, d, q(neck + m * NECK_STEP), net, clearance, ignore):
                lo = m
            else:
                hi = m
        return q(neck + lo * NECK_STEP)

    def rec(a, b, c, d, known_bad):
        if not known_bad and oracle.seg_legal(layer, a, b, c, d, width, net, clearance, ignore):
            spans.append([a, b, c, d, width])
            return
        mx, my = _mid(a, b, c, d)
        if math.hypot(c - a, d - b) <= lim or (mx, my) == (a, b) or (mx, my) == (c, d):
            spans.append([a, b, c, d, widest(a, b, c, d)])
            return
        rec(a, b, mx, my, False)
        rec(mx, my, c, d, False)

    rec(x1, y1, x2, y2, True)
    # neighbouring narrow spans share the smallest of their widths; equal neighbours merge
    i = 0
    while i < len(spans):
        if spans[i][4] < width - 1e-9:
            j = i
            w = spans[i][4]
            while j + 1 < len(spans) and spans[j + 1][4] < width - 1e-9:
                j += 1
                w = min(w, spans[j][4])
            spans[i:j + 1] = [[spans[i][0], spans[i][1], spans[j][2], spans[j][3], w]]
        i += 1
    out = []
    for s in spans:
        if out and abs(out[-1][4] - s[4]) < 1e-9:
            out[-1] = (out[-1][0], out[-1][1], s[2], s[3], s[4])
        else:
            out.append(tuple(s))
    return out


def necked_length(spans: list[tuple], width: float) -> float:
    return sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in spans if s[4] < width - 1e-9)


# ── pattern router (decoupling stubs, reserve-site stubs) ─────────────────
def pattern_shapes(ax, ay, bx, by) -> list[list[tuple]]:
    """The <= 5 direct polylines a -> b: straight (when axial or 45 degrees), two L postures with a 45 degree
    corner (diagonal first, straight first) and two Z postures (straight-diagonal-straight, diagonal-straight-
    diagonal). Points are quantised; every segment is axial or exactly 45 degrees."""
    ax, ay, bx, by = q(ax), q(ay), q(bx), q(by)
    dx, dy = bx - ax, by - ay
    adx, ady = abs(dx), abs(dy)
    if adx < 5e-7 and ady < 5e-7:
        return []
    m = q(min(adx, ady))
    s = q(max(adx, ady) - m)
    if m < 5e-7 or s < 5e-7:
        return [[(ax, ay), (bx, by)]]
    sx, sy = math.copysign(1.0, dx), math.copysign(1.0, dy)
    wide = adx > ady                         # the straight part runs along x
    out = [[(ax, ay), (q(ax + sx * m), q(ay + sy * m)), (bx, by)],
           [(ax, ay), (q(bx - sx * m), q(by - sy * m)), (bx, by)]]
    h = q(s / 2)
    if 5e-7 < h < s - 5e-7:
        p1 = (q(ax + sx * h), ay) if wide else (ax, q(ay + sy * h))
        p2 = (q(p1[0] + sx * m), q(p1[1] + sy * m))
        out.append([(ax, ay), p1, p2, (bx, by)])
    h = q(m / 2)
    if 5e-7 < h < m - 5e-7:
        p1 = (q(ax + sx * h), q(ay + sy * h))
        p2 = (q(bx - sx * (m - h)), q(by - sy * (m - h)))
        out.append([(ax, ay), p1, p2, (bx, by)])
    return out


def pattern_route(ctx: Ctx, conn: ConnV2, budget: Budget | None = None) -> list[tuple] | None:
    """Route ``conn`` pad to pad with one of ``pattern_shapes`` on a layer both terminals share.
    -> [(layer, x1, y1, x2, y2, w)] or None. The first shape that is legal at full width wins; failing that the
    shape with the least necked length (``neck_split``: narrow only where it has to be)."""
    orc = ctx.oracle
    nc = ctx.cfg.nets.get(conn.net) if ctx.cfg is not None else None
    layers = [l for l in conn.a.layers if l in conn.b.layers and (nc is None or l in nc.layers_ok)]
    best = None
    for layer in layers:
        for pts in pattern_shapes(conn.a.x, conn.a.y, conn.b.x, conn.b.y):
            if budget is not None:
                budget.charge(CHARGE_PATTERN)
            segs, ok = [], True
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                sp = neck_split(orc, layer, x1, y1, x2, y2, conn.width, conn.neck, conn.net, conn.clearance)
                if sp is None:
                    ok = False
                    break
                segs += [(layer,) + s for s in sp]
            if not ok:
                continue
            nk = sum(math.hypot(s[3] - s[1], s[4] - s[2]) for s in segs if s[5] < conn.width - 1e-9)
            if nk <= 0:
                return segs
            if best is None or nk < best[0] - 1e-9:
                best = (nk, segs)
    return best[1] if best else None


def open_lanes(ctx: Ctx, segs: list[tuple], own_net: str) -> list[tuple]:
    """Exit lanes of pads of OTHER nets near ``segs`` ([(layer, x1, y1, x2, y2, w)]) that are open right now:
    [(pad name, layer, x1, y1, x2, y2, width, net name)] — the straight way out of each such pad (pad centre to
    LANE_TEST beyond its edge, at the net's neck width) when the exact checker accepts it. Called before copper
    is added; ``closed_lanes`` afterwards says which of them the new copper shut."""
    board, orc = ctx.board, ctx.oracle
    L = _lanes(board)
    cell = L["cell"]
    own = board.nets[own_net].id if own_net in board.nets else 0
    seen, out = set(), []
    for _lay, x1, y1, x2, y2, _w in segs:
        for iy in range(int(math.floor((min(y1, y2) - 1.0) / cell)), int(math.floor((max(y1, y2) + 1.0) / cell)) + 1):
            for ix in range(int(math.floor((min(x1, x2) - 1.0) / cell)), int(math.floor((max(x1, x2) + 1.0) / cell)) + 1):
                for _g, nid, bb, (name, px, py, ux, uy, e, _chan) in L["lanes"].get((ix, iy), ()):
                    key = (name, px, py, ux, uy)
                    if nid == own or key in seen:
                        continue
                    if geom.seg_seg_dist(x1, y1, x2, y2, px, py, px + ux * (e + LANE_TEST), py + uy * (e + LANE_TEST)) > 1.0:
                        continue
                    seen.add(key)
                    net = board.net_by_id.get(nid)
                    pad = pad_by_key(board, name) if net is not None else None
                    if pad is None:
                        continue
                    info = _net_info(ctx, net.name)
                    w = min(info.get("neck") or net.width, net.width)
                    lay = _layer_of(pad)
                    lane = (name, lay, q(px), q(py), q(px + ux * (e + LANE_TEST)), q(py + uy * (e + LANE_TEST)), w, net.name)
                    if orc.seg_legal(lay, lane[2], lane[3], lane[4], lane[5], w, net.name):
                        out.append(lane)
    return out


def closed_lanes(ctx: Ctx, lanes: list[tuple]) -> list[str]:
    """Pad names of ``open_lanes`` entries the exact checker rejects now."""
    orc = ctx.oracle
    return sorted({name for name, lay, x1, y1, x2, y2, w, net in lanes if not orc.seg_legal(lay, x1, y1, x2, y2, w, net)})


# ── net facts ─────────────────────────────────────────────────────────────
def _net_info(ctx: Ctx, name: str) -> dict:
    table = (ctx.history.get("classify") or {}).get("nets")
    if table is None:
        table = ctx.history.get("_power_nets")
        if table is None:
            table = ctx.history["_power_nets"] = classify.net_table(ctx.board, ctx.cfg, ctx.x)
    t = table.get(name)
    if t is None:
        net = ctx.board.nets[name]
        t = {"role": "plane" if net.has_pour else "signal", "width": net.width, "neck": net.width,
             "clearance": net.clearance, "via": (net.via, net.drill), "skip": False}
    return t


def _book(ctx: Ctx) -> dict:
    h = ctx.history.get("power")
    if h is None:
        h = ctx.history["power"] = {"dogbones": {}, "soft": {}, "unreserved": [], "why": {}, "spokes": {}, "reserve": {},
                                    "book": {"spokes": [], "vias": []}}
    return h


# ── dog-bones ─────────────────────────────────────────────────────────────
def _stub_paths(px, py, vx, vy) -> list[list[tuple]]:
    dx, dy = abs(vx - px), abs(vy - py)
    if dx < 5e-7 or dy < 5e-7 or abs(dx - dy) < 5e-7:
        return [[(px, py), (vx, vy)]]
    return pattern_shapes(px, py, vx, vy)[:2]


def place_dogbone(ctx: Ctx, pad, exclude: frozenset = frozenset(), *, soft: bool = False,
                  lanes: bool = True) -> list[Piece] | None:
    """Reserve the dog-bone of one pad: a stub from the pad centre and a through via inside the net's pour, added
    to ``ctx.state`` as ``protected`` pieces (``stage='power'``, ``conn=-1``; the via is the last piece). A pad
    with no usable site of its own gets a stub to a neighbouring dog-bone via of its net instead (no via in the
    result then; the record has ``shares``). ``exclude``: via sites ``(x, y)``
    that must not be used (S5 re-placing a ripped dog-bone). ``soft=True`` (what S1 itself uses): when the best
    site reaches the pour only on layers signals route on, no copper is placed; the site is reserved as a soft
    ``'reserve'`` disc (``history['power']['soft'][key]``) and ``[]`` is returned — a later call without
    ``soft`` (the pours pass) places it there if it is still legal. None when no candidate is legal; the state is then
    unchanged. The site and piece ids are recorded in ``ctx.history['power']['dogbones'][pad_key(board, pad)]``
    (the pad name unless the footprint repeats that pad number). ``lanes=False`` (the pours pass, when the
    board is routed): the exit lanes of neighbouring pads no longer count — whoever needed one has used it, and
    the exact checker alone decides."""
    board, orc, st = ctx.board, ctx.oracle, ctx.state
    net = board.net_by_id.get(pad.net)
    if net is None or not pad.layers:
        return None
    _gc(ctx)
    info = _net_info(ctx, net.name)
    size, drill = info["via"]
    clr = info["clearance"]
    width, neck = info["width"], info["neck"]
    layer = _layer_of(pad)
    px, py = q(pad.x), q(pad.y)
    cands = [(v, False) for _se, v in dogbone_candidates(pad, (size, drill), clr)]
    key = pad_key(board, pad)
    mine = _book(ctx)["soft"].get(key)
    sites = _reserve_sites(ctx, net.name)
    if mine is not None and not soft:                       # the site S1 kept for this pad: first choice now
        sites = [tuple(mine["site"])] + sites               # (the soft disc goes when copper is really placed)
    elif mine is not None:
        return []
    for site in sites:
        if (mine is not None and site == tuple(mine["site"])) or (
                math.hypot(site[0] - px, site[1] - py) <= RESERVE_REACH and _site_pad(ctx, net, site) is pad):
            if (site, False) in cands:
                cands[cands.index((site, False))] = (site, True)
            elif (site, True) not in cands:
                cands.append((site, True))
    reach = size / 2 + clr
    scored = []
    for i, (v, planned) in enumerate(cands):
        if v in exclude:
            continue
        dx, dy = abs(v[0] - px), abs(v[1] - py)
        length = max(dx, dy) + (_S2 - 1.0) * min(dx, dy)
        base = length + 2.0 * congestion(ctx, v[0], v[1])
        if lanes and not planned:
            base += lane_penalty(board, pad, v[0], v[1], reach)
        hits = 0
        for l in range(board.L):
            hits += len(orc.soft_hits(l, v[0], v[1], v[0], v[1], size, net.name))
            if hits >= 2:
                break
        base += SOFT_PENALTY * min(hits, 2)
        if not planned and not on_plane(ctx, net.name, v[0], v[1]):
            base += SHARED_LAYER_PENALTY
        scored.append((round(base, 6), i, v))
    scored.sort()
    planned = {v for v, p in cands if p}
    others = [(r["site"][0], r["site"][1], r.get("size", size) / 2)
              for book_ in (_book(ctx)["dogbones"], _book(ctx)["soft"]) for k, r in sorted(book_.items())
              if abs(r["site"][0] - px) < 10.0 and abs(r["site"][1] - py) < 10.0 and k != key and not r.get("shares")]
    best = None
    walled = None
    for base, i, v in scored:
        if best is not None and base >= best[0]:
            break                                           # the pour penalty is >= 0: nothing later can win
        if not orc.via_legal(v[0], v[1], size, drill, net.name, clearance=clr):
            continue
        if lanes and v not in planned:
            w = lane_blocked(board, pad, v[0], v[1], size / 2, others)
            if w is not None:                               # legal, but it would close another pad's way out
                walled = walled or w
                continue
        stub = _stub_to(ctx, layer, px, py, v, width, neck, net.name, clr)
        if stub is None:
            continue
        pen = pour_penalty(board, net.name, v[0], v[1], size / 2)
        if pen is None:
            continue
        total = round(base + pen, 6)
        if best is None or total < best[0]:
            best = (total, i, v, stub)
    if best is None:
        shared = _share_via(ctx, pad, net, info, exclude)
        if shared is not None:
            return shared
        h = _book(ctx)
        in_pour = any(pour_penalty(board, net.name, v[0], v[1], size / 2) is not None for _b, _i, v in scored)
        h["why"][pad_key(board, pad)] = (f"would wall in {walled}" if walled and in_pour else
                                         "no legal via site" if in_pour else "pour out of reach")
        return None
    _total, _i, v, stub = best
    if soft and not on_plane(ctx, net.name, v[0], v[1]):
        h = _book(ctx)
        sid = orc.add_soft("reserve", net.name, -1, geom.box_circle(v[0], v[1], size / 2 + clr))
        h["soft"][key] = {"site": v, "ids": [sid], "net": net.name, "size": size}
        h["why"].pop(key, None)
        return []
    tok = st.begin()
    pieces = []
    for x1, y1, x2, y2, w in stub:
        pieces.append(st.add(net=net.name, conn=-1, kind="seg", layer=layer, x1=x1, y1=y1, x2=x2, y2=y2, w=w,
                             stage="power", protected=True, target_w=width))
    pieces.append(st.add(net=net.name, conn=-1, kind="via", layer=-1, x1=v[0], y1=v[1], x2=v[0], y2=v[1], w=size,
                         drill=drill, stage="power", protected=True))
    st.commit(tok)
    h = _book(ctx)
    _drop_soft(ctx, key)
    h["dogbones"][key] = {"site": v, "pieces": [p.id for p in pieces], "net": net.name, "size": size,
                          "spokes": _via_spokes(ctx, pad, net, v, size)}
    h["why"].pop(key, None)
    sid = h["reserve"].pop(v, None)
    if sid is not None:
        orc.remove_soft(sid)                                # the reserved site now carries real copper
    return pieces


def _via_spokes(ctx: Ctx, pad, net, v, size: float) -> list[int]:
    """The way from a dog-bone via into its pour, kept open the same way as a pad's: 4 soft 'thermal' corridors
    (+x, +y, -x, -y) on every layer where the via stands in the net's pour. Without them signals routed on the
    pour layer can ring the via and cut it off from the pour. -> soft ids (also in ``history['power']['spokes']``
    under the pad's name, numbered 4..7)."""
    h = _book(ctx)
    ids = []
    for layer in range(ctx.board.L):
        z = _zone_at(ctx.board, net.name, layer, v[0], v[1])
        if z is None:
            continue
        L = z["gap"] + SPOKE_REACH
        for k, (ux, uy) in enumerate(((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))):
            d = size / 2 + L / 2
            sid = ctx.oracle.add_soft("thermal", net.name, layer, (v[0] + ux * d, v[1] + uy * d, ux, uy, L / 2, z["spoke"] / 2, 0.0))
            h["spokes"][sid] = (pad.name, layer, 4 + k)
            h["book"]["spokes"].append((layer, v[0] + ux * size / 2, v[1] + uy * size / 2,
                                        v[0] + ux * (size / 2 + L), v[1] + uy * (size / 2 + L), z["spoke"]))
            ids.append(sid)
    return ids


def _drop_spokes(ctx: Ctx, rec: dict) -> None:
    h = _book(ctx)
    for sid in rec.get("spokes") or ():
        if sid in h["spokes"]:
            del h["spokes"][sid]
            ctx.oracle.remove_soft(sid)
    rec["spokes"] = []


def _gc(ctx: Ctx) -> None:
    """Forget dog-bones whose copper is gone (ripped by another stage, or rolled back): drop their records and
    the via corridors, so stale reservations do not keep pricing the search."""
    h = _book(ctx)
    st = ctx.state
    for key in sorted(h["dogbones"]):
        rec = h["dogbones"][key]
        if not any(st.piece(i) is not None for i in rec["pieces"]):
            _drop_spokes(ctx, rec)
            del h["dogbones"][key]
    for key in sorted(h["dogbones"]):
        rec = h["dogbones"][key]
        if rec.get("shares") and rec["shares"] not in h["dogbones"]:
            rec.pop("shares")                # its owner is gone: the stub dangles until the pours pass sees it
        elif not rec.get("shares") and not rec.get("spokes") and rec["pieces"]:
            via = st.piece(rec["pieces"][-1])            # a record another stage put back after a rollback
            pad = pad_by_key(ctx.board, key)
            if via is not None and via.kind == "via" and pad is not None and rec["net"] in ctx.board.nets:
                rec["spokes"] = _via_spokes(ctx, pad, ctx.board.nets[rec["net"]], tuple(rec["site"]), rec["size"])
    # a plan reserve site whose dog-bone was rolled back is a reservation again
    used = {tuple(r["site"]) for r in h["dogbones"].values()}
    for name, at in _plan_reserve(ctx) if h.get("pad_spokes") is not None else ():
        if at not in h["reserve"] and at not in used and name in ctx.board.nets:
            info = _net_info(ctx, name)
            h["reserve"][at] = ctx.oracle.add_soft("reserve", name, -1,
                                                   geom.box_circle(at[0], at[1], info["via"][0] / 2 + info["clearance"]))


def signal_layers(ctx: Ctx) -> frozenset:
    """Layers the plan lets unpoured nets route on. A pour on any other layer is a plane no signal can cut up."""
    h = _book(ctx)
    s = h.get("signal_layers")
    if s is None:
        got = set()
        nets = getattr(ctx.cfg, "nets", None)
        for n, nc in sorted((nets or {}).items()):
            net = ctx.board.nets.get(n)
            if net is not None and not net.has_pour and not (nc.skip and nc.explicit):
                got.update(nc.layers_ok)
        # (no plan at all: assume signals everywhere; a plan without a single unpoured net: nothing can cut a pour)
        s = h["signal_layers"] = frozenset(range(ctx.board.L)) if nets is None else frozenset(got)
    return s


def on_plane(ctx: Ctx, net_name: str, x: float, y: float) -> bool:
    """Does a via at (x, y) stand in the net's pour on a layer that signals do not use?"""
    sig = signal_layers(ctx)
    for l, zones in sorted(_pours(ctx.board).get(net_name, {}).items()):
        if l not in sig and any(z["outline"].contains(x, y) for z in zones):
            return True
    return False


def _drop_soft(ctx: Ctx, key: str):
    rec = _book(ctx)["soft"].pop(key, None)
    if rec is not None:
        for sid in rec["ids"]:
            ctx.oracle.remove_soft(sid)
    return rec


def _site_pad(ctx: Ctx, net, site):
    """The pad a plan reserve site is meant for: the nearest pad of its net that needs a dog-bone."""
    best = None
    for p in net.pads:
        if needs_dogbone(ctx.board, p):
            k = (round(math.hypot(p.x - site[0], p.y - site[1]), 6), classify._pad_key(p))
            if best is None or k < best[0]:
                best = (k, p)
    return best[1] if best else None


def _stub_to(ctx: Ctx, layer: int, px, py, v, width, neck, net_name, clr):
    """Legal stub pad centre -> via centre: [(x1, y1, x2, y2, w)] (locally necked where needed) or None."""
    for pts in _stub_paths(px, py, v[0], v[1]):
        segs = []
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            sp = neck_split(ctx.oracle, layer, x1, y1, x2, y2, width, neck, net_name, clr)
            if sp is None:
                segs = None
                break
            segs += sp
        if segs:
            return segs
    return None


def _share_via(ctx: Ctx, pad, net, info, exclude) -> list[Piece] | None:
    """Fallback when a pad has no usable site of its own: a stub to the dog-bone via of a neighbouring pad of the
    same net (within SHARE_REACH of stub). Only the stub is added; the record carries ``shares`` = the owner's key."""
    board, st = ctx.board, ctx.state
    h = _book(ctx)
    key = pad_key(board, pad)
    px, py = q(pad.x), q(pad.y)
    cands = []
    for k, r in sorted(h["dogbones"].items()):
        v = tuple(r["site"])
        if r["net"] != net.name or r.get("shares") or k == key or v in exclude:
            continue
        via = st.piece(r["pieces"][-1]) if r["pieces"] else None
        if via is None or via.kind != "via":
            continue
        dx, dy = abs(v[0] - px), abs(v[1] - py)
        length = max(dx, dy) + (_S2 - 1.0) * min(dx, dy)
        if length <= SHARE_REACH:
            cands.append((round(length, 6), k, v))
    layer = _layer_of(pad)
    for _length, k, v in sorted(cands):
        stub = _stub_to(ctx, layer, px, py, v, info["width"], info["neck"], net.name, info["clearance"])
        if stub is None:
            continue
        tok = st.begin()
        pieces = [st.add(net=net.name, conn=-1, kind="seg", layer=layer, x1=x1, y1=y1, x2=x2, y2=y2, w=w,
                         stage="power", protected=True, target_w=info["width"]) for x1, y1, x2, y2, w in stub]
        st.commit(tok)
        _drop_soft(ctx, key)
        h["dogbones"][key] = {"site": v, "pieces": [p.id for p in pieces], "net": net.name, "size": info["via"][0],
                              "shares": k}
        h["why"].pop(key, None)
        return pieces
    return None


def replace_dogbone(ctx: Ctx, pad_name: str) -> list[Piece] | None:
    """The reservation rip rule (§4.4): move the dog-bone of ``pad_name`` (a key of
    ``ctx.history['power']['dogbones']``) to another legal candidate inside one transaction. None (and nothing
    changed) when there is no other legal site, or when other pads share this dog-bone's via."""
    h = _book(ctx)
    rec = h["dogbones"].get(pad_name)
    pad = pad_by_key(ctx.board, pad_name)
    if pad is None or any(r.get("shares") == pad_name for r in h["dogbones"].values()):
        return None
    st = ctx.state
    tok = st.begin()
    tried = frozenset()
    if rec:
        tried = frozenset(rec.get("tried", ())) | {tuple(rec["site"])}
        live = {p.id for p in st.pieces(net=rec["net"])}
        for pid in rec["pieces"]:
            if pid in live:
                st.remove(pid)
        _drop_spokes(ctx, rec)
        del h["dogbones"][pad_name]
    new = place_dogbone(ctx, pad, exclude=tried)
    if new is None:
        st.rollback(tok)
        if rec:
            h["dogbones"][pad_name] = rec
            via = next((p for p in st.pieces(net=rec["net"]) if p.id == rec["pieces"][-1] and p.kind == "via"), None)
            if via is not None and not rec.get("shares"):
                rec["spokes"] = _via_spokes(ctx, pad, ctx.board.nets[rec["net"]], tuple(rec["site"]), rec["size"])
        return None
    st.commit(tok)
    h["dogbones"][pad_name]["tried"] = sorted(tried)
    return new


# ── thermal spokes ────────────────────────────────────────────────────────
def left_for_pour(cfg, name: str) -> bool:
    """Is ``name`` a net the plan's own rule leaves for a pour that does not exist yet ("ground net with N pads and
    no pour: left for pcb_pour")? Its pads will get thermal reliefs when that pour is made."""
    nc = (getattr(cfg, "nets", None) or {}).get(name)
    return bool(nc is not None and nc.skip and not getattr(nc, "explicit", False)
                and "pour" in str(getattr(nc, "skip_reason", "") or ""))


def thermal_spokes(board, pad, future: bool = False) -> list:
    """The 4 thermal-spoke corridors of a pad of a poured net, per pour layer the pad sits in:
    ``geom.Shape`` rectangles (cls 'soft', one layer each, ``tag = ('thermal', pad name, k)``) from the pad edge
    through the thermal gap into the pour. Spokes follow the pad's axes (the diagonals for a round pad, KiCad's
    default). Spoke width / gap: the zone's values when the extract has them, else 0.5 mm.
    ``future``: the net has no pour yet but is left for one (``left_for_pour``): the corridors a default pour
    (0.5 mm gap and spokes) will need, on every copper layer of the pad."""
    net = board.net_by_id.get(pad.net)
    if net is None or (not net.has_pour and not future):
        return []
    out = []
    for layer in pad.layers:
        z = _zone_at(board, net.name, layer, pad.x, pad.y) if net.has_pour else None
        if z is None and future and not net.has_pour:
            z = {"gap": SPOKE_GAP, "spoke": SPOKE_WIDTH}
        if z is None or not pad.shapes.get(layer):
            continue
        ax, ay, _hu, _hv, rnd = _axes(pad, layer)
        if rnd:
            ax, ay = 1.0 / _S2, 1.0 / _S2
        L = z["gap"] + SPOKE_REACH
        for k, (ux, uy) in enumerate(((ax, ay), (-ay, ax), (-ax, -ay), (ay, -ax))):
            e = _exit(pad, layer, ux, uy)
            g = (pad.x + ux * (e + L / 2), pad.y + uy * (e + L / 2), ux, uy, L / 2, z["spoke"] / 2, 0.0)
            out.append(Shape("box", g, "soft", pad.net, 0.0, (layer,), tag=("thermal", pad.name, k)))
    return out


def book(ctx: Ctx, tiles=None, force: bool = False) -> int:
    """Book the power stage's reservations as demand in the tile graph (once, after the tiles exist): each
    thermal spoke on the tile border it crosses (``Tiles.book``), each dog-bone via in its column
    (``Tiles.book_via``). -> number of bookings made; 0 when they are booked already (this function, or
    globalroute, which books every soft shape and protected piece itself and sets the same flag), unless
    ``force``. Side 'E' = the border to (tx+1, ty), 'N' = to (tx, ty+1), as in tiles.py."""
    tiles = tiles if tiles is not None else ctx.tiles
    if tiles is None:
        return 0
    h = _book(ctx)
    if h["book"].get("booked") and not force:
        return 0
    n = 0
    for layer, x0, y0, x1, y1, w in h["book"]["spokes"]:
        a, b = tiles.tile_of(x0, y0), tiles.tile_of(x1, y1)
        if a[0] != b[0]:
            tiles.book(layer, min(a[0], b[0]), a[1], "E", w)
            n += 1
        if a[1] != b[1]:
            tiles.book(layer, b[0], min(a[1], b[1]), "N", w)
            n += 1
    for x, y in h["book"]["vias"]:
        tx, ty = tiles.tile_of(x, y)
        tiles.book_via(tx, ty, 1)
        n += 1
    h["book"]["booked"] = True
    return n


# ── the stage ─────────────────────────────────────────────────────────────
def run(ctx: Ctx, budget: Budget) -> StageReport:
    """S1. Adds thermal-spoke and reserve soft shapes to the oracle, routes the decoupling connections, places
    the dog-bones. ``StageReport.data``: ``dogbones`` {pad: [piece ids]}, ``power_unreserved`` [pad names],
    ``unreserved_why`` {pad: 'no legal via site' | 'would wall in <pad>' | 'pour out of reach' | 'budget'},
    ``served_conns`` [idx of plane connections whose pad got a dog-bone], ``decaps`` {routed, failed},
    ``spokes`` (count), ``reserve`` (count), ``shared`` [pads whose stub ends on a neighbour's via],
    ``soft_reserved`` {pad: [x, y]} (site kept as a soft disc; copper comes in the pours pass), ``closed_conns``
    [idx of plane connections set to 'routed' because ``state.connected`` sees the dog-bone reach the pour].
    ``risk_dogbones`` [pads that got a dog-bone / soft site although their pour reaches them today
    (``pour_at_risk``); they are in ``dogbones`` / ``soft_reserved`` as well].
    ``StageReport.routed`` = decoupling connections routed + ``closed_conns``."""
    board, orc, st = ctx.board, ctx.oracle, ctx.state
    h = _book(ctx)
    rep = StageReport(name="power")
    checks0 = orc.checks
    used0 = budget.used

    def pay():
        nonlocal checks0
        n = (orc.checks - checks0) // ORACLE_CHECKS_PER_UNIT
        if n:
            budget.charge(n)
            checks0 += n * ORACLE_CHECKS_PER_UNIT

    # 1. thermal spokes of every pad inside its pour (soft: priced by the search, never illegal)
    nspoke = h.get("pad_spokes", 0)
    for name in sorted(board.nets) if "pad_spokes" not in h else ():      # (once per board: the stage may run again)
        net = board.nets[name]
        # a ground net the plan leaves for pcb_pour has no pour yet: the reliefs that pour will need are kept free
        # the same way (the router's promise is "pcb_pour next"; a track through a spoke now is a starved
        # thermal relief then, which no later routing run would see as its own)
        future = (not net.has_pour) and left_for_pour(ctx.cfg, name)
        if not future and (not net.has_pour or _net_info(ctx, name).get("skip")):
            continue
        for pad in net.pads:
            for s in thermal_spokes(board, pad, future=future):
                sid = orc.add_soft("thermal", name, s.layers[0], s)
                h["spokes"][sid] = (pad.name, s.layers[0], s.tag[2])
                cx, cy, ux, uy, hl, _hw, _r = s.g
                h["book"]["spokes"].append((s.layers[0], cx - ux * hl, cy - uy * hl, cx + ux * hl, cy + uy * hl,
                                            2 * s.g[5]))
                nspoke += 1
    h["pad_spokes"] = nspoke
    # 2. plan reserve sites: soft discs (via + clearance) owned by their net
    for name, at in _plan_reserve(ctx):
        if name in board.nets and at not in h["reserve"]:
            info = _net_info(ctx, name)
            h["reserve"][at] = orc.add_soft("reserve", name, -1,
                                            geom.box_circle(at[0], at[1], info["via"][0] / 2 + info["clearance"]))
    # 3. decoupling: capacitor pad -> IC pin, first, at power width
    conns = {c.idx: c for c in ctx.conns}
    done, failed, walled = [], [], []
    for idx in (ctx.history.get("classify") or {}).get("decaps") or []:
        c = conns.get(idx)
        if c is None or st.status(idx) == "routed":
            continue
        if budget.spent_out():
            failed.append(idx)
            continue
        segs = pattern_route(ctx, c, budget)
        pay()
        if not segs:
            failed.append(idx)
            continue
        # DECAP_LANE_CHECK: a decoupling track that shuts the straight way out of a neighbouring pad of another
        # net (0.4 mm pitch: a power track beside a pad leaves no legal exit for it) is left to the search
        # stages. Off by default: on the one board where it fires (a 0.4 mm QFN with 8 supply pins) the supply
        # pads then stay open instead of their neighbours — the same number of connections, traded.
        lanes = open_lanes(ctx, segs, c.net) if DECAP_LANE_CHECK else []
        tok = st.begin()
        for layer, x1, y1, x2, y2, w in segs:
            st.add(net=c.net, conn=idx, kind="seg", layer=layer, x1=x1, y1=y1, x2=x2, y2=y2, w=w, stage="power",
                   protected=False, target_w=c.width)
        shut = closed_lanes(ctx, lanes) if lanes else []
        pay()
        if shut:
            st.rollback(tok)
            failed.append(idx)
            walled.append((idx, shut))
            continue
        if st.connected(idx):
            st.set_status(idx, "routed")
            st.commit(tok)
            done.append(idx)
        else:
            st.rollback(tok)
            failed.append(idx)
    # 4. dog-bones: one per cluster of SMD pads the pour does not reach, pads in canonical order
    served, unreserved, late = set(), [], 0
    by_cluster = {}
    for pad in board.pads:
        if needs_dogbone(board, pad) and not _net_info(ctx, board.net_by_id[pad.net].name).get("skip"):
            by_cluster.setdefault((pad.net, pad.cluster or pad.name), []).append(pad)
    for key in sorted(by_cluster, key=lambda k: classify._pad_key(by_cluster[k][0])):
        pads = by_cluster[key]
        ok = False
        if budget.spent_out():
            late += 1
        else:
            for pad in pads:
                if pad_key(board, pad) in h["dogbones"] or pad_key(board, pad) in h["soft"]:
                    ok = True
                    break
                pieces = place_dogbone(ctx, pad, soft=SOFT_ON_SHARED_LAYERS)
                pay()
                if pieces is not None and not pieces:       # softly reserved: no copper yet
                    ok = True
                    break
                if pieces:
                    v = pieces[-1]
                    if v.kind == "via":
                        h["book"]["vias"].append((v.x1, v.y1))
                    served.add((pad.name, q(pad.x), q(pad.y)))
                    ok = True
                    break
        if not ok:
            k0 = pad_key(board, pads[0])
            unreserved.append(k0)
            if budget.spent_out() and k0 not in h["why"]:
                h["why"][k0] = "budget"
    # 4b. pads the pour reaches now but will lose to their neighbours' escapes (fine-pitch rows, a plane below):
    # their dog-bone too, while there is room for it. Optional: a pad without a site is not reported
    risk = 0
    if RISK_DOGBONES:
        for pad in sorted(board.pads, key=classify._pad_key):
            if budget.spent_out():
                break
            k = pad_key(board, pad)
            if k in h["dogbones"] or k in h["soft"] or not pour_at_risk(ctx, pad):
                continue
            why0 = h["why"].get(k)
            pieces = place_dogbone(ctx, pad, soft=SOFT_ON_SHARED_LAYERS)
            pay()
            if pieces:
                risk += 1
                h.setdefault("risk_keys", []).append(k)
                v = pieces[-1]
                if v.kind == "via":
                    h["book"]["vias"].append((v.x1, v.y1))
            elif pieces is not None:
                h.setdefault("risk_keys", []).append(k)             # softly reserved
            elif pieces is None:
                if why0 is None:
                    h["why"].pop(k, None)
                else:
                    h["why"][k] = why0
    h["risk"] = risk
    h["unreserved"] = list(unreserved)
    if ctx.tiles is not None and not h["book"].get("booked"):
        try:
            book(ctx)
        except NotImplementedError:
            pass
    have = set()
    for key in h["dogbones"]:
        p = pad_by_key(board, key)
        if p is not None:
            have.add((p.name, q(p.x), q(p.y)))
    served_conns = sorted(c.idx for c in ctx.conns if c.role == "plane" and (c.a.pad, c.a.x, c.a.y) in have)
    # a served pad whose via stands in a fill island of another cluster is connected as far as the state can
    # tell (RouteState.connected counts pour islands): no search has to spend budget on it. KiCad's refill in
    # S7 has the last word; a pad the new fill misses comes back through the pours pass.
    closed = []
    for idx in served_conns:
        if st.status(idx) == "todo" and st.connected(idx):
            st.set_status(idx, "routed")
            closed.append(idx)
    rep.routed, rep.failed = len(done) + len(closed), len(failed)
    rep.work = budget.used - used0
    rep.score = st.score()
    rep.data = {"dogbones": {k: list(v["pieces"]) for k, v in sorted(h["dogbones"].items())},
                "power_unreserved": list(unreserved), "served_conns": served_conns,
                "unreserved_why": {p: h["why"].get(p, "no legal via site") for p in unreserved},
                "decaps": {"routed": done, "failed": failed}, "spokes": nspoke, "reserve": len(h["reserve"]),
                "shared": sorted(k for k, v in h["dogbones"].items() if v.get("shares")),
                "soft_reserved": {k: list(v["site"]) for k, v in sorted(h["soft"].items())},
                "risk_dogbones": sorted(k for k in h.get("risk_keys", []) if k in h["dogbones"] or k in h["soft"]),
                "closed_conns": closed}
    far = sum(1 for p in unreserved if h["why"].get(p) == "pour out of reach")
    wall = sum(1 for p in unreserved if str(h["why"].get(p, "")).startswith("would wall in"))
    if h["soft"]:
        rep.notes.append(f"{len(h['soft'])} dog-bone site(s) reserved softly (their pour shares its layer with signals: "
                         f"the via is placed in the pours pass)")
    rep.notes.append(f"{len(h['dogbones'])} dog-bone(s) reserved, {len(unreserved) - far - late - wall} pour pad(s) "
                     f"without a legal via site, {wall} left for the pours pass (a via now would wall in a "
                     f"neighbouring pad), {far} too far from their pour for a dog-bone (left to routing), "
                     f"{nspoke} thermal spoke corridor(s), {len(done)}/{len(done) + len(failed)} decoupling "
                     f"connection(s) routed by pattern")
    if risk:
        rep.notes.append(f"{risk} dog-bone(s) for pads in fine-pitch rows that reach their pour only on a signal layer "
                         "(their neighbours' escapes would cut that contact)")
    if walled:
        rep.notes.append(f"{len(walled)} decoupling connection(s) left to the search: their direct track would close "
                         f"the way out of a neighbouring pad ({', '.join(sorted({p for _i, s in walled for p in s})[:8])})")
    if unreserved:
        rep.notes.append("power_unreserved (retried in the pours pass): " + ", ".join(unreserved[:12])
                         + (" ..." if len(unreserved) > 12 else ""))
    if late:
        rep.notes.append(f"{late} pour pad(s) not tried: the power stage budget ran out")
    return rep
