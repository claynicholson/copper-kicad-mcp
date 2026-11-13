"""Extra pcbnew worker ops: imported outlines, elegance measurements, silkscreen tidy, track polish.

Loaded by ``board.py`` right before it runs (``import copper_ext``), so every op here is available
to the normal ``board`` worker; it can also be run as the worker script itself. Ops:

    outline_shapes   write an outline given as line / arc / circle prims (+ cutouts, inward offset)
    outline_prop     set / clear keys of the COPPER_OUTLINE board property (provenance, height limits)
    footprint_keepout  rule areas that forbid footprints (enclosure ribs under the board)
    elegance_extra   everything the elegance score needs that only pcbnew knows (read-only)
    silk_tidy2       re-seat reference designators that are unreadable (the score's own definition)
    polish           merge collinear segments, chamfer 90 degree corners, centre pad entries

It also upgrades two helpers of ``board.py`` in place: ``outline_info`` (polygon always present,
decimated; cutouts; area; provenance) and ``fp_local_geom`` (adds the fab-layer box ``lfab``).
"""
import importlib.util
import math
import os
import sys

_main = sys.modules.get("__main__")
if _main is not None and hasattr(_main, "OPS") and hasattr(_main, "edge_shapes"):
    B = _main
else:
    import board as B  # noqa: N812

P = B.P
op, mm, tomm, V, vxy, uuid, WorkerError = B.op, B.mm, B.tomm, B.V, B.vxy, B.uuid, B.WorkerError
_PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_PKG, *rel.split("/")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ir = _load("copper_cad_ir", "cad/ir.py")
silkcheck = _load("copper_silkcheck", "review/silkcheck.py")

PROP = "COPPER_OUTLINE"


# ── board property (provenance, height limits) ────────────────────────────
def get_prop(b):
    try:
        raw = ""
        for k, v in dict(b.GetProperties()).items():      # keys come back as wxString objects
            if str(k) == PROP:
                raw = str(v)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for part in str(raw).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def set_prop(b, update=None, unset=()):
    cur = get_prop(b)
    for k in unset:
        cur.pop(k, None)
    for k, v in (update or {}).items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[str(k)] = str(v).replace(";", ",").replace("=", ":")
    pr = b.GetProperties()
    pr[PROP] = ";".join("%s=%s" % kv for kv in cur.items())
    b.SetProperties(pr)
    return cur


@op("outline_prop")
def op_outline_prop(b, a, ctx):
    cur = set_prop(b, a.get("set"), a.get("unset") or ())
    ctx["modified"] = True
    return {"property": cur}


# ── outline_info / fp_local_geom upgrades ─────────────────────────────────
def outline_info2(b):
    bb = b.GetBoardEdgesBoundingBox()
    if not B.edge_shapes(b) or bb.GetWidth() <= 0:
        return None
    d = {"bbox": [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())],
         "width": tomm(bb.GetWidth()), "height": tomm(bb.GetHeight())}
    try:
        ps = P.SHAPE_POLY_SET()
        if b.GetBoardPolygonOutlines(ps, False) and ps.OutlineCount() > 0:
            k = max(range(ps.OutlineCount()), key=lambda i: abs(ps.Outline(i).Area()))
            o = ps.Outline(k)
            full = [vxy(o.CPoint(i)) for i in range(o.PointCount())]
            pts = full
            for tol in (0.05, 0.1, 0.2, 0.5):
                if len(pts) <= 400:
                    break
                pts = ir.decimate(full, tol)
                d["polygon_tol"] = tol
            d["polygon"] = [[round(x, 4), round(y, 4)] for x, y in pts]
            holes = []
            for i in range(ps.HoleCount(k)):
                h = ps.Hole(k, i)
                hp = [vxy(h.CPoint(j)) for j in range(h.PointCount())]
                if len(hp) > 48:
                    hp = ir.decimate(hp, 0.05)
                holes.append([[round(x, 4), round(y, 4)] for x, y in hp])
            if holes:
                d["holes"] = holes
            if full:
                xs = [q[0] for q in full]
                ys = [q[1] for q in full]
                d["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
                d["width"], d["height"] = round(max(xs) - min(xs), 4), round(max(ys) - min(ys), 4)
            d["area"] = round(ps.Area() / 1e12, 3)
            d["closed"] = True
        else:
            d["closed"] = False
    except Exception:  # noqa: BLE001
        pass
    pr = get_prop(b)
    if pr.get("src"):
        d["src"] = pr["src"]
    return d


_orig_local_geom = B.fp_local_geom


def fp_local_geom2(fp):
    out = _orig_local_geom(fp)          # the footprint is now at the origin, unrotated, front side
    try:
        box = None
        for g in fp.GraphicalItems():
            if g.GetClass() == "PCB_SHAPE" and g.GetLayer() == P.F_Fab:
                q = B._bb(g)
                box = q if box is None else [min(box[0], q[0]), min(box[1], q[1]), max(box[2], q[2]), max(box[3], q[3])]
        if box:
            out["lfab"] = box
    except Exception:  # noqa: BLE001
        pass
    return out


B.outline_info = outline_info2
B.fp_local_geom = fp_local_geom2


# ── outline from prims ────────────────────────────────────────────────────
def _circle(b, c, r, width, layer):
    s = P.PCB_SHAPE(b)
    s.SetShape(P.SHAPE_T_CIRCLE)
    s.SetCenter(V(c[0], c[1]))
    s.SetEnd(V(c[0] + r, c[1]))
    s.SetLayer(layer)
    s.SetWidth(mm(width))
    b.Add(s)
    return s


def _flat(loop, err=0.002):
    """Loop -> polygon points with a chord error below ``err`` mm."""
    pts = []
    for p in loop:
        if p[0] == "L":
            q = [p[1], p[2]]
        else:
            r = p[2] if p[0] == "C" else ((ir.circle3(p[1], p[2], p[3]) or (None, 1.0))[1])
            da = 2 * math.acos(max(0.0, 1 - err / max(r, err * 2)))
            q = ir.prim_pts(p, da=max(min(da, math.pi / 24), math.pi / 720))
        pts += q if p[0] == "C" else q[:-1]
    return pts


def _offset_loop(loop, delta):
    """Offset a closed loop by ``delta`` mm (negative = inward) with round corners; arcs are re-fitted."""
    if abs(delta) < 1e-9:
        return loop
    if len(loop) == 1 and loop[0][0] == "C":
        r = loop[0][2] + delta
        if r <= 0.05:
            raise WorkerError("a circular contour vanishes at this clearance", "OUTLINE_SELF_CHECK")
        return [("C", loop[0][1], r)]
    lc = P.SHAPE_LINE_CHAIN()
    for x, y in _flat(loop):
        lc.Append(P.VECTOR2I(mm(x), mm(y)))
    lc.SetClosed(True)
    ps = P.SHAPE_POLY_SET()
    ps.AddOutline(lc)
    if delta < 0:
        ps.Deflate(mm(-delta), P.CORNER_STRATEGY_ROUND_ALL_CORNERS, 2000)
    else:
        ps.Inflate(mm(delta), P.CORNER_STRATEGY_ROUND_ALL_CORNERS, 2000)
    if ps.OutlineCount() < 1:
        raise WorkerError("the outline vanishes at this clearance", "OUTLINE_SELF_CHECK", "use a smaller outline_clearance")
    k = max(range(ps.OutlineCount()), key=lambda i: abs(ps.Outline(i).Area()))
    o = ps.Outline(k)
    pts = [(o.CPoint(i).x / 1e6, o.CPoint(i).y / 1e6) for i in range(o.PointCount())]
    segs = [("L", pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)) if math.dist(pts[i], pts[(i + 1) % len(pts)]) > 1e-7]
    return ir.simplify(segs, tol=0.01)


def _write_loop(b, loop, width, layer):
    n = 0
    for p in loop:
        if p[0] == "L":
            if math.dist(p[1], p[2]) > 1e-6:
                B._seg(b, p[1][0], p[1][1], p[2][0], p[2][1], width, layer)
                n += 1
        elif p[0] == "A":
            B._arc(b, p[1], p[2], p[3], width, layer)
            n += 1
        else:
            _circle(b, p[1], p[2], width, layer)
            n += 1
    return n


@op("outline_shapes")
def op_outline_shapes(b, a, ctx):
    """a = {"outer": [prims], "cutouts": [[prims]], "offset": mm (negative = inward), "replace": True,
            "line_width": 0.05, "provenance": {...}}
    prim = ["L", x0, y0, x1, y1] | ["A", sx, sy, mx, my, ex, ey] | ["C", cx, cy, r]"""
    outer = ir.prims_from_json(a["outer"])
    cutouts = [ir.prims_from_json(c) for c in a.get("cutouts") or []]
    off = float(a.get("offset") or 0.0)
    if off:
        outer = _offset_loop(outer, off)
        cutouts = [_offset_loop(c, -off) for c in cutouts]
    removed = 0
    if a.get("replace", True):
        for s in B.edge_shapes(b):
            b.Delete(s)
            removed += 1
    width = float(a.get("line_width", 0.05))
    n = _write_loop(b, outer, width, P.Edge_Cuts)
    for c in cutouts:
        n += _write_loop(b, c, width, P.Edge_Cuts)
    expect = abs(ir.poly_area(_flat(outer))) - sum(abs(ir.poly_area(_flat(c))) for c in cutouts)
    ps = P.SHAPE_POLY_SET()
    ok = b.GetBoardPolygonOutlines(ps, False)
    area = ps.Area() / 1e12 if ok else 0.0
    if not ok or ps.OutlineCount() < 1 or abs(area - expect) > 0.005 * max(expect, 1e-9):
        raise WorkerError("the written outline does not form the expected closed board (file %.2f mm2, board %.2f mm2); "
                          "nothing was saved" % (expect, area), "OUTLINE_SELF_CHECK",
                          "pass outline_layer / outline_pick to choose a cleaner contour, or fix the drawing")
    kd = {"lines": 0, "arcs": 0, "circles": 0}
    for lp in [outer] + cutouts:
        for k, v in ir.kinds(lp).items():
            kd[k] += v
    prov = dict(a.get("provenance") or {})
    if prov:
        set_prop(b, prov, unset=("top", "bottom", "placed", "file", "mode"))
    kz = _footprint_keepouts(b, a.get("keepouts_bottom") or [], "copper-encl-bottom-", P.B_Cu) if "keepouts_bottom" in a else None
    ctx["modified"] = True
    ctx["needs_fill"] = True
    return {"edge_shapes_removed": removed, "edge_shapes_added": n, "shapes": kd, "area": round(area, 3),
            "outline": B.outline_info(b), "keepouts": kz}


def _footprint_keepouts(b, polys, prefix, layer):
    removed = 0
    for z in list(b.Zones()):
        if z.GetIsRuleArea() and z.GetZoneName().startswith(prefix):
            b.Delete(z)
            removed += 1
    made = []
    for i, poly in enumerate(polys):
        z = P.ZONE(b)
        z.SetIsRuleArea(True)
        z.SetDoNotAllowZoneFills(False)
        z.SetDoNotAllowTracks(False)
        z.SetDoNotAllowVias(False)
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(True)
        ls = P.LSET()
        ls.AddLayer(layer)
        z.SetLayerSet(ls)
        o = z.Outline()
        o.NewOutline()
        for x, y in poly:
            o.Append(mm(x), mm(y))
        z.SetZoneName("%s%d" % (prefix, i + 1))
        b.Add(z)
        made.append(z.GetZoneName())
    return {"added": made, "removed": removed}


@op("holes_multi")
def op_holes_multi(b, a, ctx):
    """Mounting holes at given positions, several sizes in one go:
    a = {"groups": [{"size": "M3", "positions": [[x, y], ...]}], "lib_path", "reserved_refs", "net", "plated"}.
    Board-only H* MountingHole footprints are replaced (the rule of the ``holes`` op); mounting holes that
    come from the schematic are never touched, and a position within 0.5 mm of one is skipped."""
    lib = a.get("lib_path")
    groups = [g for g in a.get("groups") or [] if g.get("positions")]
    if groups and not lib:
        raise WorkerError("MountingHole footprint library not found", "LIB_NOT_FOUND",
                          "library(action='repair') so the stock 'MountingHole' library is in the fp-lib-table")
    removed, kept = [], []
    for fp in list(b.GetFootprints()):
        r = fp.GetReference()
        if "MountingHole" not in str(fp.GetFPID().GetUniStringLibId()):
            continue
        if fp.IsBoardOnly() and r.startswith("H"):
            b.Delete(fp)
            removed.append(r)
        else:
            kept.append(vxy(fp.GetPosition()))
    used = {f.GetReference() for f in b.GetFootprints()} | set(a.get("reserved_refs") or [])
    added, skipped, k = [], 0, 0
    for g in groups:
        size = g.get("size")
        if size not in B.HOLES:
            raise WorkerError("hole size must be one of %s" % sorted(B.HOLES), "BAD_ARGUMENT")
        name, dia = B.HOLES[size]
        if a.get("plated"):
            name += "_Pad"
        for x, y in g["positions"]:
            if any(math.hypot(x - q[0], y - q[1]) <= 0.5 for q in kept):
                skipped += 1
                continue
            k += 1
            while "H%d" % k in used:
                k += 1
            href = "H%d" % k
            used.add(href)
            fp = P.FootprintLoad(lib, name)
            if fp is None:
                raise WorkerError("footprint MountingHole:%s not found in %s" % (name, lib), "FOOTPRINT_NOT_FOUND")
            fp.SetFPID(P.LIB_ID("MountingHole", name))
            fp.SetReference(href)
            fp.SetValue(size)
            fp.SetBoardOnly(True)
            fp.SetExcludedFromBOM(True)
            fp.SetExcludedFromPosFiles(True)
            fp.Reference().SetVisible(False)
            fp.SetPosition(V(x, y))
            fp.SetLocked(True)
            if a.get("net"):
                for pad in fp.Pads():
                    if pad.GetAttribute() != P.PAD_ATTRIB_NPTH:
                        pad.SetNet(B.net_item(b, a["net"], create=True))
            b.Add(fp)
            added.append({"ref": href, "at": [x, y], "size": size})
    ctx["modified"] = True
    return {"removed": removed, "added": added, "skipped_schematic_holes": skipped}


@op("edge_pads_solid")
def op_edge_pads_solid(b, a, ctx):
    """Through-hole pads of the given footprints (within ``reach`` mm of the board edge when given) get a
    solid zone connection. Used for the shell tabs of connectors placed flush with the edge: squeezed
    between the edge and the pin row, a thermal relief there loses spokes (KiCad: starved_thermal), and a
    shell tab is a mechanical anchor that wants solid copper anyway."""
    ol = B.outline_info(b)
    if not ol:
        return {"pads": []}
    x0, y0, x1, y1 = ol["bbox"]
    reach = float(a.get("reach", 1e9))
    done = []
    for ref in a.get("refs") or []:
        fp = B.fp_by_ref(b, ref)
        for pad in fp.Pads():
            if pad.GetAttribute() != P.PAD_ATTRIB_PTH or pad.GetNetCode() <= 0:
                continue
            q = B._bb(pad)
            near = min(q[0] - x0, x1 - q[2], q[1] - y0, y1 - q[3])
            if near <= reach and pad.GetLocalZoneConnection() == P.ZONE_CONNECTION_INHERITED:
                pad.SetLocalZoneConnection(P.ZONE_CONNECTION_FULL)
                done.append("%s.%s" % (ref, pad.GetNumber()))
    if done:
        ctx["modified"] = True
        ctx["needs_fill"] = True
    return {"pads": done}


@op("pads_solid")
def op_pads_solid(b, a, ctx):
    """Give the named pads ("REF.PAD") a solid zone connection (a thermal relief that cannot form its
    minimum number of spokes is a DRC error; solid copper is KiCad's own remedy)."""
    done = []
    for spec in a.get("pads") or []:
        try:
            ref, num = str(spec).rsplit(".", 1)
            fp = B.fp_by_ref(b, ref)
        except Exception:  # noqa: BLE001
            continue
        for pad in fp.Pads():
            if pad.GetNumber() == num and pad.GetLocalZoneConnection() != P.ZONE_CONNECTION_FULL:
                pad.SetLocalZoneConnection(P.ZONE_CONNECTION_FULL)
                done.append(spec)
    if done:
        ctx["modified"] = True
        ctx["needs_fill"] = True
    return {"pads": sorted(set(done))}


# ── measurements for the elegance score ───────────────────────────────────
def _silk_prims(g, ref, side, out):
    cls = g.GetClass()
    if cls in ("PCB_TEXT", "PCB_TEXTBOX"):
        try:
            if not g.IsVisible():
                return
        except Exception:  # noqa: BLE001
            pass
        q = B._bb(g)
        w = 0.1
        for x1, y1, x2, y2 in ((q[0], q[1], q[2], q[1]), (q[2], q[1], q[2], q[3]), (q[2], q[3], q[0], q[3]),
                               (q[0], q[3], q[0], q[1]), (q[0], q[1], q[2], q[3])):
            out.append(["s", ref, side, x1, y1, x2, y2, w])
        return
    if cls != "PCB_SHAPE":
        return
    w = max(tomm(g.GetWidth()), 0.05)
    sh = g.GetShape()
    if sh == P.SHAPE_T_SEGMENT:
        s, e = vxy(g.GetStart()), vxy(g.GetEnd())
        out.append(["s", ref, side, s[0], s[1], e[0], e[1], w])
    elif sh == P.SHAPE_T_CIRCLE:
        c = vxy(g.GetCenter())
        out.append(["c", ref, side, c[0], c[1], tomm(g.GetRadius()), w])
    elif sh == P.SHAPE_T_ARC:
        pts = ir.prim_pts(("A", tuple(vxy(g.GetStart())), tuple(vxy(g.GetArcMid())), tuple(vxy(g.GetEnd()))), da=math.pi / 8)
        for i in range(len(pts) - 1):
            out.append(["s", ref, side, round(pts[i][0], 4), round(pts[i][1], 4), round(pts[i + 1][0], 4), round(pts[i + 1][1], 4), w])
    else:
        pts = []
        try:
            if sh == P.SHAPE_T_RECT:
                pts = [vxy(p) for p in g.GetRectCorners()]
            elif sh == P.SHAPE_T_POLY:
                o = g.GetPolyShape().Outline(0)
                pts = [vxy(o.CPoint(i)) for i in range(o.PointCount())]
        except Exception:  # noqa: BLE001
            pts = []
        if not pts:
            q = B._bb(g)
            pts = [[q[0], q[1]], [q[2], q[1]], [q[2], q[3]], [q[0], q[3]]]
        for i in range(len(pts)):
            a_, b_ = pts[i], pts[(i + 1) % len(pts)]
            out.append(["s", ref, side, a_[0], a_[1], b_[0], b_[1], w])


def collect(b, planes=True):
    """Everything about silk, pads, courtyards, vias, planes and the outline that the score needs."""
    refs, courts, pads, silk, fab, marks = [], {}, [], [], {}, {}
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        side = "bottom" if fp.IsFlipped() else "top"
        t = fp.Reference()
        vis = bool(t.IsVisible()) and t.GetLayer() in (P.F_SilkS, P.B_SilkS)
        r = {"ref": ref, "visible": vis, "side": "bottom" if t.GetLayer() == P.B_SilkS else "top",
             "locked": bool(t.IsLocked()) or bool(fp.IsLocked() and not fp.IsBoardOnly()), "h": tomm(t.GetTextHeight()),
             "angle": round(t.GetTextAngleDegrees(), 2)}
        if fp.IsBoardOnly():
            r["board_only"] = True
        if vis:
            r["box"] = B._bb(t)
        refs.append(r)
        cb, has = B.fp_abs_bbox(fp)
        courts[ref] = {"box": cb, "side": side, "courtyard": bool(has)}
        for pad in fp.Pads():
            q = B._bb(pad)
            if pad.GetAttribute() == P.PAD_ATTRIB_NPTH:
                mask = 3
            else:
                mask = (1 if pad.IsOnLayer(P.F_Cu) else 0) | (2 if pad.IsOnLayer(P.B_Cu) else 0)
            if mask:
                pads.append([q[0], q[1], q[2], q[3], mask, ref])
        fbox = None
        for g in fp.GraphicalItems():
            lay = g.GetLayer()
            if lay in (P.F_SilkS, P.B_SilkS):
                _silk_prims(g, ref, "top" if lay == P.F_SilkS else "bottom", silk)
            elif lay in (P.F_Fab, P.B_Fab) and g.GetClass() == "PCB_SHAPE":
                q = B._bb(g)
                fbox = q if fbox is None else [min(fbox[0], q[0]), min(fbox[1], q[1]), max(fbox[2], q[2]), max(fbox[3], q[3])]
            if g.GetClass() == "PCB_TEXT" and "pcb edge" in g.GetText().lower():
                marks[ref] = vxy(g.GetPosition())
        if fbox:
            fab[ref] = fbox
    for d in b.GetDrawings():
        if d.GetLayer() in (P.F_SilkS, P.B_SilkS):
            _silk_prims(d, "", "top" if d.GetLayer() == P.F_SilkS else "bottom", silk)
    vias = []
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            q = vxy(t.GetPosition())
            vias.append([q[0], q[1], tomm(t.GetWidth(P.F_Cu))])
    ol = B.outline_info(b)
    out = {"refs": refs, "courts": courts, "pads": pads, "silk": silk, "vias": vias, "fab": fab, "edge_marks": marks,
           "edge": {"bbox": ol["bbox"], "polygon": ol.get("polygon"), "holes": ol.get("holes") or []} if ol else {},
           "board_area": (ol or {}).get("area"), "outline_closed": bool(ol and ol.get("closed")), "props": get_prop(b)}
    ds = b.GetDesignSettings()
    out["rules"] = {"copper_edge": tomm(ds.m_CopperEdgeClearance), "hole_clearance": tomm(ds.m_HoleClearance)}
    if planes:
        pl = []
        for z in b.Zones():
            if z.GetIsRuleArea() or B._is_teardrop(z):
                continue
            for lay in z.GetLayerSet().Seq():
                if not P.IsCopperLayer(lay):
                    continue
                try:
                    fpoly = z.GetFilledPolysList(lay)
                    pl.append({"layer": b.GetLayerName(lay), "net": z.GetNetname(), "islands": int(fpoly.OutlineCount()),
                               "area": round(fpoly.Area() / 1e12, 2)})
                except Exception:  # noqa: BLE001
                    pass
        out["planes"] = pl
    return out


@op("elegance_extra")
def op_elegance_extra(b, a, ctx):
    return collect(b)


# ── silkscreen: re-seat unreadable references ─────────────────────────────
_W = {"edge": 1000.0, "under_part": 400.0, "pad": 300.0, "text": 200.0, "silk": 150.0, "small": 0.0, "angle": 0.0}


@op("silk_tidy2")
def op_silk_tidy2(b, a, ctx):
    """Move reference designators that are bad by ``silkcheck.problems`` to a clean spot next to their
    part (0 or 90 degrees, at most ``max_dist`` mm away). Readable references, locked text and locked
    parts are left alone. refs=[...] limits the pass to those parts. hide='auto' hides a reference
    that fits nowhere (at most 15 % of all); otherwise it stays where it was and is reported."""
    only = set(a.get("refs") or [])
    hide = a.get("hide", "never")
    if hide == "if_auto":       # only a layout that is entirely Copper's own may lose a designator
        hide = "auto" if get_prop(b).get("placed") == "auto" else "never"
    min_size = float(a.get("min_size", 0.8))
    max_dist = float(a.get("max_dist", 4.0))
    extra = collect(b, planes=False)
    scene = silkcheck.Scene(extra)
    texts = dict(scene.texts)
    fps = {fp.GetReference(): fp for fp in b.GetFootprints()}
    info = {r["ref"]: r for r in extra["refs"]}
    n_refs = sum(1 for r in extra["refs"] if not r.get("board_only"))
    hidden0 = sum(1 for r in extra["refs"] if not r["visible"] and not r.get("board_only"))

    def probs(ref, box, h, ang):
        return silkcheck.problems(scene, ref, box, info[ref]["side"], h, ang, texts)

    def bad_now(ref):
        r = info[ref]
        return probs(ref, texts[ref][1], r["h"], r["angle"])

    targets = [r["ref"] for r in extra["refs"] if r["visible"] and not r["locked"] and not r.get("board_only")
               and (not only or r["ref"] in only) and bad_now(r["ref"])]

    def candidates(ref, w, h):
        c = scene.courts[ref]["box"]
        cx, cy = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2
        cw, ch = c[2] - c[0], c[3] - c[1]
        out = []
        for ang in (0, 90):
            tw, th = (w, h) if ang == 0 else (h, w)
            for k, g in enumerate((0.15, 0.4, 0.8, 1.3, 2.0, 3.0)):
                if g > max_dist:
                    break
                for f in (0.0, -0.33, 0.33, -0.5, 0.5):
                    out += [(cx + f * cw, c[1] - g - th / 2, k, ang), (cx + f * cw, c[3] + g + th / 2, k, ang),
                            (c[2] + g + tw / 2, cy + f * ch, k, ang), (c[0] - g - tw / 2, cy + f * ch, k, ang)]
                out += [(c[2] + g + tw / 2, c[1] - g - th / 2, k + 0.5, ang), (c[0] - g - tw / 2, c[1] - g - th / 2, k + 0.5, ang),
                        (c[2] + g + tw / 2, c[3] + g + th / 2, k + 0.5, ang), (c[0] - g - tw / 2, c[3] + g + th / 2, k + 0.5, ang)]
            if cw > tw + 0.6 and ch > th + 0.6:
                out.append((cx, cy, 0.2, ang))
        return out, (cx, cy)

    def best_spot(ref, w, h, height, ranked=None):
        cands, (cx, cy) = candidates(ref, w, h)
        best, clean = None, 0
        side = info[ref]["side"]
        for x, y, k, ang in cands:
            tw, th = (w, h) if ang == 0 else (h, w)
            box = [x - tw / 2, y - th / 2, x + tw / 2, y + th / 2]
            pr = probs(ref, box, height, ang)
            if not pr:
                clean += 1
            pen = sum(_W[p] for p in pr) + 20.0 * silkcheck.own_silk_hits(scene, ref, box, side) \
                + 40.0 * silkcheck.via_hits(scene, box) + 0.5 * k + 0.05 * math.hypot(x - cx, y - cy) + (0.8 if ang else 0.0)
            grown = [box[0] - 0.12, box[1] - 0.12, box[2] + 0.12, box[3] + 0.12]
            if any(silkcheck.hit(grown, p) for p in scene.pads[side]):
                pen += 3.0
            if best is None or pen < best[0]:
                best = (pen, x, y, ang, box, pr)
            if ranked is not None and not pr:
                ranked.append((pen, x, y, ang, box, pr))
        if ranked is not None:
            ranked.sort(key=lambda t: t[0])
        return best, clean

    def dims(t):
        """Text box size at angle 0 and the offset between its position and its box centre."""
        a0 = t.GetTextAngleDegrees()
        t.SetTextAngleDegrees(0)
        q = B._bb(t)
        t.SetTextAngleDegrees(a0)
        return q[2] - q[0], q[3] - q[1]

    def apply(t, x, y, ang):
        t.SetTextAngleDegrees(ang)
        if hasattr(t, "SetKeepUpright"):
            t.SetKeepUpright(True)
        t.SetPosition(V(round(x, 3), round(y, 3)))
        q = B._bb(t)                       # justification: bring the box centre onto the target
        dx, dy = x - (q[0] + q[2]) / 2, y - (q[1] + q[3]) / 2
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            p = t.GetPosition()
            t.SetPosition(P.VECTOR2I(p.x + mm(dx), p.y + mm(dy)))
        return B._bb(t)

    plan = []
    for ref in targets:
        t = fps[ref].Reference()
        w, h = dims(t)
        _, clean = best_spot(ref, w, h, info[ref]["h"])
        plan.append((clean, ref))
    plan.sort()
    moved, crowded, shrunk, hidden = [], [], [], []

    def seat(ref):
        t = fps[ref].Reference()
        orig = (t.GetPosition(), t.GetTextAngleDegrees(), t.GetTextSize(), t.GetTextThickness())
        w, h = dims(t)
        height = info[ref]["h"]
        ranked = []
        best, _ = best_spot(ref, w, h, height, ranked)
        if best[5] and t.GetTextHeight() > mm(min_size) + 1000:
            t.SetTextSize(P.VECTOR2I(mm(min_size), mm(min_size)))
            t.SetTextThickness(mm(0.15))
            w2, h2 = dims(t)
            r2 = []
            b2, _ = best_spot(ref, w2, h2, min_size, r2)
            if not b2[5]:
                best, height, ranked = b2, min_size, r2
                if ref not in shrunk:
                    shrunk.append(ref)
            else:
                t.SetTextSize(orig[2])
                t.SetTextThickness(orig[3])
        # the planned box is an estimate (justification, stroke width, keep-upright): seat, then judge the
        # box pcbnew really draws, and fall through to the next clean candidate when it is not clean
        for cand in ranked[:8]:
            box = apply(t, cand[1], cand[2], cand[3])
            if not probs(ref, box, height, cand[3]):
                info[ref]["h"], info[ref]["angle"] = height, cand[3]
                texts[ref] = (info[ref]["side"], box)
                return True
        t.SetTextSize(orig[2])
        t.SetTextThickness(orig[3])
        t.SetTextAngleDegrees(orig[1])
        t.SetPosition(orig[0])
        if ref in shrunk:
            shrunk.remove(ref)
        return False

    for _, ref in plan:
        if seat(ref):
            moved.append(ref)
        else:
            crowded.append(ref)
    # repair round: a reference placed early may now collide with one placed later
    for ref in list(moved) + list(crowded):
        if bad_now(ref):
            ok = seat(ref)
            if ok and ref in crowded:
                crowded.remove(ref)
                moved.append(ref)
            elif not ok and ref in moved:
                moved.remove(ref)
                crowded.append(ref)
    if hide == "auto":
        budget = int(0.15 * n_refs) - hidden0
        for ref in list(crowded):
            if budget <= 0:
                break
            fps[ref].Reference().SetVisible(False)
            texts.pop(ref, None)
            crowded.remove(ref)
            hidden.append(ref)
            budget -= 1
    still = sorted(r["ref"] for r in extra["refs"] if r["ref"] in texts and not r.get("board_only") and bad_now(r["ref"]))
    if moved or hidden or shrunk:
        ctx["modified"] = True
    return {"moved": sorted(moved), "crowded": sorted(crowded), "shrunk": sorted(shrunk), "hidden": sorted(hidden),
            "bad_before": len(targets), "bad_after": still, "min_size": min_size}


# ── track polish ──────────────────────────────────────────────────────────
_GRAVE = []     # removed items stay referenced (and un-owned) for the life of the worker, see _take_off


def _take_off(b, t):
    """Remove an item from the board without ever letting Python delete it. BOARD.Remove hands the
    C++ object to Python; when that wrapper is garbage-collected pcbnew 10.0 loses its SWIG type table
    (every later GetFootprints()/GetTracks() returns raw SwigPyObjects). So: disown and keep."""
    b.Remove(t)
    try:
        t.thisown = False
    except Exception:  # noqa: BLE001
        pass
    _GRAVE.append(t)


class _Undo:
    def __init__(self, b):
        self.b, self.log = b, []

    def add(self, t):
        self.b.Add(t)
        self.log.append(("add", t))

    def remove(self, t):
        self.log.append(("del", t))
        _take_off(self.b, t)

    def move(self, t, start=None, end=None):
        self.log.append(("mod", t, P.VECTOR2I(t.GetStart()), P.VECTOR2I(t.GetEnd())))
        if start is not None:
            t.SetStart(start)
        if end is not None:
            t.SetEnd(end)

    def mark(self):
        return len(self.log)

    def undo(self, to=0):
        while len(self.log) > to:
            e = self.log.pop()
            if e[0] == "viaw":
                e[1].SetWidth(e[2])
            elif e[0] == "add":
                _take_off(self.b, e[1])
            elif e[0] == "del":
                self.b.Add(e[1])
            else:
                e[1].SetStart(e[2])
                e[1].SetEnd(e[3])


def _nodes(b):
    """(x, y, layer) -> [(track, end index)] over straight segments; extra = points held by arcs / vias."""
    nodes, vias, arcs = {}, set(), set()
    for t in b.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            p = t.GetPosition()
            vias.add((p.x, p.y))
        elif cls == "PCB_ARC":
            for p in (t.GetStart(), t.GetEnd()):
                arcs.add((p.x, p.y, t.GetLayer()))
        else:
            s, e = t.GetStart(), t.GetEnd()
            nodes.setdefault((s.x, s.y, t.GetLayer()), []).append((t, 0))
            nodes.setdefault((e.x, e.y, t.GetLayer()), []).append((t, 1))
    return nodes, vias, arcs


def _other(t, idx):
    p = t.GetEnd() if idx == 0 else t.GetStart()
    return (p.x, p.y)


def _pad_points(b):
    pts = set()
    for fp in b.GetFootprints():
        for pad in fp.Pads():
            p = pad.GetPosition()
            pts.add((p.x, p.y))
    return pts


def _same(t1, t2):
    return t1.GetNetCode() == t2.GetNetCode() and t1.GetWidth() == t2.GetWidth() and t1.GetLayer() == t2.GetLayer()


def _step_merge(b, ok, undo, a):
    n = 0
    padpts = _pad_points(b)
    for t in list(b.GetTracks()):
        if t.GetClass() == "PCB_TRACK" and ok(t) and t.GetStart() == t.GetEnd():
            undo.remove(t)
            n += 1
    seen = {}
    for t in list(b.GetTracks()):
        if t.GetClass() != "PCB_TRACK":
            continue
        s, e = t.GetStart(), t.GetEnd()
        k = (min((s.x, s.y), (e.x, e.y)), max((s.x, s.y), (e.x, e.y)), t.GetLayer(), t.GetNetCode())
        if k in seen and ok(t) and t.GetWidth() <= seen[k].GetWidth():
            undo.remove(t)
            n += 1
        else:
            seen.setdefault(k, t)
    changed = True
    rounds = 0
    while changed and rounds < 50:
        changed = False
        rounds += 1
        nodes, vias, arcs = _nodes(b)
        gone = set()
        for key, lst in nodes.items():
            if len(lst) != 2 or (key[0], key[1]) in vias or key in arcs or (key[0], key[1]) in padpts:
                continue
            (t1, i1), (t2, i2) = lst
            if t1 is t2 or id(t1) in gone or id(t2) in gone or not (ok(t1) and ok(t2) and _same(t1, t2)):
                continue
            ax, ay = _other(t1, i1)[0] - key[0], _other(t1, i1)[1] - key[1]
            cx, cy = _other(t2, i2)[0] - key[0], _other(t2, i2)[1] - key[1]
            la, lc = math.hypot(ax, ay), math.hypot(cx, cy)
            if la < 1 or lc < 1:
                continue
            if abs(ax * cy - ay * cx) <= 1e-4 * la * lc and ax * cx + ay * cy < 0:
                far = P.VECTOR2I(*_other(t2, i2))
                if i1 == 0:
                    undo.move(t1, start=far)
                else:
                    undo.move(t1, end=far)
                undo.remove(t2)
                gone.add(id(t1))
                gone.add(id(t2))
                n += 1
                changed = True
    return n


def _step_vias(b, ok, undo, a):
    """Two vias of one net whose drills overlap (a router artefact; fabs reject overlapping holes): keep
    one, and bring the tracks that ended on the other over to it with a stub inside the kept via's pad."""
    vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]
    gone, n = set(), 0
    # a via whose ring is below the board's minimum annular width (a router padstack mix-up: one class's
    # drill with another's diameter) is grown to the minimum when that keeps every clearance
    min_ann = b.GetDesignSettings().m_ViasMinAnnularWidth
    for v in vias:
        w, dr = v.GetWidth(P.F_Cu), v.GetDrillValue()
        if not ok(v) or min_ann <= 0 or (w - dr) / 2 >= min_ann - 1000:
            continue
        want = int(math.ceil((dr + 2 * min_ann) / 50000.0 - 1e-9) * 50000)
        v.SetWidth(want)
        if B.copper_collisions(b, [v], limit=1):
            v.SetWidth(w)
        else:
            undo.log.append(("viaw", v, w))
            a["_ring"] = a.get("_ring", 0) + 1
    for i, v in enumerate(vias):
        if id(v) in gone:
            continue
        for w in vias[i + 1:]:
            if id(w) in gone or w.GetNetCode() != v.GetNetCode() or v.GetNetCode() <= 0:
                continue
            pv, pw = v.GetPosition(), w.GetPosition()
            d = math.hypot(pv.x - pw.x, pv.y - pw.y)
            if d >= max(v.GetDrillValue(), w.GetDrillValue()) or d > min(v.GetWidth(P.F_Cu), w.GetWidth(P.F_Cu)) / 2:
                continue
            keep, drop = (v, w) if ok(w) else (w, v)
            if not ok(drop):
                continue
            pk, pd = keep.GetPosition(), drop.GetPosition()
            if d > 0:
                ends = {}
                for t in b.GetTracks():
                    if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == drop.GetNetCode() and (t.GetStart() == pd or t.GetEnd() == pd):
                        ends[t.GetLayer()] = min(ends.get(t.GetLayer(), t.GetWidth()), t.GetWidth())
                for lay, width in ends.items():
                    st = P.PCB_TRACK(b)
                    st.SetStart(P.VECTOR2I(pd.x, pd.y))
                    st.SetEnd(P.VECTOR2I(pk.x, pk.y))
                    st.SetWidth(width)
                    st.SetLayer(lay)
                    st.SetNet(keep.GetNet())
                    undo.add(st)
                    a.setdefault("_new", set()).add(uuid(st))
            undo.remove(drop)
            gone.add(id(drop))
            n += 1
            if drop is v:
                break
    return n


def _step_chamfer(b, ok, undo, a):
    """Up to three sweeps: a segment shortened at one end is only chamfered at its other end in the next
    sweep, when its new length is known."""
    tot = {"chamfered": 0, "blocked": 0}
    for _ in range(3):
        r = _chamfer_sweep(b, ok, undo, a)
        tot["chamfered"] += r["chamfered"]
        tot["blocked"] = r["blocked"]
        if not r["chamfered"]:
            break
    return tot


def _chamfer_sweep(b, ok, undo, a):
    n = skipped = 0
    max_leg = mm(float(a.get("chamfer", 1.0)))
    padpts = _pad_points(b)
    nodes, vias, arcs = _nodes(b)
    done = set()
    for key, lst in list(nodes.items()):
        if len(lst) != 2 or (key[0], key[1]) in vias or key in arcs or (key[0], key[1]) in padpts:
            continue
        (t1, i1), (t2, i2) = lst
        if t1 is t2 or id(t1) in done or id(t2) in done or not (ok(t1) and ok(t2) and _same(t1, t2)):
            continue
        ax, ay = _other(t1, i1)[0] - key[0], _other(t1, i1)[1] - key[1]
        cx, cy = _other(t2, i2)[0] - key[0], _other(t2, i2)[1] - key[1]
        la, lc = math.hypot(ax, ay), math.hypot(cx, cy)
        if la < 1 or lc < 1 or abs(ax * cx + ay * cy) > 2e-3 * la * lc:
            continue
        leg = min(max_leg, 0.4 * min(la, lc))
        if leg < mm(0.15):
            continue
        p1 = P.VECTOR2I(int(round(key[0] + ax / la * leg)), int(round(key[1] + ay / la * leg)))
        p2 = P.VECTOR2I(int(round(key[0] + cx / lc * leg)), int(round(key[1] + cy / lc * leg)))
        m = undo.mark()
        if i1 == 0:
            undo.move(t1, start=p1)
        else:
            undo.move(t1, end=p1)
        if i2 == 0:
            undo.move(t2, start=p2)
        else:
            undo.move(t2, end=p2)
        t = P.PCB_TRACK(b)
        t.SetStart(p1)
        t.SetEnd(p2)
        t.SetWidth(t1.GetWidth())
        t.SetLayer(t1.GetLayer())
        t.SetNet(t1.GetNet())
        undo.add(t)
        if B.copper_collisions(b, [t], limit=1):
            undo.undo(m)
            skipped += 1
            continue
        a.setdefault("_new", set()).add(uuid(t))
        # both legs are now shorter: do not chamfer their far ends against stale lengths in this sweep
        done.add(id(t1))
        done.add(id(t2))
        n += 1
    return {"chamfered": n, "blocked": skipped}


def _step_pad_entry(b, ok, undo, a):
    n = 0
    nodes, vias, arcs = _nodes(b)
    pads_by_net = {}
    for fp in b.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() > 0:
                pads_by_net.setdefault(pad.GetNetCode(), []).append(pad)
    ends = {}
    for key, lst in nodes.items():
        for t, i in lst:
            ends.setdefault((key[0], key[1], t.GetNetCode()), 0)
            ends[(key[0], key[1], t.GetNetCode())] += 1
    for key, lst in list(nodes.items()):
        if len(lst) != 1 or (key[0], key[1]) in vias or key in arcs:
            continue
        t, i = lst[0]
        if not ok(t):
            continue
        pt = P.VECTOR2I(key[0], key[1])
        for pad in pads_by_net.get(t.GetNetCode(), ()):
            if not pad.IsOnLayer(t.GetLayer()) or not pad.HitTest(pt):
                continue
            c = pad.GetPosition()
            d = math.hypot(c.x - key[0], c.y - key[1])
            if d <= mm(0.05) or d > mm(2.0) or ends.get((c.x, c.y, t.GetNetCode())) or not pad.HitTest(c):
                break
            sz = pad.GetSize(t.GetLayer())
            s = P.PCB_TRACK(b)
            s.SetStart(pt)
            s.SetEnd(P.VECTOR2I(c.x, c.y))
            s.SetWidth(max(mm(0.1), min(t.GetWidth(), sz.x, sz.y)))
            s.SetLayer(t.GetLayer())
            s.SetNet(t.GetNet())
            m = undo.mark()
            undo.add(s)
            if B.copper_collisions(b, [s], limit=1):
                undo.undo(m)
                break
            a.setdefault("_new", set()).add(uuid(s))
            ends[(c.x, c.y, t.GetNetCode())] = 1
            n += 1
            break
    return n


_STEPS = {"vias": _step_vias, "merge": _step_merge, "chamfer90": _step_chamfer, "pad_entry": _step_pad_entry}


@op("polish")
def op_polish(b, a, ctx):
    """Deterministic clean-up of the tracks named in ``uuids`` (all unlocked tracks with all=true).
    Each step is undone in memory when it leaves any net less connected than before."""
    allowed = None if a.get("all") else set(a.get("uuids") or [])
    nets = set(a.get("nets") or [])
    a["_new"] = set()

    def ok(t):
        if t.IsLocked():
            return False
        if nets and t.GetNetname() not in nets:
            return False
        return allowed is None or uuid(t) in allowed or uuid(t) in a["_new"]
    steps = [s for s in (a.get("steps") or ["vias", "merge", "chamfer90", "merge", "pad_entry"]) if s in _STEPS]
    before = B.unconnected_by_net(b) or {}
    stats, changed = {}, False
    for s in steps:
        undo = _Undo(b)
        res = _STEPS[s](b, ok, undo, a)
        cnt = res if isinstance(res, int) else res.get("chamfered", 0)
        ring = a.pop("_ring", 0) if s == "vias" else 0
        cnt += ring
        after = B.unconnected_by_net(b) if cnt else before
        if after is None or any(after.get(k, 0) > before.get(k, 0) for k in after):
            undo.undo()
            stats.setdefault("reverted", []).append(s)
            continue
        before = after
        if cnt:
            changed = True
        if ring:
            stats["via_rings_fixed"] = stats.get("via_rings_fixed", 0) + ring
        if isinstance(res, dict):
            for k, v in res.items():
                stats[k] = stats.get(k, 0) + v
        else:
            key = {"merge": "merged", "pad_entry": "pad_entries", "vias": "duplicate_vias"}[s]
            stats[key] = stats.get(key, 0) + res
    if a.get("fail_after"):          # test hook: prove that a failing polish leaves the file untouched
        raise WorkerError("polish test hook: forced failure", "POLISH_FORCED_FAILURE")
    if changed:
        ctx["modified"] = True
        ctx["needs_fill"] = True
    stats["steps"] = steps
    a.pop("_new", None)
    return stats


if __name__ == "__main__":
    B.main()
