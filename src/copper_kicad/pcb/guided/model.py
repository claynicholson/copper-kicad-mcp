"""Board model for the guided router, built from the worker's ``extract`` dict.

Turns pads / tracks / vias / holes / rule areas / the outline into exact
``geom.Shape`` objects, resolves design rules (board minimums, net classes,
simple ``.kicad_dru`` constraints) and builds the per-class occupancy maps.
"""
from __future__ import annotations

import math
import re
from array import array
from pathlib import Path

from . import geom
from .geom import Shape, ShapeIndex
from .grid import EPS, ClassMap, Grid


class Net:
    __slots__ = ("id", "name", "cls", "clearance", "width", "via", "drill", "dp_gap", "dp_width", "pads",
                 "has_pour", "pour_layers")

    def __init__(self, nid, name, d, rules):
        df = rules.get("default") or {}
        self.id, self.name = nid, name
        self.cls = d.get("class") or "Default"
        self.clearance = max(d.get("clearance") or df.get("clearance") or 0.2, rules["clearance"])
        self.width = max(d.get("width") or df.get("width") or 0.25, rules["track_width"])
        self.via = max(d.get("via") or df.get("via") or 0.6, rules["via"])
        self.drill = max(d.get("drill") or df.get("drill") or 0.3, rules["drill"])
        if self.via < self.drill + 2 * rules["annular"]:
            self.via = round(self.drill + 2 * rules["annular"], 4)
        self.dp_gap = d.get("dp_gap") or df.get("dp_gap") or 0.2
        self.dp_width = d.get("dp_width") or df.get("dp_width") or self.width
        self.pads = []
        self.has_pour = False
        self.pour_layers = []


class Pad:
    __slots__ = ("ref", "num", "net", "x", "y", "kind", "layers", "shapes", "cluster", "fn", "pt", "name")

    def __init__(self):
        self.shapes = {}


_DRU_RULE = re.compile(r"\(rule\s+(\"[^\"]*\"|\S+)(.*?)(?=\(rule\s|\Z)", re.S)
_DRU_CONSTRAINT = re.compile(r"\(constraint\s+(\w+)\s*\(min\s+([-\d.]+)\s*(mm|mil|in)?\)")


def dru_minimums(path: Path) -> dict:
    """Conservative read of a .kicad_dru: the largest ``(min …)`` of the constraints the router
    cares about. Conditions are ignored on purpose (obeying a rule everywhere is always legal),
    except ``hole_size`` which only matters for vias."""
    out = {}
    try:
        txt = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    txt = "\n".join(l for l in txt.splitlines() if not l.lstrip().startswith("#"))
    unit = {"mm": 1.0, None: 1.0, "mil": 0.0254, "in": 25.4}
    for m in _DRU_RULE.finditer(txt):
        body = m.group(2)
        cond = re.search(r"\(condition\s+\"([^\"]*)\"", body)
        cond = cond.group(1).lower() if cond else ""
        if "(layer" in body and "cu" not in body.lower():
            continue
        for c in _DRU_CONSTRAINT.finditer(body):
            name, val = c.group(1), float(c.group(2)) * unit.get(c.group(3), 1.0)
            if name in ("hole_size", "via_diameter", "annular_width") and cond and "via" not in cond:
                continue
            if name in ("clearance", "edge_clearance", "hole_clearance", "hole_to_hole", "track_width",
                        "via_diameter", "hole_size", "annular_width"):
                out[name] = max(out.get(name, 0.0), val)
    return out


def canonical(ex: dict) -> dict:
    """Order everything by position / name, never by KiCad's (random) UUID file order, and renumber
    the connectivity clusters accordingly — so the same design always routes the same way.

    The extract is never altered: one extract is the source of several Boards (session, engine, judge), and the
    tracks / vias of every one of them must be renumbered from the SAME raw pad cluster ids. (Renumbering the pad
    dicts in place made the second Board look up raw track ids in already renumbered pad ids: existing copper
    then lost its cluster, or joined the cluster of an unrelated pad whose new number equalled the raw id.)"""
    def pkey(p):
        return (p["ref"], p["num"], p["x"], p["y"])
    pads = sorted((dict(p) for p in ex.get("pads") or []), key=pkey)
    first = {}
    for p in pads:
        c = p.get("c")
        if c and c not in first:
            first[c] = len(first) + 1
    for p in pads:
        if p.get("c"):
            p["c"] = first[p["c"]]
    out = dict(ex)
    out["pads"] = pads
    tracks = [list(t) for t in ex.get("tracks") or []]
    for t in tracks:
        t[7] = first.get(t[7], 0)
    out["tracks"] = sorted(tracks, key=lambda t: (t[0], t[1], t[2], t[3], t[4], t[5], t[6]))
    vias = [list(v) for v in ex.get("vias") or []]
    for v in vias:
        v[7] = first.get(v[7], 0)
    out["vias"] = sorted(vias, key=lambda v: (v[0], v[1], v[2], v[3]))

    def zkey(z):
        o = (z.get("polys") or [{}])[0].get("o") or [[0, 0]]
        return (z.get("name") or "", z.get("net") or "", z.get("ref") or "", str(z.get("layers")), min(map(tuple, o)))
    zones = sorted(ex.get("zones") or [], key=zkey)
    for z in zones:
        for k, plist in (z.get("fills") or {}).items():
            plist.sort(key=lambda p: (min(map(tuple, p["o"])), len(p["o"])))
    out["zones"] = zones
    out["footprints"] = sorted(ex.get("footprints") or [], key=lambda f: (f["ref"], f["x"], f["y"]))
    out["graphics"] = sorted(ex.get("graphics") or [], key=lambda g: (g["layer"], g["bbox"]))
    out["mask_openings"] = sorted(ex.get("mask_openings") or [], key=lambda m: (m["layer"], min(map(tuple, m["poly"]))))
    return out


class Board:
    def __init__(self, ex: dict, dru: dict | None = None):
        ex = canonical(ex)
        self.ex = ex
        self.layers = list(ex["layers"])
        self.L = len(self.layers)
        r = dict(ex.get("rules") or {})
        dru = dru or {}
        self.rules = {
            "clearance": max(r.get("clearance", 0.0), dru.get("clearance", 0.0)),
            "track_width": max(r.get("track_width", 0.0), dru.get("track_width", 0.0)) or 0.1,
            "via": max(r.get("via", 0.0), dru.get("via_diameter", 0.0)) or 0.4,
            "drill": max(r.get("drill", 0.0), dru.get("hole_size", 0.0)) or 0.2,
            "annular": max(r.get("annular", 0.0), dru.get("annular_width", 0.0)),
            "edge_clearance": max(r.get("edge_clearance", 0.0), dru.get("edge_clearance", 0.0)),
            "hole_clearance": max(r.get("hole_clearance", 0.0), dru.get("hole_clearance", 0.0)),
            "hole_to_hole": max(r.get("hole_to_hole", 0.0), dru.get("hole_to_hole", 0.0)),
            "default": r.get("default") or {},
        }
        self.warnings = []
        names = sorted(ex["nets"])
        self.nets = {n: Net(i + 1, n, ex["nets"][n], self.rules) for i, n in enumerate(names)}
        self.net_by_id = {n.id: n for n in self.nets.values()}
        self.default_clr = max((self.rules["default"].get("clearance") or 0.2), self.rules["clearance"])
        self.shapes = []
        self.index = ShapeIndex()
        self.pads = []
        self.smd_pad_shapes = []
        self.clusters = {}        # net name -> {cid: {'pads': [...], 'tracks': [...], 'vias': [...], 'islands': [...]}}
        self._build_pads(ex)
        self._build_copper(ex)
        self._build_zones(ex)
        self._build_outline(ex)
        # parts that are not on the board (unplaced, parked beside the outline) can never be routed: say so once
        off = sorted({p.ref for p in self.pads if p.net and not self.inside(p.x, p.y)})
        self.off_board = off
        if off and self.outline_closed:
            self.warnings.append(f"{len(off)} part(s) with connected pads lie outside the board outline "
                                 f"({', '.join(off[:8])}{'…' if len(off) > 8 else ''}): their connections cannot be "
                                 f"routed — place them on the board first (pcb_place)")
        for s in self.shapes:
            self.index.add(s)
        self.grid = None
        self.step = None

    # ── construction ──────────────────────────────────────────────────────
    def _add(self, s):
        self.shapes.append(s)
        return s

    def _cluster(self, net, cid):
        return self.clusters.setdefault(net, {}).setdefault(cid, {"pads": [], "tracks": [], "vias": [], "islands": []})

    def _build_pads(self, ex):
        allL = tuple(range(self.L))
        for d in ex["pads"]:
            p = Pad()
            p.ref, p.num, p.x, p.y, p.kind = d["ref"], d["num"], d["x"], d["y"], d["kind"]
            p.name = f"{p.ref}.{p.num}" if p.num else p.ref
            net = self.nets.get(d.get("net") or "")
            p.net = net.id if net else 0
            p.layers = tuple(d.get("layers") or ())
            p.cluster = d.get("c", 0)
            p.fn, p.pt = d.get("fn", ""), d.get("pt", "")
            clr = d.get("clearance") or (net.clearance if net else self.default_clr)
            for lays, kind, g in d.get("shapes") or []:
                if kind == "box":
                    shs = [Shape("box", geom.box_from_pad(g), "cu", p.net, clr, lays, tag=p)]
                else:
                    shs = [Shape("poly", [tuple(q) for q in o], "cu", p.net, clr, lays, tag=p) for o in g if len(o) >= 3]
                for s in shs:
                    self._add(s)
                    for l in lays:
                        p.shapes.setdefault(l, []).append(s)
                    if p.kind in ("smd", "conn"):
                        self.smd_pad_shapes.append(s)
            h = d.get("hole")
            if h:
                cx, cy, ux, uy, hx, hy = h
                r = min(hx, hy)
                self._add(Shape("box", (cx, cy, ux, uy, hx - r, hy - r, r), "hole", p.net, 0.0, allL, tag=p))
            self.pads.append(p)
            if net:
                net.pads.append(p)
                if p.cluster and p.layers:
                    self._cluster(net.name, p.cluster)["pads"].append(p)

    def _build_copper(self, ex):
        allL = tuple(range(self.L))
        self.kept_tracks, self.kept_vias = [], []
        for net, lay, x1, y1, x2, y2, w, cid, locked in ex.get("tracks") or []:
            n = self.nets.get(net)
            s = self._add(Shape("box", geom.box_from_seg(x1, y1, x2, y2, w), "cu", n.id if n else 0,
                                n.clearance if n else self.default_clr, (lay,)))
            t = (net, lay, x1, y1, x2, y2, w)
            self.kept_tracks.append(t)
            if n and cid and cid in self.clusters.get(net, {}):
                self._cluster(net, cid)["tracks"].append(t)
        for net, x, y, size, drill, top, bot, cid, locked in ex.get("vias") or []:
            n = self.nets.get(net)
            nid = n.id if n else 0
            self._add(Shape("box", geom.box_circle(x, y, size / 2), "cu", nid,
                            n.clearance if n else self.default_clr, tuple(range(top, bot + 1))))
            self._add(Shape("box", geom.box_circle(x, y, drill / 2), "hole", nid, 0.0, allL))
            v = (net, x, y, size, drill, top, bot)
            self.kept_vias.append(v)
            if n and cid and cid in self.clusters.get(net, {}):
                self._cluster(net, cid)["vias"].append(v)
        for m in ex.get("mask_openings") or []:
            pts = [tuple(q) for q in m["poly"]]
            if len(pts) >= 3:
                self._add(Shape("poly", pts, "kt", 0, 0.0, (m["layer"],)))
                self._add(Shape("poly", pts, "kv", 0, 0.0, (m["layer"],)))
        for g in ex.get("graphics") or []:
            x0, y0, x1, y1 = g["bbox"]
            n = self.nets.get(g.get("net") or "")
            self._add(Shape("box", ((x0 + x1) / 2, (y0 + y1) / 2, 1.0, 0.0, (x1 - x0) / 2, (y1 - y0) / 2, 0.0), "cu",
                            n.id if n else 0, n.clearance if n else self.default_clr, (g["layer"],)))

    def _build_zones(self, ex):
        self.keepouts = []
        self.pours = []
        for z in ex.get("zones") or []:
            polys = [[tuple(q) for q in p["o"]] for p in z.get("polys") or [] if len(p.get("o") or []) >= 3]
            if z.get("rule_area"):
                if not (z.get("no_tracks") or z.get("no_vias")):
                    continue
                bb = None
                for pts in polys:
                    if z.get("no_tracks"):
                        self._add(Shape("poly", pts, "kt", 0, 0.0, z["layers"]))
                    if z.get("no_vias"):
                        self._add(Shape("poly", pts, "kv", 0, 0.0, z["layers"]))
                    b = geom.poly_bbox(pts)
                    bb = b if bb is None else (min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3]))
                if bb:
                    self.keepouts.append({"name": z.get("name") or (f"{z['ref']} keepout" if z.get("ref") else ""),
                                          "rect": [round(v, 3) for v in bb], "layers": [self.layers[l] for l in z["layers"]],
                                          "no_tracks": bool(z.get("no_tracks")), "no_vias": bool(z.get("no_vias")),
                                          **({"ref": z["ref"]} if z.get("ref") else {})})
                continue
            net = self.nets.get(z.get("net") or "")
            if z.get("teardrop"):
                for pts in polys:
                    self._add(Shape("poly", pts, "cu", net.id if net else 0,
                                    net.clearance if net else self.default_clr, z["layers"]))
                continue
            if not net:
                continue
            net.has_pour = True
            for l in z["layers"]:
                if l not in net.pour_layers:
                    net.pour_layers.append(l)
            area = sum(geom.poly_area(p) for p in polys)
            self.pours.append({"net": net.name, "layers": [self.layers[l] for l in z["layers"]], "name": z.get("name", ""),
                               "filled": bool(z.get("filled")), "area_mm2": round(area, 1), "priority": z.get("priority", 0)})
            for lk, plist in (z.get("fills") or {}).items():
                lay = int(lk)
                for p in plist:
                    pts = [tuple(q) for q in p["o"]]
                    if len(pts) < 3:
                        continue
                    cid = self._island_cluster(net, lay, pts)
                    if cid:
                        self._cluster(net.name, cid)["islands"].append((lay, pts))

    def _island_cluster(self, net, lay, pts):
        """Which connectivity cluster does this fill island belong to? (the cluster of a pad it touches)"""
        bb = geom.poly_bbox(pts)
        for p in net.pads:
            if not p.cluster or lay not in p.layers:
                continue
            if not (bb[0] - 1 <= p.x <= bb[2] + 1 and bb[1] - 1 <= p.y <= bb[3] + 1):
                continue
            if geom.pt_in_poly(pts, p.x, p.y):
                return p.cluster
            for s in p.shapes.get(lay, ()):
                sb = s.bbox
                for x, y in pts:
                    if sb[0] - 0.05 <= x <= sb[2] + 0.05 and sb[1] - 0.05 <= y <= sb[3] + 0.05 and s.dist_pt(x, y) <= 0.03:
                        return p.cluster
        for cid, c in self.clusters.get(net.name, {}).items():
            for v in c["vias"]:
                if v[5] <= lay <= v[6] and geom.pt_in_poly(pts, v[1], v[2]):
                    return cid
        return 0

    def _build_outline(self, ex):
        ol = ex.get("outline") or {}
        polys = []
        for p in ol.get("polys") or []:
            if len(p.get("o") or []) >= 3:
                polys.append(([tuple(q) for q in p["o"]], [[tuple(q) for q in h] for h in p.get("h") or [] if len(h) >= 3]))
        self.outline_closed = bool(polys)
        if not polys:
            xs, ys = [], []
            for p in self.pads:
                xs.append(p.x)
                ys.append(p.y)
            for s in self.shapes:
                xs += [s.bbox[0], s.bbox[2]]
                ys += [s.bbox[1], s.bbox[3]]
            if ol.get("bbox"):
                x0, y0, x1, y1 = ol["bbox"]
            elif xs:
                x0, y0, x1, y1 = min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2
            else:
                x0, y0, x1, y1 = 0, 0, 10, 10
            polys = [([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], [])]
            self.warnings.append("the board has no closed Edge.Cuts outline: routing inside the bounding box of the "
                                 "parts (+2 mm). pcb_setup(width=..., height=...) draws an outline.")
        self.outline = polys
        xs = [q[0] for o, _h in polys for q in o]
        ys = [q[1] for o, _h in polys for q in o]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        self.area = sum(geom.poly_area(o) - sum(geom.poly_area(h) for h in hs) for o, hs in polys)

    def inside(self, x, y) -> bool:
        """Is the point on the board (inside an outline polygon and not in one of its cut-outs)?"""
        for o, holes in self.outline:
            if geom.pt_in_poly(o, x, y) and not any(geom.pt_in_poly(h, x, y) for h in holes):
                return True
        return False

    # ── grid + class maps ─────────────────────────────────────────────────
    def make_grid(self, step: float):
        self.step = step
        self.grid = Grid(self.bbox, step, self.L)
        return self.grid

    def auto_step(self, max_cells=1_600_000):
        w, h = self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]
        for s in (0.1, 0.125, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
            if (w / s + 3) * (h / s + 3) * self.L <= max_cells:
                return s
        return 0.5

    def class_key(self, width, clearance, via, drill):
        return (round(width / 2, 4), round(max(clearance, self.rules["clearance"]), 4), round(via / 2, 4),
                round(drill, 4))

    def class_map(self, key) -> ClassMap:
        g = self.grid
        cm = g.classes.get(key)
        if cm is None:
            cm = self._build_class(key)
            g.classes[key] = cm
        return cm

    def _build_class(self, key):
        g = self.grid
        G, L, W, H = g.G, g.L, g.W, g.H
        h, c, vr, vd = key
        R = self.rules
        cm = ClassMap(key, g.N, G)
        # everything outside the outline is blocked
        one = array("i", [-1]) * G
        inside = array("i", one)
        zero = array("i", [0]) * W
        for o, holes in self.outline:
            for iy, i0, i1 in g.poly_spans(o):
                a = iy * W + i0
                inside[a:a + i1 - i0 + 1] = zero[:i1 - i0 + 1]
            for hp in holes:
                for iy, i0, i1 in g.poly_spans(hp):
                    a = iy * W + i0
                    inside[a:a + i1 - i0 + 1] = one[:i1 - i0 + 1]
        sv = array("i", inside)
        et, ev = R["edge_clearance"] + h + EPS, R["edge_clearance"] + vr + EPS
        for o, holes in self.outline:
            for pts in [o] + holes:
                n = len(pts)
                for i in range(n):
                    x1, y1 = pts[i]
                    x2, y2 = pts[(i + 1) % n]
                    g.stamp(inside, 0, g.capsule_spans(x1, y1, x2, y2, et), -1)
                    g.stamp(sv, 0, g.capsule_spans(x1, y1, x2, y2, ev), -1)
        S = cm.S
        for l in range(L):
            S[l * G:(l + 1) * G] = inside
        SV = cm.SV
        SV[0:G] = sv
        hole_t = R["hole_clearance"] + h + EPS
        hole_v = max(R["hole_clearance"] + vr, R["hole_to_hole"] + vd / 2) + EPS
        for s in self.shapes:
            cls = s.cls
            if cls == "cu":
                sep = c if c > s.clr else s.clr
                own = s.net or -1
                spans = list(g.shape_spans(s, h + sep + EPS))
                for l in s.layers:
                    g.stamp(S, l * G, spans, own)
                g.stamp(SV, 0, g.shape_spans(s, vr + sep + EPS), own)
            elif cls == "hole":
                spans = list(g.shape_spans(s, hole_t))
                own = s.net or -1
                for l in range(L):
                    g.stamp(S, l * G, spans, own)
                g.stamp(SV, 0, g.shape_spans(s, hole_v), -1)
            elif cls == "kt":
                spans = list(g.shape_spans(s, h + EPS))
                for l in s.layers:
                    g.stamp(S, l * G, spans, -1)
            elif cls == "kv":
                g.stamp(SV, 0, g.shape_spans(s, vr + EPS), -1)
        # no via-in-pad: a via may touch an SMD pad of its net but not sit on it
        for s in self.smd_pad_shapes:
            g.stamp(SV, 0, g.shape_spans(s, vr * 0.6), -1)
        cm.freeze()
        return cm

    # ── exact checks ──────────────────────────────────────────────────────
    def seg_clear(self, lay, x1, y1, x2, y2, hw, net_id, clr, extra=None):
        """True when a track segment (half width ``hw``) keeps its clearance to every static shape and
        to every shape in ``extra`` (a second ShapeIndex holding routed copper)."""
        R = self.rules
        bb = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        grow = hw + max(clr, R["edge_clearance"], R["hole_clearance"]) + 0.6
        for idx in (self.index, extra):
            if idx is None:
                continue
            for s in idx.near(lay, bb, grow):
                cls = s.cls
                if cls == "cu":
                    if s.net == net_id and net_id:
                        continue
                    need = hw + (clr if clr > s.clr else s.clr)
                elif cls == "hole":
                    if s.net == net_id and net_id:
                        continue
                    need = hw + R["hole_clearance"]
                elif cls == "kt":
                    need = hw
                else:
                    continue
                sb = s.bbox
                if sb[0] - need > bb[2] or sb[2] + need < bb[0] or sb[1] - need > bb[3] or sb[3] + need < bb[1]:
                    continue
                if s.dist_seg(x1, y1, x2, y2) < need + 0.002:
                    return False
        # board edge
        need = hw + R["edge_clearance"] + 0.002
        for o, holes in self.outline:
            for pts in [o] + holes:
                n = len(pts)
                px, py = pts[n - 1]
                for i in range(n):
                    qx, qy = pts[i]
                    if not (min(px, qx) - need > bb[2] or max(px, qx) + need < bb[0]
                            or min(py, qy) - need > bb[3] or max(py, qy) + need < bb[1]):
                        if geom.seg_seg_dist(x1, y1, x2, y2, px, py, qx, qy) < need:
                            return False
                    px, py = qx, qy
        return True
