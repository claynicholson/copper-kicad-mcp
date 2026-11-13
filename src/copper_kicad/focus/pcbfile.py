"""Read what the focus scene needs straight from a ``.kicad_pcb`` — no pcbnew.

Used first for the board scene because it is quick, has no dependency on KiCad's Python and cannot hang
on a board pcbnew is slow to open; `scene._pcb` falls back to the pcbnew read worker if this parser does
not understand a file. tests/test_focus.py checks it against the worker (pads to 1 µm, courtyards to 10 µm).

    read(pcb) -> {'copper_layers', 'outline': {'bbox'} | None,
                  'footprints': [{'ref','value','footprint','x','y','rot','side','bbox','courtyard',
                                  'pad_list': [{'num','net','x','y','hw','hh','a'?}]}],
                  # hw/hh: pad half extents along board X/Y — or, when 'a' (degrees, not a multiple of 90) is
                  # present, along the pad's own axes: local = (dx·cos a − dy·sin a, dx·sin a + dy·cos a)
                  'track_list': [{'type': 'track'|'via', 'net', 'start','end','width','layer' | 'at','size'}]}

KiCad stores footprint children in the footprint's own frame; a point (x, y) of a footprint at (X, Y)
rotated by θ sits at  X + x·cosθ + y·sinθ ,  Y − x·sinθ + y·cosθ  (Y down, θ counter-clockwise).
Bottom-side footprints are already stored mirrored, so the same formula holds.
"""
from __future__ import annotations

import math
from pathlib import Path

from .. import sexpr


def _num(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _xy(node, key):
    n = sexpr.find(node, key)
    return (_num(n[1]), _num(n[2])) if n is not None and len(n) > 2 else None


def _layer(node) -> str:
    v = sexpr.value(node, "layer", "")
    return str(v or "")


def _circle_centre(a, b, c):
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-12:
        return None
    ux = ((a[0] ** 2 + a[1] ** 2) * (b[1] - c[1]) + (b[0] ** 2 + b[1] ** 2) * (c[1] - a[1]) + (c[0] ** 2 + c[1] ** 2) * (a[1] - b[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (c[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (a[0] - c[0]) + (c[0] ** 2 + c[1] ** 2) * (b[0] - a[0])) / d
    return ux, uy


def _arc_points(s, m, e, n: int = 16):
    """Points along the arc start→mid→end (enough for a bounding box)."""
    c = _circle_centre(s, m, e)
    if c is None:
        return [s, m, e]
    r = math.hypot(s[0] - c[0], s[1] - c[1])
    a0, a1, a2 = (math.atan2(p[1] - c[1], p[0] - c[0]) for p in (s, m, e))
    two = 2 * math.pi
    d_m, d_e = (a1 - a0) % two, (a2 - a0) % two
    sweep = d_e if d_m <= d_e else d_e - two          # go the way that passes through mid
    return [(c[0] + r * math.cos(a0 + sweep * i / n), c[1] + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def _shape_points(g) -> list:
    """Outline points (local coords) of an fp_*/gr_* graphic."""
    h = str(g[0]).split("_", 1)[-1]
    if h == "line":
        return [p for p in (_xy(g, "start"), _xy(g, "end")) if p]
    if h == "rect":
        a, b = _xy(g, "start"), _xy(g, "end")
        return [a, (a[0], b[1]), b, (b[0], a[1])] if a and b else []
    if h == "circle":
        c, e = _xy(g, "center"), _xy(g, "end")
        if not (c and e):
            return []
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return [(c[0] - r, c[1]), (c[0] + r, c[1]), (c[0], c[1] - r), (c[0], c[1] + r)]
    if h == "arc":
        s, m, e = _xy(g, "start"), _xy(g, "mid"), _xy(g, "end")
        return _arc_points(s, m, e) if s and m and e else [p for p in (s, e) if p]
    if h in ("poly", "curve"):
        pts = []
        for node in sexpr.find_all(sexpr.find(g, "pts") or [], "xy"):
            pts.append((_num(node[1]), _num(node[2])))
        for node in sexpr.find_all(sexpr.find(g, "pts") or [], "arc"):
            s, m, e = _xy(node, "start"), _xy(node, "mid"), _xy(node, "end")
            if s and m and e:
                pts += _arc_points(s, m, e)
        return pts
    return []


def _bbox(pts):
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


_GFX = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_curve")


def _net_name(node, table: dict) -> str:
    n = sexpr.find(node, "net")
    if n is None:
        return ""
    for v in reversed(n[1:]):
        if isinstance(v, str) and not isinstance(v, sexpr.Sym):
            return v
    return table.get(int(_num(n[1], -1)), "") if len(n) > 1 else ""


def read(pcb: Path) -> dict:
    tree = sexpr.loads(Path(pcb).read_text(encoding="utf-8"))
    if not tree or tree[0] != "kicad_pcb":
        raise ValueError("not a .kicad_pcb file")
    if int(_num(sexpr.value(tree, "version", 0))) < 20211014:
        raise ValueError("board file older than KiCad 6")
    nets = {}
    for n in sexpr.find_all(tree, "net"):
        if len(n) > 2:
            nets[int(_num(n[1], -1))] = str(n[2])
    layers = sexpr.find(tree, "layers") or []
    copper = sum(1 for ly in layers[1:] if isinstance(ly, list) and len(ly) > 1 and str(ly[1]).endswith(".Cu"))
    out = {"copper_layers": copper or 2, "footprints": [], "track_list": [], "outline": None}

    for fp in sexpr.find_all(tree, "footprint"):
        at = sexpr.find(fp, "at")
        if at is None:
            continue
        X, Y = _num(at[1]), _num(at[2])
        rot = _num(at[3]) if len(at) > 3 else 0.0
        a = math.radians(rot)
        ca, sa = math.cos(a), math.sin(a)

        def world(p, X=X, Y=Y, ca=ca, sa=sa):
            return (X + p[0] * ca + p[1] * sa, Y - p[0] * sa + p[1] * ca)

        side = "bottom" if _layer(fp).startswith("B.") else "top"
        props = {}
        for p in sexpr.find_all(fp, "property"):
            if len(p) > 2 and isinstance(p[1], str):
                props.setdefault(str(p[1]), str(p[2]))
        if "Reference" not in props:                      # KiCad 6/7: (fp_text reference "R1" …)
            for t in sexpr.find_all(fp, "fp_text"):
                if len(t) > 2 and str(t[1]) in ("reference", "value"):
                    props.setdefault(str(t[1]).capitalize(), str(t[2]))
        cy_layer = "B.CrtYd" if side == "bottom" else "F.CrtYd"
        cy, body = [], []
        for g in fp:
            if not isinstance(g, list) or not g or g[0] not in _GFX:
                continue
            pts = [world(p) for p in _shape_points(g)]
            (cy if _layer(g) == cy_layer else body).extend(pts)
        pads, pad_ext = [], []
        for pad in sexpr.find_all(fp, "pad"):
            pa = sexpr.find(pad, "at")
            if pa is None:
                continue
            px, py = world((_num(pa[1]), _num(pa[2])))
            size = sexpr.find(pad, "size")
            w, h = (_num(size[1]), _num(size[2])) if size is not None and len(size) > 2 else (1.0, 1.0)
            if len(pad) > 3 and str(pad[3]) == "custom":             # the copper is the primitives, not the anchor
                ext = [q for g in (sexpr.find(pad, "primitives") or []) if isinstance(g, list) and g
                       and str(g[0]).startswith("gr_") for q in _shape_points(g)]
                if ext:
                    w = max(w, 2 * max(abs(q[0]) for q in ext))
                    h = max(h, 2 * max(abs(q[1]) for q in ext))
            full = _num(pa[3]) if len(pa) > 3 else 0.0                # stored pad angle is absolute (board frame)
            ang = full % 180
            d = {"num": str(pad[1]) if len(pad) > 1 else "", "net": _net_name(pad, nets),
                 "x": round(px, 4), "y": round(py, 4)}
            if abs(ang - 90) < 0.5:
                w, h = h, w
                ex, ey = w / 2, h / 2
            elif min(ang, 180 - ang) > 0.5:                           # e.g. 45°: keep the pad's own frame + angle
                d["a"] = round(ang, 3)
                ra = math.radians(ang)
                ex = abs(w / 2 * math.cos(ra)) + abs(h / 2 * math.sin(ra))
                ey = abs(w / 2 * math.sin(ra)) + abs(h / 2 * math.cos(ra))
            else:
                ex, ey = w / 2, h / 2
            pad_ext += [(px - ex, py - ey), (px + ex, py + ey)]
            d["hw"], d["hh"] = round(w / 2, 4), round(h / 2, 4)
            pads.append(d)
        bb = _bbox(cy)
        has_cy = bb is not None and bb[2] > bb[0]
        if not has_cy:
            bb = _bbox(body + pad_ext) or [X - 0.5, Y - 0.5, X + 0.5, Y + 0.5]
        out["footprints"].append({
            "ref": props.get("Reference", ""), "value": props.get("Value", ""),
            "footprint": str(fp[1]) if len(fp) > 1 and not isinstance(fp[1], list) else "",
            "x": round(X, 4), "y": round(Y, 4), "rot": rot, "side": side,
            "bbox": [round(v, 4) for v in bb], "courtyard": has_cy, "pad_list": pads})

    for kind in ("segment", "arc"):
        for t in sexpr.find_all(tree, kind):
            s, e = _xy(t, "start"), _xy(t, "end")
            if not (s and e):
                continue
            out["track_list"].append({"type": "track", "net": _net_name(t, nets), "start": list(s), "end": list(e),
                                      "width": _num(sexpr.value(t, "width", 0.2), 0.2), "layer": _layer(t)})
    for v in sexpr.find_all(tree, "via"):
        p = _xy(v, "at")
        if p:
            out["track_list"].append({"type": "via", "net": _net_name(v, nets), "at": list(p),
                                      "size": _num(sexpr.value(v, "size", 0.6), 0.6)})

    edge = []
    for g in tree:
        if isinstance(g, list) and g and str(g[0]).startswith("gr_") and _layer(g) == "Edge.Cuts":
            edge += _shape_points(g)
    bb = _bbox(edge)
    if bb:
        out["outline"] = {"bbox": [round(v, 4) for v in bb]}
    return out
