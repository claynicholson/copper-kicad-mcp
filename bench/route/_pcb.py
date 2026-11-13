"""pcbnew side of the routing benchmark. Runs inside KiCad's Python as a SUBPROCESS of run.py / prepare.py
(a subprocess so a pcbnew crash or a modal wx dialog can only cost a timeout, never the harness).

    python _pcb.py <request.json> <result.json>

request = {"op": "survey" | "prepare" | "measure" | "synth", ...}. Independent of copper_kicad on purpose:
the benchmark must measure any router the same way, so nothing here imports the code under test.
All lengths are millimetres.
"""
import json
import math
import os
import sys

import pcbnew as P  # noqa: N812

try:  # never let wxLog pop a modal dialog (missing font etc.) in a headless run
    import wx
    wx.Log.EnableLogging(False)
    try:
        wx.DisableAsserts()
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass


def tomm(v):
    return round(v / 1e6, 4)


def mm(v):
    return int(round(float(v) * 1e6))


def uid(item):
    return item.m_Uuid.AsString()


def cu_layers(b):
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
    return len(zs)


def is_teardrop(z):
    try:
        return bool(z.IsTeardropArea())
    except Exception:  # noqa: BLE001
        return (z.GetZoneName() or "").startswith("$teardrop")


def pads_by_net(b):
    out = {}
    for fp in b.GetFootprints():
        for p in fp.Pads():
            if p.GetNetCode() > 0:
                out.setdefault(p.GetNetname(), []).append((fp, p))
    return out


def unconnected_by_net(b):
    """{net: missing connections} = (clusters of connected pads) - 1, by KiCad's connectivity."""
    b.BuildConnectivity()
    c = b.GetConnectivity()
    out = {}
    for n, ps in pads_by_net(b).items():
        if len(ps) < 2:
            continue
        seen, clusters = set(), 0
        for _fp, p in ps:
            k = uid(p)
            if k in seen:
                continue
            clusters += 1
            seen.add(k)
            for q in c.GetConnectedItems(p):
                seen.add(uid(q))
        if clusters > 1:
            out[n] = clusters - 1
    return out


def mst_len(pts):
    """Euclidean minimum spanning tree length: the classic lower-bound-ish wirelength estimate of a net."""
    n = len(pts)
    if n < 2:
        return 0.0
    used = [False] * n
    dist = [1e18] * n
    dist[0] = 0.0
    total = 0.0
    for _ in range(n):
        k = -1
        for i in range(n):
            if not used[i] and (k < 0 or dist[i] < dist[k]):
                k = i
        used[k] = True
        total += dist[k]
        x, y = pts[k]
        for i in range(n):
            if not used[i]:
                d = math.hypot(pts[i][0] - x, pts[i][1] - y)
                if d < dist[i]:
                    dist[i] = d
    return total


def copper(b, detail=True):
    """Every track / arc / via with its net, plus per-net and total figures."""
    names = dict(cu_layers(b))
    segs, vias = [], []
    nets = {}
    for t in b.GetTracks():
        n = t.GetNetname()
        e = nets.setdefault(n, {"length": 0.0, "vias": 0, "segments": 0, "widths": {}, "layers": {}})
        cls = t.GetClass()
        if cls == "PCB_VIA":
            e["vias"] += 1
            pos = t.GetPosition()
            try:
                size = t.GetWidth(P.F_Cu)
            except Exception:  # noqa: BLE001
                size = t.GetWidth()
            kind = {P.VIATYPE_THROUGH: "through"}.get(t.GetViaType(), "blind/micro")
            vias.append([n, tomm(pos.x), tomm(pos.y), tomm(size), tomm(t.GetDrill()), kind])
            continue
        ln = t.GetLength() / 1e6
        w = tomm(t.GetWidth())
        lay = names.get(t.GetLayer(), str(t.GetLayer()))
        e["length"] += ln
        e["segments"] += 1
        e["widths"][str(w)] = e["widths"].get(str(w), 0.0) + ln
        e["layers"][lay] = e["layers"].get(lay, 0.0) + ln
        s, en = t.GetStart(), t.GetEnd()
        segs.append([n, lay, tomm(s.x), tomm(s.y), tomm(en.x), tomm(en.y), w, 1 if cls == "PCB_ARC" else 0])
    for e in nets.values():
        e["length"] = round(e["length"], 3)
        e["widths"] = {k: round(v, 3) for k, v in e["widths"].items()}
        e["layers"] = {k: round(v, 3) for k, v in e["layers"].items()}
    out = {"tracks": len(segs), "vias": len(vias), "arcs": sum(1 for s in segs if s[7]),
           "blind_or_micro_vias": sum(1 for v in vias if v[5] != "through"),
           "length_mm": round(sum(e["length"] for e in nets.values()), 2), "nets": nets}
    if detail:
        out["segments"] = segs
        out["via_list"] = vias
    return out


def board_facts(b):
    layers = cu_layers(b)
    pbn = pads_by_net(b)
    n_pads = sum(len(list(fp.Pads())) for fp in b.GetFootprints())
    smd = tht = 0
    min_pitch = None
    for fp in b.GetFootprints():
        pts = []
        for p in fp.Pads():
            if p.GetAttribute() == P.PAD_ATTRIB_SMD:
                smd += 1
            elif p.GetAttribute() == P.PAD_ATTRIB_PTH:
                tht += 1
            pos = p.GetPosition()
            pts.append((pos.x / 1e6, pos.y / 1e6))
        pts.sort()
        for i in range(len(pts) - 1):
            # nearest neighbour among the next few pads in x order is enough for a pitch estimate
            for j in range(i + 1, min(i + 6, len(pts))):
                d = math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1])
                if d > 0.05 and (min_pitch is None or d < min_pitch):
                    min_pitch = d
    bb = b.GetBoardEdgesBoundingBox()
    zones = [z for z in b.Zones() if not is_teardrop(z) and not z.GetIsRuleArea()]
    mst = {n: round(mst_len([(p.GetPosition().x / 1e6, p.GetPosition().y / 1e6) for _fp, p in ps]), 3)
           for n, ps in pbn.items() if len(ps) >= 2}
    ds = b.GetDesignSettings()
    return {"layers": [n for _l, n in layers], "footprints": len(b.GetFootprints()), "pads": n_pads,
            "smd_pads": smd, "tht_pads": tht, "min_pad_pitch_mm": round(min_pitch, 3) if min_pitch else None,
            "nets": sum(1 for ps in pbn.values() if len(ps) >= 2),
            "size_mm": [tomm(bb.GetWidth()), tomm(bb.GetHeight())],
            "origin_mm": [tomm(bb.GetLeft()), tomm(bb.GetTop())],
            "zones": len(zones), "zone_nets": sorted({z.GetNetname() for z in zones if z.GetNetname()}),
            "rule_areas": sum(1 for z in b.Zones() if z.GetIsRuleArea()),
            "min_clearance": tomm(ds.m_MinClearance), "min_track": tomm(ds.m_TrackMinWidth),
            "mst_mm": mst, "mst_total_mm": round(sum(mst.values()), 1)}


def pad_boxes(b):
    out = []
    names = dict(cu_layers(b))
    for fp in b.GetFootprints():
        for p in fp.Pads():
            bb = p.GetBoundingBox()
            ls = [n for l, n in names.items() if p.IsOnLayer(l)]
            side = "both" if len(ls) > 1 else (ls[0] if ls else "")
            out.append([tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom()), side,
                        p.GetNetname()])
    return out


def outline(b):
    out = []
    for d in b.GetDrawings():
        if d.GetLayer() != P.Edge_Cuts:
            continue
        try:
            sh = d.GetShape()
            if sh == P.SHAPE_T_SEGMENT:
                out.append([tomm(d.GetStart().x), tomm(d.GetStart().y), tomm(d.GetEnd().x), tomm(d.GetEnd().y)])
            elif sh == P.SHAPE_T_RECT:
                x0, y0, x1, y1 = d.GetStart().x, d.GetStart().y, d.GetEnd().x, d.GetEnd().y
                for a in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)):
                    out.append([tomm(v) for v in a])
            else:
                bb = d.GetBoundingBox()
                out.append(["box", tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())])
        except Exception:  # noqa: BLE001
            pass
    return out


# ── ops ───────────────────────────────────────────────────────────────────
def op_survey(a):
    b = P.LoadBoard(a["pcb"])
    cu = copper(b, detail=False)
    del cu["nets"]
    return {"facts": {k: v for k, v in board_facts(b).items() if k != "mst_mm"}, "copper": cu,
            "unconnected": sum(unconnected_by_net(b).values())}


def op_prepare(a):
    """Human reference metrics of ``src``; then tracks + vias (and their teardrops) removed, zones
    refilled, saved as ``dst``."""
    b = P.LoadBoard(a["src"])
    fill_all(b)
    ref = copper(b, detail=True)
    ref["unconnected_by_net"] = unconnected_by_net(b)
    ref["unconnected"] = sum(ref["unconnected_by_net"].values())
    facts = board_facts(b)
    removed = locked = 0
    for t in list(b.GetTracks()):
        if t.IsLocked():
            locked += 1
        b.Delete(t)
        removed += 1
    tears = 0
    for z in list(b.Zones()):
        if is_teardrop(z):
            b.Delete(z)
            tears += 1
    fill_all(b)
    P.SaveBoard(a["dst"], b)
    b2 = P.LoadBoard(a["dst"])
    unc = unconnected_by_net(b2)
    return {"facts": facts, "reference": ref, "removed": removed, "removed_locked": locked,
            "teardrops_removed": tears, "unrouted": {"connections": sum(unc.values()), "by_net": unc}}


def op_measure(a):
    b = P.LoadBoard(a["pcb"])
    if a.get("fill", True):
        fill_all(b)
    cu = copper(b, detail=True)
    unc = unconnected_by_net(b)
    out = {"copper": cu, "unconnected_by_net": unc, "unconnected": sum(unc.values())}
    if a.get("drawing"):
        out["pads"] = pad_boxes(b)
        out["outline"] = outline(b)
        bb = b.GetBoardEdgesBoundingBox()
        out["bbox"] = [tomm(bb.GetLeft()), tomm(bb.GetTop()), tomm(bb.GetRight()), tomm(bb.GetBottom())]
    return out


def op_synth(a):
    """Build a synthetic board from a plain description (no footprint libraries needed):
    {"dst", "size": [w, h], "layers": 2, "origin": [x, y],
     "parts": [{"ref", "x", "y", "pads": [{"n": "1", "x", "y", "w", "h", "net", "tht": drill | 0, "shape"}]}]}"""
    b = P.BOARD()
    b.SetCopperLayerCount(int(a.get("layers", 2)))
    ox, oy = a.get("origin", [100.0, 100.0])
    w, h = a["size"]
    nets = {}

    def net(name):
        if not name:
            return None
        if name not in nets:
            ni = P.NETINFO_ITEM(b, name)
            b.Add(ni)
            nets[name] = ni
        return nets[name]
    for x1, y1, x2, y2 in ((0, 0, w, 0), (w, 0, w, h), (w, h, 0, h), (0, h, 0, 0)):
        s = P.PCB_SHAPE(b)
        s.SetShape(P.SHAPE_T_SEGMENT)
        s.SetStart(P.VECTOR2I(mm(ox + x1), mm(oy + y1)))
        s.SetEnd(P.VECTOR2I(mm(ox + x2), mm(oy + y2)))
        s.SetLayer(P.Edge_Cuts)
        s.SetWidth(mm(0.1))
        b.Add(s)
    for part in a["parts"]:
        fp = P.FOOTPRINT(b)
        fp.SetReference(part["ref"])
        fp.SetValue(part.get("value", part["ref"]))
        try:        # a footprint without a library id cannot make the Specctra DSN -> SES round trip
            fp.SetFPID(P.LIB_ID("bench", "FP_" + part["ref"]))
        except Exception:  # noqa: BLE001
            pass
        fp.SetPosition(P.VECTOR2I(mm(ox + part["x"]), mm(oy + part["y"])))
        b.Add(fp)
        for pd in part["pads"]:
            p = P.PAD(fp)
            p.SetNumber(str(pd["n"]))
            p.SetPosition(P.VECTOR2I(mm(ox + part["x"] + pd["x"]), mm(oy + part["y"] + pd["y"])))
            p.SetSize(P.F_Cu, P.VECTOR2I(mm(pd["w"]), mm(pd["h"])))
            if pd.get("tht"):
                p.SetAttribute(P.PAD_ATTRIB_PTH)
                p.SetShape(P.F_Cu, P.PAD_SHAPE_CIRCLE if pd.get("shape", "circle") == "circle" else P.PAD_SHAPE_RECT)
                p.SetDrillSize(P.VECTOR2I(mm(pd["tht"]), mm(pd["tht"])))
                p.SetLayerSet(P.PAD.PTHMask())
            else:
                p.SetAttribute(P.PAD_ATTRIB_SMD)
                p.SetShape(P.F_Cu, P.PAD_SHAPE_RECT)
                p.SetLayerSet(P.PAD.SMDMask())
            ni = net(pd.get("net"))
            if ni is not None:
                p.SetNet(ni)
            fp.Add(p)
    for zd in a.get("zones") or []:
        z = P.ZONE(b)
        z.SetLayer(b.GetLayerID(zd["layer"]))
        z.SetNet(net(zd["net"]))
        x0, y0, x1, y1 = zd["rect"]
        ol = z.Outline()
        ol.NewOutline()
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            ol.Append(mm(ox + x), mm(oy + y))
        b.Add(z)
    fill_all(b)
    P.SaveBoard(a["dst"], b)
    b2 = P.LoadBoard(a["dst"])
    unc = unconnected_by_net(b2)
    return {"facts": board_facts(b2), "unrouted": {"connections": sum(unc.values()), "by_net": unc}}


OPS = {"survey": op_survey, "prepare": op_prepare, "measure": op_measure, "synth": op_synth}


def main():
    req = json.load(open(sys.argv[1], encoding="utf-8"))
    try:
        res = {"ok": True, "result": OPS[req["op"]](req)}
    except Exception as e:  # noqa: BLE001
        import traceback
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-2000:]}
    tmp = sys.argv[2] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f)
    os.replace(tmp, sys.argv[2])


if __name__ == "__main__":
    main()
