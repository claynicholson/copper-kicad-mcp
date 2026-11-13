"""The oracle: router v2's single exact legality test (design §4.1).

Wraps a ``guided.model.Board`` (exact pad / track / via / hole / keep-out shapes in
its ``ShapeIndex``) and adds

* a *dynamic* layer: the copper v2 has placed so far (``add_piece`` / ``remove_piece``),
* a *soft* layer: thermal-spoke corridors and reserved via sites, which never make copper
  illegal (``soft_hits`` reports them so callers can price them) until ``harden``-ed.

Vocabulary used throughout:

    dist   actual gap between the copper edge of the item being checked and a shape, mm
           (negative = they overlap)
    need   required gap, mm (``need()``); ``NO_NEED`` (-inf) = no constraint (same net)
    legal  iff ``dist >= need - tol`` for every shape, on coordinates quantised to 1 nm

What ``need`` models (the maximum of whatever applies; every line was measured against
kicad-cli 10.0 DRC, see docs/internal/ROUTER_V2_DESIGN.md Appendix A2 and bench/route/oracle_diff.py):

    clearance   max(net-class clearance of both nets, board minimum). A pad with a clearance
                override (its own, else its footprint's) uses max(override, board minimum)
                INSTEAD of the classes. The two nets of a coupled differential pair (KiCad's
                name rule, ``dp_complement``) use the class's pair gap when that is smaller.
    short       foreign copper overlapping (dist < 0) is reported as rule 'short'
    hole        copper to a foreign / NPTH hole: board hole clearance; a via's own drill to
                foreign copper likewise; via drill to any round hole, same net or not: hole-to-hole
    edge        copper to the outline's centre line: edge clearance (and never outside the outline)
    keepout     rule areas (no tracks / no vias): copper may not enter (need 0, no tolerance)
    plan        the plan's forbidden areas (``set_plan``): ``regions[].forbid`` and ``nets[..].avoid`` rectangles
                and a strict corridor. The centre line of a track / the centre of a via of a net they apply to may
                not enter or touch them (reported as rule 'keepout', static). Every stage that asks this oracle
                honours the plan that way, whatever its own search looked at.
    mask        graphics on the mask layers (hard for every net, as in v1 — stricter than KiCad);
                foreign pads on outer layers: pad mask margin + half the minimum web (no tolerance)

Tolerance: KiCad flags clearance / hole / edge only when the gap is more than 0.5 um short, so
those three rules are judged with ``tol`` (types.TOL_MM); mask and keep-out rules with none.

Not modelled (left to the judge stage, KiCad's DRC): conditional ``.kicad_dru`` rules beyond
``model.dru_minimums`` (an unconditional custom clearance below the class is read conservatively),
zone fills (foreign pours are refilled by KiCad around new copper; starved thermals are only
*predicted* by soft spoke shapes), zone-level clearances, silk / courtyard rules, length / skew
rules, blind and buried vias, KiCad's hole-to-hole exemption for slots (only round holes count
here too). Known approximations inherited from the worker's extract, all on the strict side
except the last: copper graphics as bounding boxes, custom pad shapes as outside polygons
(5 um arc error), static geometry rounded to 0.1 um, and arcs of copper ALREADY on the board
as chords of up to 10 degrees (up to 0.4 % of the radius inside the true arc: not strict).
"""
from __future__ import annotations

import math
import re

from ..guided import geom
from ..guided.geom import Shape, ShapeIndex
from .types import TOL_MM, Blocker, Piece, q

INF = float("inf")
NO_NEED = -INF        # need() result meaning "these two never conflict"
FUZZ = 5e-7           # mm (0.5 nm): float noise of the distance functions, far below KiCad's 1 nm unit
SOFT_RULES = ("thermal", "reserve")
PLAN_FIRST_AUTO = 1000  # plan.regions idx from here on are the router's own feedback regions (judge, halos): not rules
CORRIDOR_SLACK = 0.3    # mm a centre line may leave a strict corridor by (the searches judge it per grid cell)
CORRIDOR_STEP = 0.1     # mm between the points of a segment that are tested against a strict corridor
_TOLERANT = frozenset(("clearance", "hole", "edge"))   # rules KiCad judges with its 0.5 um epsilon (mask, keep-out: none)

def raster_guard(pitch: float, reach: float) -> float:
    """Extra inflation (mm) a grid of ``pitch`` needs so that a straight step between two free neighbouring
    cell centres (axial or diagonal) keeps ``reach`` (= half width + need) to a point obstacle such as a pad
    corner: the sagitta ``sqrt(reach^2 + pitch^2 / 2) - reach``. v1's fixed EPS of 12 um is smaller than this
    whenever pitch^2 > ~0.05 x reach (0.15 mm grid, 0.25 mm track, 0.1 mm clearance: 23.7 um) — the measured
    root cause of the one_air_max self-check failures (design Appendix A1). Grids stay accelerators: every
    emitted segment still goes through seg_ok."""
    return math.sqrt(reach * reach + pitch * pitch / 2.0) - reach


_TOK = re.compile(r'"(?:\\.|[^"\\])*"|[()]')
_NUM = re.compile(r"\s+(-?[\d.]+)")
_STR = re.compile(r'\s+"((?:\\.|[^"\\])*)"')


def local_overrides_from_file(pcb_path) -> dict:
    """What the worker's extract does not carry, read from the .kicad_pcb text (stdlib only):

        {'mask_margin': mm, 'mask_web': mm,                      board setup (0.0 when absent)
         'fp_clearance': {ref: mm}, 'pad_clearance': {(ref, pad number): mm},
         'fp_mask_margin': {ref: mm}, 'pad_mask_margin': {(ref, pad number): mm}}

    KiCad resolves a pad's clearance override as pad value, else footprint value (the extract only has the
    pad's own); the solder-mask margin as pad, else footprint, else board. Hand the dict to ``Oracle(overrides=)``."""
    out = {"mask_margin": 0.0, "mask_web": 0.0, "fp_clearance": {}, "pad_clearance": {}, "fp_mask_margin": {},
           "pad_mask_margin": {}}
    try:
        with open(pcb_path, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return out

    def num(pos):
        m = _NUM.match(txt, pos)
        return float(m.group(1)) if m else None

    depth = 0
    fp = pad = None           # open footprint: [ref, clearance, mask, [[pad number, clearance, mask], ...]]
    for m in _TOK.finditer(txt):
        t = m.group()
        if t == "(":
            depth += 1
            e = m.end()
            if depth == 2:
                if txt.startswith("footprint", e):
                    fp = [None, None, None, []]
            elif depth == 3:
                if fp is not None:
                    if txt.startswith("pad ", e):
                        sm = _STR.match(txt, e + 3)
                        pad = [sm.group(1) if sm else "", None, None]
                    elif txt.startswith("clearance", e):
                        fp[1] = num(e + 9)
                    elif txt.startswith("solder_mask_margin", e):
                        fp[2] = num(e + 18)
                    elif txt.startswith('property "Reference"', e):
                        sm = _STR.match(txt, e + 20)
                        fp[0] = sm.group(1) if sm else fp[0]
                    elif txt.startswith("fp_text reference", e):
                        sm = _STR.match(txt, e + 17)
                        fp[0] = sm.group(1) if sm else fp[0]
                elif txt.startswith("pad_to_mask_clearance", e):
                    out["mask_margin"] = num(e + 21) or 0.0
                elif txt.startswith("solder_mask_min_width", e):
                    out["mask_web"] = num(e + 21) or 0.0
            elif depth == 4 and pad is not None:
                if txt.startswith("clearance", e):
                    pad[1] = num(e + 9)
                elif txt.startswith("solder_mask_margin", e):
                    pad[2] = num(e + 18)
        elif t == ")":
            if depth == 3 and pad is not None:
                fp[3].append(pad)
                pad = None
            elif depth == 2 and fp is not None:
                ref = fp[0] or ""
                if fp[1]:
                    out["fp_clearance"][ref] = fp[1]
                if fp[2] is not None:
                    out["fp_mask_margin"][ref] = fp[2]
                for pn, c, mg in fp[3]:
                    if c:
                        out["pad_clearance"][(ref, pn)] = c
                    if mg is not None:
                        out["pad_mask_margin"][(ref, pn)] = mg
                fp = None
            depth -= 1
    return out


def dp_complement(name: str) -> str | None:
    """KiCad's DRC_ENGINE::MatchDpSuffix: the name of the other half of a differential pair, or None.
    Trailing digits and underscores are skipped; the first other character from the end must be + - P or N."""
    i = len(name) - 1
    while i >= 0:
        ch = name[i]
        if ch in "0123456789_":
            i -= 1
            continue
        swap = {"+": "-", "-": "+", "P": "N", "N": "P"}.get(ch)
        if swap is None:
            return None
        return name[:i] + swap + name[i + 1:]
    return None


def _rect_edges(b):
    """Corners of a box's inner rectangle as 4 segments (degenerate boxes give coincident points)."""
    cx, cy, ux, uy, ihw, ihh, _r = b
    pts = []
    for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
        pts.append((cx + sx * ihw * ux - sy * ihh * uy, cy + sx * ihw * uy + sy * ihh * ux))
    return [(pts[i][0], pts[i][1], pts[(i + 1) % 4][0], pts[(i + 1) % 4][1]) for i in range(4)]


def shape_gap(a: Shape, b: Shape) -> float:
    """Exact edge-to-edge distance between two shapes (<= 0 when they touch or overlap)."""
    if a.kind == "box" and b.kind == "box":
        ra, rb = a.g[6], b.g[6]
        ia = (a.g[0], a.g[1], a.g[2], a.g[3], a.g[4], a.g[5], 0.0)
        ib = (b.g[0], b.g[1], b.g[2], b.g[3], b.g[4], b.g[5], 0.0)
        d = INF
        for x1, y1, x2, y2 in _rect_edges(ia):
            d = min(d, geom.seg_box_dist(x1, y1, x2, y2, ib))
        for x1, y1, x2, y2 in _rect_edges(ib):
            d = min(d, geom.seg_box_dist(x1, y1, x2, y2, ia))
        return max(d, 0.0) - ra - rb
    if a.kind == "poly" and b.kind == "poly":
        d = INF
        n = len(a.g)
        for i in range(n):
            x1, y1 = a.g[i]
            x2, y2 = a.g[(i + 1) % n]
            d = min(d, geom.seg_poly_dist(x1, y1, x2, y2, b.g))
        if d > 0 and geom.pt_in_poly(a.g, b.g[0][0], b.g[0][1]):
            return 0.0
        return d
    box, poly = (a, b) if a.kind == "box" else (b, a)
    inner = (box.g[0], box.g[1], box.g[2], box.g[3], box.g[4], box.g[5], 0.0)
    d = INF
    n = len(poly.g)
    for i in range(n):
        x1, y1 = poly.g[i]
        x2, y2 = poly.g[(i + 1) % n]
        d = min(d, geom.seg_box_dist(x1, y1, x2, y2, inner))
    if d > 0 and geom.pt_in_poly(poly.g, box.g[0], box.g[1]):
        return -box.g[6]
    return max(d, 0.0) - box.g[6]


class Oracle:
    """Exact legality of a segment or via against the board and the copper placed so far.

    ``tol``: legal iff ``dist >= need - tol`` (keep-outs and the outline's inside test: always 0).
    ``overrides``: the dict of ``local_overrides_from_file(pcb)`` — footprint-level clearance overrides and
    solder-mask margins, which the extract does not carry. Without it a pad uses its own override or the
    net classes (exact unless its footprint carries an override) and the solder-mask-bridge need is off.
    """

    def __init__(self, board, tol: float = TOL_MM, *, overrides: dict | None = None):
        self.b = board
        self.tol = tol
        R = board.rules
        self.L = board.L
        self._allL = tuple(range(board.L))
        self._outer = (0, board.L - 1)
        self.min_clr = R["clearance"]
        self.edge_clr = R["edge_clearance"]
        self.hole_clr = R["hole_clearance"]
        self.h2h = R["hole_to_hole"]
        ov = overrides or {}
        self.mask_margin = float(ov.get("mask_margin") or 0.0)
        self.mask_web = float(ov.get("mask_web") or 0.0)
        self.mask_openings_hard = True      # graphics on the mask layers block every net (v1 behaviour)
        self.net_clr = {}                   # net name -> clearance raised by the plan (set_clearance)
        self.checks = 0                     # seg_ok / via_ok / blockers calls (budget: 1 unit per 8)
        self._version = 0
        self._static = board.index
        # pad facts the Shape objects do not carry: local clearance override, mask margin
        self._override = {}                 # id(shape) -> local clearance, mm
        self._hole_override = {}            # id(pad) -> local clearance: KiCad applies it to the pad's hole as well
        #                                     ("hole_clearance (pad clearance 1.7 mm ...)" next to an NPTH pad)
        self._padmask = {}                  # id(shape) -> required gap from the mask rule, mm
        self._pads_by_name = {}
        pad_dicts = (getattr(board, "ex", None) or {}).get("pads") or []
        fpc, pdc = ov.get("fp_clearance") or {}, ov.get("pad_clearance") or {}
        fpm, pdm = ov.get("fp_mask_margin") or {}, ov.get("pad_mask_margin") or {}
        for i, pad in enumerate(board.pads):
            self._pads_by_name.setdefault(pad.name, []).append(pad)
            d = pad_dicts[i] if i < len(pad_dicts) else {}
            lc = d.get("clearance") or pdc.get((pad.ref, pad.num)) or fpc.get(pad.ref)
            mg = pdm.get((pad.ref, pad.num))
            if mg is None:
                mg = fpm.get(pad.ref)
            if mg is None:
                mg = self.mask_margin
            mneed = (mg + self.mask_web / 2) if overrides is not None else 0.0
            if lc:
                self._hole_override[id(pad)] = float(lc)
            for shs in pad.shapes.values():
                for s in shs:
                    if lc:
                        self._override[id(s)] = float(lc)
                    if mneed > 0:
                        self._padmask[id(s)] = mneed
        # coupled differential pairs: KiCad's implicit rule lets P and N approach to the class's pair gap
        self._dp = {}                       # net id -> (partner net id, clearance between the two)
        for name, net in board.nets.items():
            other = board.nets.get(dp_complement(name) or "")
            if other is None or other is net:
                continue
            ga = net.dp_gap if net.dp_gap < net.clearance else net.clearance
            gb = other.dp_gap if other.dp_gap < other.clearance else other.clearance
            self._dp[net.id] = (other.id, max(ga, gb, self.min_clr))
        # graphics on the mask layers arrive as 'kt' + 'kv' polygons without a net: tell them from rule areas
        self._maskart = set()
        marks = {(m["layer"], tuple(tuple(p) for p in m["poly"]))
                 for m in (getattr(board, "ex", None) or {}).get("mask_openings") or []}
        if marks:
            for s in board.shapes:
                if s.kind == "poly" and s.cls in ("kt", "kv") and len(s.layers) == 1 \
                        and (s.layers[0], tuple(s.g)) in marks:
                    self._maskart.add(id(s))
        # the outline as zero-width segments, every layer
        self._edge = ShapeIndex()
        self._edges = []
        for o, holes in board.outline:
            for pts in [o] + list(holes):
                n = len(pts)
                for i in range(n):
                    x1, y1 = pts[i]
                    x2, y2 = pts[(i + 1) % n]
                    s = Shape("box", geom.box_from_seg(x1, y1, x2, y2, 0.0), "edge", 0, 0.0, (0,), tag="edge")
                    self._edges.append(s)
                    self._edge.add(s)
        self._inside = {}
        # the plan's forbidden areas (set_plan): nothing until the engine hands the plan over
        self._plan_regs = ()                # user forbid regions (guided.plan.Region)
        self._plan_avoid = {}               # net -> [(x0, y0, x1, y1)]
        self._plan_cor = {}                 # net -> (points, half width, [(pad x, pad y)], pad radius): strict corridor
        self._plan_cache = {}               # net -> ((cx, cy, hw, hh, layers | None, tag), ...)
        self.plan_on = False
        # dynamic copper and soft shapes
        self._dyn = ShapeIndex()
        self._pieces = {}                   # piece id -> (Piece, [Shape])
        self._soft = ShapeIndex()
        self._softs = {}                    # soft id -> (rule, net id, Shape, hard)
        self._soft_seq = 0
        self._ally = {}                     # net id -> net id whose copper is not looked at (set_ally)
        self._hard = ShapeIndex()           # hardened soft shapes
        clrs = [board.default_clr, self.min_clr] + [n.clearance for n in board.nets.values()] \
            + list(self._override.values()) + list(self._padmask.values()) + list(self._hole_override.values())
        self._reach = max(max(clrs), self.edge_clr, self.hole_clr, self.h2h) + 0.01

    # ── small helpers ─────────────────────────────────────────────────────
    @property
    def version(self) -> int:
        """Bumps on every add / remove of a piece or soft shape."""
        return self._version

    def net_id(self, net: str | None) -> int:
        n = self.b.nets.get(net or "")
        return n.id if n else 0

    def net_name(self, nid: int) -> str | None:
        n = self.b.net_by_id.get(nid)
        return n.name if n else None

    def clearance(self, net: str) -> float:
        """The clearance new copper of this net asks for (class value, or what set_clearance raised it to)."""
        c = self.net_clr.get(net)
        if c is not None:
            return c
        n = self.b.nets.get(net)
        return n.clearance if n else self.b.default_clr

    def set_ally(self, a: str | None, b: str | None = None) -> None:
        """While set, copper (and pad holes) of net ``b`` is not looked at when an item of net ``a`` is checked,
        as if it were ``a``'s own: the centre-line search of a differential pair stands for BOTH nets, so
        neither net's pads may wall it in (``pairs._lay``; every lane it lays is checked again as its own net
        with no ally). ``set_ally(None)`` clears it. Windows rasterised meanwhile must not be reused afterwards
        (``window.clear_cache``)."""
        na, nb = self.b.nets.get(a or ""), self.b.nets.get(b or "")
        self._ally = {na.id: nb.id} if na is not None and nb is not None and na.id and nb.id else {}

    def pair_need(self, a: str, b: str) -> float:
        """The gap this oracle asks between copper of net ``a`` and copper of net ``b`` (no local pad
        overrides): the larger of the two clearances, or the class's pair gap when KiCad reads the two names
        as one differential pair and neither clearance was raised by the plan."""
        na, nb = self.b.nets.get(a), self.b.nets.get(b)
        need = max(self.clearance(a), self.clearance(b))
        raised = na is not None and nb is not None and (self.net_clr.get(a, 0.0) > na.clearance
                                                        or self.net_clr.get(b, 0.0) > nb.clearance)
        if na is None or nb is None or raised:
            return need
        dp = self._dp.get(na.id)
        if dp is not None and dp[0] == nb.id and dp[1] < need:
            return dp[1]
        return need

    def set_clearance(self, net: str, clearance: float) -> None:
        """Plan override: this net's copper (new checks and pieces added from now on) keeps ``clearance``."""
        n = self.b.nets.get(net)
        self.net_clr[net] = max(clearance, n.clearance if n else self.b.default_clr)
        self._reach = max(self._reach, self.net_clr[net] + 0.01)

    def inside(self, x: float, y: float) -> bool:
        k = (x, y)
        v = self._inside.get(k)
        if v is None:
            if len(self._inside) > 200_000:
                self._inside.clear()
            v = self._inside[k] = self.b.inside(x, y)
        return v

    def _tag(self, s: Shape, dyn: bool) -> str | None:
        if dyn:
            return "via" if len(s.layers) > 1 or s.cls == "hole" else "track"
        t = s.tag
        if t is not None and hasattr(t, "name"):
            return t.name
        if s.cls == "edge":
            return "edge"
        if s.cls == "hole":
            return "hole"
        if s.cls in ("kt", "kv"):
            return "mask" if id(s) in self._maskart else "keepout"
        if s.cls == "soft":
            return str(t)
        if s.kind == "poly":
            return "zone"
        g = s.g
        if g[4] == 0.0 and g[5] == 0.0:
            return "via"
        if g[5] == 0.0 and g[6] > 0.0:
            return "track"
        return "copper"

    # ── need ──────────────────────────────────────────────────────────────
    def _need(self, nid: int, clr: float, raised: bool, hw: float, drill: float, s: Shape, layer: int, via: bool):
        """-> (need, rule) or None. ``need`` is the required gap between the item's copper edge and ``s``."""
        cls = s.cls
        if cls == "cu":
            if nid and s.net == nid:
                return None
            if self._ally and self._ally.get(nid) == s.net:
                return None
            ov = self._override.get(id(s)) if self._override else None
            if ov is not None:
                n = ov if ov > self.min_clr else self.min_clr
                if raised and clr > n:
                    n = clr
            else:
                n = clr if clr > s.clr else s.clr
                if self._dp and not raised:
                    dp = self._dp.get(nid)
                    if dp is not None and dp[0] == s.net and dp[1] < n:
                        n = dp[1]
            rule = "clearance"
            if self._padmask and layer in self._outer:
                m = self._padmask.get(id(s))
                if m is not None and m > n:
                    n, rule = m, "mask"
            if via:
                h = self.hole_clr + drill / 2 - hw      # the via's own drill against foreign copper
                if h > n:
                    n, rule = h, "hole"
            return n, rule
        if cls == "hole":
            n = None
            if not (nid and (s.net == nid or (self._ally and self._ally.get(nid) == s.net))):
                n = self.hole_clr if self.hole_clr > 0 else 0.0
                if self._hole_override:
                    ov = self._hole_override.get(id(s.tag))
                    if ov is not None and ov > n:
                        n = ov
            if via and self.h2h > 0 and s.g[4] == 0.0 and s.g[5] == 0.0:
                h = self.h2h + drill / 2 - hw           # drill to drill, round holes only (KiCad's test)
                if n is None or h > n:
                    n = h
            return None if n is None else (n, "hole")
        if cls == "edge":
            return self.edge_clr, "edge"
        if cls == "kt":
            if via:
                return None
            if id(s) in self._maskart:
                return (0.0, "mask") if self.mask_openings_hard else None
            return 0.0, "keepout"
        if cls == "kv":
            if not via:
                return None
            if id(s) in self._maskart:
                return (0.0, "mask") if self.mask_openings_hard else None
            return 0.0, "keepout"
        if cls == "soft":                               # hardened soft shape: closed to foreign nets
            if nid and s.net == nid:
                return None
            return 0.0, s.tag[0]
        return None

    def need(self, net: str, half_w: float, shape: Shape, layer: int, kind: str = "seg", drill: float = 0.0,
             clearance: float | None = None) -> float:
        """Required gap (mm) between the copper edge of a new ``kind`` ('seg' | 'via') of ``net`` with half
        width / radius ``half_w`` and ``shape``; ``NO_NEED`` (-inf) when they never conflict. The centre line
        must stay ``half_w + need`` away from the shape. Keep-outs have need 0 (copper may not enter)."""
        nid, clr, raised = self._args(net, clearance)
        r = self._need(nid, clr, raised, half_w, drill, shape, layer, kind == "via")
        return NO_NEED if r is None else r[0]

    # ── the plan's forbidden areas ────────────────────────────────────────
    def set_plan(self, cfg) -> int:
        """Make the plan's forbidden areas rules of this oracle: every ``regions[].forbid`` the plan wrote (not
        the router's own feedback regions, ``idx >= PLAN_FIRST_AUTO``), every ``nets[..].avoid`` rectangle and
        every strict corridor. Returns how many areas / corridors. Call it once, before any copper is placed."""
        regs = [r for r in (getattr(cfg, "regions", None) or ())
                if getattr(r, "forbid", False) and getattr(r, "idx", 0) < PLAN_FIRST_AUTO]
        avoid, cor = {}, {}
        for name, nc in sorted((getattr(cfg, "nets", None) or {}).items()):
            if getattr(nc, "avoid", None):
                avoid[name] = [tuple(r) for r in nc.avoid]
            c = getattr(nc, "corridor", None)
            if c and c.get("strict") and c.get("points"):
                net = self.b.nets.get(name)
                half = float(c["width"]) / 2
                cor[name] = (tuple(c["points"]), half, tuple((p.x, p.y) for p in (net.pads if net else ())),
                             max(half, 1.0))
        self._plan_regs, self._plan_avoid, self._plan_cor = tuple(regs), avoid, cor
        self._plan_cache = {}
        self.plan_on = bool(regs or avoid or cor)
        self._version += 1
        return len(regs) + sum(len(v) for v in avoid.values()) + len(cor)

    def _plan_rects(self, net):
        t = self._plan_cache.get(net)
        if t is None:
            out = []
            for r in self._plan_regs:
                if r.applies(net):
                    x0, y0, x1, y1 = r.rect
                    out.append(((x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2,
                                None if r.layers is None else frozenset(r.layers), f"plan region '{r.name}'"))
            for x0, y0, x1, y1 in self._plan_avoid.get(net, ()):
                out.append(((x0 + x1) / 2, (y0 + y1) / 2, abs(x1 - x0) / 2, abs(y1 - y0) / 2, None,
                            f"plan avoid rectangle of {net}"))
            t = self._plan_cache[net] = tuple(out)
        return t

    def plan_hit(self, net: str, layer: int | None, x1, y1, x2, y2) -> str | None:
        """Name of the plan area the centre line (x1, y1)-(x2, y2) of ``net`` enters or touches on ``layer``
        (None = a through via: every layer), or None when the plan allows it."""
        for cx, cy, hw, hh, lays, tag in self._plan_rects(net):
            if layer is not None and lays is not None and layer not in lays:
                continue
            if geom.seg_rect_dist(x1 - cx, y1 - cy, x2 - cx, y2 - cy, hw, hh) <= 0:
                return tag
        c = self._plan_cor.get(net)
        if c is not None:
            pts, half, pads, rpad = c
            n = max(1, int(math.ceil(math.hypot(x2 - x1, y2 - y1) / CORRIDOR_STEP)))
            for i in range(n + 1):
                x, y = x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n
                if any(math.hypot(x - px, y - py) <= rpad + CORRIDOR_SLACK for px, py in pads):
                    continue
                if len(pts) == 1:
                    ok = math.hypot(x - pts[0][0], y - pts[0][1]) <= half + CORRIDOR_SLACK
                else:
                    ok = any(geom.pt_seg_dist(x, y, pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1])
                             <= half + CORRIDOR_SLACK for k in range(len(pts) - 1))
                if not ok:
                    return f"outside the strict corridor of {net}"
        return None

    def _plan_blocker(self, net, layer, x1, y1, x2, y2, via=False) -> Blocker | None:
        tag = self.plan_hit(net, None if via else layer, x1, y1, x2, y2)
        if tag is None:
            return None
        return Blocker(rule="keepout", net=None, tag=tag, static=True, piece=None, dist=-INF, need=0.0,
                       at=(x1, y1), layer=layer)

    # ── checks ────────────────────────────────────────────────────────────
    def _args(self, net, clearance):
        """-> (net id, clearance asked for, True when that is above the net class value)."""
        n = self.b.nets.get(net)
        cls = n.clearance if n else self.b.default_clr
        clr = self.net_clr.get(net, cls)
        if clearance is not None and clearance > clr:
            clr = clearance
        return (n.id if n else 0), clr, clr > cls

    def _scan_seg(self, layer, x1, y1, x2, y2, hw, nid, clr, raised, ignore, first):
        """Violations of one segment: [(margin, dist, need, rule, shape, dyn)], margin = dist - need < -tol."""
        out = []
        bb = (x1 if x1 < x2 else x2, y1 if y1 < y2 else y2, x1 if x1 > x2 else x2, y1 if y1 > y2 else y2)
        grow = hw + self._reach
        lim = -self.tol - FUZZ
        need_of = self._need
        for idx, dyn, lay in ((self._static, False, layer), (self._dyn, True, layer), (self._edge, False, 0),
                              (self._hard, False, layer)):
            if not idx.b:
                continue
            for s in idx.near(lay, bb, grow):
                if dyn and ignore and s.tag in ignore:
                    continue
                r = need_of(nid, clr, raised, hw, 0.0, s, layer, False)
                if r is None:
                    continue
                n = r[0]
                sb = s.bbox
                reach = hw + n
                if sb[0] - reach > bb[2] or sb[2] + reach < bb[0] or sb[1] - reach > bb[3] or sb[3] + reach < bb[1]:
                    continue
                d = s.dist_seg(x1, y1, x2, y2) - hw
                if d - n < (lim if r[1] in _TOLERANT else -FUZZ):
                    out.append((d - n, d, n, r[1], s, dyn))
                    if first:
                        return out
        return out

    def _scan_via(self, x, y, hw, drill, nid, clr, raised, ignore, first):
        out = []
        bb = (x, y, x, y)
        grow = hw + self._reach
        lim = -self.tol - FUZZ
        need_of = self._need
        seen = set()
        for layer in self._allL:
            for idx, dyn, lay in ((self._static, False, layer), (self._dyn, True, layer),
                                  (self._edge, False, 0), (self._hard, False, layer)):
                if not idx.b or (lay == 0 and layer and idx is self._edge):
                    continue
                for s in idx.near(lay, bb, grow):
                    k = id(s)
                    if k in seen:
                        continue
                    if len(s.layers) > 1 or s.cls in ("hole", "edge", "kv"):
                        seen.add(k)             # layer-independent answer: once is enough
                    if dyn and ignore and s.tag in ignore:
                        continue
                    r = need_of(nid, clr, raised, hw, drill, s, layer, True)
                    if r is None:
                        continue
                    n = r[0]
                    sb = s.bbox
                    reach = hw + n
                    if sb[0] - reach > x or sb[2] + reach < x or sb[1] - reach > y or sb[3] + reach < y:
                        continue
                    d = s.dist_pt(x, y) - hw
                    if d - n < (lim if r[1] in _TOLERANT else -FUZZ):
                        out.append((d - n, d, n, r[1], s, dyn, layer))
                        if first:
                            return out
        return out

    @staticmethod
    def _order(v):
        s = v[4]
        return (round(v[0], 7), v[3], s.layers, s.bbox, s.net)

    def _blocker(self, v, layer, x1, y1, x2, y2) -> Blocker:
        _m, d, n, rule, s, dyn = v[:6]
        if len(v) > 6:
            layer = v[6]
        if rule in ("clearance", "mask") and d < -FUZZ and s.cls == "cu":
            rule = "short"
        sb = s.bbox
        at = geom.pt_seg_proj((sb[0] + sb[2]) / 2, (sb[1] + sb[3]) / 2, x1, y1, x2, y2)
        return Blocker(rule=rule, net=self.net_name(s.net), tag=self._tag(s, dyn), static=not dyn,
                       piece=s.tag if dyn else None, dist=round(d, 6), need=round(n, 6),
                       at=(round(at[0], 6), round(at[1], 6)), layer=layer)

    def _outside(self, x1, y1, x2, y2, layer) -> Blocker | None:
        for x, y in ((x1, y1), (x2, y2)):
            if not self.inside(x, y):
                return Blocker(rule="edge", net=None, tag="edge", static=True, piece=None, dist=-INF,
                               need=self.edge_clr, at=(x, y), layer=layer)
        return None

    def seg_ok(self, layer: int, x1, y1, x2, y2, w: float, net: str, clearance: float | None = None,
               ignore: frozenset = frozenset()) -> Blocker | None:
        """None = a track segment of ``net`` (width ``w``) here is legal; else the worst hard Blocker.
        ``ignore``: piece ids whose copper is not looked at. Soft shapes are never returned (soft_hits)."""
        self.checks += 1
        x1, y1, x2, y2 = q(x1), q(y1), q(x2), q(y2)
        nid, clr, raised = self._args(net, clearance)
        vs = self._scan_seg(layer, x1, y1, x2, y2, w / 2, nid, clr, raised, ignore, False)
        if vs:
            return self._blocker(min(vs, key=self._order), layer, x1, y1, x2, y2)
        return self._outside(x1, y1, x2, y2, layer) or (self._plan_blocker(net, layer, x1, y1, x2, y2)
                                                         if self.plan_on else None)

    def seg_legal(self, layer: int, x1, y1, x2, y2, w: float, net: str, clearance: float | None = None,
                  ignore: frozenset = frozenset()) -> bool:
        """seg_ok() is None, faster: stops at the first violation and builds no Blocker."""
        self.checks += 1
        x1, y1, x2, y2 = q(x1), q(y1), q(x2), q(y2)
        nid, clr, raised = self._args(net, clearance)
        if self._scan_seg(layer, x1, y1, x2, y2, w / 2, nid, clr, raised, ignore, True):
            return False
        if self.plan_on and self.plan_hit(net, layer, x1, y1, x2, y2) is not None:
            return False
        return self.inside(x1, y1) and self.inside(x2, y2)

    def via_ok(self, x, y, size: float, drill: float, net: str, ignore: frozenset = frozenset(),
               clearance: float | None = None) -> Blocker | None:
        """None = a through via of ``net`` here is legal on every layer; else the worst hard Blocker."""
        self.checks += 1
        x, y = q(x), q(y)
        nid, clr, raised = self._args(net, clearance)
        vs = self._scan_via(x, y, size / 2, drill, nid, clr, raised, ignore, False)
        if vs:
            return self._blocker(min(vs, key=self._order), 0, x, y, x, y)
        return self._outside(x, y, x, y, 0) or (self._plan_blocker(net, 0, x, y, x, y, via=True)
                                                if self.plan_on else None)

    def via_legal(self, x, y, size: float, drill: float, net: str, ignore: frozenset = frozenset(),
                  clearance: float | None = None) -> bool:
        self.checks += 1
        x, y = q(x), q(y)
        nid, clr, raised = self._args(net, clearance)
        if self._scan_via(x, y, size / 2, drill, nid, clr, raised, ignore, True):
            return False
        if self.plan_on and self.plan_hit(net, None, x, y, x, y) is not None:
            return False
        return self.inside(x, y)

    def piece_ok(self, p: Piece, ignore: frozenset = frozenset(), clearance: float | None = None) -> Blocker | None:
        """seg_ok / via_ok of a Piece; the piece itself is always ignored (it may already be added)."""
        ig = ignore | {p.id}
        if p.kind == "via":
            return self.via_ok(p.x1, p.y1, p.w, p.drill, p.net, ig, clearance)
        return self.seg_ok(p.layer, p.x1, p.y1, p.x2, p.y2, p.w, p.net, clearance, ig)

    def blockers(self, layer: int, x1, y1, x2, y2, w: float, net: str, limit: int = 8,
                 clearance: float | None = None, ignore: frozenset = frozenset()) -> list[Blocker]:
        """Every hard violation and every soft shape crossed by this segment, nearest first."""
        self.checks += 1
        x1, y1, x2, y2 = q(x1), q(y1), q(x2), q(y2)
        nid, clr, raised = self._args(net, clearance)
        vs = self._scan_seg(layer, x1, y1, x2, y2, w / 2, nid, clr, raised, ignore, False)
        out = [self._blocker(v, layer, x1, y1, x2, y2) for v in sorted(vs, key=self._order)]
        o = self._outside(x1, y1, x2, y2, layer)
        if o:
            out.append(o)
        o = self._plan_blocker(net, layer, x1, y1, x2, y2) if self.plan_on else None
        if o:
            out.append(o)
        out += self.soft_hits(layer, x1, y1, x2, y2, w, net)
        out.sort(key=lambda b: (b.dist, b.rule, b.layer, b.at, b.net or "", b.tag or ""))
        return out[:limit]

    def via_blockers(self, x, y, size: float, drill: float, net: str, limit: int = 8,
                     clearance: float | None = None, ignore: frozenset = frozenset()) -> list[Blocker]:
        self.checks += 1
        x, y = q(x), q(y)
        nid, clr, raised = self._args(net, clearance)
        vs = self._scan_via(x, y, size / 2, drill, nid, clr, raised, ignore, False)
        out = [self._blocker(v, 0, x, y, x, y) for v in sorted(vs, key=self._order)]
        o = self._outside(x, y, x, y, 0)
        if o:
            out.append(o)
        o = self._plan_blocker(net, 0, x, y, x, y, via=True) if self.plan_on else None
        if o:
            out.append(o)
        for layer in self._allL:
            out += self.soft_hits(layer, x, y, x, y, size, net)
        out.sort(key=lambda b: (b.dist, b.rule, b.layer, b.at, b.net or "", b.tag or ""))
        return out[:limit]

    # ── soft shapes ───────────────────────────────────────────────────────
    def add_soft(self, rule: str, net: str, layer: int, shape) -> int:
        """Register a soft shape (a thermal-spoke corridor, a reserved via site) owned by ``net``.
        ``shape``: a geom.Shape, a box 7-tuple ``(cx, cy, ux, uy, ihw, ihh, r)`` or a polygon point list.
        ``layer`` -1 = every layer. Returns the soft id (for harden / remove_soft)."""
        if rule not in SOFT_RULES:
            raise ValueError(f"soft rule must be one of {SOFT_RULES}, not {rule!r}")
        if isinstance(shape, Shape):
            kind, g = shape.kind, shape.g
        elif len(shape) == 7 and not isinstance(shape[0], (tuple, list)):
            kind, g = "box", tuple(shape)
        else:
            kind, g = "poly", [tuple(p) for p in shape]
        self._soft_seq += 1
        sid = self._soft_seq
        lays = self._allL if layer < 0 else (layer,)
        s = Shape(kind, g, "soft", self.net_id(net), 0.0, lays, tag=(rule, sid))
        self._softs[sid] = [rule, s, False]
        self._soft.add(s)
        self._version += 1
        return sid

    def harden(self, soft_id: int) -> None:
        """From now on the soft shape is closed to every foreign net (seg_ok / via_ok return its rule)."""
        e = self._softs[soft_id]
        if not e[2]:
            self._soft.remove(e[1])
            e[1].alive = True
            self._hard.add(e[1])
            e[2] = True
            self._version += 1

    def remove_soft(self, soft_id: int) -> None:
        e = self._softs.pop(soft_id, None)
        if e is not None:
            (self._hard if e[2] else self._soft).remove(e[1])
            self._version += 1

    def soft_hits(self, layer: int, x1, y1, x2, y2, w: float, net: str) -> list[Blocker]:
        """Soft shapes of other nets this segment's copper would overlap (thermal / reserve), nearest first.
        A via: call with x2 == x1, y2 == y1 and w = via size, per layer."""
        if not self._soft.b:
            return []
        x1, y1, x2, y2 = q(x1), q(y1), q(x2), q(y2)
        nid = self.net_id(net)
        hw = w / 2
        bb = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        hits = []
        for s in self._soft.near(layer, bb, hw + 0.01):
            if nid and s.net == nid:
                continue
            d = s.dist_seg(x1, y1, x2, y2) - hw
            if d < -FUZZ:
                hits.append((round(d, 7), s.tag[1], s))
        out = []
        for d, sid, s in sorted(hits, key=lambda h: h[:2]):
            sb = s.bbox
            at = geom.pt_seg_proj((sb[0] + sb[2]) / 2, (sb[1] + sb[3]) / 2, x1, y1, x2, y2)
            out.append(Blocker(rule=s.tag[0], net=self.net_name(s.net), tag=f"soft:{sid}", static=True, piece=None,
                               dist=round(d, 6), need=0.0, at=(round(at[0], 6), round(at[1], 6)), layer=layer))
        return out

    # ── dynamic copper ────────────────────────────────────────────────────
    def add_piece(self, p: Piece) -> None:
        if p.id in self._pieces:
            raise ValueError(f"piece {p.id} is already in the oracle")
        nid = self.net_id(p.net)
        clr = self.clearance(p.net)
        if p.kind == "via":
            x, y = q(p.x1), q(p.y1)
            shapes = [Shape("box", geom.box_circle(x, y, p.w / 2), "cu", nid, clr, self._allL, tag=p.id),
                      Shape("box", geom.box_circle(x, y, p.drill / 2), "hole", nid, 0.0, self._allL, tag=p.id)]
        else:
            shapes = [Shape("box", geom.box_from_seg(q(p.x1), q(p.y1), q(p.x2), q(p.y2), p.w), "cu", nid, clr,
                            (p.layer,), tag=p.id)]
        for s in shapes:
            self._dyn.add(s)
        self._pieces[p.id] = (p, shapes)
        self._version += 1

    def remove_piece(self, p: Piece) -> None:
        e = self._pieces.pop(p.id if isinstance(p, Piece) else p, None)
        if e is None:
            return
        for s in e[1]:
            self._dyn.remove(s)
        self._version += 1

    def has_piece(self, pid: int) -> bool:
        return pid in self._pieces

    # ── geometry for the other stages ─────────────────────────────────────
    def near(self, layer: int, bbox, grow: float) -> list:
        """Every hard shape that can matter on ``layer`` within ``bbox`` grown by ``grow``: static shapes
        (board order), outline segments (cls 'edge'), hardened soft shapes (cls 'soft'), then the dynamic
        copper sorted by piece id (``shape.tag`` is the piece id). Feed each to ``need()``."""
        out = list(self._static.near(layer, bbox, grow))
        out += self._edge.near(0, bbox, grow)
        if self._hard.b:
            out += sorted(self._hard.near(layer, bbox, grow), key=lambda s: s.tag[1])
        if self._dyn.b:
            out += sorted(self._dyn.near(layer, bbox, grow), key=lambda s: (s.tag, s.cls))
        return out

    def near_soft(self, layer: int, bbox, grow: float) -> list:
        """Soft (not hardened) shapes near ``bbox``, by soft id; ``shape.tag`` = (rule, soft id), ``shape.net`` = owner."""
        if not self._soft.b:
            return []
        return sorted(self._soft.near(layer, bbox, grow), key=lambda s: s.tag[1])

    def pad_gap(self, pad_a: str, pad_b: str, layer: int) -> float:
        """Exact edge-to-edge copper gap (mm) between two pads ('U3.4') on ``layer``; inf when either has no
        copper there. KeyError for an unknown pad name."""
        best = INF
        for pa in self._pads_by_name[pad_a]:
            for pb in self._pads_by_name[pad_b]:
                for sa in pa.shapes.get(layer, ()):
                    for sb in pb.shapes.get(layer, ()):
                        d = shape_gap(sa, sb)
                        if d < best:
                            best = d
        return best
