"""pcbnew worker: runs inside KiCad's Python (``import pcbnew``).

Protocol: one JSON request on stdin::

    {"pcb": "C:/.../board.kicad_pcb", "create": false, "save": true,
     "ops": [{"op": "read", ...}, {"op": "place", "items": [...]}, ...]}

Exactly one line ``@@COPPER_RESULT@@{json}`` is printed on stdout::

    {"ok": true, "result": {"results": [...one per op...], "saved": true, "modified": true}}
    {"ok": false, "error": "...", "code": "...", "fix": "...", "trace": [...]}

Ops run in order against one in-memory board; the file is written once at
the end (atomically: temp file + replace) only if every op succeeded.
All coordinates are millimetres, angles degrees (KiCad convention).
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


# Before any board is loaded: never show wx dialogs (pcbnew reports e.g. a missing font through wxLog, which
# pops up a MODAL dialog in a headless worker and blocks LoadBoard until the server kills the worker), and
# refuse to run when this pcbnew belongs to another KiCad series than the kicad-cli Copper uses.
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


# ── unit helpers ──────────────────────────────────────────────────────────
def mm(v):
    return int(round(float(v) * 1e6))


def tomm(v):
    return round(v / 1e6, 4)


def V(x, y):
    return P.VECTOR2I(mm(x), mm(y))


def vxy(v):
    return [tomm(v.x), tomm(v.y)]


def layer_id(board, name):
    aliases = {"F.Silkscreen": "F.SilkS", "B.Silkscreen": "B.SilkS", "F.Courtyard": "F.CrtYd",
               "B.Courtyard": "B.CrtYd", "top": "F.Cu", "bottom": "B.Cu", "F": "F.Cu", "B": "B.Cu"}
    n = aliases.get(name, name)
    lid = board.GetLayerID(n)
    if lid < 0 or lid == P.UNDEFINED_LAYER:
        raise WorkerError("unknown layer '%s'" % name, "BAD_LAYER",
                          "use a KiCad layer name such as F.Cu, B.Cu, In1.Cu, F.SilkS, Edge.Cuts")
    if not board.IsLayerEnabled(lid):
        raise WorkerError("layer '%s' is not enabled on this %d-layer board" % (name, board.GetCopperLayerCount()),
                          "BAD_LAYER", "pcb_setup(layers=4) to add inner layers, or use F.Cu / B.Cu")
    return lid


def fp_by_ref(board, ref):
    for fp in board.GetFootprints():
        if fp.GetReference() == ref:
            return fp
    raise WorkerError("no footprint '%s' on the board" % ref, "NOT_FOUND",
                      "pcb_read() to list footprints, or pcb_sync() to pull parts from the schematic")


def pad_of(board, spec):
    """'R1.2' / {'ref':'R1','pad':'2'} -> PAD"""
    if isinstance(spec, dict):
        ref, num = spec.get("ref"), str(spec.get("pad"))
    else:
        if "." not in str(spec):
            raise WorkerError("pad reference '%s' must look like REF.PAD (e.g. U1.3)" % spec, "BAD_ARGUMENT")
        ref, num = str(spec).rsplit(".", 1)
    fp = fp_by_ref(board, ref)
    for pad in fp.Pads():
        if pad.GetNumber() == num:
            return pad
    raise WorkerError("footprint %s has no pad '%s' (pads: %s)" % (ref, num, sorted({p.GetNumber() for p in fp.Pads()})),
                      "NOT_FOUND")


def net_item(board, name, create=False):
    if name in (None, ""):
        return None
    ni = board.FindNet(name)
    if ni is None and create:
        ni = P.NETINFO_ITEM(board, name)
        board.Add(ni)
    if ni is None:
        raise WorkerError("no net '%s' on the board" % name, "NOT_FOUND", "pcb_read(what='nets') to list nets")
    return ni


def uuid(item):
    return item.m_Uuid.AsString()


# ── geometry of a footprint ───────────────────────────────────────────────
def _rot_inv(dx, dy, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return dx * c - dy * s, dx * s + dy * c


def fp_abs_bbox(fp):
    """Courtyard bbox (board coords, mm) or the footprint body bbox without text."""
    try:
        fp.BuildCourtyardCaches()
        lay = P.B_CrtYd if fp.IsFlipped() else P.F_CrtYd
        cy = fp.GetCourtyard(lay)
        if cy.OutlineCount() > 0:
            bb = cy.BBox()
            if bb.GetWidth() > 0:
                return [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())], True
    except Exception:  # noqa: BLE001
        pass
    bb = fp.GetBoundingBox(False, False)
    return [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())], False


def pad_info(pad, local=False):
    attr = pad.GetAttribute()
    d = {"num": pad.GetNumber(), "net": pad.GetNetname(),
         "type": {P.PAD_ATTRIB_SMD: "smd", P.PAD_ATTRIB_PTH: "tht", P.PAD_ATTRIB_NPTH: "npth",
                  P.PAD_ATTRIB_CONN: "conn"}.get(attr, "smd")}
    pos = pad.GetPosition()
    d["x"], d["y"] = tomm(pos.x), tomm(pos.y)
    if local:
        rp = pad.GetFPRelativePosition()
        d["lx"], d["ly"] = tomm(rp.x), tomm(rp.y)
        sz = pad.GetSize(P.F_Cu) if attr != P.PAD_ATTRIB_NPTH else pad.GetDrillSize()
        ang = pad.GetFPRelativeOrientation().AsDegrees() if hasattr(pad, "GetFPRelativeOrientation") else 0
        w, h = tomm(sz.x), tomm(sz.y)
        if abs(round(ang) % 180) == 90:
            w, h = h, w
        d["w"], d["h"] = w, h
        if pad.GetPinFunction():
            d["fn"] = pad.GetPinFunction()
        if pad.GetPinType():
            d["pt"] = pad.GetPinType()
    return d


USER_SKIP = {"Reference", "Value", "Footprint", "Datasheet", "Description"}


def fp_info(fp, pads=False):
    pos = fp.GetPosition()
    fields = {}
    for f in fp.GetFields():
        n = f.GetName()
        if n not in USER_SKIP and f.GetText():
            fields[n] = f.GetText()
    bbox, has_cy = fp_abs_bbox(fp)
    d = {"ref": fp.GetReference(), "value": fp.GetValue(),
         "footprint": str(fp.GetFPID().GetUniStringLibId()),
         "x": tomm(pos.x), "y": tomm(pos.y), "rot": round(fp.GetOrientationDegrees(), 3),
         "side": "bottom" if fp.IsFlipped() else "top", "locked": bool(fp.IsLocked()),
         "pads": len(fp.Pads()), "bbox": bbox, "courtyard": has_cy, "uuid": uuid(fp)}
    for k in ("LCSC", "MPN"):
        if k in fields:
            d[k.lower()] = fields[k]
    if fields:
        d["fields"] = fields
    flags = []
    if fp.IsBoardOnly():
        flags.append("board_only")
    if fp.IsDNP():
        flags.append("dnp")
    if fp.IsExcludedFromBOM():
        flags.append("no_bom")
    if fp.IsExcludedFromPosFiles():
        flags.append("no_pos")
    if flags:
        d["flags"] = flags
    try:
        pth = fp.GetPath().AsString()
        if pth:
            d["path"] = pth
    except Exception:  # noqa: BLE001
        pass
    if pads:
        d["pad_list"] = [pad_info(p) for p in fp.Pads()]
    return d


import re as _re

_RF_RE = _re.compile(r"wifi|wlan|wroom|wrover|esp32|esp8266|esp-\d|-mini-1|nrf5\d|\bble\b|bluetooth|rf.?module|"
                     r"antenna|lora|rfm9\d|sx12\d\d|cc1101|xbee|mdbt|hm-1\d|bc832|rak\d{4}|wfm|wifim|bl602|w600|rtl87", _re.I)


def _antenna_region(fp, lbbox, pads):
    """RF modules with a PCB/chip antenna: the courtyard extends well past the pads on one side.
    → {'side': 'top'|'bottom'|'left'|'right', 'rect': [x0,y0,x1,y1] local mm} or None."""
    try:
        text = " ".join([fp.GetFPIDAsString(), fp.GetValue(), fp.GetLibDescription() or "", fp.GetKeywords() or ""])
    except Exception:  # noqa: BLE001
        text = fp.GetFPIDAsString()
    if not _RF_RE.search(text) or not pads or not lbbox:
        return None
    px0 = min(p["lx"] - p.get("w", 0) / 2 for p in pads)
    px1 = max(p["lx"] + p.get("w", 0) / 2 for p in pads)
    py0 = min(p["ly"] - p.get("h", 0) / 2 for p in pads)
    py1 = max(p["ly"] + p.get("h", 0) / 2 for p in pads)
    x0, y0, x1, y1 = lbbox
    gaps = {"top": (py0 - y0, y1 - y0), "bottom": (y1 - py1, y1 - y0),
            "left": (px0 - x0, x1 - x0), "right": (x1 - px1, x1 - x0)}
    side, (g, dim) = max(gaps.items(), key=lambda kv: kv[1][0])
    if g < 3.5 or g < 0.2 * dim:
        return None
    rect = {"top": [x0, y0, x1, py0 - 0.3], "bottom": [x0, py1 + 0.3, x1, y1],
            "left": [x0, y0, px0 - 0.3, y1], "right": [px1 + 0.3, y0, x1, y1]}[side]
    return {"side": side, "rect": [round(v, 3) for v in rect]}


def fp_local_geom(fp):
    """Normalise a footprint in place (front, rot 0, at origin) and return its
    local courtyard bbox + pads. Only used by read(geom=True) — never saved."""
    if fp.IsFlipped():
        fp.Flip(fp.GetPosition(), P.FLIP_DIRECTION_LEFT_RIGHT)
    fp.SetOrientationDegrees(0)
    fp.SetPosition(P.VECTOR2I(0, 0))
    bbox, has_cy = fp_abs_bbox(fp)
    bb = fp.GetBoundingBox(False, False)
    body = [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())]
    out = {"lbbox": bbox, "lbody": body, "courtyard": has_cy,
           "pad_list": [pad_info(p, local=True) for p in fp.Pads()]}
    ant = _antenna_region(fp, bbox, out["pad_list"])
    if ant:
        out["antenna"] = ant
    # connectors in the KiCad library mark where the board edge goes ("PCB Edge" text / line)
    try:
        for g in fp.GraphicalItems():
            if g.GetClass() == "PCB_TEXT" and "pcb edge" in g.GetText().lower():
                out["edge_hint"] = vxy(g.GetPosition())
                break
    except Exception:  # noqa: BLE001
        pass
    return out


# ── board-level info ──────────────────────────────────────────────────────
def edge_shapes(board):
    out = []
    for d in board.GetDrawings():
        if d.GetLayer() == P.Edge_Cuts and d.GetClass() in ("PCB_SHAPE",):
            out.append(d)
    return out


def outline_info(board):
    bb = board.GetBoardEdgesBoundingBox()
    if not edge_shapes(board) or bb.GetWidth() <= 0:
        return None
    d = {"bbox": [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())],
         "width": tomm(bb.GetWidth()), "height": tomm(bb.GetHeight())}
    try:
        ps = P.SHAPE_POLY_SET()
        if board.GetBoardPolygonOutlines(ps, False):
            if ps.OutlineCount() > 0:
                o = ps.Outline(0)
                pts = [vxy(o.CPoint(i)) for i in range(o.PointCount())]
                if len(pts) <= 400:
                    d["polygon"] = pts
                if pts:
                    xs = [q[0] for q in pts]
                    ys = [q[1] for q in pts]
                    d["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
                    d["width"], d["height"] = round(max(xs) - min(xs), 4), round(max(ys) - min(ys), 4)
                d["closed"] = True
        else:
            d["closed"] = False
    except Exception:  # noqa: BLE001
        pass
    return d


def unconnected_count(board):
    try:
        board.BuildConnectivity()
        c = board.GetConnectivity()
        c.RecalculateRatsnest()
        return int(c.GetUnconnectedCount(False))
    except Exception:  # noqa: BLE001
        return None


def unconnected_by_net(board):
    """{net: unrouted connections} = pad clusters - 1 per net (matches KiCad's ratsnest count)."""
    try:
        board.BuildConnectivity()
        c = board.GetConnectivity()
        pads = {}
        for fp in board.GetFootprints():
            for p in fp.Pads():
                if p.GetNetCode() > 0:
                    pads.setdefault(p.GetNetname(), []).append(p)
        out = {}
        for n, ps in pads.items():
            seen, clusters = set(), 0
            for p in ps:
                k = p.m_Uuid.AsString()
                if k in seen:
                    continue
                clusters += 1
                seen.add(k)
                for q in c.GetConnectedItems(p):
                    seen.add(q.m_Uuid.AsString())
            if clusters > 1:
                out[n] = clusters - 1
        return out
    except Exception:  # noqa: BLE001
        return None


def track_stats(board):
    n_t = n_v = n_a = 0
    length = 0.0
    for t in board.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            n_v += 1
        else:
            if cls == "PCB_ARC":
                n_a += 1
            n_t += 1
            length += t.GetLength()
    return {"tracks": n_t, "arcs": n_a, "vias": n_v, "track_length_mm": round(length / 1e6, 2)}


# ── ops ───────────────────────────────────────────────────────────────────
OPS = {}


def op(name):
    def deco(fn):
        OPS[name] = fn
        return fn
    return deco


def _is_teardrop(z):
    try:
        if z.IsTeardropArea():
            return True
    except Exception:  # noqa: BLE001
        pass
    return (z.GetZoneName() or "").startswith("$teardrop")


@op("read")
def op_read(b, a, ctx):
    what = set(a.get("what") or ["summary", "footprints", "nets"])
    refs = set(a.get("refs") or [])
    out = {"file_version": b.GetFileFormatVersionAtLoad() if hasattr(b, "GetFileFormatVersionAtLoad") else None,
           "copper_layers": b.GetCopperLayerCount(), "outline": outline_info(b)}
    out.update(track_stats(b))
    out["unconnected"] = unconnected_count(b)
    fps = list(b.GetFootprints())
    out["footprint_count"] = len(fps)
    if "footprints" in what or "geom" in what:
        lst = []
        for fp in fps:
            if refs and fp.GetReference() not in refs:
                continue
            lst.append(fp_info(fp, pads="pads" in what))
        if "geom" in what:
            # board-level copper graphics/text and footprint keep-outs are placement obstacles
            obst = []
            for d in b.GetDrawings():
                try:
                    if d.IsOnCopperLayer():
                        obst.append({"kind": d.GetClass(), "bbox": _bb(d)})
                except Exception:  # noqa: BLE001
                    pass
            for z in b.Zones():
                try:
                    if z.GetIsRuleArea() and z.GetDoNotAllowFootprints():
                        obst.append({"kind": "keepout", "bbox": _bb(z)})
                except Exception:  # noqa: BLE001
                    pass
            out["obstacles"] = obst
            # existing copper: parts placed into a routed board must not land on it
            segs = []
            for t in b.GetTracks():
                if t.GetClass() == "PCB_VIA":
                    q = vxy(t.GetPosition())
                    segs.append([q[0], q[1], q[0], q[1], tomm(t.GetWidth(P.F_Cu)) / 2, "*"])
                else:
                    s0, e0 = vxy(t.GetStart()), vxy(t.GetEnd())
                    segs.append([s0[0], s0[1], e0[0], e0[1], tomm(t.GetWidth()) / 2, b.GetLayerName(t.GetLayer())])
            out["track_segs"] = segs
            # normalise AFTER collecting absolute info (read never saves)
            by = {fp.GetReference(): fp for fp in fps}
            for d in lst:
                d.update(fp_local_geom(by[d["ref"]]))
        out["footprints"] = lst
    if "nets" in what:
        nets = {}
        for fp in fps:
            for pad in fp.Pads():
                n = pad.GetNetname()
                if n:
                    nets.setdefault(n, []).append("%s.%s" % (fp.GetReference(), pad.GetNumber()))
        nc = {}
        try:
            for name, ni in b.GetNetsByName().items():
                nc[str(name)] = ni.GetNetClassName() if hasattr(ni, "GetNetClassName") else ""
        except Exception:  # noqa: BLE001
            pass
        out["nets"] = [{"name": n, "pads": len(p), "class": nc.get(n, "").split(",")[0], "nodes": p[:12] + (["…"] if len(p) > 12 else [])}
                       for n, p in sorted(nets.items())]
    if "tracks" in what:
        tl = []
        for t in b.GetTracks():
            d = {"uuid": uuid(t), "type": t.GetClass().replace("PCB_", "").lower(), "net": t.GetNetname()}
            if t.GetClass() == "PCB_VIA":
                d.update({"at": vxy(t.GetPosition()), "size": tomm(t.GetWidth(P.F_Cu)), "drill": tomm(t.GetDrillValue())})
            else:
                d.update({"start": vxy(t.GetStart()), "end": vxy(t.GetEnd()), "width": tomm(t.GetWidth()),
                          "layer": b.GetLayerName(t.GetLayer())})
            tl.append(d)
        out["track_list"] = tl[: int(a.get("limit", 100000))]
    if "zones" in what or "summary" in what:
        zl, tear = [], 0
        for z in b.Zones():
            if _is_teardrop(z):   # one per pad/via on boards with teardrops: a count, not hundreds of entries
                tear += 1
                continue
            zl.append({"uuid": uuid(z), "net": z.GetNetname(), "name": z.GetZoneName(),
                       "layers": [b.GetLayerName(l) for l in z.GetLayerSet().Seq()],
                       "filled": bool(z.IsFilled()), "priority": z.GetAssignedPriority(),
                       "rule_area": bool(z.GetIsRuleArea())})
        out["zones"] = zl
        if tear:
            out["teardrop_zones"] = tear
    if "drawings" in what:
        dl = []
        for d in b.GetDrawings():
            e = {"uuid": uuid(d), "class": d.GetClass(), "layer": b.GetLayerName(d.GetLayer())}
            if d.GetClass() in ("PCB_TEXT",):
                e["text"] = d.GetText()
                e["at"] = vxy(d.GetPosition())
            dl.append(e)
        out["drawings"] = dl
    return out


@op("design")
def op_design(b, a, ctx):
    ds = b.GetDesignSettings()
    return {"min_track": tomm(ds.m_TrackMinWidth), "min_clearance": tomm(ds.m_MinClearance),
            "min_via": tomm(ds.m_ViasMinSize), "min_drill": tomm(ds.m_MinThroughDrill),
            "edge_clearance": tomm(ds.m_CopperEdgeClearance), "hole_to_hole": tomm(ds.m_HoleToHoleMin),
            "copper_layers": b.GetCopperLayerCount()}


# ── sync (Update PCB from Schematic) ──────────────────────────────────────
def _load_fp(libs, fpid):
    if ":" not in fpid:
        raise WorkerError("footprint '%s' has no library nickname" % fpid, "BAD_FOOTPRINT")
    nick, name = fpid.split(":", 1)
    path = libs.get(nick)
    if not path:
        raise WorkerError("footprint library '%s' is not in any fp-lib-table" % nick, "LIB_NOT_FOUND")
    # check the file first: a failing FootprintLoad() leaves pcbnew's SWIG state corrupt (seen on KiCad 10.0.6)
    if os.path.isdir(path) and not os.path.isfile(os.path.join(path, name + ".kicad_mod")):
        raise WorkerError("footprint '%s' not found in %s" % (name, path), "FOOTPRINT_NOT_FOUND")
    fp = P.FootprintLoad(path, name)
    if fp is None:
        raise WorkerError("footprint '%s' not found in %s" % (name, path), "FOOTPRINT_NOT_FOUND")
    fp.SetFPID(P.LIB_ID(nick, name))
    return fp


def _set_user_field(fp, name, value, visible=False):
    existed = fp.HasField(name)
    fp.SetField(name, value)
    f = fp.GetField(name)
    if not existed:
        f.SetVisible(bool(visible))
        f.SetLayer(P.B_Fab if fp.IsFlipped() else P.F_Fab)
    return f


def _copy_text_layout(old, new):
    """Keep the board-side placement of Reference/Value (KiCad's footprint exchange does the same),
    so swapping a footprint does not scatter silkscreen text."""
    for get in ("Reference", "Value"):
        try:
            o, n = getattr(old, get)(), getattr(new, get)()
            n.SetPosition(o.GetPosition())
            n.SetTextAngle(o.GetTextAngle())
            n.SetLayer(o.GetLayer())
            n.SetVisible(o.IsVisible())
            n.SetTextSize(o.GetTextSize())
            n.SetTextThickness(o.GetTextThickness())
            n.SetMirrored(o.IsMirrored())
            n.SetHorizJustify(o.GetHorizJustify())
            n.SetVertJustify(o.GetVertJustify())
            if hasattr(n, "SetKeepUpright"):
                n.SetKeepUpright(o.IsKeepUpright())
        except Exception:  # noqa: BLE001
            pass


def _fp_key(fp):
    try:
        return fp.GetPath().AsString() or fp.GetReference()
    except Exception:  # noqa: BLE001
        return fp.GetReference()


def _resync_copper(b, nets, old_members, remove_extra, get_net):
    """Tracks, vias and zones whose net no longer exists after the sync. A pure rename (every pad the
    old net had is still there, now on one new net) moves the copper to the new net; copper of a net
    whose pads were deleted is orphaned: teardrops are always removed, tracks/vias only with
    remove_extra (otherwise reported), user pours are reported."""
    valid = {n["name"] for n in nets}
    cur = {}
    for fp in b.GetFootprints():
        k0 = _fp_key(fp)
        for pad in fp.Pads():
            cur[(k0, pad.GetNumber())] = pad.GetNetname()
    stale = {}
    for t in b.GetTracks():
        if t.GetNetCode() > 0 and t.GetNetname() not in valid:
            stale.setdefault(t.GetNetname(), []).append(t)
    for z in b.Zones():
        try:
            if not z.GetIsRuleArea() and z.GetNetCode() > 0 and z.GetNetname() not in valid:
                stale.setdefault(z.GetNetname(), []).append(z)
        except Exception:  # noqa: BLE001
            pass
    out = {"renamed_copper": {}, "orphan_copper": {}, "removed_copper": {}, "stale_pours": []}
    for old, items in stale.items():
        mem = old_members.get(old, set())
        news = {cur.get(k) for k in mem}
        if mem and None not in news and len(news) == 1 and next(iter(news)) in valid:
            new = next(iter(news))
            ni = get_net(new)
            for it in items:
                it.SetNet(ni)
            out["renamed_copper"]["%s → %s" % (old, new)] = len(items)
            continue
        for it in items:
            is_zone = it.GetClass() == "ZONE"
            tear = is_zone and it.IsTeardropArea()
            if tear or (remove_extra and not is_zone):
                b.Delete(it)
                out["removed_copper"][old] = out["removed_copper"].get(old, 0) + 1
            elif is_zone:
                out["stale_pours"].append(old)
            else:
                out["orphan_copper"].setdefault(old, []).append(uuid(it))
    return {k: v for k, v in out.items() if v}


@op("sync")
def op_sync(b, a, ctx):
    comps = a["components"]          # [{ref,value,footprint,fields,path,sheetname,sheetfile,dnp,exclude_bom,exclude_board}]
    nets = a["nets"]                 # [{name, nodes:[[ref,pin],...]}]
    libs = a.get("libs", {})
    remove_extra = bool(a.get("remove_extra", False))
    replace_fp = bool(a.get("update_footprints", True))
    report = {"added": [], "updated": [], "replaced": [], "removed": [], "extra": [], "errors": [],
              "unassigned_pads": [], "net_changes": 0, "nets": len(nets)}
    # which pads each net had before the sync: tells a renamed net from one whose parts were deleted
    old_members = {}
    for fp in b.GetFootprints():
        key0 = _fp_key(fp)
        for pad in fp.Pads():
            if pad.GetNetCode() > 0:
                old_members.setdefault(pad.GetNetname(), set()).add((key0, pad.GetNumber()))
    by_path, by_ref = {}, {}
    for fp in b.GetFootprints():
        try:
            pth = fp.GetPath().AsString()
        except Exception:  # noqa: BLE001
            pth = ""
        if pth:
            by_path[pth] = fp
        by_ref[fp.GetReference()] = fp
    new_fps = []
    sch_refs = set()
    sch_paths = {c.get("path") for c in comps if c.get("path") and not c.get("exclude_board")}
    claimed = set()
    for c in comps:
        ref = c["ref"]
        if c.get("exclude_board"):
            continue
        sch_refs.add(ref)
        fp = by_path.get(c.get("path") or "\0")
        if fp is None:
            # re-link by reference only when that footprint does not belong to another schematic symbol
            cand = by_ref.get(ref)
            if cand is not None:
                try:
                    cp = cand.GetPath().AsString()
                except Exception:  # noqa: BLE001
                    cp = ""
                if not cp or cp not in sch_paths:
                    fp = cand
        if fp is not None and uuid(fp) in claimed:
            fp = None
        if fp is not None:
            claimed.add(uuid(fp))
        fpid = c.get("footprint") or ""
        if not fpid:
            report["errors"].append({"ref": ref, "error": "symbol has no Footprint field",
                                     "fix": "sch_apply([{op:'set_field', ref:'%s', field:'Footprint', value:'Lib:Name'}])" % ref})
            continue
        changes = []
        fresh = False
        if fp is None:
            try:
                fp = _load_fp(libs, fpid)
            except WorkerError as e:
                report["errors"].append({"ref": ref, "footprint": fpid, "error": e.msg,
                                         "fix": "library(action='search_footprints', query=...) then set the symbol's Footprint field"})
                continue
            b.Add(fp)
            claimed.add(uuid(fp))
            new_fps.append(fp)
            fresh = True
            report["added"].append({"ref": ref, "footprint": fpid})
        else:
            cur = str(fp.GetFPID().GetUniStringLibId())
            if cur != fpid and replace_fp:
                try:
                    nfp = _load_fp(libs, fpid)
                except WorkerError as e:
                    report["errors"].append({"ref": ref, "footprint": fpid, "error": e.msg})
                    nfp = None
                if nfp is not None:
                    nfp.SetPosition(fp.GetPosition())
                    if fp.IsFlipped():
                        nfp.Flip(nfp.GetPosition(), P.FLIP_DIRECTION_LEFT_RIGHT)
                    nfp.SetOrientation(fp.GetOrientation())
                    nfp.SetLocked(fp.IsLocked())
                    _copy_text_layout(fp, nfp)
                    b.Delete(fp)
                    b.Add(nfp)
                    claimed.add(uuid(nfp))
                    report["replaced"].append({"ref": ref, "from": cur, "to": fpid})
                    fp = nfp
                    fresh = True
        if fp.GetReference() != ref:
            changes.append("ref %s→%s" % (fp.GetReference(), ref))
            fp.SetReference(ref)
        if fp.GetValue() != c.get("value", ""):
            if not fresh:
                changes.append("value %s→%s" % (fp.GetValue(), c.get("value", "")))
            fp.SetValue(c.get("value", ""))
        for k, v in (c.get("fields") or {}).items():
            if k in ("Datasheet", "Description"):
                v = "" if v is None else str(v)
                if fp.GetFieldText(k) != v:
                    fp.SetField(k, v)
                    if not fresh:
                        changes.append("%s=%s" % (k, v))
                continue
            if k in USER_SKIP or v in (None, ""):
                continue
            old = fp.GetFieldText(k) if fp.HasField(k) else None
            if old != v:
                _set_user_field(fp, k, str(v))
                if not fresh:
                    changes.append("%s=%s" % (k, v))
        if c.get("path"):
            try:
                if fp.GetPath().AsString() != c["path"]:
                    fp.SetPath(P.KIID_PATH(c["path"]))
            except Exception:  # noqa: BLE001
                pass
        if c.get("sheetname") is not None:
            fp.SetSheetname(c.get("sheetname", ""))
        if c.get("sheetfile") is not None:
            fp.SetSheetfile(c.get("sheetfile", ""))
        for flag, getter, setter in (("dnp", fp.IsDNP, fp.SetDNP), ("exclude_bom", fp.IsExcludedFromBOM, fp.SetExcludedFromBOM)):
            want = bool(c.get(flag, False))
            if bool(getter()) != want:
                setter(want)
                if not fresh:
                    changes.append("%s=%s" % (flag, want))
        if changes and not fresh:
            report["updated"].append({"ref": ref, "changes": changes})

    # extra footprints (on board, not in schematic)
    for fp in list(b.GetFootprints()):
        ref = fp.GetReference()
        if uuid(fp) in claimed or fp.IsBoardOnly():
            continue
        if remove_extra and not fp.IsLocked():
            b.Delete(fp)
            report["removed"].append(ref)
        else:
            report["extra"].append(ref)

    # nets → pads
    pin_net, pin_meta = {}, {}
    for n in nets:
        for ref, pin in n["nodes"]:
            pin_net[(ref, str(pin))] = n["name"]
        for pm in n.get("pins") or []:
            pin_meta[(pm["ref"], str(pm["pin"]))] = (pm.get("fn") or "", pm.get("type") or "")
    net_cache = {}

    def get_net(name):
        if name not in net_cache:
            net_cache[name] = net_item(b, name, create=True)
        return net_cache[name]
    unassigned = set()
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        if uuid(fp) not in claimed:
            continue
        for pad in fp.Pads():
            num = pad.GetNumber()
            if not num or pad.GetAttribute() == P.PAD_ATTRIB_NPTH:
                continue
            want = pin_net.get((ref, num))
            if want is None:
                if pad.GetNetCode() != 0:
                    pad.SetNetCode(0)
                    report["net_changes"] += 1
                if (ref, num) not in unassigned:
                    unassigned.add((ref, num))
                    report["unassigned_pads"].append("%s.%s" % (ref, num))
                continue
            if pad.GetNetname() != want:
                pad.SetNet(get_net(want))
                report["net_changes"] += 1
            meta = pin_meta.get((ref, num))
            if meta:
                if pad.GetPinFunction() != meta[0]:
                    pad.SetPinFunction(meta[0])
                if pad.GetPinType() != meta[1]:
                    pad.SetPinType(meta[1])
    report.update(_resync_copper(b, nets, old_members, remove_extra, get_net))
    # footprint pins that do not exist as pads
    pads_present = {(fp.GetReference(), p.GetNumber()) for fp in b.GetFootprints() for p in fp.Pads()}
    missing = sorted("%s.%s" % k for k in pin_net if k not in pads_present and k[0] in sch_refs
                     and not pin_net[k].startswith("unconnected-"))
    if missing:
        report["pins_without_pad"] = missing[:50]

    # tidy grid for new footprints, beside the board outline
    if new_fps:
        ol = outline_info(b)
        if a.get("place_at"):
            x0, y0 = a["place_at"]
        elif ol:
            x0, y0 = ol["bbox"][2] + 5, ol["bbox"][1]
        else:
            x0, y0 = 170.0, 100.0   # right of where pcb_setup puts a default board (origin 100,100)
        new_fps.sort(key=lambda f: (f.GetReference().rstrip("0123456789"), int("0" + "".join(ch for ch in f.GetReference() if ch.isdigit()))))
        x, y, row_h, col_w = x0, y0, 0.0, 60.0
        for fp in new_fps:
            bb, _ = fp_abs_bbox(fp)
            pos = fp.GetPosition()
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            if x + w > x0 + col_w and x > x0:
                x, y, row_h = x0, y + row_h + 1.0, 0.0
            # place so the bbox's top-left lands at (x, y)
            fp.SetPosition(V(round((x + (tomm(pos.x) - bb[0])) * 2) / 2, round((y + (tomm(pos.y) - bb[1])) * 2) / 2))
            x += w + 1.0
            row_h = max(row_h, h)
    ctx["modified"] = True
    ctx["needs_fill"] = True
    return report


# ── setup ops ─────────────────────────────────────────────────────────────
def _seg(b, x1, y1, x2, y2, width, layer):
    s = P.PCB_SHAPE(b)
    s.SetShape(P.SHAPE_T_SEGMENT)
    s.SetStart(V(x1, y1))
    s.SetEnd(V(x2, y2))
    s.SetLayer(layer)
    s.SetWidth(mm(width))
    b.Add(s)
    return s


def _arc(b, start, mid, end, width, layer):
    s = P.PCB_SHAPE(b)
    s.SetShape(P.SHAPE_T_ARC)
    s.SetArcGeometry(V(*start), V(*mid), V(*end))
    s.SetLayer(layer)
    s.SetWidth(mm(width))
    b.Add(s)
    return s


@op("outline")
def op_outline(b, a, ctx):
    removed = 0
    if a.get("replace", True):
        for s in edge_shapes(b):
            b.Delete(s)
            removed += 1
    width = float(a.get("line_width", 0.05))
    L = P.Edge_Cuts
    if a.get("polygon"):
        pts = a["polygon"]
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            _seg(b, x1, y1, x2, y2, width, L)
        n = len(pts)
    else:
        x0, y0 = a.get("origin", [100, 100])
        w, h = float(a["width"]), float(a["height"])
        r = min(float(a.get("radius", 0) or 0), w / 2, h / 2)
        x1, y1 = x0 + w, y0 + h
        if r <= 0:
            for (p1, p2) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
                _seg(b, p1[0], p1[1], p2[0], p2[1], width, L)
            n = 4
        else:
            k = r * (1 - math.sqrt(0.5))
            _seg(b, x0 + r, y0, x1 - r, y0, width, L)
            _seg(b, x1, y0 + r, x1, y1 - r, width, L)
            _seg(b, x1 - r, y1, x0 + r, y1, width, L)
            _seg(b, x0, y1 - r, x0, y0 + r, width, L)
            _arc(b, (x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r), width, L)
            _arc(b, (x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1), width, L)
            _arc(b, (x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r), width, L)
            _arc(b, (x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0), width, L)
            n = 8
    ctx["modified"] = True
    return {"edge_shapes_removed": removed, "edge_shapes_added": n, "outline": outline_info(b)}


HOLES = {"M2": ("MountingHole_2.2mm_M2", 2.2), "M2.5": ("MountingHole_2.7mm_M2.5", 2.7),
         "M3": ("MountingHole_3.2mm_M3", 3.2), "M4": ("MountingHole_4.3mm_M4", 4.3)}


@op("holes")
def op_holes(b, a, ctx):
    size = a.get("size", "M3")
    if size not in HOLES:
        raise WorkerError("hole size must be one of %s" % sorted(HOLES), "BAD_ARGUMENT")
    name, dia = HOLES[size]
    if a.get("plated"):
        name += "_Pad_Via" if a.get("plated") == "via" else "_Pad"
    lib = a.get("lib_path")
    if not lib:
        raise WorkerError("MountingHole footprint library not found", "LIB_NOT_FOUND",
                          "library(action='repair') so the stock 'MountingHole' library is in the fp-lib-table")
    removed = []
    for fp in list(b.GetFootprints()):
        r = fp.GetReference()
        if fp.IsBoardOnly() and r.startswith("H") and "MountingHole" in str(fp.GetFPID().GetUniStringLibId()):
            b.Delete(fp)
            removed.append(r)
    pts = a.get("positions")
    if not pts:
        ol = outline_info(b)
        if not ol:
            raise WorkerError("board has no outline; cannot place corner holes", "NO_OUTLINE",
                              "pcb_setup(width=..., height=...) first, or pass explicit hole positions")
        x0, y0, x1, y1 = ol["bbox"]
        i = float(a.get("inset", 3.5))
        pts = [[x0 + i, y0 + i], [x1 - i, y0 + i], [x1 - i, y1 - i], [x0 + i, y1 - i]]
    added = []
    used = {f.GetReference() for f in b.GetFootprints()}
    used |= set(a.get("reserved_refs") or [])   # schematic refs (e.g. MountingHole symbols H1..H4)
    hrefs, k = [], 0
    while len(hrefs) < len(pts):
        k += 1
        if "H%d" % k not in used:
            hrefs.append("H%d" % k)
    for href, (x, y) in zip(hrefs, pts):
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
                    pad.SetNet(net_item(b, a["net"], create=True))
        b.Add(fp)
        added.append({"ref": href, "at": [x, y], "footprint": "MountingHole:" + name})
    ctx["modified"] = True
    return {"removed": removed, "added": added, "hole_diameter": dia}


@op("layers")
def op_layers(b, a, ctx):
    n = int(a["count"])
    if n not in (2, 4, 6, 8):
        raise WorkerError("copper layer count must be 2, 4, 6 or 8", "BAD_ARGUMENT")
    old = b.GetCopperLayerCount()
    if n < old:
        keep = P.LSET.AllCuMask(n)
        busy = set()
        for t in b.GetTracks():
            if t.GetClass() != "PCB_VIA" and not keep.Contains(t.GetLayer()):
                busy.add(b.GetLayerName(t.GetLayer()))
        for z in b.Zones():
            for lay in z.GetLayerSet().Seq():
                if P.IsCopperLayer(lay) and not keep.Contains(lay):
                    busy.add(b.GetLayerName(lay))
        for d in b.GetDrawings():
            if P.IsCopperLayer(d.GetLayer()) and not keep.Contains(d.GetLayer()):
                busy.add(b.GetLayerName(d.GetLayer()))
        if busy:
            raise WorkerError("cannot drop to %d layers: tracks/zones/graphics still use %s" % (n, ", ".join(sorted(busy))),
                              "LAYERS_IN_USE", "remove them first (pcb_pour(net=..., remove=true) for zones, "
                              "pcb_edit delete for tracks), then retry")
    if old != n:
        b.SetCopperLayerCount(n)
        ds = b.GetDesignSettings()
        ls = ds.GetEnabledLayers()
        ls.AddLayerSet(P.LSET.AllCuMask(n))
        b.SetEnabledLayers(ls)
        b.SetVisibleLayers(ls)
        ctx["modified"] = True
    return {"from": old, "to": n}


@op("text_size")
def op_text_size(b, a, ctx):
    size, th = float(a.get("size", 1.0)), float(a.get("thickness", 0.15))
    n = 0
    for fp in b.GetFootprints():
        texts = [fp.Reference()]
        if a.get("values"):
            texts.append(fp.Value())
        for t in texts:
            if t.GetLayer() in (P.F_SilkS, P.B_SilkS):
                t.SetTextSize(P.VECTOR2I(mm(size), mm(size)))
                t.SetTextThickness(mm(th))
                n += 1
    ctx["modified"] = True
    return {"texts_resized": n, "size": size, "thickness": th}


# ── placement ─────────────────────────────────────────────────────────────
def _touching_tracks(b, fp):
    out = []
    pads = list(fp.Pads())
    for t in b.GetTracks():
        pts = [t.GetPosition()] if t.GetClass() == "PCB_VIA" else [t.GetStart(), t.GetEnd()]
        if any(pd.GetNetCode() == t.GetNetCode() and pd.HitTest(q) for pd in pads for q in pts):
            out.append(t)
    return out


@op("lock")
def op_lock(b, a, ctx):
    """Lock or unlock footprints: {"refs":[...]} or {"all":true}, "locked": true|false (default true).
    all=true leaves board-only mounting holes alone (auto placement never moves them anyway)."""
    want = bool(a.get("locked", True))
    every = bool(a.get("all"))
    if not every and not a.get("refs"):
        raise WorkerError("lock/unlock needs refs=[...] or all=true", "BAD_ARGUMENT",
                          'e.g. {"op":"unlock","refs":["U1"]}')
    fps = list(b.GetFootprints()) if every else [fp_by_ref(b, r) for r in a["refs"]]
    changed, already = [], []
    for fp in fps:
        if every and fp.IsBoardOnly():
            continue
        if bool(fp.IsLocked()) == want:
            already.append(fp.GetReference())
        else:
            fp.SetLocked(want)
            changed.append(fp.GetReference())
    if changed:
        ctx["modified"] = True
    return {"locked": want, "changed": changed, "already": already}


@op("place")
def op_place(b, a, ctx):
    """Move footprints. Items with check=true (explicit moves) are refused when a pad would land on
    another net's pad or track (force=true overrides), and report tracks the move left dangling.
    A LOCKED footprint: an item that only sets lock is always applied; a move needs unlock=true (or
    lock=false; force=true moves it and keeps it locked) - otherwise an explicit move is refused (LOCKED)
    and an automatic one is reported as skipped."""
    done = []
    for it in a["items"]:
        fp = fp_by_ref(b, it["ref"])
        geom = any(it.get(k) is not None for k in ("x", "y", "rot", "side"))
        was_locked = bool(fp.IsLocked())
        if was_locked and (it.get("unlock") or (geom and it.get("lock") is False)):
            fp.SetLocked(False)
        elif was_locked and geom and not it.get("force"):
            if it.get("check"):
                raise WorkerError("%s is locked, so it was not moved - nothing was changed" % it["ref"], "LOCKED",
                                  'add "unlock": true to that move (plus "lock": true to lock it again afterwards), '
                                  "or unlock it first: pcb_edit(ops=[{\"op\":\"unlock\",\"refs\":[\"%s\"]}])" % it["ref"])
            done.append({"ref": it["ref"], "skipped": "locked",
                         "fix": 'pcb_place(auto=true, moves=[{"ref":"%s","unlock":true}])' % it["ref"]})
            continue
        before = {"x": tomm(fp.GetPosition().x), "y": tomm(fp.GetPosition().y), "rot": fp.GetOrientationDegrees(),
                  "side": "bottom" if fp.IsFlipped() else "top"}
        attached = _touching_tracks(b, fp) if it.get("check") else []
        side = it.get("side")
        if side in ("top", "bottom") and (side == "bottom") != fp.IsFlipped():
            fp.Flip(fp.GetPosition(), P.FLIP_DIRECTION_LEFT_RIGHT)
        if it.get("x") is not None and it.get("y") is not None:
            fp.SetPosition(V(it["x"], it["y"]))
        if it.get("rot") is not None:
            fp.SetOrientationDegrees(float(it["rot"]) % 360)
        if it.get("lock") is not None:
            fp.SetLocked(bool(it["lock"]))
        rec = {"ref": it["ref"], "before": before,
               "after": {"x": tomm(fp.GetPosition().x), "y": tomm(fp.GetPosition().y),
                         "rot": fp.GetOrientationDegrees(), "side": "bottom" if fp.IsFlipped() else "top"}}
        if bool(fp.IsLocked()) != was_locked:
            rec["locked"] = bool(fp.IsLocked())
        if it.get("check") and rec["after"] != before:
            hits = copper_collisions(b, list(fp.Pads()), limit=4, clearance=0)
            if hits and not it.get("force"):
                raise WorkerError("moving %s to (%s, %s) puts its pads on other nets' copper: %s — nothing was moved"
                                  % (it["ref"], rec["after"]["x"], rec["after"]["y"], "; ".join(hits)), "PAD_SHORT",
                                  "pick a free spot (pcb_read(what=['footprints']) shows bboxes) or "
                                  "pcb_place(auto=true, refs=['%s']); force=true moves it anyway" % it["ref"])
            pads = list(fp.Pads())
            dang = [uuid(t) for t in attached
                    if not any(pd.HitTest(q) for pd in pads
                               for q in ([t.GetPosition()] if t.GetClass() == "PCB_VIA" else [t.GetStart(), t.GetEnd()]))]
            if dang:
                rec["dangling_tracks"] = dang
        done.append(rec)
    ctx["modified"] = True
    ctx["needs_fill"] = True
    return {"placed": done}


def _bb(item):
    b = item.GetBoundingBox()
    return [tomm(b.GetLeft()), tomm(b.GetTop()), tomm(b.GetRight()), tomm(b.GetBottom())]


def _hit(a, b, g=0.0):
    return a[0] < b[2] + g and a[2] + g > b[0] and a[1] < b[3] + g and a[3] + g > b[1]


@op("silk_tidy")
def op_silk_tidy(b, a, ctx):
    """Put every visible reference designator of ``refs`` (all parts if empty) next to its own part,
    horizontal and readable, where it overlaps no pad, no other reference and no other part's body.
    Candidates: around the courtyard at growing gaps, slid along each side, and inside large bodies;
    a reference that fits nowhere is shrunk to the fab minimum (0.8 mm) and tried again, then reported."""
    refs = set(a.get("refs") or [])
    min_size = float(a.get("min_size", 0.8))
    ol = outline_info(b)
    lim = ol["bbox"] if ol else None
    fps = [fp for fp in b.GetFootprints()]
    courts = {fp.GetReference(): fp_abs_bbox(fp)[0] for fp in fps}
    pads = {}
    for fp in fps:
        for pad in fp.Pads():
            pb = _bb(pad)
            pads.setdefault(fp.GetReference(), []).append([pb[0] - 0.1, pb[1] - 0.1, pb[2] + 0.1, pb[3] + 0.1])
    all_pads = [q for v in pads.values() for q in v]
    silk = []   # footprint silkscreen artwork (KiCad's silk_overlap checks text against it, own part included)
    for fp in fps:
        for g in fp.GraphicalItems():
            try:
                if g.GetLayer() == P.F_SilkS and g.GetClass() not in ("PCB_TEXT", "PCB_FIELD"):
                    silk.append(_bb(g))
            except Exception:  # noqa: BLE001
                pass
    todo = [fp for fp in fps if fp.Reference().IsVisible() and not fp.IsFlipped()
            and (not refs or fp.GetReference() in refs)]
    todo_refs = {fp.GetReference() for fp in todo}
    texts = []
    for fp in fps:   # references we are not moving stay where they are, as obstacles
        t = fp.Reference()
        if t.IsVisible() and fp.GetReference() not in todo_refs:
            texts.append(_bb(t))
    moved, failed, shrunk = [], [], []

    def place(fp, t):
        tb = _bb(t)
        w, h = tb[2] - tb[0], tb[3] - tb[1]
        c = courts[fp.GetReference()]
        cx, cy = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2
        cw, ch = c[2] - c[0], c[3] - c[1]
        cands = []
        for k, g in enumerate((0.15, 0.45, 0.9, 1.5)):
            for f in (0.0, -0.33, 0.33):
                cands += [(cx + f * cw, c[1] - g - h / 2, k), (cx + f * cw, c[3] + g + h / 2, k),
                          (c[2] + g + w / 2, cy + f * ch, k), (c[0] - g - w / 2, cy + f * ch, k)]
            cands += [(c[2] + g + w / 2, c[1] - g - h / 2, k + 0.5), (c[0] - g - w / 2, c[1] - g - h / 2, k + 0.5),
                      (c[2] + g + w / 2, c[3] + g + h / 2, k + 0.5), (c[0] - g - w / 2, c[3] + g + h / 2, k + 0.5)]
        if cw > w + 0.6 and ch > h + 0.6:
            cands.append((cx, cy, 0.2))   # inside a big body, if no pad is there
        best = None
        own = fp.GetReference()
        for x, y, k in cands:
            r = [x - w / 2, y - h / 2, x + w / 2, y + h / 2]
            pen = 0.0
            if lim and (r[0] < lim[0] + 0.3 or r[1] < lim[1] + 0.3 or r[2] > lim[2] - 0.3 or r[3] > lim[3] - 0.3):
                pen += 1000
            pen += 200 * sum(1 for o in texts if _hit(r, o, 0.15))
            pen += 150 * sum(1 for o in silk if _hit(r, o, 0.1))
            pen += 100 * sum(1 for o in all_pads if _hit(r, o))
            pen += 30 * sum(1 for ref, o in courts.items() if ref != own and _hit(r, o))
            pen += 0.5 * k + 0.05 * math.hypot(x - cx, y - cy)
            if best is None or pen < best[0]:
                best = (pen, x, y)
        return best

    for fp in sorted(todo, key=lambda f: -len(f.Pads())):
        t = fp.Reference()
        t.SetTextAngleDegrees(0)
        if hasattr(t, "SetKeepUpright"):
            t.SetKeepUpright(True)
        best = place(fp, t)
        if best[0] >= 30 and t.GetTextHeight() > mm(min_size) + 1000:
            old = (t.GetTextSize(), t.GetTextThickness())
            t.SetTextSize(P.VECTOR2I(mm(min_size), mm(min_size)))
            t.SetTextThickness(mm(0.15))
            b2 = place(fp, t)
            if b2[0] < best[0]:
                best = b2
                shrunk.append(fp.GetReference())
            else:
                t.SetTextSize(old[0])
                t.SetTextThickness(old[1])
        pen, x, y = best
        t.SetPosition(V(round(x, 3), round(y, 3)))
        texts.append(_bb(t))
        (moved if pen < 30 else failed).append(fp.GetReference())
    ctx["modified"] = True
    return {"placed_clean": len(moved), "crowded": failed, "shrunk": shrunk, "min_size": min_size}


# ── tracks / vias / text / fields / delete ────────────────────────────────
def _add_track(b, p1, p2, width, layer, net):
    t = P.PCB_TRACK(b)
    t.SetStart(V(*p1))
    t.SetEnd(V(*p2))
    t.SetWidth(mm(width))
    t.SetLayer(layer)
    if net is not None:
        t.SetNet(net)
    b.Add(t)
    return t


def _add_via(b, xy, size, drill, net):
    v = P.PCB_VIA(b)
    v.SetPosition(V(*xy))
    v.SetViaType(P.VIATYPE_THROUGH)
    v.SetLayerPair(P.F_Cu, P.B_Cu)
    v.SetWidth(mm(size))
    v.SetDrill(mm(drill))
    if net is not None:
        v.SetNet(net)
    b.Add(v)
    return v


def _path(p1, p2, style):
    (x1, y1), (x2, y2) = p1, p2
    dx, dy = x2 - x1, y2 - y1
    if style == "direct" or abs(dx) < 1e-6 or abs(dy) < 1e-6 or abs(abs(dx) - abs(dy)) < 1e-6:
        return [p1, p2]
    if style == "L":
        return [p1, [x2, y1], p2]
    if style == "L2":
        return [p1, [x1, y2], p2]
    # 45°: straight segment first, then the diagonal into the target
    if abs(dx) > abs(dy):
        mid = [x1 + math.copysign(abs(dx) - abs(dy), dx), y1]
    else:
        mid = [x1, y1 + math.copysign(abs(dy) - abs(dx), dy)]
    return [p1, mid, p2]


def _pad_layer(b, pad, want=None):
    ls = pad.GetLayerSet()
    if want is not None and ls.Contains(want):
        return want
    if ls.Contains(P.F_Cu):
        return P.F_Cu
    if ls.Contains(P.B_Cu):
        return P.B_Cu
    return want if want is not None else P.F_Cu


def netclass_of(b, net):
    """The net's effective NETCLASS. KiCad 10's NETINFO_ITEM.GetNetClass() hands Python a bare
    shared_ptr (no methods), so try the accessors that return a usable proxy."""
    if net is None:
        return None
    for get in (lambda: net.GetNetClassSlow(),
                lambda: b.GetDesignSettings().m_NetSettings.GetEffectiveNetClass(net.GetNetname()),
                lambda: net.GetNetClass()):
        try:
            nc = get()
            nc.GetTrackWidth()
            return nc
        except Exception:  # noqa: BLE001
            continue
    return None


def _netclass_width(b, net, fallback):
    try:
        nc = netclass_of(b, net)
        w = nc.GetTrackWidth()
        if w > 0:
            return tomm(w)
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _netclass_via(b, net):
    try:
        nc = netclass_of(b, net)
        return tomm(nc.GetViaDiameter()), tomm(nc.GetViaDrill())
    except Exception:  # noqa: BLE001
        return 0.6, 0.3


def _item_name(it):
    if it.GetClass() == "PAD":
        fp = it.GetParentFootprint()
        return "pad %s.%s [%s]" % (fp.GetReference() if fp else "?", it.GetNumber(), it.GetNetname() or "no net")
    return "%s [%s] at %s" % (it.GetClass().replace("PCB_", "").lower(), it.GetNetname() or "no net",
                              vxy(it.GetPosition() if it.GetClass() == "PCB_VIA" else it.GetStart()))


def copper_collisions(b, items, limit=6, clearance=None):
    """Other-net copper (pads, tracks, vias) the new items come closer to than the clearance rule
    (clearance=0: only real overlaps)."""
    mine = {uuid(i) for i in items}
    parents = {uuid(i.GetParentFootprint()) for i in items if i.GetClass() == "PAD" and i.GetParentFootprint()}
    pads = [p for fp in b.GetFootprints() if uuid(fp) not in parents for p in fp.Pads()]
    others = [t for t in b.GetTracks() if uuid(t) not in mine]
    hits = []
    for it in items:
        nc = it.GetNetCode()
        lays = [l for l in it.GetLayerSet().Seq() if P.IsCopperLayer(l)]
        for lay in lays:
            shp = it.GetEffectiveShape(lay)
            own = it.GetOwnClearance(lay)
            for o in pads + others:
                if o.GetNetCode() == nc and nc > 0:
                    continue
                if not o.IsOnLayer(lay):
                    continue
                clr = max(0, (max(own, o.GetOwnClearance(lay)) if clearance is None else mm(clearance)) - 1000)
                try:
                    if o.GetEffectiveShape(lay).Collide(shp, clr):
                        hits.append(_item_name(o))
                except Exception:  # noqa: BLE001
                    continue
                if len(hits) >= limit:
                    return hits
    return hits


@op("route")
def op_route(b, a, ctx):
    """Explicit pad-to-pad connection: {from:'U1.3', to:'R1.1', width, layer, style, via_at}. The path is
    checked against other-net copper; when the requested style collides the other styles are tried, and if
    every one collides nothing is drawn (force=true draws it anyway)."""
    p1, p2 = pad_of(b, a["from"]), pad_of(b, a["to"])
    n1, n2 = p1.GetNetname(), p2.GetNetname()
    if n1 != n2 and not a.get("force"):
        raise WorkerError("pads %s (%s) and %s (%s) are on different nets — refusing to short them"
                          % (a["from"], n1 or "no net", a["to"], n2 or "no net"), "NET_MISMATCH",
                          "check the schematic; pass force=true only to deliberately connect different nets")
    net = p1.GetNet() if n1 else (p2.GetNet() if n2 else None)
    want = layer_id(b, a["layer"]) if a.get("layer") else None
    l1, l2 = _pad_layer(b, p1, want), _pad_layer(b, p2, want)
    width = float(a.get("width") or _netclass_width(b, net, 0.25))
    s, e = vxy(p1.GetPosition()), vxy(p2.GetPosition())
    first = a.get("style", "45")
    styles = [first] + ([x for x in ("45", "L", "L2", "direct") if x != first] if not a.get("force") else [])

    def draw(style):
        created = []
        if l1 == l2:
            pts = [s] + [list(p) for p in a.get("waypoints", [])] + [e]
            full = [pts[0]]
            for i in range(1, len(pts)):
                full += _path(full[-1], pts[i], style)[1:]
            for i in range(len(full) - 1):
                created.append(_add_track(b, full[i], full[i + 1], width, l1, net))
        else:
            vat = a.get("via_at") or [(s[0] + e[0]) / 2, (s[1] + e[1]) / 2]
            vs, vd = _netclass_via(b, net)
            v = _add_via(b, vat, float(a.get("via_size") or vs), float(a.get("via_drill") or vd), net)
            for seg_from, seg_to, lay in ((s, vat, l1), (vat, e, l2)):
                pts = _path(seg_from, list(vat) if seg_to is vat else seg_to, style)
                for i in range(len(pts) - 1):
                    created.append(_add_track(b, pts[i], pts[i + 1], width, lay, net))
            created.append(v)
        return created

    hits_first, created, used = None, None, None
    for st in styles:
        cand = draw(st)
        hits = [] if a.get("force") else copper_collisions(b, cand)
        if not hits:
            created, used = cand, st
            break
        if hits_first is None:
            hits_first = hits
        for c in cand:
            b.Delete(c)
    if created is None:
        raise WorkerError("%s → %s at %.2f mm would hit other-net copper on every path style (%s); nothing drawn"
                          % (a["from"], a["to"], width, "; ".join(hits_first[:4])), "ROUTE_BLOCKED",
                          "give the path waypoints=[[x,y],...] around the obstacle (or via_at=[x,y] to change layer), "
                          "a narrower width, or leave it to pcb_route() (autorouter); force=true draws it anyway")
    ctx["modified"] = True
    ctx["needs_fill"] = True
    return {"net": n1 or n2, "segments": sum(1 for c in created if c.GetClass() != "PCB_VIA"),
            "vias": sum(1 for c in created if c.GetClass() == "PCB_VIA"), "width": width,
            "style": used, "uuids": [uuid(c) for c in created]}


@op("add_track")
def op_add_track(b, a, ctx):
    pts = a["points"]
    if len(pts) < 2:
        raise WorkerError("add_track needs at least 2 points", "BAD_ARGUMENT")
    net = net_item(b, a.get("net")) if a.get("net") else None
    lay = layer_id(b, a.get("layer", "F.Cu"))
    width = float(a.get("width") or _netclass_width(b, net, 0.25))
    out = [uuid(_add_track(b, pts[i], pts[i + 1], width, lay, net)) for i in range(len(pts) - 1)]
    ctx["modified"] = True
    ctx["needs_fill"] = True
    return {"segments": len(out), "uuids": out}


@op("add_via")
def op_add_via(b, a, ctx):
    net = net_item(b, a.get("net")) if a.get("net") else None
    vs, vd = _netclass_via(b, net) if net else (0.6, 0.3)
    out = []
    for xy in (a.get("points") or [a["at"]]):
        out.append(uuid(_add_via(b, xy, float(a.get("size") or vs), float(a.get("drill") or vd), net)))
    ctx["modified"] = True
    ctx["needs_fill"] = True
    return {"vias": len(out), "uuids": out}


@op("add_text")
def op_add_text(b, a, ctx):
    t = P.PCB_TEXT(b)
    t.SetText(a["text"])
    t.SetPosition(V(*a["at"]))
    lay = layer_id(b, a.get("layer", "F.SilkS"))
    t.SetLayer(lay)
    sz = float(a.get("size", 1.0))
    t.SetTextSize(P.VECTOR2I(mm(sz), mm(sz)))
    t.SetTextThickness(mm(float(a.get("thickness", max(0.15, sz * 0.15)))))
    if a.get("rot"):
        t.SetTextAngleDegrees(float(a["rot"]))
    if lay in (P.B_SilkS, P.B_Cu, P.B_Fab, P.B_Mask):
        t.SetMirrored(True)
    b.Add(t)
    ctx["modified"] = True
    return {"uuid": uuid(t)}


@op("set_field")
def op_set_field(b, a, ctx):
    fp = fp_by_ref(b, a["ref"])
    name, val = a["field"], str(a["value"])
    if name == "Reference":
        fp.SetReference(val)
    elif name == "Value":
        fp.SetValue(val)
    else:
        f = _set_user_field(fp, name, val, a.get("visible", False))
        if a.get("visible") is not None:
            f.SetVisible(bool(a["visible"]))
    ctx["modified"] = True
    return {"ref": a["ref"], "field": name, "value": val}


@op("delete")
def op_delete(b, a, ctx):
    removed = {"footprints": [], "tracks": 0, "vias": 0, "zones": 0, "items": 0}
    for ref in a.get("refs") or []:
        fp = fp_by_ref(b, ref)
        b.Delete(fp)
        removed["footprints"].append(ref)
    uu = set(a.get("uuids") or [])
    nets = set(a.get("nets") or [])
    if uu or nets or a.get("all_tracks"):
        for t in list(b.GetTracks()):
            if uuid(t) in uu or t.GetNetname() in nets or a.get("all_tracks"):
                removed["vias" if t.GetClass() == "PCB_VIA" else "tracks"] += 1
                b.Delete(t)
    if uu or a.get("zone_nets") is not None:
        zn = set(a.get("zone_nets") or [])
        for z in list(b.Zones()):
            if uuid(z) in uu or (z.GetNetname() in zn) or ("*" in zn):
                b.Delete(z)
                removed["zones"] += 1
    if uu:
        for d in list(b.GetDrawings()):
            if uuid(d) in uu:
                b.Delete(d)
                removed["items"] += 1
        for fp in list(b.GetFootprints()):
            if uuid(fp) in uu:
                removed["footprints"].append(fp.GetReference())
                b.Delete(fp)
    ctx["modified"] = True
    ctx["needs_fill"] = True
    return removed


# ── zones ─────────────────────────────────────────────────────────────────
def _zone_outline_pts(b, a):
    if a.get("polygon"):
        return a["polygon"]
    ol = outline_info(b)
    if not ol:
        raise WorkerError("board has no outline to pour into", "NO_OUTLINE", "pcb_setup(width=..., height=...) first")
    if ol.get("polygon"):
        return ol["polygon"]
    x0, y0, x1, y1 = ol["bbox"]
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


@op("zone")
def op_zone(b, a, ctx):
    net = net_item(b, a["net"])
    layers = a.get("layers") or ["F.Cu", "B.Cu"]
    replaced = 0
    if a.get("replace", True):
        for z in list(b.Zones()):
            if not z.GetIsRuleArea() and z.GetNetname() == a["net"] and any(
                    b.GetLayerName(l) in layers for l in z.GetLayerSet().Seq()):
                b.Delete(z)
                replaced += 1
    pts = _zone_outline_pts(b, a)
    made = []
    conn = {"thermal": P.ZONE_CONNECTION_THERMAL, "solid": P.ZONE_CONNECTION_FULL,
            "none": P.ZONE_CONNECTION_NONE, "tht_thermal": P.ZONE_CONNECTION_THT_THERMAL}[a.get("pad_connection", "thermal")]
    for ln in layers:
        if not P.IsCopperLayer(layer_id(b, ln)):
            raise WorkerError("'%s' is not a copper layer" % ln, "BAD_LAYER", "use F.Cu, B.Cu or In1.Cu ...")
    for ln in layers:
        z = P.ZONE(b)
        z.SetLayer(layer_id(b, ln))
        z.SetNetCode(net.GetNetCode())
        o = z.Outline()
        o.NewOutline()
        for x, y in pts:
            o.Append(mm(x), mm(y))
        z.SetLocalClearance(mm(float(a.get("clearance", 0.3))))
        z.SetMinThickness(mm(float(a.get("min_width", 0.25))))
        z.SetThermalReliefGap(mm(float(a.get("thermal_gap", 0.5))))
        z.SetThermalReliefSpokeWidth(mm(float(a.get("thermal_spoke", 0.5))))
        z.SetPadConnection(conn)
        z.SetAssignedPriority(int(a.get("priority", 0)))
        z.SetIslandRemovalMode(P.ISLAND_REMOVAL_MODE_AREA if a.get("keep_islands") else P.ISLAND_REMOVAL_MODE_ALWAYS)
        if a.get("keep_islands"):
            z.SetMinIslandArea(int(mm(1) * mm(1) * 4))
        z.SetZoneName(a.get("name") or "%s_%s" % (a["net"].strip("/"), ln.replace(".", "")))
        b.Add(z)
        made.append({"uuid": uuid(z), "layer": ln, "net": a["net"]})
    ctx["modified"] = True
    return {"zones_added": made, "zones_replaced": replaced}


@op("keepout")
def op_keepout(b, a, ctx):
    """Rule-area keepouts (no tracks / vias / pour) on every copper layer. Zones whose name starts
    with `prefix` are replaced, so re-running after a move never leaves stale keepouts."""
    prefix = a.get("prefix", "copper-antenna-")
    removed = 0
    for z in list(b.Zones()):
        if z.GetIsRuleArea() and z.GetZoneName().startswith(prefix):
            b.Delete(z)
            removed += 1
    made = []
    for k in a.get("areas") or []:
        z = P.ZONE(b)
        z.SetIsRuleArea(True)
        z.SetDoNotAllowZoneFills(True)
        z.SetDoNotAllowTracks(True)
        z.SetDoNotAllowVias(True)
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(False)
        z.SetLayerSet(P.LSET.AllCuMask(b.GetCopperLayerCount()))
        o = z.Outline()
        o.NewOutline()
        for x, y in k["polygon"]:
            o.Append(mm(x), mm(y))
        z.SetZoneName(k["name"])
        b.Add(z)
        made.append(k["name"])
    if made or removed:
        ctx["modified"] = True
        ctx["needs_fill"] = True
    return {"keepouts": made, "removed": removed}


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _inside(poly, x, y):
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            c = not c
    return c


def _poly_edge_dist(poly, x, y):
    return min(_seg_dist(x, y, poly[i][0], poly[i][1], poly[(i + 1) % len(poly)][0], poly[(i + 1) % len(poly)][1])
               for i in range(len(poly)))


def fill_all(b):
    b.BuildConnectivity()
    f = P.ZONE_FILLER(b)
    zs = b.Zones()
    f.Fill(zs)
    b.BuildConnectivity()
    # rule areas (antenna keepouts …) are never "filled": count copper zones only
    return sum(1 for z in zs if z.IsFilled() and not z.GetIsRuleArea())


@op("stitch")
def op_stitch(b, a, ctx):
    """Grid of stitching vias for a net poured on both outer layers."""
    net = net_item(b, a["net"])
    pitch = float(a.get("pitch", 5.0))
    size, drill = float(a.get("size", 0.6)), float(a.get("drill", 0.3))
    # never below the board's own minimums (an older project with a 0.3 mm annular-ring rule got one DRC
    # error per stitching via)
    ds = b.GetDesignSettings()
    drill = max(drill, tomm(ds.m_MinThroughDrill))
    size = max(size, tomm(ds.m_ViasMinSize), round(drill + 2 * tomm(ds.m_ViasMinAnnularWidth), 4))
    clr = float(a.get("clearance", 0.3))
    edge = float(a.get("edge", 1.0))
    poly = _zone_outline_pts(b, {})
    # obstacles: pads, tracks, vias, holes of every footprint (courtyards of parts are avoided too)
    obst_pts, obst_segs, courts = [], [], []
    for fp in b.GetFootprints():
        bb, has = fp_abs_bbox(fp)
        courts.append(bb)
        for pad in fp.Pads():
            pb = pad.GetBoundingBox()
            obst_pts.append((tomm(pb.GetCenter().x), tomm(pb.GetCenter().y),
                             max(tomm(pb.GetWidth()), tomm(pb.GetHeight())) / 2, pad.GetNetCode()))
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            obst_pts.append((tomm(p.x), tomm(p.y), tomm(t.GetWidth(P.F_Cu)) / 2, t.GetNetCode()))
        else:
            s, e = t.GetStart(), t.GetEnd()
            obst_segs.append((tomm(s.x), tomm(s.y), tomm(e.x), tomm(e.y), tomm(t.GetWidth()) / 2, t.GetNetCode()))
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    added = []
    r = size / 2
    nc = net.GetNetCode()
    y = min(ys) + edge + r
    while y <= max(ys) - edge - r:
        x = min(xs) + edge + r
        while x <= max(xs) - edge - r:
            ok = _inside(poly, x, y) and _poly_edge_dist(poly, x, y) >= edge + r
            if ok and a.get("avoid_courtyards", True):
                ok = not any(c[0] - 0.2 <= x <= c[2] + 0.2 and c[1] - 0.2 <= y <= c[3] + 0.2 for c in courts)
            if ok:
                for (px, py, pr, pn) in obst_pts:
                    if math.hypot(px - x, py - y) < pr + r + clr + 0.05:
                        ok = False
                        break
            if ok:
                for (ax, ay, bx, by, hw, tn) in obst_segs:
                    if _seg_dist(x, y, ax, ay, bx, by) < hw + r + clr + 0.05:
                        ok = False
                        break
            if ok:
                v = _add_via(b, [round(x, 3), round(y, 3)], size, drill, net)
                added.append(v)
                obst_pts.append((x, y, r, nc))
            x += pitch
        y += pitch
    # keep only vias that actually land in filled copper of this net on both layers
    removed_isolated = 0
    if added and a.get("prune", True) and any(z.GetNetCode() == nc for z in b.Zones()):
        fill_all(b)
        zs = [z for z in b.Zones() if z.GetNetCode() == nc]
        for v in added[:]:
            pos = v.GetPosition()
            hit_f = any(z.GetLayerSet().Contains(P.F_Cu) and z.HitTestFilledArea(P.F_Cu, pos) for z in zs)
            hit_b = any(z.GetLayerSet().Contains(P.B_Cu) and z.HitTestFilledArea(P.B_Cu, pos) for z in zs)
            if not (hit_f and hit_b):
                added.remove(v)
                b.Delete(v)
                removed_isolated += 1
        ctx["needs_fill"] = True
    ctx["modified"] = True
    return {"vias_added": len(added), "pruned_not_in_copper": removed_isolated, "pitch": pitch}


def _pad_touched(b, pad, tracks):
    """Some same-net track end or via already sits on this pad."""
    nc = pad.GetNetCode()
    for t in tracks:
        if t.GetNetCode() != nc:
            continue
        pts = [t.GetPosition()] if t.GetClass() == "PCB_VIA" else [t.GetStart(), t.GetEnd()]
        if any(pad.HitTest(q) for q in pts):
            return True
    return False


def _seg_seg_dist(ax, ay, bx, by, cx, cy, dx, dy):
    def ccw(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1, d2 = ccw(cx, cy, dx, dy, ax, ay), ccw(cx, cy, dx, dy, bx, by)
    d3, d4 = ccw(ax, ay, bx, by, cx, cy), ccw(ax, ay, bx, by, dx, dy)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(_seg_dist(ax, ay, cx, cy, dx, dy), _seg_dist(bx, by, cx, cy, dx, dy),
               _seg_dist(cx, cy, ax, ay, bx, by), _seg_dist(dx, dy, ax, ay, bx, by))


def _escape_rays(b, skip_nets, length=2.5):
    """The way out of every signal pad of a multi-pin part: a segment from the pad's outer end straight
    away from the part. Fan-out vias avoid standing in them and escape stubs are drawn along them.
    Only pads on the rim of a part with >= 6 pads count (two-pin parts can leave in any direction).
    Each ray: (x0, y0, x1, y1, (ref, pad), layer, dx, dy, pad x, pad y, pitch mm, index along its side)."""
    rays = []
    multi = {}
    for fp in b.GetFootprints():
        for p in fp.Pads():
            if p.GetNetCode() > 0:
                multi[p.GetNetname()] = multi.get(p.GetNetname(), 0) + 1
    for fp in b.GetFootprints():
        pads = [p for p in fp.Pads() if p.GetAttribute() in (P.PAD_ATTRIB_SMD, P.PAD_ATTRIB_CONN)]
        if len(pads) < 6:
            continue
        xs = [tomm(p.GetPosition().x) for p in pads]
        ys = [tomm(p.GetPosition().y) for p in pads]
        mx, my = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        hw, hh = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2
        sides = {}
        for p in pads:
            x, y = tomm(p.GetPosition().x), tomm(p.GetPosition().y)
            bb = p.GetBoundingBox()
            pw, ph = tomm(bb.GetWidth()), tomm(bb.GetHeight())
            # which rim the pad sits on (nearest side of the pad field); inner pads (EP, BGA core) have no ray
            gaps = [(hw - (x - mx), 1, 0), (hw + (x - mx), -1, 0), (hh - (y - my), 0, 1), (hh + (y - my), 0, -1)]
            g, dx, dy = min(gaps)
            if g > 0.3:
                continue
            # a long pad leaves along its long axis (QFN / SOIC rows), whatever rim was nearest
            if pw > 1.5 * ph and dx == 0:
                dx, dy = (1 if x >= mx else -1), 0
            elif ph > 1.5 * pw and dy == 0:
                dx, dy = 0, (1 if y >= my else -1)
            sides.setdefault((dx, dy), []).append((y if dx else x, p, x, y, pw, ph))
        for (dx, dy), row in sides.items():
            row.sort(key=lambda r: r[0])
            steps = [q[0] - r[0] for r, q in zip(row, row[1:]) if q[0] - r[0] > 0.05]
            pitch = min(steps) if steps else 99.0
            for r in row:
                _c, p, x, y, pw, ph = r
                n = p.GetNetname()
                if p.GetNetCode() <= 0 or n in skip_nets or multi.get(n, 0) < 2:
                    continue
                idx = int(round((r[0] - row[0][0]) / pitch)) if pitch < 99 else 0
                sx, sy = x + dx * pw / 2, y + dy * ph / 2
                lay = P.B_Cu if p.IsOnLayer(P.B_Cu) and not p.IsOnLayer(P.F_Cu) else P.F_Cu
                rays.append((sx, sy, sx + dx * length, sy + dy * length, (fp.GetReference(), p.GetNumber()), lay,
                             dx, dy, x, y, pitch, idx))
    return rays


@op("fanout")
def op_fanout(b, a, ctx):
    """Plane nets on multilayer boards: give every SMD pad of a net that has an inner plane a short
    stub + via into the plane (Freerouting does not drop vias into planes by itself), and stitch large
    exposed pads (QFN/DFN thermal pads) with a grid of vias. Pads that already have copper attached are
    left alone, so re-running adds nothing."""
    nets = set(a.get("nets") or [])
    if a.get("planes"):
        nets |= set(plane_layers(b).values())
    nets -= set(a.get("skip") or [])
    if not nets:
        return {"fanout_vias": 0, "thermal_vias": 0, "pads": [], "failed": []}
    ds = b.GetDesignSettings()
    vdrill = max(float(a.get("via_drill", 0.3)), tomm(ds.m_MinThroughDrill))
    vsize = max(float(a.get("via_size", 0.6)), tomm(ds.m_ViasMinSize), round(vdrill + 2 * tomm(ds.m_ViasMinAnnularWidth), 4))
    tmin = max(tomm(ds.m_TrackMinWidth), 0.127)
    edge = tomm(ds.m_CopperEdgeClearance) or 0.3
    ol = outline_info(b)
    poly = (ol or {}).get("polygon") or ([[ol["bbox"][0], ol["bbox"][1]], [ol["bbox"][2], ol["bbox"][1]],
                                         [ol["bbox"][2], ol["bbox"][3]], [ol["bbox"][0], ol["bbox"][3]]] if ol else None)
    tracks = list(b.GetTracks())
    h2h = max(tomm(ds.m_HoleToHoleMin), 0.25)
    holes = [(tomm(t.GetPosition().x), tomm(t.GetPosition().y), tomm(t.GetDrillValue()) / 2)
             for t in tracks if t.GetClass() == "PCB_VIA"]
    for fp in b.GetFootprints():
        for pad in fp.Pads():
            if pad.HasHole():
                ds_ = pad.GetDrillSize()
                holes.append((tomm(pad.GetPosition().x), tomm(pad.GetPosition().y), tomm(max(ds_.x, ds_.y)) / 2))

    def hole_ok(x, y):
        return all(math.hypot(x - hx, y - hy) - hr - vdrill / 2 >= h2h for hx, hy, hr in holes)
    done, failed, thermal, stubs = [], [], 0, 0
    rays = _escape_rays(b, nets)
    reach = vsize / 2 + 0.2 + 0.1          # via radius + clearance + half a signal track
    for fp in b.GetFootprints():
        fc = fp.GetPosition()
        for pad in fp.Pads():
            if pad.GetNetname() not in nets or pad.GetAttribute() not in (P.PAD_ATTRIB_SMD, P.PAD_ATTRIB_CONN):
                continue
            if _pad_touched(b, pad, tracks):
                continue
            net = pad.GetNet()
            lay = P.B_Cu if pad.IsOnLayer(P.B_Cu) and not pad.IsOnLayer(P.F_Cu) else P.F_Cu
            bb = pad.GetBoundingBox()
            pw, ph = tomm(bb.GetWidth()), tomm(bb.GetHeight())
            cx, cy = tomm(pad.GetPosition().x), tomm(pad.GetPosition().y)
            if min(pw, ph) >= 2.0 and a.get("thermal", True):
                # exposed / thermal pad: via grid inside the pad (JLC: 0.3 mm drill vias in an EP are fine)
                nx = max(1, min(3, int((pw - 0.6) // 1.1) + 1))
                ny = max(1, min(3, int((ph - 0.6) // 1.1) + 1))
                made = []
                for i in range(nx):
                    for j in range(ny):
                        x = cx + (i - (nx - 1) / 2) * 1.1
                        y = cy + (j - (ny - 1) / 2) * 1.1
                        if not hole_ok(x, y):
                            continue
                        v = _add_via(b, [round(x, 3), round(y, 3)], vsize, vdrill, net)
                        if copper_collisions(b, [v], limit=1):
                            b.Delete(v)
                        else:
                            made.append(v)
                            holes.append((x, y, vdrill / 2))
                if made:
                    thermal += len(made)
                    tracks += made
                    done.append("%s.%s" % (fp.GetReference(), pad.GetNumber()))
                    continue
            w = max(tmin, min(float(a.get("width", 0.25)), min(pw, ph)))
            ox, oy = cx - tomm(fc.x), cy - tomm(fc.y)
            dirs = [(math.cos(math.radians(k * 45)), math.sin(math.radians(k * 45))) for k in range(8)]
            dirs.sort(key=lambda d: -(d[0] * ox + d[1] * oy))   # outward from the part first
            # candidates: 8 directions x a few distances. A via costs the signal pads whose way out it
            # would stand in (their escape rays) — a decoupling cap next to a QFN must not drop its via
            # into the gap the IC's signals leave through; among equals: outward from the part, short stub
            cands = []
            for di, (dx, dy) in enumerate(dirs):
                ext = abs(dx) * pw / 2 + abs(dy) * ph / 2
                for ei, extra in enumerate((0.0, 0.3, 0.6, 1.0, 1.5)):
                    dist = ext + vsize / 2 + 0.25 + extra
                    vx, vy = round(cx + dx * dist, 3), round(cy + dy * dist, 3)
                    if poly and (not _inside(poly, vx, vy) or _poly_edge_dist(poly, vx, vy) < edge + vsize / 2):
                        continue
                    if not hole_ok(vx, vy):
                        continue
                    own = (fp.GetReference(), pad.GetNumber())
                    block = sum(1 for r in rays if r[4] != own and r[5] == lay and
                                (_seg_dist(vx, vy, r[0], r[1], r[2], r[3]) < reach or
                                 _seg_seg_dist(cx, cy, vx, vy, r[0], r[1], r[2], r[3]) < w / 2 + 0.2 + 0.1))
                    cands.append((block, ei if block else 0, di, ei, vx, vy))
            placed = False
            for _blk, _e, _di, _ei, vx, vy in sorted(cands):
                t = _add_track(b, [cx, cy], [vx, vy], w, lay, net)
                v = _add_via(b, [vx, vy], vsize, vdrill, net)
                if copper_collisions(b, [t, v], limit=1):
                    b.Delete(t)
                    b.Delete(v)
                    continue
                tracks += [t, v]
                holes.append((vx, vy, vdrill / 2))
                stubs += 1
                done.append("%s.%s" % (fp.GetReference(), pad.GetNumber()))
                placed = True
                break
            if not placed:
                failed.append("%s.%s" % (fp.GetReference(), pad.GetNumber()))
    if done:
        ctx["modified"] = True
        ctx["needs_fill"] = True
    return {"fanout_vias": stubs, "thermal_vias": thermal, "pads": done[:40], "failed": failed[:40]}


@op("pour_repair")
def op_pour_repair(b, a, ctx):
    """After a fill, make every filled fragment of the net that holds one of its pads actually join:

    * starved thermals — a pad inside the pour that no spoke reached (slotted / small-annulus pads) gets a
      solid connection to the zone;
    * islands — a fragment with a pad but no via (e.g. an IC ground pad boxed in by signal tracks) gets
      a via at a point inside it that also lands in the net's copper on another layer.
    Fragments where no via fits are reported with their position."""
    net = net_item(b, a["net"])
    nc = net.GetNetCode()
    zones = [z for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetCode() == nc]
    if not zones:
        return {"solid_pads": [], "island_vias": [], "unfixed_islands": []}
    fill_all(b)
    solid = []
    pads = [p for fp in b.GetFootprints() for p in fp.Pads() if p.GetNetCode() == nc]
    for pad in pads:
        if pad.GetLocalZoneConnection() not in (P.ZONE_CONNECTION_INHERITED,) or not a.get("fix_thermals", True):
            continue
        inside, touched = False, False
        for z in zones:
            for lay in z.GetLayerSet().Seq():
                if not pad.IsOnLayer(lay) or not z.Outline().Contains(pad.GetPosition()):
                    continue
                inside = True
                try:
                    if z.GetFilledPolysList(lay).Collide(pad.GetEffectiveShape(lay), 0):
                        touched = True
                except Exception:  # noqa: BLE001
                    touched = True
        if inside and not touched:
            pad.SetLocalZoneConnection(P.ZONE_CONNECTION_FULL)
            solid.append("%s.%s" % (pad.GetParentFootprint().GetReference(), pad.GetNumber()))
    if solid:
        fill_all(b)
    ds = b.GetDesignSettings()
    vdrill = max(float(a.get("via_drill", 0.3)), tomm(ds.m_MinThroughDrill))
    vsize = max(float(a.get("via_size", 0.6)), tomm(ds.m_ViasMinSize), round(vdrill + 2 * tomm(ds.m_ViasMinAnnularWidth), 4))
    r = vsize / 2
    h2h = max(tomm(ds.m_HoleToHoleMin), 0.25)
    vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]
    holes = [(tomm(v.GetPosition().x), tomm(v.GetPosition().y), tomm(v.GetDrillValue()) / 2) for v in vias]
    for fp in b.GetFootprints():
        for p in fp.Pads():
            if p.HasHole():
                d = p.GetDrillSize()
                holes.append((tomm(p.GetPosition().x), tomm(p.GetPosition().y), tomm(max(d.x, d.y)) / 2))
    joiners = [v.GetPosition() for v in vias if v.GetNetCode() == nc] + \
              [p.GetPosition() for p in pads if p.HasHole()]
    added, unfixed = [], []
    by_layer = {}
    for z in zones:
        for lay in z.GetLayerSet().Seq():
            by_layer.setdefault(lay, []).append(z)
    for lay, zs in by_layer.items():
        others = [(l2, z2) for l2, z2s in by_layer.items() if l2 != lay for z2 in z2s]
        if not others:
            continue
        for z in zs:
            fl = z.GetFilledPolysList(lay)
            for i in range(fl.OutlineCount()):
                unit = fl.UnitSet(i)
                if any(unit.Contains(q) for q in joiners):
                    continue
                mine = [p for p in pads if p.IsOnLayer(lay) and unit.Collide(p.GetEffectiveShape(lay), 0)]
                if not mine or any(p.HasHole() for p in mine):
                    continue   # no pad (island removal deals with it) / a plated hole already joins the layers
                bb = unit.BBox()
                step = mm(0.25)
                cands = []
                x = bb.GetLeft() + step // 2
                while x < bb.GetRight():
                    y = bb.GetTop() + step // 2
                    while y < bb.GetBottom():
                        cands.append(P.VECTOR2I(int(x), int(y)))
                        y += step
                    x += step
                tx = sum(p.GetPosition().x for p in mine) / len(mine)
                ty = sum(p.GetPosition().y for p in mine) / len(mine)
                cands.sort(key=lambda q: (q.x - tx) ** 2 + (q.y - ty) ** 2)
                ok = None
                for q in cands[:4000]:
                    if not unit.Contains(q):
                        continue
                    ring = [P.VECTOR2I(int(q.x + mm(r + 0.08) * math.cos(k * math.pi / 4)),
                                       int(q.y + mm(r + 0.08) * math.sin(k * math.pi / 4))) for k in range(8)]
                    if not all(unit.Contains(c) for c in ring):
                        continue
                    if not any(z2.HitTestFilledArea(l2, q, mm(r)) for l2, z2 in others):
                        continue
                    qx, qy = tomm(q.x), tomm(q.y)
                    if any(math.hypot(qx - hx, qy - hy) - hr - vdrill / 2 < h2h for hx, hy, hr in holes):
                        continue
                    v = _add_via(b, [qx, qy], vsize, vdrill, net)
                    if copper_collisions(b, [v], limit=1):
                        b.Delete(v)
                        continue
                    ok = v
                    holes.append((qx, qy, vdrill / 2))
                    joiners.append(q)
                    break
                names = ["%s.%s" % (p.GetParentFootprint().GetReference(), p.GetNumber()) for p in mine[:3]]
                if ok is not None:
                    added.append({"at": vxy(ok.GetPosition()), "layer": b.GetLayerName(lay), "pads": names})
                else:
                    c = bb.Centre()
                    unfixed.append({"at": [tomm(c.x), tomm(c.y)], "layer": b.GetLayerName(lay), "pads": names})
    if added or solid:
        ctx["modified"] = True
        ctx["needs_fill"] = True
    return {"solid_pads": solid, "island_vias": added, "unfixed_islands": unfixed}


@op("fill")
def op_fill(b, a, ctx):
    n = fill_all(b)
    ctx["modified"] = True
    ctx["needs_fill"] = False
    return {"zones_filled": n, "zones": sum(1 for z in b.Zones() if not z.GetIsRuleArea())}


@op("unfill")
def op_unfill(b, a, ctx):
    for z in b.Zones():
        z.UnFill() if hasattr(z, "UnFill") else z.SetIsFilled(False)
    ctx["modified"] = True
    return {"zones": len(b.Zones())}


@op("heal")
def op_heal(b, a, ctx):
    """Join dangling track ends that stop just short of a same-net pad (a Freerouting/SES rounding
    artefact KiCad reports as 'missing connection'): add a short stub to the pad centre."""
    reach = mm(float(a.get("reach", 0.6)))
    tracks = [t for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]
    pads_by_net = {}
    for fp in b.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() > 0:
                pads_by_net.setdefault(pad.GetNetCode(), []).append(pad)
    ends = {}
    for t in tracks:
        for pt in (t.GetStart(), t.GetEnd()):
            ends.setdefault((t.GetNetCode(), t.GetLayer(), pt.x, pt.y), []).append(t)
    healed = []
    for (nc, lay, x, y), owners in list(ends.items()):
        if nc <= 0 or len(owners) > 1:
            continue
        pt = P.VECTOR2I(x, y)
        # connected to anything else of the same net at this point?
        if any(p.GetLayerSet().Contains(lay) and p.HitTest(pt) for p in pads_by_net.get(nc, [])):
            continue
        if any(v.GetNetCode() == nc and v.HitTest(pt) for v in vias):
            continue
        if any(o is not owners[0] and o.GetNetCode() == nc and o.GetLayer() == lay and o.HitTest(pt)
               for o in tracks if o.GetNetCode() == nc):
            continue
        best = None
        for p in pads_by_net.get(nc, []):
            if not p.GetLayerSet().Contains(lay):
                continue
            bb = p.GetBoundingBox()
            dx = max(bb.GetLeft() - x, 0, x - bb.GetRight())
            dy = max(bb.GetTop() - y, 0, y - bb.GetBottom())
            d = math.hypot(dx, dy)
            if d <= reach and (best is None or d < best[0]):
                best = (d, p)
        if best is None:
            continue
        p = best[1]
        if p.GetPosition() == pt:
            continue
        t = P.PCB_TRACK(b)
        t.SetStart(pt)
        t.SetEnd(p.GetPosition())
        t.SetWidth(owners[0].GetWidth())
        t.SetLayer(lay)
        t.SetNetCode(nc)
        b.Add(t)
        healed.append("%s.%s" % (p.GetParentFootprint().GetReference(), p.GetNumber()))
    if healed:
        ctx["modified"] = True
    return {"healed": healed}


# ── Specctra (Freerouting) ────────────────────────────────────────────────
def _poly_area(ps):
    try:
        return float(ps.Area())
    except Exception:  # noqa: BLE001
        bb = ps.BBox()
        return float(bb.GetWidth()) * float(bb.GetHeight())


def plane_layers(b):
    """Inner copper layers that carry a pour over most of the board: {layer name: net}."""
    ol = outline_info(b)
    if not ol:
        return {}
    board_area = max(1.0, ol["width"] * ol["height"]) * 1e12
    out = {}
    inner = set(P.LSET.InternalCuMask().Seq()) if hasattr(P.LSET, "InternalCuMask") else set()
    for z in b.Zones():
        try:
            if z.GetIsRuleArea() or z.GetNetCode() <= 0 or z.IsTeardropArea():
                continue
        except Exception:  # noqa: BLE001
            continue
        for lay in z.GetLayerSet().Seq():
            if not P.IsCopperLayer(lay) or lay in (P.F_Cu, P.B_Cu) or (inner and lay not in inner):
                continue
            if _poly_area(z.Outline()) >= 0.5 * board_area:
                out[b.GetLayerName(lay)] = z.GetNetname()
    return out


def _clearances(b):
    """Clearance of every net class in use, mm (KiCad DRC uses the larger of two items' classes)."""
    out = {}
    try:
        for name, ni in b.GetNetsByName().items():
            if ni.GetNetCode() <= 0:
                continue
            nc = netclass_of(b, ni)
            if nc is not None:
                out[nc.GetName()] = tomm(nc.GetClearance())
    except Exception:  # noqa: BLE001
        pass
    return out


def neck_widths(b, clearance):
    """Nets whose class width cannot leave one of their pads without violating clearance to the
    neighbouring pad (fine-pitch QFN power pins): {net: widest routable track mm}. A track entering a
    pad of width w along its axis keeps (w - t)/2 + gap to the neighbour, so t <= w + 2(gap - clr),
    less a little slack so the router can actually use it."""
    out = {}
    ds = b.GetDesignSettings()
    tmin = tomm(ds.m_TrackMinWidth) or 0.127
    for fp in b.GetFootprints():
        pads = [p for p in fp.Pads() if p.GetAttribute() != P.PAD_ATTRIB_NPTH]
        if len(pads) < 3:
            continue
        boxes = [(p, p.GetBoundingBox()) for p in pads]
        for p, bb in boxes:
            if p.GetNetCode() <= 0:
                continue
            nc = netclass_of(b, p.GetNet())
            if nc is None:
                continue
            want = tomm(nc.GetTrackWidth())
            w = min(tomm(bb.GetWidth()), tomm(bb.GetHeight()))
            gap = None
            for q, qb in boxes:
                if q is p or q.GetNetCode() == p.GetNetCode():
                    continue
                dx = max(qb.GetLeft() - bb.GetRight(), bb.GetLeft() - qb.GetRight(), 0)
                dy = max(qb.GetTop() - bb.GetBottom(), bb.GetTop() - qb.GetBottom(), 0)
                d = tomm(math.hypot(dx, dy))
                if gap is None or d < gap:
                    gap = d
            if gap is None:
                continue
            # 25 um of slack per side: a width that fits with exactly zero margin is legal on paper, but the
            # router cannot leave the pad with it (the RP2040's +1V1 pins stayed unrouted at 0.2 mm)
            tmax = math.floor((w + 2 * (gap - clearance - 0.025)) * 100 + 1e-6) / 100
            if tmax < want - 1e-6:
                n = p.GetNetname()
                out[n] = max(tmin, min(out.get(n, want), tmax))
    return out


@op("export_dsn")
def op_export_dsn(b, a, ctx):
    path = a["path"]
    # (near) zero-length segments (healing stubs, SES artefacts) become repeated path points in the DSN,
    # which crash Freerouting 2.2 (NullPointerException in the autorouter) and stop it after pass 1
    zero = 0
    for t in list(b.GetTracks()):
        if t.GetClass() == "PCB_TRACK" and (t.GetEnd() - t.GetStart()).EuclideanNorm() < 5000:   # < 5 µm
            b.Delete(t)
            zero += 1
    if zero:
        ctx["modified"] = True
    # existing tracks/vias go out as (type fix): Freerouting routes around them and its session holds
    # only the NEW wiring, so an import never duplicates (or re-sizes) what is already on the board
    relock = []
    if a.get("protect", True):
        for t in b.GetTracks():
            if not t.IsLocked():
                t.SetLocked(True)
                relock.append(t)
    try:
        ok = P.ExportSpecctraDSN(b, path)
    finally:
        for t in relock:
            t.SetLocked(False)
    if not ok or not os.path.exists(path):
        raise WorkerError("Specctra DSN export failed (does the board have a closed Edge.Cuts outline?)",
                          "DSN_EXPORT_FAILED", "pcb_setup(width=..., height=...) to draw a closed outline, then retry")
    # board-level copper text/graphics are not exported as obstacles by KiCad's DSN writer;
    # return them so the caller can add keepouts (otherwise the router runs tracks through them)
    keep = []
    for d in b.GetDrawings():
        try:
            if d.IsOnCopperLayer() and d.GetClass() in ("PCB_TEXT", "PCB_TEXTBOX", "PCB_SHAPE") and \
                    (d.GetClass() != "PCB_SHAPE" or d.GetNetCode() <= 0):
                for lay in d.GetLayerSet().Seq():
                    if P.IsCopperLayer(lay):
                        keep.append({"layer": b.GetLayerName(lay), "bbox": _bb(d), "kind": d.GetClass()})
        except Exception:  # noqa: BLE001
            pass
    cl = _clearances(b)
    clr = max(cl.values()) if cl else tomm(b.GetDesignSettings().m_MinClearance)
    padnets = {}
    for fp in b.GetFootprints():
        for pd in fp.Pads():
            if pd.GetNetCode() > 0:
                padnets[pd.GetNetname()] = padnets.get(pd.GetNetname(), 0) + 1
    return {"dsn": path, "bytes": os.path.getsize(path), "copper_graphics": keep, "protected": len(relock),
            "nets": sorted(n for n, k in padnets.items() if k >= 2),
            "clearances": cl, "max_clearance": clr, "planes": plane_layers(b),
            "edge_clearance": tomm(b.GetDesignSettings().m_CopperEdgeClearance),
            "copper_layers": [b.GetLayerName(l) for l in P.LSET.AllCuMask(b.GetCopperLayerCount()).Seq()],
            "neck": neck_widths(b, clr)}


def _track_key(t):
    s, e = t.GetStart(), t.GetEnd()
    return (tuple(sorted([(s.x, s.y), (e.x, e.y)])), t.GetLayer(), t.GetNetCode(), t.GetClass())


@op("import_ses")
def op_import_ses(b, a, ctx):
    path = a["path"]
    before = track_stats(b)
    old_vias = {}
    old_tracks = set()
    stale = 0          # exact duplicates that were on the board before this import
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            k = (p.x, p.y, t.GetNetCode())
            stale += k in old_vias
            old_vias[k] = (t.GetWidth(P.F_Cu), t.GetDrillValue())
        else:
            k = _track_key(t)
            stale += k in old_tracks
            old_tracks.add(k)
    # KiCad's session import deletes every UNLOCKED track/via before adding the session's wiring; the
    # session only carries new wiring (existing copper was exported as fixed), so lock what is there
    relock = [t for t in b.GetTracks() if not t.IsLocked()]
    for t in relock:
        t.SetLocked(True)
    try:
        ok = P.ImportSpecctraSES(b, path)
    finally:
        keep = {uuid(t) for t in b.GetTracks()}
        for t in relock:
            if uuid(t) in keep:
                t.SetLocked(False)
    if not ok:
        raise WorkerError("Specctra SES import failed", "SES_IMPORT_FAILED")
    # safety net: drop wiring the session re-delivered on top of identical existing copper, and keep
    # the original size/drill of vias that came back through the round trip
    seen, vseen, dups, resized = set(), set(), 0, 0
    for t in list(b.GetTracks()):
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            k = (p.x, p.y, t.GetNetCode())
            if k in vseen:
                b.Delete(t)
                dups += 1
                continue
            vseen.add(k)
            if k in old_vias and (t.GetWidth(P.F_Cu), t.GetDrillValue()) != old_vias[k]:
                t.SetWidth(old_vias[k][0])
                t.SetDrill(old_vias[k][1])
                resized += 1
        else:
            k = _track_key(t)
            if k in seen:
                b.Delete(t)
                dups += 1
                continue
            seen.add(k)
    # KiCad has the last word: Freerouting sometimes runs a new trace through copper that was on the board
    # (it drops or re-plans "fixed" wiring of unfinished nets internally, while the board keeps it). Wiring
    # of this session that comes closer to another net than the clearance rule is taken out again, whole
    # net at a time, so the board never gains a short or a crossing; those connections stay unrouted.
    clashes = {}
    if a.get("check", True):
        new = {}
        for t in b.GetTracks():
            is_new = ((t.GetPosition().x, t.GetPosition().y, t.GetNetCode()) not in old_vias
                      if t.GetClass() == "PCB_VIA" else _track_key(t) not in old_tracks)
            if is_new:
                new.setdefault(t.GetNetname(), []).append(t)
        if new:
            pads = [pd for fp in b.GetFootprints() for pd in fp.Pads()]
            for net in sorted(new):
                items = new[net]
                others = pads + [o for o in b.GetTracks() if o.GetNetCode() != items[0].GetNetCode()]
                if any(_clashes(it, others) for it in items):
                    for it in items:
                        b.Delete(it)
                    clashes[net] = len(items)
    ctx["modified"] = True
    after = track_stats(b)
    return {"before": before, "after": after, "duplicates_removed": dups, "stale_duplicates": min(stale, dups),
            "vias_restored": resized, "clashes_removed": clashes}


def _clashes(it, others, tol=2000):
    """True when ``it`` comes closer to other-net copper than the larger of the two clearances (2 um slack)."""
    nc = it.GetNetCode()
    for lay in it.GetLayerSet().Seq():
        if not P.IsCopperLayer(lay):
            continue
        shp = it.GetEffectiveShape(lay)
        own = it.GetOwnClearance(lay)
        for o in others:
            if (o.GetNetCode() == nc and nc > 0) or not o.IsOnLayer(lay):
                continue
            try:
                if o.GetEffectiveShape(lay).Collide(shp, max(0, max(own, o.GetOwnClearance(lay)) - tol)):
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False
# ── main ──────────────────────────────────────────────────────────────────
def handle(req):
    path = req["pcb"]
    ctx = {"modified": False, "needs_fill": False}
    if os.path.exists(path):
        b = P.LoadBoard(path)
        if b is None:
            raise WorkerError("pcbnew could not load %s" % path, "LOAD_FAILED")
    elif req.get("create"):
        b = P.CreateEmptyBoard()
        ds = b.GetDesignSettings()
        ctx["modified"] = True
        ctx["created"] = True
    else:
        raise WorkerError("board file not found: %s" % path, "NOT_FOUND", "pcb_sync() creates the board from the schematic")
    results = []
    for o in req.get("ops", []):
        fn = OPS.get(o.get("op"))
        if fn is None:
            raise WorkerError("unknown worker op '%s'" % o.get("op"), "BAD_OP")
        results.append(fn(b, o, ctx))
    if ctx.get("needs_fill") and req.get("refill", True) and len(b.Zones()):
        # zones are refilled after anything that moves copper, so the saved board, its DRC and verify agree
        fill_all(b)
    after = None
    if req.get("summary", True):
        # computed before saving: on some boards SWIG container proxies misbehave after SaveBoard
        try:
            after = dict(track_stats(b), unconnected=unconnected_count(b), footprints=len(b.GetFootprints()))
            if req.get("by_net"):
                after["unconnected_by_net"] = unconnected_by_net(b)
        except Exception as e:  # noqa: BLE001
            after = {"error": "stats unavailable: %s" % e}
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
    out = {"results": results, "saved": saved, "modified": ctx["modified"], "created": bool(ctx.get("created"))}
    if after is not None:
        out["after"] = after
    return out


def main():
    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
        res = handle(req)
        out = {"ok": True, "result": res}
    except WorkerError as e:
        out = {"ok": False, "error": e.msg, "code": e.code, "fix": e.fix}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "code": "WORKER_ERROR",
               "trace": traceback.format_exc().splitlines()[-8:]}
    sys.stdout.write("\n" + MARK + json.dumps(out, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        import copper_ext  # noqa: F401 — registers the outline / elegance / silk / polish ops (workers/copper_ext.py)
    except ImportError:
        pass
    main()
