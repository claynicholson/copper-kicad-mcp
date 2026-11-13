"""Choose which planar section of a 3D model is the board outline (pure stdlib).

Works on *plane groups*: all planar faces of one solid that lie in one plane with one outward normal.
The boundary of a group is the union of its faces, so a board top that a CAD system split into 48
faces (imprinted pads) still gives one outline with its true holes.

Decision (``decide``):
  board      two opposite parallel groups of one solid, equal extents, a PCB-like distance apart
  enclosure  the largest recessed plane that is open above (tested along every axis of the model)
  face       the caller picked a candidate id
  largest_plane   nothing better; always reported as ambiguous

The result is never silently "confident" unless the evidence is strong: see ``report['ambiguous']``
and ``report['why']``. Readers that are not STEP (STL) supply objects with the same small interface
as ``Group`` (n, d, solid, gross, min_id, loops(up)).
"""
from __future__ import annotations

import math

from . import ir
from . import step as p21

AXES = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1), "-z": (0, 0, -1)}


def axis_name(n):
    k = max(range(3), key=lambda i: abs(n[i]))
    if abs(n[k]) < 0.999:
        return "(%.2f,%.2f,%.2f)" % tuple(n)
    return ("+" if n[k] > 0 else "-") + "xyz"[k]


def frame(w):
    """Projection axes for a view from +w: x = world X made orthogonal to w (world Y when w is along X)."""
    ex = (1.0, 0.0, 0.0) if abs(w[0]) < 0.7 else (0.0, 1.0, 0.0)
    d = p21.dot(ex, w)
    ex = p21.norm((ex[0] - d * w[0], ex[1] - d * w[1], ex[2] - d * w[2]))
    return ex, p21.cross(w, ex)


class Group:
    """All planar STEP faces of one solid lying in one plane with one outward normal."""

    def __init__(self, n, d, solid=None):
        self.n, self.d, self.solid, self.faces = n, d, solid, []
        self.gross = 0.0
        self.min_id = 1 << 60
        self.unknown = 0
        self._ext = None
        self._cache = {}

    def loops(self, up=None):
        """(closed loops, open chains): boundary of the union of the faces, KiCad axes seen from +up."""
        up = tuple(up or self.n)
        key = tuple(round(v, 6) for v in up)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        cnt = {}
        for f in self.faces:
            for eids in f.eids:
                for e in eids:
                    cnt[e] = cnt.get(e, 0) + 1
        prims = []
        for f in self.faces:
            for (li, pi), ps in _face_prims(f, up).items():
                if cnt[f.eids[li][pi]] == 1:
                    prims += ps
        loops, opens = ir.chain(prims, tol=2e-3)
        res = ([ir.simplify(l) for l in loops], opens)
        self._cache[key] = res
        return res

    def extent(self):
        """(area, bbox) of the largest boundary loop in the group's own view; refines .gross."""
        if self._ext is None:
            self._ext = _extent(self.loops()[0])
            if self._ext[0]:
                self.gross = self._ext[0]
        return self._ext

    def bbox_on(self, up):
        pts = [p for lp in self.loops(up)[0] for p in ir.loop_pts(lp)]
        if not pts:
            return None
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _extent(loops):
    best = (0.0, None)
    for lp in loops:
        pts = ir.loop_pts(lp)
        if len(pts) < 3:
            continue
        a = abs(ir.poly_area(pts))
        if a > best[0]:
            best = (a, (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)))
    return best


def _face_prims(f, w):
    ex, ey = frame(w)

    def k(p):
        q = (f.origin[0] + p[0] * f.u[0] + p[1] * f.v[0], f.origin[1] + p[0] * f.u[1] + p[1] * f.v[1],
             f.origin[2] + p[0] * f.u[2] + p[1] * f.v[2])
        return (p21.dot(q, ex), -p21.dot(q, ey))
    out = {}
    for li, (_, prims) in enumerate(f.loops):
        for pi, p in enumerate(prims):
            if p[0] == "L":
                ps = [("L", k(p[1]), k(p[2]))]
            elif p[0] == "C":
                ps = [("C", k(p[1]), p[2])]
            elif p[0] == "A":
                pts = p21.arc_pts(p[1], p[2], p[3], p[4], p[5], seg=2)
                ps = [("A", k(pts[0]), k(pts[1]), k(pts[2]))]
            else:
                pts = [k(q) for q in p[1]]
                ps = [("L", pts[i], pts[i + 1]) for i in range(len(pts) - 1) if math.dist(pts[i], pts[i + 1]) > 1e-9]
            out[(li, pi)] = ps
    return out


def groups(faces, ang_q=1e-3, d_q=0.002):
    """STEP faces -> plane groups, stable order: larger extent first, then level, then lowest entity id."""
    gs = {}
    for f in faces:
        key = (f.solid, round(f.n[0] / ang_q), round(f.n[1] / ang_q), round(f.n[2] / ang_q), round(f.d / d_q))
        g = gs.get(key)
        if g is None:
            g = gs[key] = Group(f.n, f.d, f.solid)
        g.faces.append(f)
        g.gross = max(g.gross, f.gross)      # largest member face; refined by extent() for the top candidates
        g.min_id = min(g.min_id, f.id)
        g.unknown += f.unknown
    return order(list(gs.values()))


def order(gs, refine=16):
    gs.sort(key=lambda g: (-g.gross, g.min_id))
    for g in gs[:refine]:
        g.extent()
    gs.sort(key=lambda g: (-round(g.gross, 3), round(g.d, 3), g.min_id))
    return gs


def lvl(g, up):
    """Level of the plane along +up."""
    return g.d * (1 if p21.dot(g.n, up) > 0 else -1)


# ── board body ────────────────────────────────────────────────────────────
def find_plate(gs, axis=None, tmin=0.2, tmax=6.5):
    """Largest pair of opposite plane groups in one solid, tmin..tmax apart, equal extents -> (top, bottom, t)."""
    ps = find_plates(gs, axis, tmin, tmax)
    return ps[0] if ps else None


def find_plates(gs, axis=None, tmin=0.2, tmax=6.5):
    """Every plate (top, bottom, t), largest first, one per solid."""
    if not gs:
        return []
    found = {}
    for a in gs[:12]:
        for b in gs[:16]:
            if b is a or b.solid != a.solid or p21.dot(a.n, b.n) > -0.999:
                continue
            t = a.d + b.d             # outward normals: a.d = o_a.n, b.d = o_b.(-n)  ->  thickness
            ea, eb = a.extent(), b.extent()
            if not (tmin <= t <= tmax and ea[1] and eb[1]):
                continue
            if abs(ea[0] - eb[0]) > 0.05 * ea[0]:
                continue
            if abs((ea[1][2] - ea[1][0]) - (eb[1][2] - eb[1][0])) >= 0.05 or abs((ea[1][3] - ea[1][1]) - (eb[1][3] - eb[1][1])) >= 0.05:
                continue
            if ea[0] >= 0.3 * gs[0].gross and (a.solid not in found or ea[0] > found[a.solid][3]):
                if axis is not None and abs(p21.dot(a.n, axis)) > 0.999:
                    top, bot = (a, b) if p21.dot(a.n, axis) > 0 else (b, a)
                else:
                    top, bot = (a, b) if (a.n[2], a.n[1], a.n[0]) > (b.n[2], b.n[1], b.n[0]) else (b, a)
                found[a.solid] = (top, bot, t, ea[0])
    return [v[:3] for v in sorted(found.values(), key=lambda v: (-v[3], v[0].min_id))]


def plate_ok(top, t):
    """Plausible PCB: standard thickness (substrate-only exports are about 0.09 mm thin) and short side >= 8 mm."""
    e = top.extent()[1]
    short = min(e[2] - e[0], e[3] - e[1])
    return any(-0.13 <= t - s <= 0.05 for s in ir.STD_T) and short >= 8.0


def _sig(lp):
    pts = ir.loop_pts(lp)
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts), abs(ir.poly_area(pts)))


def through(loops_a, loops_b):
    """Loops of side A that also exist on side B (same centroid 0.05 mm, same area 3 %) = real holes."""
    sb = [_sig(l) for l in loops_b]
    keep = []
    for lp in loops_a:
        x, y, ar = _sig(lp)
        if any(abs(x - q[0]) < 0.05 and abs(y - q[1]) < 0.05 and abs(ar - q[2]) <= 0.03 * max(ar, 1e-9) for q in sb):
            keep.append(lp)
    return keep


# ── enclosure cavity ──────────────────────────────────────────────────────
def cavities(gs, axis=None, min_frac=0.2, top=24):
    """Recessed plane groups that are open above, over both signs of every axis of the largest groups."""
    if not gs:
        return []
    axes = []
    if axis is not None:
        ups = [tuple(axis)]
    else:
        for g in gs[:top]:
            if not any(abs(p21.dot(g.n, a)) > 0.999 for a in axes):
                axes.append(g.n)
        ups = [tuple(c * s for c in a) for a in axes for s in (1, -1)]
    out = []
    big = gs[0].gross
    for up in ups:
        par = [g for g in gs if abs(p21.dot(g.n, up)) > 0.999]
        for solid in sorted({g.solid for g in par}, key=lambda s: (s is None, s or 0)):
            ps = [g for g in par if g.solid == solid]
            lo = min(lvl(g, up) for g in ps)
            hi = max(lvl(g, up) for g in ps)
            for g in ps[:top]:
                if p21.dot(g.n, up) < 0 or g.gross < min_frac * big:
                    continue
                l = lvl(g, up)
                if not (lo + 0.3 < l < hi - 0.3):
                    continue
                bb = g.bbox_on(up)
                if not bb:
                    continue
                cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
                covered = False
                for h in ps:      # a down-facing plane of the same solid above the floor whose material covers its centre
                    if p21.dot(h.n, up) > 0 or lvl(h, up) <= l + 0.05 or h.gross < 0.25 * g.gross:
                        continue
                    hb = h.bbox_on(up)
                    if hb and hb[0] < cx < hb[2] and hb[1] < cy < hb[3]:
                        if sum(1 for lp in h.loops(up)[0] if ir.pip(ir.loop_pts(lp), cx, cy)) % 2 == 1:
                            covered = True
                            break
                if covered:
                    continue
                out.append({"floor": g, "up": up, "level": l, "bottom": lo, "rim": hi, "depth": hi - l, "bbox": bb,
                            "solid": solid, "ups": sorted(((x, lvl(x, up)) for x in ps if p21.dot(x.n, up) > 0),
                                                           key=lambda t: t[1]),
                            "downs": [x for x in ps if p21.dot(x.n, up) < 0]})
    out.sort(key=lambda c: (-c["floor"].gross, -c["up"][2], -c["up"][1], -c["up"][0]))
    return out


def _pilot_size(pd):
    return "M2" if pd <= 1.9 else "M2.5" if pd <= 2.4 else "M3" if pd <= 3.3 else "M4"


def enclosure_outline(cav, clearance):
    """Cavity floor -> (outline dict, enclosure info, ambiguous reasons). Floor islands are classified:
    window through the floor / boss (+ screw pilot -> mounting hole) / rib or wall (cutout) / low feature."""
    up = cav["up"]
    loops, opens = cav["floor"].loops(up)
    why = []
    if not loops:
        return None, None, ["the cavity floor has no closed boundary"]
    big = max(loops, key=lambda l: abs(ir.poly_area(ir.loop_pts(l))))
    inner = [l for l in loops if l is not big]
    bottom = [l for h in cav["downs"] if abs(lvl(h, up) - cav["bottom"]) < 0.01 for l in h.loops(up)[0]]
    windows = through(inner, bottom)
    win_ids = {id(l) for l in windows}
    higher = []          # (loop, level) of every up-facing plane above the floor
    for g, l in cav["ups"]:
        if l > cav["level"] + 0.05:
            for lp in g.loops(up)[0]:
                higher.append((lp, l))
    circ_hi = [(lp[0][1], lp[0][2], l) for lp, l in higher if len(lp) == 1 and lp[0][0] == "C"]
    sig_hi = [(_sig(lp), l) for lp, l in higher]
    bosses, cut, unknown = [], [], 0
    for lp in inner:
        if id(lp) in win_ids:
            continue
        if len(lp) == 1 and lp[0][0] == "C":
            c, r = lp[0][1], lp[0][2]
            conc = [(rr, l) for cc, rr, l in circ_hi if math.dist(cc, c) < 0.05]
            if conc:
                same = [l for rr, l in conc if abs(rr - r) < 0.05]
                topl = max(same) if same else max(l for _, l in conc)
                pilot = min((rr for rr, l in conc if rr < r - 0.05), default=None)
                b = {"x": round(c[0], 4), "y": round(c[1], 4), "boss_d": round(2 * r, 3), "height": round(topl - cav["level"], 3)}
                if pilot:
                    b["pilot_d"] = round(2 * pilot, 3)
                    b["size"] = _pilot_size(2 * pilot)
                bosses.append(b)
                continue
        x, y, ar = _sig(lp)
        tops = [l for (sx, sy, sa), l in sig_hi if abs(sx - x) < 0.05 and abs(sy - y) < 0.05 and abs(sa - ar) <= 0.03 * max(ar, 1e-9)]
        if tops:
            cut.append((lp, max(tops) - cav["level"]))
        else:
            unknown += 1
            cut.append((lp, None))
    seat = max((b["height"] for b in bosses), default=0.0)
    cutouts, keep_low = [], []
    for lp, h in cut:
        if h is not None and h <= seat + 0.2 and seat > 0:
            keep_low.append(lp)          # lower than the board seat: a bottom-side keepout, not a cutout
        else:
            cutouts.append(lp)
    if unknown:
        why.append("%d floor feature(s) with no flat top (sloped or curved): treated as cutouts" % unknown)
    pts = ir.loop_pts(big)
    area = abs(ir.poly_area(pts)) - sum(abs(ir.poly_area(ir.loop_pts(c))) for c in cutouts)
    ol = {"outer": big, "cutouts": cutouts, "area": area, "thickness": None,
          "holes": [{"x": b["x"], "y": b["y"], "size": b["size"], "d": ir.MOUNT_D[b["size"]]} for b in bosses if b.get("size")],
          "ignored": {"drills": 0, "narrow_slots": 0, "islands": 0, "other_loops": 0, "open_chains": len(opens),
                      "windows": len(windows)},
          "bbox": [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)],
          "keepouts_bottom": keep_low}
    enc = {"cavity_depth": round(cav["depth"], 3), "floor_thickness": round(cav["level"] - cav["bottom"], 3),
           "seat_height": round(seat, 3), "bosses": len(bosses), "up": axis_name(up),
           "max_height_bottom": round(seat, 2) if bosses else None,
           "max_height_top": round(cav["depth"] - seat - 1.6, 2)}
    ol["enclosure"] = enc
    return ol, enc, why


# ── the decision ──────────────────────────────────────────────────────────
def candidates(gs, plate=None, cavs=None, limit=5, include=None):
    marks = {}
    if plate:
        marks[id(plate[0])] = "board top"
        marks[id(plate[1])] = "board bottom"
    for c in cavs or []:
        marks.setdefault(id(c["floor"]), "cavity floor")
    out = []
    show = {i for i, g in enumerate(gs[:16]) if id(g) in marks}       # the recognised ones are always offered
    if include is not None:
        show.add(include)
    for i in range(min(len(gs), 16)):
        if len(show) >= max(limit, 1 + (include is not None)):
            break
        show.add(i)
    for i, g in enumerate(gs[:16]):
        if i not in show:
            continue
        e = g.extent()
        size = [round(e[1][2] - e[1][0], 2), round(e[1][3] - e[1][1], 2)] if e[1] else None
        out.append({"id": i, "kind": marks.get(id(g), "plane"), "normal": axis_name(g.n), "level": round(g.d, 3), "size": size})
    return out


def decide(gs, mode="auto", pick=None, axis=None, holes="mounting", clearance=None, unresolved=0):
    """-> (outline dict | None, report). report: mode, picked, ambiguous, why[], candidates[], thickness, ..."""
    up_axis = AXES.get(axis) if isinstance(axis, str) else axis
    rep = {"mode": None, "picked": None, "ambiguous": False, "why": [], "plane_groups": len(gs)}
    if not gs:
        return None, rep
    plate = find_plate(gs, up_axis) if mode in ("auto", "board") else None
    cavs = cavities(gs, up_axis) if mode in ("auto", "enclosure") else []
    cav = cavs[0] if cavs else None
    why = rep["why"]
    chosen = None
    if mode == "face" or pick is not None:
        if pick is None or not (0 <= int(pick) < min(len(gs), 16)):
            rep["candidates"] = candidates(gs, plate, cavs)
            rep["error"] = "outline_pick must be one of the candidate ids"
            return None, rep
        g = gs[int(pick)]
        match = next((c for c in cavs if c["floor"] is g), None)
        if mode == "enclosure" and match is None:
            match = next((c for c in cavities(gs, g.n) if c["floor"] is g), None)
        if plate and g in plate[:2] and mode != "enclosure":
            chosen = "board"
        elif match is not None and mode in ("auto", "enclosure"):
            chosen, cav = "enclosure", match
        else:
            chosen = "face"
            rep["picked"] = int(pick)
    elif mode == "board":
        chosen = "board" if plate else "largest_plane"
        if not plate:
            why.append("no board-like plate (two equal parallel faces a PCB thickness apart) was found")
    elif mode == "enclosure":
        chosen = "enclosure" if cav else "largest_plane"
        if not cav:
            why.append("no recessed floor that is open above was found")
    else:
        if plate and cav:
            inside = None
            for cand in find_plates(gs, up_axis):          # a PCB sitting in the cavity wins over a lid
                top, bot, t = cand
                if abs(p21.dot(top.n, cav["up"])) > 0.999 and top.solid != cav["solid"] and plate_ok(top, t):
                    pb, cb = top.bbox_on(cav["up"]), cav["bbox"]
                    l = lvl(top if p21.dot(top.n, cav["up"]) > 0 else bot, cav["up"])
                    if (pb and cb[0] - 0.1 <= pb[0] and pb[2] <= cb[2] + 0.1 and cb[1] - 0.1 <= pb[1]
                            and pb[3] <= cb[3] + 0.1 and cav["level"] < l < cav["rim"] + 0.01):
                        inside = cand
                        break
            top, bot, t = plate
            pa, ca = top.extent()[0], cav["floor"].gross
            if inside:
                chosen = "board"
                top, bot, t = plate = inside
                if up_axis is None and p21.dot(top.n, cav["up"]) < 0:      # the board is seen from the enclosure opening
                    plate = (bot, top, t)
            elif ca >= 0.5 * pa and top.solid != cav["solid"]:
                chosen = "enclosure"
                why.append("the model has both a flat plate (a lid or panel?) and a cavity")
            else:
                chosen = "board"
        elif plate:
            chosen = "board"
        elif cav:
            chosen = "enclosure"
        else:
            chosen = "largest_plane"
        if chosen == "enclosure":
            why.append("enclosure mode was guessed: check the picture")
    ol = None
    if chosen == "board":
        top, bot, t = plate
        src = top if top.extent()[0] >= bot.extent()[0] - 1e-6 else bot     # the side less eaten by fused parts
        loops, opens = src.loops(top.n)
        other, _ = (bot if src is top else top).loops(top.n)
        if loops:
            big = max(loops, key=lambda l: abs(ir.poly_area(ir.loop_pts(l))))
            n0 = len(loops)
            loops = [big] + through([l for l in loops if l is not big], other)
            ol = ir.classify(loops, holes=holes)
            ol["ignored"]["islands"] = n0 - len(loops)       # footprints of parts fused to the board, not holes
            ol["ignored"]["open_chains"] = len(opens)
            ol["thickness"] = ir.std_thickness(t)
            rep.update(mode="board", picked=gs.index(top), thickness_raw=round(t, 3), up=axis_name(top.n))
            if not plate_ok(top, t):
                why.append("the plate is not a typical PCB (thickness %.2f mm, short side %.1f mm)"
                           % (t, min(ol["bbox"][2] - ol["bbox"][0], ol["bbox"][3] - ol["bbox"][1])))
            if src.unknown:
                why.append("%d edge curves of an unsupported type were replaced by straight chords" % src.unknown)
            par = [lvl(g, top.n) for g in gs if g.solid == top.solid and abs(p21.dot(g.n, top.n)) > 0.999]
            if par and max(par) - min(par) > t + 0.05:
                # more than a bare plate (a module with parts fused on, or a connector / heatsink with a flat base)
                why.append("the plate is part of a larger body (%.1f mm tall): check that it is the PCB" % (max(par) - min(par)))
    elif chosen == "enclosure":
        cl = 0.5 if clearance is None else clearance
        ol, enc, w2 = enclosure_outline(cav, cl)
        why += w2
        if ol:
            if holes == "none":
                ol["holes"] = []
            rep.update(mode="enclosure", picked=gs.index(cav["floor"]), up=axis_name(cav["up"]), enclosure=enc)
            if mode == "enclosure" and pick is None and len(cavs) > 1 and cavs[1]["floor"].gross > 0.85 * cav["floor"].gross \
                    and cavs[1]["floor"] is not cav["floor"]:
                why.append("another recessed floor is nearly as large")
    if ol is None:
        g = gs[int(pick)] if (pick is not None and chosen == "face") else gs[0]
        loops, opens = g.loops(up_axis if (up_axis and abs(p21.dot(g.n, up_axis)) > 0.999) else None)
        ol = ir.classify(loops, holes=holes)
        if ol:
            ol["ignored"]["open_chains"] = len(opens)
        rep.update(mode="face" if chosen == "face" else "largest_plane", picked=gs.index(g), up=axis_name(g.n))
        if chosen != "face":
            why.append("no board plate or enclosure cavity was recognised: the largest flat face was used")
    if unresolved:
        why.append("assembly: %d part placement(s) could not be read, positions are relative to each part" % unresolved)
    if ol:
        b = ol["bbox"]
        if (b[2] - b[0]) < 10 or (b[3] - b[1]) < 10:
            why.append("the picked outline is smaller than 10 x 10 mm")
        if chosen != "face" and pick is None and len(gs) > 1 and rep["mode"] == "largest_plane" and gs[1].gross > 0.85 * gs[0].gross:
            why.append("the second largest face is within 15 % of the picked one")
    rep["ambiguous"] = bool(why)
    rep["candidates"] = candidates(gs, plate, cavs, include=rep.get("picked"))
    return ol, rep
