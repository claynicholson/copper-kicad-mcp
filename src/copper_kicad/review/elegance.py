"""Board elegance: a 0-100 score from measured layout quality, with the reasons.

    elegance(pr, drc=None)            -> {"score", "ungated", "parts", "gates", "worst", "metrics"}
    placement_metrics(parts, outline) -> space / connectors / order / proximity numbers (pure Python)
    placement_score(parts, outline)   -> {"score", "parts", ...} for ranking placement candidates
    brief(e, n=3)                     -> the few bytes a tool result carries

Nine sub-scores (weights in ``W``): use of the board area, connectors on the edge, order (grid,
rotation, aligned passives), critical proximity (decoupling caps, crystals), routing economy, track
geometry, plane integrity, silkscreen legibility, mounting-hole symmetry. Sub-scores that do not
apply are left out, so the same function scores a placement before routing.

Gates (unrouted connections, DRC errors, courtyard overlaps, parts outside the outline) cap the
score at 49: a board that does not work is not elegant. ``ungated`` is always reported so the trend
stays visible. Gates use the numbers ``verify`` reports (the DRC dict), never a second count.

The server process never imports pcbnew: what needs it comes from the worker op ``elegance_extra``.
Deterministic; nothing here calls a model or the network.
"""
from __future__ import annotations

import math
import re

from ..pcb import holes as _holes
from ..pcb import place as placer
from . import silkcheck

W = {"space": 20, "connectors": 12, "order": 10, "proximity": 12, "economy": 14, "geometry": 12, "planes": 6,
     "silk": 10, "holes": 4}
HS_RE = re.compile(r"(USB|D\+|D-|DP\b|DM\b|D_P|D_N|XTAL|XIN|XOUT|OSC|CLK|RF|ANT|SWCLK|QSPI|SDIO|MIPI|ETH|TX[PN]|RX[PN])", re.I)
GATE_CAP = 49.0
EMPTY_GOOD, EMPTY_BAD = 0.10, 0.30   # largest empty rectangle as a share of the board: fine / clearly bare
REPEAT_MIN_AREA = 25.0  # mm2 of courtyard from which three identical multi-pad parts count as a repeated group
REPEAT_MIN_COUNT = 4   # three DIP-8s in three corners of a design are not a row; four pots are
REPEAT_SHARE = 0.5     # share of the 'order' sub-score they take when the board has such a group
HOLE_WEIGHT = 0.5      # share of a mounting hole's keep-out that counts as "used" in the thirds of the board


def lerp(v, good, bad):
    """1 at good, 0 at bad, linear between (either direction), clamped."""
    if good == bad:
        return 1.0
    return max(0.0, min(1.0, (v - bad) / (good - bad)))


def _is_hole(p) -> bool:
    return _holes.is_hole(p.fpid) or p.prefix() in ("H", "MH")


def side_entry(p) -> bool:
    """Pads sit clearly behind the body centre: the mating face is on the opposite side (USB, JST-SH ...)."""
    if not p.pads:
        return False
    lb = p.lbox
    pcx = sum(pd["lx"] for pd in p.pads) / len(p.pads) - (lb[0] + lb[2]) / 2
    pcy = sum(pd["ly"] for pd in p.pads) / len(p.pads) - (lb[1] + lb[3]) / 2
    return math.hypot(pcx, pcy) > 0.18 * max(lb[2] - lb[0], lb[3] - lb[1])


def connector_state(p, bbox) -> dict:
    """Where a connector sits relative to the nearest board edge."""
    x0, y0, x1, y1 = bbox
    r = p.rect()
    d_edge = {"left": r[0] - x0, "right": x1 - r[2], "top": r[1] - y0, "bottom": y1 - r[3]}
    e = min(d_edge, key=d_edge.get)
    rec = {"ref": p.ref, "edge": e, "gap": round(d_edge[e], 2)}
    if p.edge_hint:
        hx, hy = p.ltf(p.edge_hint[0], p.edge_hint[1], p.rot)
        pos = {"left": p.x + hx - x0, "right": x1 - (p.x + hx), "top": p.y + hy - y0, "bottom": y1 - (p.y + hy)}
        rec["mouth_to_edge"] = round(min(pos.values(), key=abs), 2)
        rec["ok"] = abs(rec["mouth_to_edge"]) <= 0.15
        rec["kind"] = "edge_marker"
    elif side_entry(p):
        fab = p.d.get("lfab")
        gap = d_edge[e]
        if fab:      # the body (fab outline) is what must reach the edge; the courtyard is 0.25-0.5 mm larger
            pts = [p.ltf(cx, cy, p.rot) for cx, cy in ((fab[0], fab[1]), (fab[2], fab[1]), (fab[2], fab[3]), (fab[0], fab[3]))]
            fr = (p.x + min(q[0] for q in pts), p.y + min(q[1] for q in pts), p.x + max(q[0] for q in pts), p.y + max(q[1] for q in pts))
            gap = {"left": fr[0] - x0, "right": x1 - fr[2], "top": fr[1] - y0, "bottom": y1 - fr[3]}[e]
            rec["body_gap"] = round(gap, 2)
        rec["kind"] = "side_entry"
        rec["ok"] = gap <= 0.1
    else:
        rec["kind"] = "vertical"
        w, h = r[2] - r[0], r[3] - r[1]
        if max(w, h) >= 2 * min(w, h):      # a pin header belongs ALONG an edge, not end-on to it
            par = ("top", "bottom") if w > h else ("left", "right")
            e = min(par, key=lambda k: d_edge[k])
            rec["edge"], rec["gap"] = e, round(d_edge[e], 2)
        rec["ok"] = d_edge[e] <= 1.5
    return rec


def largest_empty(rects, outline, grow: float = 1.0) -> float:
    """Share of the board taken by its largest empty axis-parallel rectangle (parts grown by ``grow`` mm,
    cells outside a non-rectangular outline count as used). A board whose parts huddle in one half has a
    large one even when every third of it holds something. The rectangle counts up to an aspect of 2:1,
    so the long thin lanes between the rows of an evenly spread layout are not "bare areas"."""
    x0, y0, x1, y1 = outline["bbox"]
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0:
        return 0.0
    cell = max(0.5, min(bw, bh) / 28.0)
    nx, ny = max(1, int(round(bw / cell))), max(1, int(round(bh / cell)))
    cx, cy = bw / nx, bh / ny
    used = [[False] * nx for _ in range(ny)]
    poly = outline.get("polygon")
    if poly and len(poly) > 4:
        for j in range(ny):
            py = y0 + (j + 0.5) * cy
            for i in range(nx):
                if not placer._pip(poly, x0 + (i + 0.5) * cx, py):
                    used[j][i] = True
    for r in rects:
        i0, i1 = max(0, int((r[0] - grow - x0) / cx)), min(nx - 1, int((r[2] + grow - x0) / cx))
        j0, j1 = max(0, int((r[1] - grow - y0) / cy)), min(ny - 1, int((r[3] + grow - y0) / cy))
        for j in range(j0, j1 + 1):
            row = used[j]
            for i in range(i0, i1 + 1):
                row[i] = True
    best, h = 0.0, [0] * nx
    for j in range(ny):
        row = used[j]
        for i in range(nx):
            h[i] = 0 if row[i] else h[i] + 1
        st = []
        for i in range(nx + 1):
            cur = h[i] if i < nx else 0
            start = i
            while st and st[-1][1] >= cur:
                start, hh = st.pop()
                w_mm, h_mm = (i - start) * cx, hh * cy
                best = max(best, min(w_mm, 2 * h_mm) * min(h_mm, 2 * w_mm))
            st.append((start, cur))
    return round(best / (bw * bh), 3)


def placement_metrics(parts, outline, classified=False) -> dict:
    """Pure-Python placement numbers on ``place.Part`` objects (roles are assigned here unless done)."""
    if not classified:
        placer.classify(parts)
    x0, y0, x1, y1 = outline["bbox"]
    bw, bh = x1 - x0, y1 - y0
    area = outline.get("area") or bw * bh
    comps = [p for p in parts if not _is_hole(p) and not p.ref.startswith("#OBST")]
    holes = [p for p in parts if _is_hole(p) and not p.ref.startswith("#OBST")]
    M = {"board": [round(bw, 2), round(bh, 2)], "board_area": round(area, 1), "parts": len(comps)}
    M["utilisation"] = round(sum(p.area() for p in comps) / max(area, 1e-9), 3)

    hole_set = set(holes)

    def band_occ(axis):
        occ = [0.0, 0.0, 0.0]
        lo, span = (x0, bw) if axis == 0 else (y0, bh)
        for p in comps + holes:          # a third that holds mounting holes is not empty: they count half
            r = p.rect()
            a0, a1 = (r[0], r[2]) if axis == 0 else (r[1], r[3])
            other = ((r[3] - r[1]) if axis == 0 else (r[2] - r[0])) * (HOLE_WEIGHT if p in hole_set else 1.0)
            for k in range(3):
                b0, b1 = lo + span * k / 3, lo + span * (k + 1) / 3
                occ[k] += max(0.0, min(a1, b1) - max(a0, b0)) * other
        return [round(v / (area / 3), 3) for v in occ]
    M["thirds_x"], M["thirds_y"] = band_occ(0), band_occ(1)
    filled = (sum(p.area() for p in comps) + HOLE_WEIGHT * sum(p.area() for p in holes)) / max(area, 1e-9)
    M["emptiest_third"] = round(min(M["thirds_x"] + M["thirds_y"]) / max(filled, 1e-6), 3)
    M["largest_empty"] = largest_empty([p.rect() for p in comps + holes], outline)
    sa = sum(p.area() for p in comps)
    if sa:
        cx = sum(p.area() * (p.rect()[0] + p.rect()[2]) / 2 for p in comps) / sa
        cy = sum(p.area() * (p.rect()[1] + p.rect()[3]) / 2 for p in comps) / sa
    else:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    M["imbalance"] = round(math.hypot(cx - (x0 + x1) / 2, cy - (y0 + y1) / 2) / (math.hypot(bw, bh) / 2), 3)
    M["connectors"] = [connector_state(p, outline["bbox"]) for p in comps if p.role == "connector"]
    n = max(len(comps), 1)
    M["rot_ortho"] = round(sum(1 for p in comps if abs(((p.rot + 45) % 90) - 45) < 0.01) / n, 3)
    M["on_grid"] = round(sum(1 for p in comps if abs(p.x / .25 - round(p.x / .25)) < 1e-3 and
                             abs(p.y / .25 - round(p.y / .25)) < 1e-3) / n, 3)
    small = [p for p in comps if len(p.pads) == 2]
    al = sum(1 for p in small if any(q is not p and math.hypot(q.x - p.x, q.y - p.y) < 12 and
                                     (abs(q.x - p.x) < 0.02 or abs(q.y - p.y) < 0.02) for q in small))
    M["passives_aligned"] = round(al / len(small), 3) if small else 1.0
    # four or more identical large parts (pots, jacks, switches, sockets ...) read as a row or a matrix:
    # same rotation, and each one in line with another of its kind
    groups = {}
    for p in comps:
        if len(p.pads) >= 3 and p.area() >= REPEAT_MIN_AREA:
            groups.setdefault((p.fpid, len(p.pads)), []).append(p)
    ok = tot = 0.0
    for g in groups.values():
        if len(g) < REPEAT_MIN_COUNT:
            continue
        rots = [round(p.rot) % 360 for p in g]
        modal = max(sorted(set(rots)), key=rots.count)
        for p, r in zip(g, rots):
            lined = any(q is not p and (abs(q.x - p.x) < 0.02 or abs(q.y - p.y) < 0.02) for q in g)
            ok += 0.5 * (r == modal) + 0.5 * lined
            tot += 1
    M["repeat_uniform"] = round(ok / tot, 3) if tot else None
    dec, xt = [], []
    for p in comps:
        if p.anchor and p.role in ("decap", "xtal_cap", "crystal"):
            q, qp = p.anchor
            qx, qy = q.pad_pos(qp)
            mine = [pd for pd in p.pads if pd.get("net") and pd.get("net") == qp.get("net")]
            px, py = p.pad_pos(mine[0]) if mine else (p.x, p.y)
            (dec if p.role == "decap" else xt).append([p.ref, f"{q.ref}.{qp.get('num')}", round(math.hypot(qx - px, qy - py), 2)])
    M["decap_mm"], M["xtal_mm"] = dec, xt
    hp = [(p.x, p.y) for p in holes]
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    sym = sum(1 for (x, y) in hp if any(abs(2 * mx - x - u) < 0.06 and abs(y - v) < 0.06 for u, v in hp)
              and any(abs(x - u) < 0.06 and abs(2 * my - y - v) < 0.06 for u, v in hp))
    M["holes"] = len(hp)
    M["holes_symmetric"] = round(sym / len(hp), 3) if hp else None
    out = 0
    for p in comps + holes:      # a mounting hole left beside the board is as wrong as a part there
        if not (x0 - 0.01 <= p.x <= x1 + 0.01 and y0 - 0.01 <= p.y <= y1 + 0.01):
            out += 1
    M["outside"] = out
    return M


def _placement_parts(M, src=None, ranking=False) -> dict:
    s = {}
    u = M["utilisation"]
    sized_by_tool = src == "auto"
    third = lerp(M["emptiest_third"], 0.60, 0.15)
    if M.get("largest_empty") is not None:      # one big bare area costs as much as an empty third
        third = 0.55 * third + 0.45 * lerp(M["largest_empty"], EMPTY_GOOD, EMPTY_BAD)
    bal = lerp(M["imbalance"], 0.05, 0.30)
    if sized_by_tool or ranking:
        hi = 0.55 if ranking else 0.62      # ranking: a tighter board must not out-score a routable one
        s_util = lerp(u, 0.28, 0.08) if u < 0.28 else lerp(u, hi, 0.85)
        s["space"] = 0.30 * s_util + 0.45 * third + 0.25 * bal
    else:                                   # the size was the user's (or an enclosure's): judge the spread only
        s["space"] = (0.45 * third + 0.25 * bal) / 0.70
    c = M["connectors"]
    s["connectors"] = sum(1 for r in c if r["ok"]) / len(c) if c else None
    s["order"] = 0.4 * M["rot_ortho"] + 0.3 * M["on_grid"] + 0.3 * M["passives_aligned"]
    if M.get("repeat_uniform") is not None:
        s["order"] = (1 - REPEAT_SHARE) * s["order"] + REPEAT_SHARE * M["repeat_uniform"]
    pr = [lerp(d[2], 3.0, 8.0) for d in M["decap_mm"]] + [lerp(d[2], 5.0, 12.0) for d in M["xtal_mm"]]
    s["proximity"] = sum(pr) / len(pr) if pr else None
    s["holes"] = M["holes_symmetric"] if src != "file" else None
    return s


def _total(s) -> float:
    wsum = sum(W[k] for k, v in s.items() if v is not None)
    return 100.0 * sum(W[k] * v for k, v in s.items() if v is not None) / wsum if wsum else 0.0


def placement_score(parts, outline, src=None, classified=False) -> dict:
    """Score of the placement-only sub-scores, for ranking candidates (utilisation above 0.55 is not rewarded)."""
    M = placement_metrics(parts, outline, classified)
    s = _placement_parts(M, src, ranking=True)
    return {"score": round(_total(s), 2), "parts": {k: (None if v is None else round(v, 3)) for k, v in s.items()}, "metrics": M}


# ── routing / silk / plane metrics ────────────────────────────────────────
def _seg_x(a, b, c, d):
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return o(a, b, c) * o(a, b, d) < 0 and o(c, d, a) * o(c, d, b) < 0


def routing_metrics(parts, track_list, extra) -> dict:
    M = {}
    segs = [t for t in track_list if t["type"] != "via"]
    vias = [t for t in track_list if t["type"] == "via"]
    M["tracks"], M["vias"] = len(segs), len(vias)
    planes = extra.get("planes") or []
    area = extra.get("board_area") or 1.0
    zone_nets = {p["net"] for p in planes if p.get("net")}
    padbox = []
    for p in parts:
        for pd in p.pads:
            if pd.get("net"):
                x, y = p.pad_pos(pd)
                w, h = pd.get("w", 1.0), pd.get("h", 1.0)
                if int(round(p.rot)) % 180 == 90:
                    w, h = h, w
                padbox.append((x, y, w / 2, h / 2, pd["net"]))

    def pad_at(px, py, net):
        for (x, y, hw, hh, n) in padbox:
            if n == net and abs(px - x) <= hw + 1e-6 and abs(py - y) <= hh + 1e-6:
                return (x, y)
        return None
    netlen = {}
    off45 = tot = 0.0
    joints, nodes = {}, {}
    via_pts = {(round(v["at"][0], 3), round(v["at"][1], 3)) for v in vias}
    for t in segs:
        (ax, ay), (bx, by) = t["start"], t["end"]
        l = math.hypot(bx - ax, by - ay)
        if l < 1e-6:
            continue
        netlen[t["net"]] = netlen.get(t["net"], 0) + l
        pa, pb = pad_at(ax, ay, t["net"]), pad_at(bx, by, t["net"])
        hidden = pa is not None and pa == pb          # a stub that lives inside one pad is not visible routing
        if not hidden and l <= 0.3 and ((round(ax, 3), round(ay, 3)) in via_pts or (round(bx, 3), round(by, 3)) in via_pts):
            hidden = True                             # ... nor is one inside a via's own pad
        if not hidden:
            tot += l
            ang = math.degrees(math.atan2(by - ay, bx - ax)) % 45
            if min(ang, 45 - ang) > 0.5:
                off45 += l
        for (px, py), (qx, qy) in (((ax, ay), (bx, by)), ((bx, by), (ax, ay))):
            k = (round(px, 3), round(py, 3), t["layer"], t["net"])
            joints.setdefault(k, []).append(((qx - px) / l, (qy - py) / l, hidden))
            nodes[k] = nodes.get(k, 0) + 1
    bends = {"straight": 0, "135": 0, "90": 0, "acute": 0, "other": 0}
    for k, v in joints.items():
        if len(v) != 2 or v[0][2] or v[1][2]:
            continue
        dp = v[0][0] * v[1][0] + v[0][1] * v[1][1]
        a = math.degrees(math.acos(max(-1.0, min(1.0, dp))))
        if a > 179:
            bends["straight"] += 1
        elif abs(a - 135) < 1.5:
            bends["135"] += 1
        elif abs(a - 90) < 1.5:
            bends["90"] += 1
        elif a < 89:
            bends["acute"] += 1
        else:
            bends["other"] += 1
    M["bends"] = bends
    M["off45_len_frac"] = round(off45 / tot, 3) if tot else 0.0
    M["track_mm"] = round(tot, 1)
    pads_by_net = {}
    for p in parts:
        for pd in p.pads:
            n = pd.get("net")
            if n and not n.startswith("unconnected-"):
                pads_by_net.setdefault(n, []).append(p.pad_pos(pd))
    rl = ml = 0.0
    routed = 0
    for n, pts in pads_by_net.items():
        if n in zone_nets or len(pts) < 2 or n not in netlen:
            continue
        rl += netlen[n]
        ml += placer.mst_length(pts)
        routed += 1
    M["detour"] = round(rl / ml, 3) if ml else None
    M["routed_nets"] = routed
    M["vias_per_net"] = round(len([v for v in vias if v["net"] not in zone_nets]) / max(routed, 1), 3)
    M["stitch_vias"] = len([v for v in vias if v["net"] in zone_nets])
    ends = cent = 0
    for k, n in nodes.items():
        if n != 1 or (k[0], k[1]) in via_pts:
            continue
        c = pad_at(k[0], k[1], k[3])
        if c is not None:
            ends += 1
            if math.hypot(k[0] - c[0], k[1] - c[1]) <= 0.05:
                cent += 1
    M["pad_entry_centred"] = round(cent / ends, 3) if ends else None
    pl = []
    for p in planes:
        if not p.get("net") or p.get("area", 0) <= 0:
            continue
        pl.append({"layer": p["layer"], "net": p["net"], "coverage": round(p["area"] / area, 3), "islands": p["islands"]})
    M["planes"] = pl
    hs = [t for t in segs if HS_RE.search(t["net"] or "")]
    cross = 0
    for q in pl:
        under = [t for t in segs if t["layer"] == q["layer"] and t["net"] != q["net"]]
        for h in hs:
            if h["layer"] == q["layer"]:
                continue
            cross += sum(1 for u in under if u["net"] != h["net"] and _seg_x(h["start"], h["end"], u["start"], u["end"]))
    M["hs_plane_crossings"] = cross
    return M


def measure(pr) -> dict:
    """Raw metrics of the project's board (one worker start, read-only)."""
    from ..pcb import board, worker
    pcb = board.board_path(pr)
    res = worker.run("board", {"pcb": str(pcb), "save": False, "summary": False,
                               "ops": [{"op": "elegance_extra"}, {"op": "read", "what": ["geom", "tracks", "nets"]}]},
                     timeout=180)
    extra, snap = res["results"]
    return measure_from(snap, extra, pr)


def measure_from(snap, extra, pr=None) -> dict:
    ol = snap.get("outline")
    if not ol:
        return {"no_outline": True}
    if pr is not None:
        try:
            from ..tools.pcb_tools import _attach_sch_positions
            _attach_sch_positions(pr, snap)
        except Exception:  # noqa: BLE001
            pass
    parts = [placer.Part(d) for d in snap["footprints"]]
    ol = dict(ol)
    if extra.get("board_area"):
        ol["area"] = extra["board_area"]
    M = placement_metrics(parts, ol)
    M["src"] = (extra.get("props") or {}).get("src")
    M["placed_auto"] = (extra.get("props") or {}).get("placed") == "auto"
    M.update(routing_metrics(parts, snap.get("track_list") or [], extra))
    sv = silkcheck.evaluate(extra)
    M["silk_refs"], M["silk_hidden"], M["silk_bad"] = sv["visible"], sv["hidden"], sv["bad"]
    M["courtyard_overlaps"] = len(placer.overlaps(snap))
    M["unconnected_file"] = snap.get("unconnected")
    return M


def _worst(M, s, has_tracks) -> list:
    """Up to 5 {what, value, target, fix} entries ranked by lost points. Every fix is a call that exists
    and is safe on the board as it is."""
    src = M.get("src")
    wsum = sum(W[k] for k, v in s.items() if v is not None) or 1
    out = []

    def add(part, share, what, value, target, fix):
        lost = 100.0 * W[part] * share / wsum
        if lost >= 0.5:
            out.append((lost, {"what": what, "value": value, "target": target, "fix": fix, "points": round(lost, 1)}))
    relayout = ("pcb_place(auto=true) spreads the parts over the board" if not has_tracks else
                "the layout is routed: pcb_place(auto=true, clear_tracks=true) then pcb_route() re-does it")
    if s.get("space") is not None:
        k = 0.45 if src == "auto" else 0.45 / 0.70
        fix = relayout
        if src == "auto" and not has_tracks:
            fix = "pcb_setup(auto_size=true) then pcb_place(auto=true)"
        le = M.get("largest_empty")
        add("space", k * (0.55 if le is not None else 1.0) * (1 - lerp(M["emptiest_third"], 0.60, 0.15)), "emptiest_third",
            M["emptiest_third"], ">= 0.6", fix)
        if le is not None:
            add("space", k * 0.45 * (1 - lerp(le, EMPTY_GOOD, EMPTY_BAD)), "largest_empty_area", le, f"<= {EMPTY_GOOD} of the board",
                "one bare area this large: " + (fix if src == "auto" and not has_tracks else
                                                "a smaller board (pcb_setup(width=..., height=...)) or spread the parts "
                                                "into it with pcb_place(moves=[...])"))
        k = 0.25 if src == "auto" else 0.25 / 0.70
        add("space", k * (1 - lerp(M["imbalance"], 0.05, 0.30)), "imbalance", M["imbalance"], "<= 0.05", relayout)
        if src == "auto":
            u = M["utilisation"]
            su = lerp(u, 0.28, 0.08) if u < 0.28 else lerp(u, 0.62, 0.85)
            add("space", 0.30 * (1 - su), "utilisation", u, "0.28-0.62",
                "pcb_setup(auto_size=true)" if not has_tracks else "pcb_setup(width=..., height=...)")
    bad_c = [c for c in M["connectors"] if not c["ok"]]
    if bad_c:
        c = bad_c[0]
        gap = c.get("body_gap", c.get("mouth_to_edge", c["gap"]))
        add("connectors", len(bad_c) / len(M["connectors"]), "connector_off_edge",
            ", ".join(f"{x['ref']} {x.get('body_gap', x.get('mouth_to_edge', x['gap']))} mm" for x in bad_c[:4]),
            "flush with the edge" if c["kind"] != "vertical" else "within 1.5 mm of the edge",
            f"pcb_place(moves=[{{ref:'{c['ref']}', x:.., y:..}}]) moving it {abs(round(gap, 2))} mm toward the {c['edge']} edge"
            + ("" if has_tracks else ", or pcb_place(auto=true)"))
    if s.get("order") is not None:
        ru = M.get("repeat_uniform")
        k = (1 - REPEAT_SHARE) if ru is not None else 1.0
        add("order", k * 0.3 * (1 - M["passives_aligned"]), "passives_aligned", M["passives_aligned"], ">= 0.8", relayout)
        add("order", k * 0.3 * (1 - M["on_grid"]), "on_grid", M["on_grid"], "1.0", relayout)
        add("order", k * 0.4 * (1 - M["rot_ortho"]), "rot_ortho", M["rot_ortho"], "1.0",
            "pcb_place(moves=[{ref, rot}]) with rot a multiple of 90")
        if ru is not None:
            add("order", REPEAT_SHARE * (1 - ru), "identical_parts_irregular", ru, "1.0",
                "identical parts (pots, jacks, switches ...) belong in one row or matrix at the same rotation: "
                "pcb_place(moves=[{ref, x, y, rot}, ...]) with a shared x or y and one rot")
    far = sorted([d for d in M["decap_mm"] if d[2] > 3.0], key=lambda d: -d[2])
    xfar = sorted([d for d in M["xtal_mm"] if d[2] > 5.0], key=lambda d: -d[2])
    if (far or xfar) and s.get("proximity") is not None:
        d = (far or xfar)[0]
        add("proximity", 1 - s["proximity"], "decap_far" if far else "crystal_far",
            ", ".join(f"{x[0]} {x[2]} mm from {x[1]}" for x in (far + xfar)[:4]), "<= 3 mm (crystal 5 mm)",
            f"pcb_place(moves=[{{ref:'{d[0]}', x:.., y:..}}]) next to {d[1]}" + ("" if has_tracks else ", or pcb_place(auto=true)"))
    if s.get("economy") is not None:
        add("economy", 0.6 * (1 - lerp(M.get("detour") or 1.0, 1.25, 2.2)), "detour", M.get("detour"), "<= 1.25",
            "tracks are long for this placement: move the parts of the longest nets closer, then re-route those nets")
        add("economy", 0.4 * (1 - lerp(M["vias_per_net"], 0.15, 1.2)), "vias_per_net", M["vias_per_net"], "<= 0.15",
            "many layer changes: informational (a 2-layer board with a ground pour needs some)")
    if s.get("geometry") is not None:
        bd = M["bends"]
        nb = bd["135"] + bd["90"] + bd["acute"] + bd["other"]
        good = bd["135"] / nb if nb else 1.0
        add("geometry", 0.45 * (1 - good), "bends_not_45", f"{bd['90']} at 90, {bd['acute']} acute, {bd['other']} odd of {nb}",
            "all 135 degree bends", "pcb_route(mode='polish') chamfers corners on unlocked tracks")
        add("geometry", 0.25 * (1 - lerp(M["off45_len_frac"], 0.0, 0.15)), "off45_len_frac", M["off45_len_frac"], "0",
            "pcb_route(mode='polish'), or re-route the odd-angle nets")
        if M.get("pad_entry_centred") is not None:
            add("geometry", 0.15 * (1 - M["pad_entry_centred"]), "pad_entry_centred", M["pad_entry_centred"], "1.0",
                "pcb_route(mode='polish')")
    if s.get("planes") is not None and M["planes"]:
        cov = max(p["coverage"] for p in M["planes"])
        isl = min(p["islands"] for p in M["planes"])
        add("planes", 0.5 * (1 - lerp(cov, 0.80, 0.40)) + 0.25 * (1 - lerp(isl, 1, 5)), "plane",
            f"best coverage {cov}, {isl} islands", ">= 0.8 coverage, 1 island",
            "pcb_pour(net='GND', stitch=true); long tracks on the pour layer cut it up")
        add("planes", 0.25 * (1 - lerp(M["hs_plane_crossings"], 0, 3)), "hs_plane_crossings", M["hs_plane_crossings"], "0",
            "a track on the plane layer crosses under a fast signal: route it on the other layer")
    nbad = len(M.get("silk_bad") or {})
    if s.get("silk") is not None and (nbad or M.get("silk_hidden")):
        refs = sorted(M["silk_bad"])
        add("silk", 1 - s["silk"], "silk_unreadable",
            (", ".join(f"{r} ({'/'.join(M['silk_bad'][r])})" for r in refs[:6]) or "none") +
            (f"; {M['silk_hidden']} hidden" if M.get("silk_hidden") else ""), "0 bad, 0 hidden",
            "pcb_route(mode='polish') re-seats unreadable reference designators" if nbad else
            "hidden references cost half a point each: informational (pcb_edit can show them again where there is room)")
    if s.get("holes") is not None and M.get("holes"):
        add("holes", 1 - s["holes"], "holes_asymmetric", M["holes_symmetric"], "1.0",
            "pcb_setup(holes='M3') puts board-only holes at the corners; holes from the schematic: pcb_place(auto=true) "
            "(unrouted board) or pcb_place(moves=[...])")
    out.sort(key=lambda t: -t[0])
    return [e for _, e in out[:5]]


def score(M, drc=None) -> dict:
    """Metrics (+ the DRC dict verify uses) -> {"score", "ungated", "parts", "gates", "worst"}."""
    if M.get("no_outline"):
        return {"score": 0.0, "ungated": 0.0, "parts": {}, "gates": ["no_outline"],
                "worst": [{"what": "no_outline", "value": None, "target": "a closed outline",
                           "fix": "pcb_setup(width=..., height=...) or pcb_setup(auto_size=true)"}]}
    src = M.get("src")
    s = _placement_parts(M, src)
    has_tracks = bool(M.get("tracks"))
    if has_tracks:
        s["economy"] = 0.6 * lerp(M.get("detour") or 1.0, 1.25, 2.2) + 0.4 * lerp(M["vias_per_net"], 0.15, 1.2)
        bd = M["bends"]
        nb = bd["135"] + bd["90"] + bd["acute"] + bd["other"]
        good = bd["135"] / nb if nb else 1.0
        s["geometry"] = (0.45 * good + 0.25 * lerp(M["off45_len_frac"], 0.0, 0.15) + 0.15 * lerp(bd["acute"], 0, 4)
                         + 0.15 * (M["pad_entry_centred"] if M["pad_entry_centred"] is not None else 1.0))
        if M["planes"]:
            cov = max(p["coverage"] for p in M["planes"])
            isl = min(p["islands"] for p in M["planes"])
            s["planes"] = 0.5 * lerp(cov, 0.80, 0.40) + 0.25 * lerp(isl, 1, 5) + 0.25 * lerp(M["hs_plane_crossings"], 0, 3)
        else:
            s["planes"] = None
    else:
        s["economy"] = s["geometry"] = s["planes"] = None
    vis, hid = M.get("silk_refs", 0), M.get("silk_hidden", 0)
    if vis + hid:
        # hidden designators cannot buy points: each counts as half a bad one
        s["silk"] = lerp((len(M["silk_bad"]) + 0.5 * hid) / (vis + hid), 0.0, 0.30)
    else:
        s["silk"] = 0.5
    total = _total(s)
    gates = []
    if drc is not None:
        if drc.get("unconnected"):
            gates.append(f"unrouted:{drc['unconnected']}")
        if drc.get("errors"):
            gates.append(f"drc_errors:{drc['errors']}")
    if M.get("courtyard_overlaps") and drc is None:
        # the count is a bounding-box estimate (rotated / L-shaped courtyards, two-sided boards overlap in it
        # without touching). With a DRC result a real overlap is a courtyards_overlap error and already gates.
        gates.append(f"courtyard_overlap:{M['courtyard_overlaps']}")
    if M.get("outside"):
        gates.append(f"outside_outline:{M['outside']}")
    out = {"score": round(min(total, GATE_CAP) if gates else total, 1), "ungated": round(total, 1),
           "parts": {k: (None if v is None else round(v, 2)) for k, v in s.items()}, "gates": gates,
           "worst": _worst(M, s, has_tracks)}
    return out


def elegance(pr, drc=None, metrics=False) -> dict:
    """Score the project's board. ``drc`` = the dict from ``board.drc`` when the caller already ran it
    (gates then cost nothing); None runs DRC here so the gates always match verify."""
    from ..pcb import board
    M = measure(pr)
    if drc is None and not M.get("no_outline"):
        try:
            drc = board.drc(pr, parity=False, record=False)
        except Exception:  # noqa: BLE001
            drc = None
    e = score(M, drc)
    if metrics:
        e["metrics"] = M
    return e


def brief(e: dict, n: int = 3) -> dict:
    """What a tool result carries: the score, why it is gated, and the top hints."""
    out = {"score": e["score"]}
    if e.get("gates"):
        out["ungated"] = e["ungated"]
        out["gates"] = e["gates"]
    if n:
        out["worst"] = [{k: w[k] for k in ("what", "value", "fix")} for w in e.get("worst", [])[:n]]
    return out


def safe_brief(pr, drc=None, n: int = 3):
    """``brief(elegance(...))`` that never fails a tool call."""
    try:
        return brief(elegance(pr, drc), n)
    except Exception:  # noqa: BLE001
        return None
