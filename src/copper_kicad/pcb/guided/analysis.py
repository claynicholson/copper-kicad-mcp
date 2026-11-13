"""Read-only analysis for ``pcb_route_plan`` and the pictures both routing tools return.

    facts(board)                 compact per-net table + layer stack + keep-outs + pours
    congestion(board, cfg)       coarse grid of routing demand vs. free space, hot spots with coordinates
    heatmap(board, cong, path)   PNG of that grid
    route_map(board, path, ...)  PNG of the copper as KiCad has it (tracks by layer, vias, failed
                                 connections, plan regions) — drawn without zone fills so routes are visible
"""
from __future__ import annotations

import math

from . import plan as planmod

LAYER_COLORS = [(214, 62, 62), (64, 128, 232), (70, 190, 100), (224, 170, 50), (170, 100, 220), (60, 190, 200)]


def net_table(board, facts, limit=150):
    """Rows: [net, kind, pads, unrouted, est_mm, class, width, clearance, flags]. Unrouted first."""
    rows = []
    for n in sorted(facts, key=lambda k: (-facts[k]["unrouted"], k)):
        f = facts[n]
        net = board.nets[n]
        flags = list(f["tags"])
        if f["pair"]:
            flags.append(("P of " if f["role"] == "p" else "N of ") + f["pair"])
        if net.has_pour:
            flags.append("pour:" + "+".join(board.layers[l] for l in sorted(net.pour_layers)))
        rows.append([n, f["kind"], f["pads"], f["unrouted"], f["est"], net.cls, net.width, net.clearance,
                     ",".join(flags)])
    more = max(0, len(rows) - limit)
    return rows[:limit], more


def layer_stack(board):
    planes = planmod.plane_layers(board)
    smd = [0] * board.L
    for p in board.pads:
        if p.kind in ("smd", "conn"):
            for l in p.layers:
                smd[l] += 1
    out = []
    for i, name in enumerate(board.layers):
        d = {"layer": name, "smd_pads": smd[i]}
        nets = sorted({z["net"] for z in board.pours if name in z["layers"]})
        if nets:
            d["pour"] = nets
        if i in planes:
            d["plane"] = True
        out.append(d)
    return out


def ratsnest(board, cfg=None):
    """[(net, x1, y1, x2, y2)] — straight lines for every connection still to make (MST per net)."""
    out = []
    for name in sorted(board.clusters):
        cl = board.clusters[name]
        if len(cl) < 2:
            continue
        if cfg is not None and name in cfg.nets and cfg.nets[name].skip:
            continue
        groups = [[(p.x, p.y) for p in cl[c]["pads"]] for c in sorted(cl) if cl[c]["pads"]]
        if len(groups) < 2:
            continue
        tree = [0]
        rest = list(range(1, len(groups)))
        while rest:
            best = None
            for r in rest:
                for t in tree:
                    for x1, y1 in groups[r]:
                        for x2, y2 in groups[t]:
                            d = abs(x1 - x2) + abs(y1 - y2)
                            if best is None or d < best[0]:
                                best = (d, r, x1, y1, x2, y2)
            _d, r, x1, y1, x2, y2 = best
            out.append((name, x1, y1, x2, y2))
            tree.append(r)
            rest.remove(r)
    return out


def congestion(board, cfg, cell=None):
    """Coarse demand / supply grid.

    per cell: pads, lines (ratsnest lines crossing), free (fraction of routing-grid cells a default-width
    track may use, averaged over the signal layers). score = lines * (1.15 - free) + pads / 10.
    """
    x0, y0, x1, y1 = board.bbox
    w, h = x1 - x0, y1 - y0
    if cell is None:
        cell = max(2.0, round(max(w, h) / 16, 1))
    nx, ny = max(1, int(math.ceil(w / cell))), max(1, int(math.ceil(h / cell)))
    pads = [[0] * nx for _ in range(ny)]
    lines = [[0] * nx for _ in range(ny)]
    refs = [[set() for _ in range(nx)] for _ in range(ny)]

    def ij(x, y):
        return min(nx - 1, max(0, int((x - x0) / cell))), min(ny - 1, max(0, int((y - y0) / cell)))
    for p in board.pads:
        if p.layers:
            i, j = ij(p.x, p.y)
            pads[j][i] += 1
            refs[j][i].add(p.ref)
    rn = ratsnest(board, cfg)
    for _n, xa, ya, xb, yb in rn:
        seen = set()
        steps = max(1, int(math.hypot(xb - xa, yb - ya) / (cell / 3)))
        for k in range(steps + 1):
            t = k / steps
            seen.add(ij(xa + (xb - xa) * t, ya + (yb - ya) * t))
        for i, j in seen:
            lines[j][i] += 1
    # free space from the default-class occupancy map
    df = board.rules["default"]
    key = board.class_key(df.get("width") or 0.2, df.get("clearance") or 0.2, df.get("via") or 0.6, df.get("drill") or 0.3)
    if board.grid is None:
        board.make_grid(cfg.step)
    g = board.grid
    S = board.class_map(key).S
    sig_layers = sorted({l for nc in cfg.nets.values() for l in nc.layers_ok}) or list(range(board.L))
    free = [[0] * nx for _ in range(ny)]
    tot = [[0] * nx for _ in range(ny)]
    G, W = g.G, g.W
    for iy in range(1, g.H - 1, 2):
        y = g.my(iy)
        j = min(ny - 1, max(0, int((y - y0) / cell)))
        for ix in range(1, g.W - 1, 2):
            i = min(nx - 1, max(0, int((g.mx(ix) - x0) / cell)))
            k = iy * W + ix
            for l in sig_layers:
                tot[j][i] += 1
                if S[l * G + k] == 0:
                    free[j][i] += 1
    cells = []
    for j in range(ny):
        for i in range(nx):
            f = free[j][i] / tot[j][i] if tot[j][i] else 0.0
            score = lines[j][i] * (1.15 - f) + pads[j][i] / 10.0
            cells.append({"i": i, "j": j, "pads": pads[j][i], "lines": lines[j][i], "free": round(f, 2),
                          "score": round(score, 2), "refs": sorted(refs[j][i])})
    hot = sorted((c for c in cells if c["lines"] or c["pads"]), key=lambda c: (-c["score"], c["j"], c["i"]))[:6]
    hot = [{"at": [round(x0 + (c["i"] + 0.5) * cell, 1), round(y0 + (c["j"] + 0.5) * cell, 1)],
            "rect": [round(x0 + c["i"] * cell, 1), round(y0 + c["j"] * cell, 1),
                     round(x0 + (c["i"] + 1) * cell, 1), round(y0 + (c["j"] + 1) * cell, 1)],
            "pads": c["pads"], "connections_crossing": c["lines"], "free": c["free"], "parts": c["refs"][:6]}
           for c in hot if c["score"] > 0.5]
    # one text row per grid row: demand digit (0-9, '+' beyond) — a model can read the map without the image
    rows = []
    for j in range(ny):
        rows.append("".join("#" if tot[j][i] and free[j][i] == 0 and not lines[j][i] else
                            ("." if lines[j][i] == 0 else (str(lines[j][i]) if lines[j][i] < 10 else "+"))
                            for i in range(nx)))
    return {"cell_mm": cell, "origin": [round(x0, 2), round(y0, 2)], "cols": nx, "rows": ny,
            "map": rows, "legend": "one char per cell, row 0 = top: digit = connections that must cross it, "
                                   ". = none, # = no free copper",
            "hot_spots": hot, "_cells": cells, "_ratsnest": rn}


# ── pictures ──────────────────────────────────────────────────────────────
def _canvas(board, width=1400, pad_mm=1.5):
    from PIL import Image as PImage, ImageDraw
    x0, y0, x1, y1 = board.bbox
    x0, y0, x1, y1 = x0 - pad_mm, y0 - pad_mm, x1 + pad_mm, y1 + pad_mm
    scale = min(width / (x1 - x0), 1500 / (y1 - y0))
    width = int((x1 - x0) * scale)
    im = PImage.new("RGB", (width, int((y1 - y0) * scale) + 34), (18, 18, 22))
    d = ImageDraw.Draw(im, "RGBA")

    def P(x, y):
        return ((x - x0) * scale, (y - y0) * scale)
    return im, d, P, scale


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size)
    except TypeError:       # Pillow < 10.1
        return ImageFont.load_default()


def _base(board, d, P, scale, labels=True):
    for o, holes in board.outline:
        d.line([P(*q) for q in o] + [P(*o[0])], fill=(230, 210, 80), width=2)
        for hp in holes:
            d.line([P(*q) for q in hp] + [P(*hp[0])], fill=(230, 210, 80), width=2)
    for s in board.shapes:
        if s.cls == "kt":
            d.polygon([P(*q) for q in s.g], fill=(160, 60, 160, 40), outline=(190, 80, 190))
    for s in board.shapes:
        if s.cls == "cu" and s.tag is not None and hasattr(s.tag, "ref"):
            top = 0 in s.layers
            col = (150, 150, 150) if top else (84, 96, 140)
            if s.kind == "box":
                cx, cy, ux, uy, ihw, ihh, r = s.g
                hw, hh = ihw + r, ihh + r
                pts = [(cx + ux * a - uy * b, cy + uy * a + ux * b) for a, b in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
                if ihw == 0 and ihh == 0:
                    d.ellipse([P(cx - r, cy - r), P(cx + r, cy + r)], fill=col)
                else:
                    d.polygon([P(*q) for q in pts], fill=col)
            else:
                d.polygon([P(*q) for q in s.g], fill=col)
        elif s.cls == "hole" and s.tag is not None and hasattr(s.tag, "ref"):
            bb = s.bbox
            d.ellipse([P(bb[0], bb[1]), P(bb[2], bb[3])], fill=(18, 18, 22),
                      outline=(120, 120, 120) if s.tag.kind == "npth" else None)
    if labels:
        f = _font(max(10, min(22, int(scale * 0.9))))
        for fp in board.ex.get("footprints") or []:
            if fp.get("pads", 0) >= 1:
                bb = fp["bbox"]
                d.text(P(bb[0], bb[1] - 0.2), fp["ref"], fill=(240, 232, 150), font=f, anchor="lb")


def _legend(im, d, text):
    f = _font(15)
    d.rectangle([0, im.size[1] - 30, im.size[0], im.size[1]], fill=(30, 30, 36))
    d.text((10, im.size[1] - 24), text, fill=(225, 225, 225), font=f)


def heatmap(board, cong, path, width=1400):
    im, d, P, scale = _canvas(board, width)
    cell = cong["cell_mm"]
    x0, y0 = cong["origin"]
    mx = max((c["score"] for c in cong["_cells"]), default=1.0) or 1.0
    for c in cong["_cells"]:
        if c["score"] <= 0:
            continue
        t = min(1.0, c["score"] / mx)
        col = (int(60 + 195 * t), int(170 - 120 * t), int(60 - 30 * t), int(60 + 130 * t))
        d.rectangle([P(x0 + c["i"] * cell, y0 + c["j"] * cell), P(x0 + (c["i"] + 1) * cell, y0 + (c["j"] + 1) * cell)],
                    fill=col)
    _base(board, d, P, scale)
    for _n, xa, ya, xb, yb in cong["_ratsnest"]:
        d.line([P(xa, ya), P(xb, yb)], fill=(255, 255, 255, 150), width=1)
    f = _font(max(11, int(scale * 0.8)))
    for k, hsp in enumerate(cong["hot_spots"]):
        r = hsp["rect"]
        d.rectangle([P(r[0], r[1]), P(r[2], r[3])], outline=(255, 255, 255), width=2)
        d.text(P(r[0] + 0.2, r[1] + 0.2), f"#{k + 1}", fill=(255, 255, 255), font=f)
    _legend(im, d, f"congestion: red = many connections must cross little free copper · white lines = unrouted "
                   f"connections · #n = hot spots · cell {cell} mm")
    im.save(path)
    return path


def route_map(board, path, failed=None, regions=None, corridors=None, width=1400, title=""):
    """Draw the board's copper as extracted from KiCad (``board`` built from a fresh extract)."""
    im, d, P, scale = _canvas(board, width)
    for r in regions or []:
        x0, y0, x1, y1 = r["rect"]
        if r.get("forbid"):
            d.rectangle([P(x0, y0), P(x1, y1)], fill=(255, 80, 80, 36), outline=(255, 110, 110), width=2)
        else:
            d.rectangle([P(x0, y0), P(x1, y1)], fill=(255, 190, 60, 30), outline=(255, 200, 90), width=2)
    for pts in corridors or []:
        d.line([P(*q) for q in pts], fill=(120, 255, 160, 110), width=max(2, int(scale * 0.4)))
    _base(board, d, P, scale)
    L = board.L
    names_c = ["red", "blue", "green", "orange", "purple", "cyan"]

    def color_index(lay):
        if lay == 0:
            return 0
        if lay == L - 1:
            return 1
        return 2 + (lay - 1) % (len(LAYER_COLORS) - 2)
    for lay in range(L - 1, -1, -1):
        col = LAYER_COLORS[color_index(lay)]
        for net, l, x1, y1, x2, y2, w in board.kept_tracks:
            if l != lay:
                continue
            wpx = max(2, int(round(w * scale)))
            d.line([P(x1, y1), P(x2, y2)], fill=col + (235,), width=wpx)
            r = w / 2
            for x, y in ((x1, y1), (x2, y2)):
                d.ellipse([P(x - r, y - r), P(x + r, y + r)], fill=col + (235,))
    for net, x, y, size, drill, top, bot in board.kept_vias:
        r = size / 2
        d.ellipse([P(x - r, y - r), P(x + r, y + r)], fill=(236, 236, 236))
        r = drill / 2
        d.ellipse([P(x - r, y - r), P(x + r, y + r)], fill=(40, 40, 46))
    f = _font(max(11, int(scale * 0.8)))
    for k, e in enumerate(failed or []):
        pa, pb = e.get("_a"), e.get("_b")
        if pa and pb:
            d.line([P(*pa), P(*pb)], fill=(255, 235, 60), width=2)
        w = e.get("where") or pa
        if w:
            x, y = P(*w)
            d.line([(x - 7, y - 7), (x + 7, y + 7)], fill=(255, 235, 60), width=3)
            d.line([(x - 7, y + 7), (x + 7, y - 7)], fill=(255, 235, 60), width=3)
            d.text((x + 9, y - 8), e.get("net", ""), fill=(255, 235, 60), font=f)
    _legend(im, d, (title + " · " if title else "") + "tracks: " + " ".join(
        f"{board.layers[i]}={names_c[color_index(i)]}" for i in range(L))
        + " · white = via · yellow X = unrouted · pours hidden")
    im.save(path)
    return path
