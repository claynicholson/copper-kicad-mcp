"""pcbnew worker for the guided autorouter: runs inside KiCad's Python.

Same protocol as ``workers/board.py`` (one JSON request on stdin, one
``@@COPPER_RESULT@@{json}`` line on stdout), but self-contained so the two
workers can evolve independently.

Request::

    {"pcb": ".../board.kicad_pcb", "save": true,
     "ops": [{"op": "ripup", "mode": "all"|"nets"|"none", "nets": [...]},
             {"op": "fill"},
             {"op": "extract", "fills": true},
             {"op": "commit", "tracks": [...], "vias": [...]},
             {"op": "remove", "uuids": [...]},
             {"op": "stats"}]}

Ops run in order on ONE in-memory board; the file is written once at the end
(temp file + replace) only when every op succeeded and ``save`` is true.
All coordinates are millimetres.
"""
import json
import math
import os
import sys
import traceback

MARK = "@@COPPER_RESULT@@"

try:
    import pcbnew as P  # noqa: N812
except Exception as _e:  # noqa: BLE001
    sys.stdout.write("\n" + MARK + json.dumps({"ok": False, "code": "NO_PCBNEW",
                                               "error": "this interpreter cannot import pcbnew: %s" % _e}) + "\n")
    sys.exit(0)


# pcbnew reports e.g. a missing font ("Font 'X' not found; substituting ...") through wxLog, which pops up
# a MODAL dialog in a headless worker: LoadBoard then blocks until the worker times out. Never show wx logs.
# _kiguard also refuses to run when this pcbnew belongs to another KiCad series than the kicad-cli Copper uses.
try:
    import _kiguard
    _kiguard.start(P, MARK)
except SystemExit:
    raise
except Exception:  # noqa: BLE001
    try:
        import wx as _wx
        _wx.Log.EnableLogging(False)
    except Exception:  # noqa: BLE001
        pass


class WorkerError(Exception):
    def __init__(self, msg, code="WORKER_ERROR", fix=None):
        Exception.__init__(self, msg)
        self.msg, self.code, self.fix = msg, code, fix


def mm(v):
    return int(round(float(v) * 1e6))


def tomm(v):
    return round(v / 1e6, 4)


def uuid(item):
    return item.m_Uuid.AsString()


def _chain_pts(chain):
    return [[tomm(chain.CPoint(i).x), tomm(chain.CPoint(i).y)] for i in range(chain.PointCount())]


def _polyset(ps):
    """SHAPE_POLY_SET -> [{'o': [[x,y],...], 'h': [[[x,y],...], ...]}]"""
    out = []
    for i in range(ps.OutlineCount()):
        d = {"o": _chain_pts(ps.Outline(i))}
        hs = [_chain_pts(ps.Hole(i, j)) for j in range(ps.HoleCount(i))]
        if hs:
            d["h"] = hs
        out.append(d)
    return out


def _bbox(item):
    bb = item.GetBoundingBox()
    return [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())]


def cu_layers(b):
    """Enabled copper layers in stack order: [(id, name), ...] top to bottom."""
    try:
        ids = list(b.GetDesignSettings().GetEnabledLayers().CuStack())
    except Exception:  # noqa: BLE001
        ids = [l for l in b.GetEnabledLayers().Seq() if P.IsCopperLayer(l)]
    return [(l, b.GetLayerName(l)) for l in ids]


def fill_all(b):
    zs = b.Zones()
    if len(zs) == 0:
        return 0
    b.BuildConnectivity()
    P.ZONE_FILLER(b).Fill(zs)
    b.BuildConnectivity()
    return sum(1 for z in zs if z.IsFilled())


def all_pads(b):
    for fp in b.GetFootprints():
        for p in fp.Pads():
            yield fp, p


def unconnected_by_net(b):
    """{net: unrouted connections} = pad clusters - 1 per net (KiCad's ratsnest count)."""
    b.BuildConnectivity()
    c = b.GetConnectivity()
    pads = {}
    for _fp, p in all_pads(b):
        if p.GetNetCode() > 0:
            pads.setdefault(p.GetNetname(), []).append(p)
    out = {}
    for n, ps in pads.items():
        if len(ps) < 2:
            continue
        seen, clusters = set(), 0
        for p in ps:
            k = uuid(p)
            if k in seen:
                continue
            clusters += 1
            seen.add(k)
            for q in c.GetConnectedItems(p):
                seen.add(uuid(q))
        if clusters > 1:
            out[n] = clusters - 1
    return out


def net_stats(b):
    """{net: [track_length_mm, vias, segments]} for every net that has copper."""
    out = {}
    for t in b.GetTracks():
        e = out.setdefault(t.GetNetname(), [0.0, 0, 0])
        if t.GetClass() == "PCB_VIA":
            e[1] += 1
        else:
            e[0] += t.GetLength() / 1e6
            e[2] += 1
    return {k: [round(v[0], 2), v[1], v[2]] for k, v in out.items()}


def totals(b):
    n_t = n_v = 0
    length = 0.0
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            n_v += 1
        else:
            n_t += 1
            length += t.GetLength()
    return {"tracks": n_t, "vias": n_v, "track_length_mm": round(length / 1e6, 2)}


# ── ops ───────────────────────────────────────────────────────────────────
OPS = {}


def op(name):
    def deco(fn):
        OPS[name] = fn
        return fn
    return deco


@op("ripup")
def op_ripup(b, a, ctx):
    """Delete routed copper (never locked items). mode: all | nets | none."""
    mode = a.get("mode", "none")
    nets = set(a.get("nets") or [])
    removed = kept_locked = 0
    if mode in ("all", "nets"):
        for t in list(b.GetTracks()):
            if mode == "nets" and t.GetNetname() not in nets:
                continue
            if t.IsLocked():
                kept_locked += 1
                continue
            b.Delete(t)
            removed += 1
    # Teardrops are zones glued to the tracks they were generated for. Left behind after a rip-up they are
    # stray copper islands: KiCad re-fills them on the next zone fill and reports them as unconnected (or as
    # shorts against the new routing). Remove the teardrops of every net that lost all of its tracks.
    teardrops = 0
    if removed:
        left = {t.GetNetCode() for t in b.GetTracks()}
        for z in list(b.Zones()):
            try:
                tear = z.IsTeardropArea()
            except Exception:  # noqa: BLE001
                tear = (z.GetZoneName() or "").startswith("$teardrop")
            if not tear or z.GetNetCode() in left:
                continue
            if mode == "nets" and z.GetNetname() not in nets:
                continue
            b.Delete(z)
            teardrops += 1
        ctx["modified"] = True
        ctx["needs_fill"] = True
    return {"removed": removed, "kept_locked": kept_locked, "teardrops_removed": teardrops}


@op("fill")
def op_fill(b, a, ctx):
    n = fill_all(b)
    ctx["needs_fill"] = False
    if len(b.Zones()):
        ctx["modified"] = True
    return {"zones_filled": n}


def _netclasses(b):
    ds = b.GetDesignSettings()
    ns = ds.m_NetSettings
    out = {}
    for name, ni in b.GetNetsByName().items():
        name = str(name)
        if ni.GetNetCode() <= 0:
            continue
        d = {"code": ni.GetNetCode(), "class": ""}
        try:
            nc = ns.GetEffectiveNetClass(name)
            d["class"] = str(ni.GetNetClassName())
            d["clearance"] = tomm(nc.GetClearance())
            d["width"] = tomm(nc.GetTrackWidth())
            d["via"] = tomm(nc.GetViaDiameter())
            d["drill"] = tomm(nc.GetViaDrill())
            d["dp_gap"] = tomm(nc.GetDiffPairGap())
            d["dp_width"] = tomm(nc.GetDiffPairWidth())
        except Exception:  # noqa: BLE001
            pass
        out[name] = d
    return out


def _rules(b):
    ds = b.GetDesignSettings()
    r = {}
    for key, attr in (("clearance", "m_MinClearance"), ("track_width", "m_TrackMinWidth"),
                      ("via", "m_ViasMinSize"), ("drill", "m_MinThroughDrill"),
                      ("edge_clearance", "m_CopperEdgeClearance"), ("hole_clearance", "m_HoleClearance"),
                      ("hole_to_hole", "m_HoleToHoleMin"), ("annular", "m_ViasMinAnnularWidth")):
        try:
            r[key] = tomm(getattr(ds, attr))
        except Exception:  # noqa: BLE001
            pass
    try:
        nc = ds.m_NetSettings.GetDefaultNetclass()
        r["default"] = {"clearance": tomm(nc.GetClearance()), "width": tomm(nc.GetTrackWidth()),
                        "via": tomm(nc.GetViaDiameter()), "drill": tomm(nc.GetViaDrill()),
                        "dp_gap": tomm(nc.GetDiffPairGap()), "dp_width": tomm(nc.GetDiffPairWidth())}
    except Exception:  # noqa: BLE001
        pass
    return r


_SIMPLE = None


def _pad_shape(pad, lay):
    """-> ('box', [cx, cy, ux, uy, half_w, half_h, corner_r]) or ('poly', [outline, ...])."""
    global _SIMPLE
    if _SIMPLE is None:
        _SIMPLE = {P.PAD_SHAPE_CIRCLE: "c", P.PAD_SHAPE_RECTANGLE: "r", P.PAD_SHAPE_OVAL: "o",
                   P.PAD_SHAPE_ROUNDRECT: "rr"}
    kind = _SIMPLE.get(pad.GetShape(lay))
    if kind:
        c = pad.ShapePos(lay)
        sz = pad.GetSize(lay)
        hw, hh = sz.x / 2e6, sz.y / 2e6
        a = math.radians(pad.GetOrientationDegrees())
        if kind == "c":
            r = hw
        elif kind == "r":
            r = 0.0
        elif kind == "o":
            r = min(hw, hh)
        else:
            r = min(pad.GetRoundRectCornerRadius(lay) / 1e6, hw, hh)
        # KiCad rotates with y pointing down: the pad's local x axis is (cos a, -sin a)
        return "box", [tomm(c.x), tomm(c.y), round(math.cos(a), 6), round(-math.sin(a), 6),
                       round(hw, 4), round(hh, 4), round(r, 4)]
    ps = P.SHAPE_POLY_SET()
    pad.TransformShapeToPolygon(ps, lay, 0, 5000, P.ERROR_OUTSIDE)
    return "poly", [_chain_pts(ps.Outline(i)) for i in range(ps.OutlineCount())]


def _arc_points(t):
    """PCB_ARC -> polyline points (mm), sampled every <= 10 degrees."""
    s, m, e = t.GetStart(), t.GetMid(), t.GetEnd()
    ax, ay, bx, by, cx, cy = s.x / 1e6, s.y / 1e6, m.x / 1e6, m.y / 1e6, e.x / 1e6, e.y / 1e6
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [[ax, ay], [cx, cy]]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, a1, a2 = (math.atan2(ay - uy, ax - ux), math.atan2(by - uy, bx - ux), math.atan2(cy - uy, cx - ux))

    def norm(x):
        while x < 0:
            x += 2 * math.pi
        while x >= 2 * math.pi:
            x -= 2 * math.pi
        return x
    sweep = norm(a2 - a0)
    if norm(a1 - a0) > sweep:          # the mid point is on the other way round
        sweep -= 2 * math.pi
    n = max(2, int(abs(sweep) / math.radians(10)) + 1)
    return [[round(ux + r * math.cos(a0 + sweep * i / n), 4), round(uy + r * math.sin(a0 + sweep * i / n), 4)]
            for i in range(n + 1)]


@op("extract")
def op_extract(b, a, ctx):
    """Everything the pure-Python router needs, in one compact dict."""
    lays = cu_layers(b)
    lidx = {lid: i for i, (lid, _n) in enumerate(lays)}
    out = {"layers": [n for _l, n in lays], "rules": _rules(b), "nets": _netclasses(b)}

    # outline
    ol = {"closed": False, "polys": []}
    try:
        ps = P.SHAPE_POLY_SET()
        if b.GetBoardPolygonOutlines(ps, False) and ps.OutlineCount() > 0:
            ol = {"closed": True, "polys": _polyset(ps)}
    except Exception:  # noqa: BLE001
        pass
    try:
        bb = b.GetBoardEdgesBoundingBox()
        if bb.GetWidth() > 0:
            ol["bbox"] = [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())]
    except Exception:  # noqa: BLE001
        pass
    out["outline"] = ol

    # connectivity clusters (pads / tracks / vias that are already joined, zone fills included)
    b.BuildConnectivity()
    conn = b.GetConnectivity()
    cluster = {}
    by_net = {}
    for _fp, p in all_pads(b):
        if p.GetNetCode() > 0:
            by_net.setdefault(p.GetNetCode(), []).append(p)
    ncl = 0
    for code in sorted(by_net):
        for p in by_net[code]:
            k = uuid(p)
            if k in cluster:
                continue
            ncl += 1
            cluster[k] = ncl
            try:
                for q in conn.GetConnectedItems(p):
                    cluster.setdefault(uuid(q), ncl)
            except Exception:  # noqa: BLE001
                pass

    # pads + holes + footprints
    pads, fps, graphics = [], [], []
    kinds = {P.PAD_ATTRIB_SMD: "smd", P.PAD_ATTRIB_PTH: "tht", P.PAD_ATTRIB_NPTH: "npth", P.PAD_ATTRIB_CONN: "conn"}
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        pos = fp.GetPosition()
        try:
            bb = fp.GetBoundingBox(False)
        except Exception:  # noqa: BLE001
            bb = fp.GetBoundingBox()
        fps.append({"ref": ref, "value": fp.GetValue(), "x": tomm(pos.x), "y": tomm(pos.y),
                    "side": "bottom" if fp.IsFlipped() else "top", "locked": bool(fp.IsLocked()),
                    "bbox": [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())],
                    "fpid": fp.GetFPIDAsString(), "pads": len(fp.Pads())})
        for pad in fp.Pads():
            kind = kinds.get(pad.GetAttribute(), "smd")
            ls = pad.GetLayerSet()
            on = [lidx[l] for l, _n in lays if ls.Contains(l)]
            pp = pad.GetPosition()
            d = {"ref": ref, "num": pad.GetNumber(), "net": pad.GetNetname() if pad.GetNetCode() > 0 else "",
                 "x": tomm(pp.x), "y": tomm(pp.y), "kind": kind, "layers": on}
            if on and kind != "npth":
                shapes = []
                for l, _n in lays:
                    if not ls.Contains(l):
                        continue
                    try:
                        sk, sg = _pad_shape(pad, l)
                    except Exception:  # noqa: BLE001
                        sk, sg = "box", _bbox(pad)[:0] or None
                        bb = pad.GetBoundingBox()
                        sg = [tomm(bb.GetCenter().x), tomm(bb.GetCenter().y), 1, 0,
                              tomm(bb.GetWidth()) / 2, tomm(bb.GetHeight()) / 2, 0]
                    if shapes and shapes[-1][1] == sk and shapes[-1][2] == sg:
                        shapes[-1][0].append(lidx[l])
                    else:
                        shapes.append([[lidx[l]], sk, sg])
                d["shapes"] = shapes
            try:
                dz = pad.GetDrillSize()
                if dz.x > 0 and dz.y > 0:
                    a_ = math.radians(pad.GetOrientationDegrees())
                    d["hole"] = [tomm(pp.x), tomm(pp.y), round(math.cos(a_), 6), round(-math.sin(a_), 6),
                                 tomm(dz.x) / 2, tomm(dz.y) / 2]
            except Exception:  # noqa: BLE001
                pass
            try:
                lc = pad.GetLocalClearance()
                if lc:
                    d["clearance"] = tomm(lc)
            except Exception:  # noqa: BLE001
                pass
            if pad.GetPinFunction():
                d["fn"] = pad.GetPinFunction()
            if pad.GetPinType():
                d["pt"] = pad.GetPinType()
            cid = cluster.get(uuid(pad))
            if cid:
                d["c"] = cid
            pads.append(d)
        # copper graphics inside footprints (printed antennas, net ties, logos) are obstacles
        try:
            for g in fp.GraphicalItems():
                if g.IsOnCopperLayer():
                    for l, _n in lays:
                        if g.IsOnLayer(l):
                            graphics.append({"layer": lidx[l], "bbox": _bbox(g), "kind": g.GetClass(), "ref": ref})
        except Exception:  # noqa: BLE001
            pass
    out["pads"], out["footprints"] = pads, fps

    for d in b.GetDrawings():
        try:
            if d.IsOnCopperLayer():
                net = ""
                try:
                    net = d.GetNetname() if d.GetNetCode() > 0 else ""
                except Exception:  # noqa: BLE001
                    pass
                for l, _n in lays:
                    if d.IsOnLayer(l):
                        graphics.append({"layer": lidx[l], "bbox": _bbox(d), "kind": d.GetClass(), "net": net})
        except Exception:  # noqa: BLE001
            pass
    out["graphics"] = graphics

    # graphics on the solder-mask layers are mask openings: KiCad flags copper of two nets under one
    # ("solder_mask_bridge"), so the router keeps new tracks out of them
    masks = []

    def mask_item(g):
        for mlay, cu in ((P.F_Mask, 0), (P.B_Mask, len(lays) - 1)):
            try:
                if not g.IsOnLayer(mlay):
                    continue
            except Exception:  # noqa: BLE001
                continue
            polys = []
            try:
                ps = P.SHAPE_POLY_SET()
                g.TransformShapeToPolygon(ps, mlay, 0, 20000, P.ERROR_OUTSIDE)
                polys = [_chain_pts(ps.Outline(i)) for i in range(ps.OutlineCount())]
            except Exception:  # noqa: BLE001
                polys = []
            polys = [q for q in polys if 3 <= len(q) <= 400]
            if not polys:
                x0, y0, x1, y1 = _bbox(g)
                polys = [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]]
            for q in polys:
                masks.append({"layer": cu, "poly": q})
    for d in b.GetDrawings():
        mask_item(d)
    for fp in b.GetFootprints():
        # only pad-less footprints (logos, fiducial art): a part's own mask graphics (solder jumpers,
        # exposed-pad windows) belong to its pads and must not wall them in
        if len(fp.Pads()):
            continue
        try:
            for g in fp.GraphicalItems():
                mask_item(g)
        except Exception:  # noqa: BLE001
            pass
    out["mask_openings"] = masks

    # tracks / vias
    tracks, vias = [], []
    for t in b.GetTracks():
        cls = t.GetClass()
        net = t.GetNetname() if t.GetNetCode() > 0 else ""
        cid = cluster.get(uuid(t), 0)
        if cls == "PCB_VIA":
            p = t.GetPosition()
            try:
                top, bot = lidx.get(t.TopLayer(), 0), lidx.get(t.BottomLayer(), len(lays) - 1)
            except Exception:  # noqa: BLE001
                top, bot = 0, len(lays) - 1
            try:
                size = tomm(t.GetWidth(P.F_Cu))
            except Exception:  # noqa: BLE001
                size = tomm(t.GetWidth())
            vias.append([net, tomm(p.x), tomm(p.y), size, tomm(t.GetDrillValue()), min(top, bot), max(top, bot),
                         cid, 1 if t.IsLocked() else 0])
            continue
        lay = lidx.get(t.GetLayer())
        if lay is None:
            continue
        w = tomm(t.GetWidth())
        if cls == "PCB_ARC":
            pts = _arc_points(t)
        else:
            pts = [[tomm(t.GetStart().x), tomm(t.GetStart().y)], [tomm(t.GetEnd().x), tomm(t.GetEnd().y)]]
        for i in range(len(pts) - 1):
            tracks.append([net, lay, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], w, cid,
                           1 if t.IsLocked() else 0])
    out["tracks"], out["vias"] = tracks, vias

    # zones: rule areas (keepouts), teardrops, copper pours (+ fill islands for connecting to a pour)
    zones = []
    want_fills = a.get("fills", True)

    def one_zone(z, ref=None):
        ls = z.GetLayerSet()
        on = [lidx[l] for l, _n in lays if ls.Contains(l)]
        if not on:
            return
        d = {"name": z.GetZoneName(), "layers": on, "polys": _polyset(z.Outline())}
        if ref:
            d["ref"] = ref
        if z.GetIsRuleArea():
            d["rule_area"] = True
            d["no_tracks"] = bool(z.GetDoNotAllowTracks())
            d["no_vias"] = bool(z.GetDoNotAllowVias())
            d["no_pour"] = bool(z.GetDoNotAllowZoneFills())
            d["no_pads"] = bool(z.GetDoNotAllowPads())
        else:
            d["net"] = z.GetNetname() if z.GetNetCode() > 0 else ""
            d["priority"] = z.GetAssignedPriority()
            d["filled"] = bool(z.IsFilled())
            try:
                d["teardrop"] = bool(z.IsTeardropArea())
            except Exception:  # noqa: BLE001
                pass
            if want_fills and d["net"]:
                fills = {}
                for l, _n in lays:
                    if ls.Contains(l) and z.HasFilledPolysForLayer(l):
                        fp_ = z.GetFilledPolysList(l)
                        if fp_.OutlineCount():
                            fills[str(lidx[l])] = _polyset(fp_)
                if fills:
                    d["fills"] = fills
        zones.append(d)
    for z in b.Zones():
        one_zone(z)
    for fp in b.GetFootprints():
        try:
            for z in fp.Zones():
                one_zone(z, fp.GetReference())
        except Exception:  # noqa: BLE001
            pass
    out["zones"] = zones
    try:
        conn.RecalculateRatsnest()
        out["unconnected"] = int(conn.GetUnconnectedCount(False))
    except Exception:  # noqa: BLE001
        out["unconnected"] = None
    out["unconnected_by_net"] = unconnected_by_net(b)
    out["totals"] = totals(b)
    if a.get("net_stats"):
        out["net_stats"] = net_stats(b)
    return out


@op("commit")
def op_commit(b, a, ctx):
    """Add routed copper. tracks: [[net, layer_name, x1, y1, x2, y2, width], ...];
    vias: [[net, x, y, size, drill], ...]. Returns the new items' uuids in input order."""
    nets = {}

    def net(name):
        if name not in nets:
            ni = b.FindNet(name)
            if ni is None:
                raise WorkerError("no net '%s' on the board" % name, "NOT_FOUND")
            nets[name] = ni
        return nets[name]
    lids = {}

    def lid(name):
        if name not in lids:
            l = b.GetLayerID(name)
            if l < 0 or not b.IsLayerEnabled(l):
                raise WorkerError("layer '%s' is not an enabled copper layer" % name, "BAD_LAYER")
            lids[name] = l
        return lids[name]
    tu, vu = [], []
    for n, lay, x1, y1, x2, y2, w in a.get("tracks") or []:
        t = P.PCB_TRACK(b)
        t.SetStart(P.VECTOR2I(mm(x1), mm(y1)))
        t.SetEnd(P.VECTOR2I(mm(x2), mm(y2)))
        t.SetWidth(mm(w))
        t.SetLayer(lid(lay))
        t.SetNet(net(n))
        b.Add(t)
        tu.append(uuid(t))
    for n, x, y, size, drill in a.get("vias") or []:
        v = P.PCB_VIA(b)
        v.SetPosition(P.VECTOR2I(mm(x), mm(y)))
        v.SetViaType(P.VIATYPE_THROUGH)
        v.SetLayerPair(P.F_Cu, P.B_Cu)
        try:
            v.SetWidth(P.F_Cu, mm(size))
        except Exception:  # noqa: BLE001
            v.SetWidth(mm(size))
        v.SetDrill(mm(drill))
        v.SetNet(net(n))
        b.Add(v)
        vu.append(uuid(v))
    if tu or vu:
        ctx["modified"] = True
        ctx["needs_fill"] = True
    return {"track_uuids": tu, "via_uuids": vu}


@op("remove")
def op_remove(b, a, ctx):
    uu = set(a.get("uuids") or [])
    n = 0
    for t in list(b.GetTracks()):
        if uuid(t) in uu:
            b.Delete(t)
            n += 1
    if n:
        ctx["modified"] = True
        ctx["needs_fill"] = True
    return {"removed": n}


@op("stats")
def op_stats(b, a, ctx):
    if ctx.get("needs_fill"):
        fill_all(b)
        ctx["needs_fill"] = False
    out = {"unconnected_by_net": unconnected_by_net(b), "totals": totals(b), "net_stats": net_stats(b)}
    try:
        c = b.GetConnectivity()
        c.RecalculateRatsnest()
        out["unconnected"] = int(c.GetUnconnectedCount(False))
    except Exception:  # noqa: BLE001
        out["unconnected"] = sum(out["unconnected_by_net"].values())
    return out


# ── main ──────────────────────────────────────────────────────────────────
def handle(req):
    path = req["pcb"]
    if not os.path.exists(path):
        raise WorkerError("board file not found: %s" % path, "NOT_FOUND", "pcb_sync() creates the board from the schematic")
    b = P.LoadBoard(path)
    if b is None:
        raise WorkerError("pcbnew could not load %s" % path, "LOAD_FAILED")
    ctx = {"modified": False, "needs_fill": False}
    results = []
    for o in req.get("ops", []):
        fn = OPS.get(o.get("op"))
        if fn is None:
            raise WorkerError("unknown worker op '%s'" % o.get("op"), "BAD_OP")
        if o.get("op") in ("extract",) and ctx.get("needs_fill"):
            fill_all(b)
            ctx["needs_fill"] = False
        results.append(fn(b, o, ctx))
    if ctx.get("needs_fill"):
        fill_all(b)
    saved = False
    if req.get("save", True) and ctx["modified"]:
        tmp = path + ".copper-tmp"
        if not P.SaveBoard(tmp, b, True):
            raise WorkerError("pcbnew failed to save the board", "SAVE_FAILED")
        try:    # keep the board's own permissions; the rename would otherwise give it the temp file's
            import shutil
            shutil.copymode(path, tmp)
        except OSError:
            pass
        for i in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if i == 4:
                    raise
                import time
                time.sleep(0.3)
        saved = True
    return {"results": results, "saved": saved, "modified": ctx["modified"]}


def main():
    raw = sys.stdin.read()
    try:
        out = {"ok": True, "result": handle(json.loads(raw))}
    except WorkerError as e:
        out = {"ok": False, "error": e.msg, "code": e.code, "fix": e.fix}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "code": "WORKER_ERROR",
               "trace": traceback.format_exc().splitlines()[-8:]}
    sys.stdout.write("\n" + MARK + json.dumps(out, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
