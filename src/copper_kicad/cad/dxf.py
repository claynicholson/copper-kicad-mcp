"""ASCII DXF -> outline prims (pure stdlib).

Entities: LINE, CIRCLE, ARC, LWPOLYLINE and POLYLINE/VERTEX (with bulge arcs), SPLINE, ELLIPSE, and
INSERT of a block one level deep. ``$INSUNITS`` scales to millimetres. y is flipped to KiCad axes.
"""
from __future__ import annotations

import math
import re

from . import ir
from .step import bspline_points

INSUNITS = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 25.4e-6, 9: 0.0254, 13: 1e-3}
OUTLINE_LAYER = re.compile(r"outline|edge|board|profile|contour|cuts|dimension|pcb|border|mech", re.I)
UNIT_SCALE = {"mm": 1.0, "inch": 25.4, "in": 25.4, "cm": 10.0, "m": 1000.0}


class DxfError(ValueError):
    pass


def _pairs(path):
    with open(path, "rb") as f:
        head = f.read(22)
        if head.startswith(b"AutoCAD Binary DXF"):
            raise DxfError("binary DXF is not supported: save as ASCII DXF (R12 or 2000+)")
        data = head + f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    out = []
    for i in range(0, len(lines) - 1, 2):
        try:
            out.append((int(lines[i].strip()), lines[i + 1].strip()))
        except ValueError:
            continue
    return out


def _bulge_poly(pts, bulge, closed):
    out = []
    n = len(pts)
    for i in range(n if closed else n - 1):
        a, b = pts[i], pts[(i + 1) % n]
        bg = bulge[i] if i < len(bulge) else 0.0
        ch = math.dist(a, b)
        if ch < 1e-12:
            continue
        if abs(bg) < 1e-9:
            out.append(("L", a, b))
        else:
            # bulge = tan(sweep/4); the arc's mid point is the chord mid point moved by sagitta = bulge*chord/2
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            nx, ny = (b[1] - a[1]) / ch, -(b[0] - a[0]) / ch      # right-hand normal (bulge > 0 = CCW)
            s = bg * ch / 2
            out.append(("A", a, (mx + nx * s, my + ny * s), b))
    return out


def _entity_prims(typ, kv, units, state):
    """One entity -> (layer, prims) or (layer, None) when the type is not geometry we read."""
    g, multi = {}, {}
    for c, v in kv:
        multi.setdefault(c, []).append(v)
        g.setdefault(c, v)
    lay = g.get(8, "0")

    def fl(c, d=0.0):
        try:
            return float(g.get(c, d)) * (units if c in (10, 20, 11, 21, 40) else 1.0)
        except ValueError:
            return d
    out = []
    if typ == "LINE":
        out.append(("L", (fl(10), fl(20)), (fl(11), fl(21))))
    elif typ == "CIRCLE":
        out.append(("C", (fl(10), fl(20)), fl(40)))
    elif typ == "ARC":
        a0, a1 = math.radians(float(g.get(50, 0))), math.radians(float(g.get(51, 0)))
        cx, cy = fl(10), fl(20)
        if float(g.get(230, 1)) < 0:      # mirrored OCS
            cx, a0, a1 = -cx, math.pi - a1, math.pi - a0
        out.append(ir.arc_from((cx, cy), fl(40), a0, a1, True))
    elif typ == "LWPOLYLINE":
        pts, bulge = [], []
        for c, v in kv:
            if c == 10:
                pts.append([float(v) * units, 0.0])
                bulge.append(0.0)
            elif c == 20 and pts:
                pts[-1][1] = float(v) * units
            elif c == 42 and pts:
                bulge[-1] = float(v)
        out += _bulge_poly([tuple(p) for p in pts], bulge, int(float(g.get(70, 0))) & 1)
    elif typ == "POLYLINE":
        state["poly"] = {"closed": int(float(g.get(70, 0))) & 1, "pts": [], "bulge": [], "layer": lay}
        return lay, "pending"
    elif typ == "VERTEX" and state.get("poly") is not None:
        state["poly"]["pts"].append((fl(10), fl(20)))
        state["poly"]["bulge"].append(float(g.get(42, 0)))
        return lay, "pending"
    elif typ == "SEQEND" and state.get("poly") is not None:
        po = state.pop("poly")
        out += _bulge_poly(po["pts"], po["bulge"], po["closed"])
        lay = po["layer"]
        typ = "POLYLINE"
    elif typ == "SPLINE":
        deg = int(float(g.get(71, 3)))
        kn = [float(v) for v in multi.get(40, [])]
        cps = [(float(x) * units, float(y) * units, 0.0) for x, y in zip(multi.get(10, []), multi.get(20, []))]
        w = [float(v) for v in multi.get(41, [])] or None
        if w and len(w) != len(cps):
            w = None
        if len(cps) > deg:
            pts = bspline_points(deg, cps, kn if len(kn) == len(cps) + deg + 1 else None, w, n=max(24, 12 * len(cps)))
            out += [("L", pts[k][:2], pts[k + 1][:2]) for k in range(len(pts) - 1)]
            if int(float(g.get(70, 0))) & 1 and math.dist(pts[0][:2], pts[-1][:2]) > 1e-9:
                out.append(("L", pts[-1][:2], pts[0][:2]))
        else:
            fp = [(float(x) * units, float(y) * units) for x, y in zip(multi.get(11, []), multi.get(21, []))]
            out += [("L", fp[k], fp[k + 1]) for k in range(len(fp) - 1)]
        state["approx"] = True
    elif typ == "ELLIPSE":
        cx, cy = fl(10), fl(20)
        mx, my = fl(11), fl(21)
        ratio = float(g.get(40, 1))
        t0, t1 = float(g.get(41, 0)), float(g.get(42, 2 * math.pi))
        if t1 <= t0:
            t1 += 2 * math.pi
        n = 96
        pts = [(cx + mx * math.cos(t) - my * ratio * math.sin(t), cy + my * math.cos(t) + mx * ratio * math.sin(t))
               for t in (t0 + (t1 - t0) * k / n for k in range(n + 1))]
        out += [("L", pts[k], pts[k + 1]) for k in range(n)]
        state["approx"] = True
    else:
        return lay, None
    return lay, out


def read(path, layer=None, units=None):
    """-> {'prims', 'layers': {layer: {TYPE: n}}, 'used_layers', 'units_code', 'scale', 'approx'}"""
    pairs = _pairs(path)
    unit_code = None
    for i, (c, v) in enumerate(pairs[:4000]):
        if c == 9 and v == "$INSUNITS" and i + 1 < len(pairs):
            try:
                unit_code = int(float(pairs[i + 1][1]))
            except ValueError:
                pass
            break
    scale = UNIT_SCALE[units] if units in UNIT_SCALE else INSUNITS.get(unit_code or 0, 1.0)
    sections = {"ENTITIES": [], "BLOCKS": []}
    sec, cur = None, None
    i = 0
    while i < len(pairs):
        c, v = pairs[i]
        if c == 0 and v == "SECTION":
            sec = pairs[i + 1][1] if i + 1 < len(pairs) else None
            cur = None
            i += 2
            continue
        if c == 0 and v == "ENDSEC":
            sec, cur = None, None
        elif sec in sections:
            if c == 0:
                cur = (v, [])
                sections[sec].append(cur)
            elif cur:
                cur[1].append((c, v))
        i += 1
    if not sections["ENTITIES"] and not any(c == 0 and v == "SECTION" for c, v in pairs[:50]):
        # R12-style minimal files may hold bare entities without sections
        cur = None
        for c, v in pairs:
            if c == 0:
                cur = (v, [])
                sections["ENTITIES"].append(cur)
            elif cur:
                cur[1].append((c, v))
    state = {}
    # block definitions (for INSERT, one level deep)
    blocks, name, base = {}, None, (0.0, 0.0)
    for typ, kv in sections["BLOCKS"]:
        g = dict((c, v) for c, v in reversed(kv))
        if typ == "BLOCK":
            name = g.get(2)
            base = (float(g.get(10, 0)) * scale, float(g.get(20, 0)) * scale)
            blocks[name] = {"base": base, "prims": []}
        elif typ == "ENDBLK":
            name = None
        elif name is not None:
            lay, pr = _entity_prims(typ, kv, scale, state)
            if pr and pr != "pending":
                blocks[name]["prims"] += [(lay, p) for p in pr]
    by_layer, prims = {}, []
    for typ, kv in sections["ENTITIES"]:
        if typ == "INSERT":
            g = dict((c, v) for c, v in reversed(kv))
            blk = blocks.get(g.get(2))
            lay = g.get(8, "0")
            if blk is None:
                by_layer.setdefault(lay, {}).setdefault("_skipped_INSERT", 0)
                by_layer[lay]["_skipped_INSERT"] += 1
                continue
            ix, iy = float(g.get(10, 0)) * scale, float(g.get(20, 0)) * scale
            sx, sy = float(g.get(41, 1)), float(g.get(42, g.get(41, 1)))
            a = math.radians(float(g.get(50, 0)))
            ca, sa = math.cos(a), math.sin(a)
            bx, by = blk["base"]

            def tf(p, ix=ix, iy=iy, sx=sx, sy=sy, ca=ca, sa=sa, bx=bx, by=by):
                x, y = (p[0] - bx) * sx, (p[1] - by) * sy
                return (ix + x * ca - y * sa, iy + x * sa + y * ca)
            for blay, p in blk["prims"]:
                use = lay if blay == "0" else blay
                if abs(abs(sx) - abs(sy)) > 1e-9 and p[0] != "L":
                    q = ir.prim_pts(p)        # non-uniform scale: arcs become polylines (re-fitted later)
                    if p[0] == "C":
                        q = q + [q[0]]
                    prims += [(use, ("L", tf(q[k]), tf(q[k + 1]))) for k in range(len(q) - 1)]
                else:
                    prims.append((use, ir.transform([p], tf)[0]))
            by_layer.setdefault(lay, {}).setdefault("INSERT", 0)
            by_layer[lay]["INSERT"] += 1
            continue
        lay, pr = _entity_prims(typ, kv, scale, state)
        if pr == "pending":
            continue
        if pr is None:
            if typ not in ("SEQEND", "VERTEX"):
                by_layer.setdefault(lay, {}).setdefault("_skipped_" + typ, 0)
                by_layer[lay]["_skipped_" + typ] += 1
            continue
        if typ == "SEQEND":
            typ = "POLYLINE"
        by_layer.setdefault(lay, {}).setdefault(typ, 0)
        by_layer[lay][typ] += 1
        prims += [(lay, p) for p in pr]
    layers = sorted({l for l, _ in prims})
    if layer is None:
        named = [l for l in layers if OUTLINE_LAYER.search(l)]
        use = named or layers
    else:
        use = [l for l in layers if l == layer] or [l for l in layers if l.lower() == str(layer).lower()]
        if not use:
            raise DxfError("layer '%s' has no outline geometry; layers in the file: %s" % (layer, ", ".join(layers) or "none"))
    fy = lambda p: (p[0], -p[1])      # DXF y is up, KiCad y is down
    sel = [ir.transform([p], fy)[0] for l, p in prims if l in use]
    return {"prims": sel, "layers": by_layer, "used_layers": use, "units_code": unit_code, "scale": scale,
            "units_assumed": units not in UNIT_SCALE and not unit_code, "approx": bool(state.get("approx"))}
