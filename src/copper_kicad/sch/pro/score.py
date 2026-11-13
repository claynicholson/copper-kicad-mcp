"""Readability metrics of one schematic sheet - pure geometry + netlist, no KiCad call.

The composite `score` ranks layouts of the SAME sheet against each other and tracks regressions on fixtures; it is
not a statement about hand-drawn sheets (text extents are an estimate of KiCad's stroke font)."""
from __future__ import annotations

import re

from ... import sexpr
from ...kicad import libs as LIBS
from .. import layout as LAY
from .. import ops as O
from .. import symbols as G
from ..model import Schematic
from . import exemplar as EX
from . import textbox as TB
from .blocks import Design, prefix

U = 1.27
FRAME_GROUP = "copper:block:"


def _hit(a, b, s=0.15):
    return a[0] + s < b[2] and b[0] + s < a[2] and a[1] + s < b[3] and b[1] + s < a[3]


def graphic_box(sch: Schematic, s):
    ls = sch.lib_symbol(s.lib_name)
    if ls is None:
        return s.body_bbox()
    return G.bbox_world(s.at, s.matrix, LIBS.unit_bbox(ls, s.unit, s.body_style, include_pins=False))


def page_use(w: float, h: float, frames: list, boxes: list) -> dict:
    """(class P) How the drawing uses its sheet. The drawable area is the sheet less 20 mm on every side (the 10 mm
    border and the 10 mm nothing may come closer to it) less the title-block corner.
    page_fill = bounding box of the frames / that area (section 9 of the design document);
    page_span = [width, height] of that bounding box as a fraction of the drawable width / height;
    page_empty_thirds = how many of the three upright and the three level thirds of the drawable area hold nothing
    at all (no frame, no symbol, no text, not the title block)."""
    if not frames:
        return {"page_fill": 0.0, "page_span": [0.0, 0.0], "page_empty_thirds": 0}
    x0, y0, x1, y1 = 20.0, 20.0, w - 20.0, h - 20.0
    tb = [w - 123.0, h - 46.5, x1, y1]
    fx0, fy0 = min(f[0] for f in frames), min(f[1] for f in frames)
    fx1, fy1 = max(f[2] for f in frames), max(f[3] for f in frames)
    free = (x1 - x0) * (y1 - y0) - max(0.0, x1 - tb[0]) * max(0.0, y1 - tb[1])
    held = list(boxes) + [tb]
    empty = 0
    for i in range(3):
        a, b = x0 + (x1 - x0) * i / 3, x0 + (x1 - x0) * (i + 1) / 3
        empty += not any(q[0] < b - 0.5 and q[2] > a + 0.5 for q in held)
        a, b = y0 + (y1 - y0) * i / 3, y0 + (y1 - y0) * (i + 1) / 3
        empty += not any(q[1] < b - 0.5 and q[3] > a + 0.5 for q in held)
    return {"page_fill": round((fx1 - fx0) * (fy1 - fy0) / max(1.0, free), 3),
            "page_span": [round((fx1 - fx0) / max(1.0, x1 - x0), 3), round((fy1 - fy0) / max(1.0, y1 - y0), 3)],
            "page_empty_thirds": int(empty)}


def frame_count(sch: Schematic) -> int:
    return sum(1 for g in sch.items("group") if len(g) > 1 and str(g[1]).startswith(FRAME_GROUP))


HARD_ZEROS = ("overlaps", "wire_through_text_or_body", "power_sideways", "labels_vertical", "labels_redundant",
              "crossing_near_junction", "offgrid_pins", "flag_islands",
              # package A (power conventions): no arch / hook to a supply or ground symbol, no ground symbol above
              # the side pin it serves and no supply symbol below it (power_ways)
              "power_hooks", "power_wrong_side",
              # package C (text hygiene): no text on another text, a wire, a junction, a no-connect cross, a symbol
              # or a frame border, no two texts read as one string (textbox.collisions)
              "text_collisions")


def _k(p):
    return (int(round(float(p[0]) * 100)), int(round(float(p[1]) * 100)))


def _wire_groups(wires, juncs):
    """Purely geometric wire groups (no joining by name) -> (find, on): find(point key) = group id, on(key) joins a
    point item to the wire it lies on."""
    par: dict = {}

    def find(a):
        par.setdefault(a, a)
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    segs = []
    for a, b in wires:
        ka, kb = _k(a), _k(b)
        ra, rb = find(ka), find(kb)
        if ra != rb:
            par[ra] = rb
        segs.append((ka, kb))

    def on(k):
        for ka, kb in segs:
            if min(ka[0], kb[0]) <= k[0] <= max(ka[0], kb[0]) and min(ka[1], kb[1]) <= k[1] <= max(ka[1], kb[1]) \
                    and (ka[0] == kb[0] == k[0] or ka[1] == kb[1] == k[1]):
                ra, rb = find(k), find(ka)
                if ra != rb:
                    par[ra] = rb
    for ka, kb in segs:
        on(ka)
        on(kb)
    for j in juncs:
        on(_k(j))
    return find, on


def power_ways(wires, juncs, syms) -> dict:
    """How every supply / ground symbol is reached from the pin it serves (package A, power conventions): the wire is
    followed from the symbol to the first part pin, junction or tee.
      power_hooks       more than one bend on that way, or a way that turns back on itself (an arch, a "candy cane").
                        A top pin that is ground (bottom pin that is a supply) may leave by "stub, sideways, down":
                        those two bends are the drawing the convention asks for and are counted in power_top_stubs
      power_wrong_side  ground symbol ABOVE the pin it serves / supply symbol BELOW it
    Sideways symbols are `power_sideways` (measure)."""
    wires = [(_k(a), _k(b)) for a, b in wires]
    juncs = {_k(j) for j in juncs}
    pin_at, powers = {}, []
    for s in syms:
        pw = s.pins_world()
        if s.is_power:
            if pw and not O.is_flag(s):
                powers.append((s, _k(pw[0]["xy"])))
        else:
            for p in pw:
                pin_at[_k(p["xy"])] = (int(p["out"][0]), int(p["out"][1]))
    ends: dict = {}
    for a, b in wires:
        ends.setdefault(a, []).append(b)
        ends.setdefault(b, []).append(a)

    def inner(pt):
        return any(pt not in (a, b) and min(a[0], b[0]) - 0.01 <= pt[0] <= max(a[0], b[0]) + 0.01
                   and min(a[1], b[1]) - 0.01 <= pt[1] <= max(a[1], b[1]) + 0.01 for a, b in wires)

    def follow(start):
        path, cur, prev = [start], start, None
        while len(path) < 40:
            if cur in pin_at:
                return path, True
            nxt = [q for q in ends.get(cur, ()) if q != prev]
            if cur in juncs or len(nxt) != 1 or (inner(cur) and cur != start):
                # a tee on the bar of two neighbouring pins: the way goes straight on into the pin below / above it
                if prev is not None:
                    for q in nxt:
                        if q in pin_at and (abs(q[0] - cur[0]) < 0.01) == (abs(prev[0] - cur[0]) < 0.01)                                 and (q[0] - cur[0]) * (cur[0] - prev[0]) >= 0 and (q[1] - cur[1]) * (cur[1] - prev[1]) >= 0:
                            return path + [q], True
                return path, False
            a, b = cur, nxt[0]
            mid = [p for p in pin_at if p not in (a, b) and min(a[0], b[0]) - 0.01 <= p[0] <= max(a[0], b[0]) + 0.01
                   and min(a[1], b[1]) - 0.01 <= p[1] <= max(a[1], b[1]) + 0.01]
            if mid:
                mid.sort(key=lambda p: abs(p[0] - a[0]) + abs(p[1] - a[1]))
                return path + [mid[0]], True
            prev, cur = cur, nxt[0]
            path.append(cur)
        return path, False
    hooks = wrong = stubs = 0
    for s, at in powers:
        gnd = LAY.is_ground(str(s.value))
        path, at_pin = follow(at)
        dirs = []
        for a, b in zip(path, path[1:]):
            d = (0 if abs(a[0] - b[0]) < 0.01 else (1 if b[0] > a[0] else -1),
                 0 if abs(a[1] - b[1]) < 0.01 else (1 if b[1] > a[1] else -1))
            if not dirs or dirs[-1] != d:
                dirs.append(d)
        bends = max(0, len(dirs) - 1)
        back = any(d1[0] == -d2[0] and d1[1] == -d2[1] for i, d1 in enumerate(dirs) for d2 in dirs[i + 1:])
        end = path[-1]
        out = pin_at.get(end) if at_pin else None
        # pin pointing up that is ground / down that is a supply: stub, sideways, then to the symbol
        if at_pin and out is not None and out[0] == 0 and (out[1] < 0) == gnd and bends == 2 and len(dirs) == 3                 and dirs[1][1] == 0:
            stubs += 1
        elif bends > 1 or back:
            hooks += 1
        if at_pin and end != at and out is not None and out[1] == 0:
            wrong += (at[1] < end[1] - 0.01) if gnd else (at[1] > end[1] + 0.01)
    return {"power_hooks": hooks, "power_wrong_side": wrong, "power_top_stubs": stubs}


def _v2_metrics(sch: Schematic, D: Design, m: dict, wires, juncs, H, V, syms, labels, bodies, texts, idioms) -> None:
    """Score v2 (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 11): the new measurements, the hard zeros and the score."""
    find, on = _wire_groups(wires, juncs)
    parts = max(1, len(D.units))
    # junctions per connected power wire group (a "comb")
    pw_root, flag_root, pin_roots = {}, [], set()
    for s in syms:
        pw = s.pins_world()
        if s.is_power:
            if pw:
                k = _k(pw[0]["xy"])
                on(k)
                if O.is_flag(s):
                    flag_root.append(k)
                else:
                    pw_root[k] = s.value
        else:
            for p in pw:
                k = _k(p["xy"])
                on(k)
                pin_roots.add(k)
    # (class D) the tee of a capacitor on a rail bar or on the ground bar under a row of caps is no tooth of a comb:
    # a bar with its caps hanging on it is the drawing that says which pins the caps serve. One junction per
    # capacitor pin of the group is not counted
    cap_pins: dict = {}
    for s in syms:
        if not s.is_power and prefix(s.ref) == "C":
            pw = s.pins_world()
            if len(pw) == 2:
                for p in pw:
                    r = find(_k(p["xy"]))
                    cap_pins[r] = cap_pins.get(r, 0) + 1
    pin_roots = {find(k) for k in pin_roots}
    power_groups = {find(k) for k in pw_root}
    jc: dict = {}
    for j in juncs:
        r = find(_k(j))
        if r in power_groups:
            jc[r] = jc.get(r, 0) + 1
    m["max_junctions_on_one_power_net"] = max((max(0, n - cap_pins.get(r, 0)) for r, n in jc.items()), default=0)
    m["flag_islands"] = sum(1 for k in flag_root if find(k) not in pin_roots)
    # labels
    named = sum(len(ln.names) for k, ln in D.view.nets.items() if k not in D.rails) + sum(len(o["names"]) for o in D.view.orphans)
    m["labels_per_part"] = round(len(labels) / parts, 2)
    m["labels_floor"] = named
    seen: dict = {}
    for l in labels:
        k = _k(l["at"])
        on(k)
        key = (find(k), l["net"], l["kind"])
        seen[key] = seen.get(key, 0) + 1
    m["labels_redundant"] = sum(n - 1 for n in seen.values() if n > 1)
    # crossings next to a junction
    near = 0
    for x0, x1, y in H:
        for y0, y1, x in V:
            if x0 + 0.01 < x < x1 - 0.01 and y0 + 0.01 < y < y1 - 0.01 and (round(x, 2), round(y, 2)) not in juncs:
                if any(max(abs(j[0] - x), abs(j[1] - y)) < 2.53 for j in juncs):
                    near += 1
    m["crossing_near_junction"] = near
    # bends
    ends: dict = {}
    for a, b in wires:
        hz = abs(a[1] - b[1]) < 1e-6
        for p in (a, b):
            ends.setdefault(_k(p), []).append(hz)
    bends = sum(1 for k, v in ends.items() if len(v) == 2 and v[0] != v[1])
    roots = {find(_k(a)) for a, _b in wires}
    m["bends_per_wire"] = round(bends / max(1, len(roots)), 2)
    # LEDs drawn against the flow (anode right of / below the cathode), staggered pairs of 2-pin parts
    rtl = 0
    two = {}
    for s in syms:
        if s.is_power:
            continue
        pw = s.pins_world()
        if len({p["number"] for p in pw}) == 2 and len(D.units.get(s.ref, ())) == 1:
            two[s.ref] = {p["number"]: p for p in pw}
            name = s.lib_id.split(":")[-1].upper()
            if prefix(s.ref) in ("D", "LED") and ("LED" in name or s.value.upper().startswith("LED")):
                an = next((p for p in pw if str(p.get("name", "")).upper() in ("A", "ANODE")), None)
                ka = next((p for p in pw if str(p.get("name", "")).upper() in ("K", "C", "CATHODE")), None)
                if an and ka:
                    dx, dy = ka["xy"][0] - an["xy"][0], ka["xy"][1] - an["xy"][1]
                    if (abs(dx) >= abs(dy) and dx < 0) or (abs(dy) > abs(dx) and dy < 0):
                        rtl += 1
    m["led_against_flow"] = rtl
    stag = 0
    refs2 = sorted(two)
    for i in range(len(refs2)):
        for j in range(i + 1, len(refs2)):
            a, b = refs2[i], refs2[j]
            for pa, na in D.pins_of.get(a, {}).items():
                for pb, nb in D.pins_of.get(b, {}).items():
                    if na == nb and D.conn(na) and not D.is_rail(na) and pa in two[a] and pb in two[b]:
                        qa, qb = two[a][pa]["xy"], two[b][pb]["xy"]
                        dx, dy = abs(qa[0] - qb[0]), abs(qa[1] - qb[1])
                        if 0.01 < min(dx, dy) < 5.08 and max(dx, dy) < 40:
                            stag += 1
    m["staggered_pairs"] = stag
    # flow, connectors, frames, border
    fr = D._frames()
    cx = {}
    for b in D.blocks:
        if b.id in fr:
            cx[b.id] = (fr[b.id][0] + fr[b.id][2]) / 2
        else:
            xs = [s.at[0] for r in b.anchors + b.sats for s in D.units.get(r, [])]
            cx[b.id] = sum(xs) / len(xs) if xs else 0.0
    fv = 0
    flow = getattr(D, "flow", {})

    def stacked(a, b):
        # (package D) two frames of ONE column (one above the other, their widths overlap by more than half of the
        # narrower one): the signal runs down the column there. A frame that keeps its own width - it hugs its
        # drawing - and stands on the right edge of its column is not "right of" the wider frame under it
        if a not in fr or b not in fr:
            return False
        fa, fb_ = fr[a], fr[b]
        return min(fa[2], fb_[2]) - max(fa[0], fb_[0]) > 0.5 * min(fa[2] - fa[0], fb_[2] - fb_[0])
    for (a, b), w in flow.items():
        if w > flow.get((b, a), 0) and a in cx and b in cx and cx[a] > cx[b] + 1.0 and not stacked(a, b):
            fv += 1
    m["flow_violations"] = fv
    conns = [r for r in D.units if D.is_conn(r) and D.npins(r) >= 2]
    if conns and fr:
        lo, hi = min(f[0] for f in fr.values()), max(f[2] for f in fr.values())
        def frame_of(r):
            # the frame the connector is DRAWN in (a debug header may stand in a frame of its own at the sheet's
            # edge although the analysis counts it to the IC it serves); else the frame of its block
            at = D.units[r][0].at
            f = next((f for f in fr.values() if f[0] <= at[0] <= f[2] and f[1] <= at[1] <= f[3]), None)
            return f if f is not None else fr.get(D.block_of.get(r))
        edge = sum(1 for r in conns if frame_of(r) is not None
                   and (frame_of(r)[0] <= lo + 1.0 or frame_of(r)[2] >= hi - 1.0))
        m["connector_edge_ratio"] = round(edge / len(conns), 2)
    else:
        m["connector_edge_ratio"] = 1.0
    cols: list = []
    for f in sorted(fr.values()):
        c = next((c for c in cols if f[0] < c["x1"] - 1.0 and c["x0"] < f[2] - 1.0), None)
        if c is None:
            cols.append({"x0": f[0], "x1": f[2], "fr": [f]})
        else:
            c["x0"], c["x1"] = min(c["x0"], f[0]), max(c["x1"], f[2])
            c["fr"].append(f)
    m["frame_ragged"] = sum(len({round(f[0], 1) for f in c["fr"]}) - 1 + len({round(f[2] - f[0], 1) for f in c["fr"]}) - 1
                            for c in cols)
    w, h = sch.page_size()
    boxes = [b for _, b in bodies] + [b for _, b in texts] + list(fr.values())
    if boxes:
        m["border_min_mm"] = round(min(min(b[0] for b in boxes) - 10.0, min(b[1] for b in boxes) - 10.0,
                                       w - 10.0 - max(b[2] for b in boxes), h - 10.0 - max(b[3] for b in boxes)), 1)
    else:
        m["border_min_mm"] = 0.0
    m.update(page_use(w, h, list(fr.values()), boxes))
    m["notes_count"] = max(0, len(sch.items("text")) - m["frames"])
    tb = sexpr.find(sch.tree, "title_block") or []
    m["title_fields"] = sum(1 for e in tb[1:] if isinstance(e, list) and len(e) > 1 and str(e[-1]).strip()
                            and str(e[0]) in ("title", "date", "rev", "company", "comment"))
    m["title_rails"] = int(any(isinstance(e, list) and str(e[0]) == "comment" and str(e[-1]).startswith("Rails:") for e in tb[1:]))
    if idioms is None:
        from .idioms.match import match_design
        md = match_design(D)
        idioms = md["claimable"] / max(1, md["parts"])
        m["idiom_coverage_kind"] = "matchable"
    else:
        m["idiom_coverage_kind"] = "drawn"
    m["idiom_coverage"] = round(float(idioms), 3)
    hard = [f"{k}={m[k]}" for k in HARD_ZEROS if m.get(k)]
    if m["border_min_mm"] < EX.BORDER_MIN_MM:
        hard.append(f"border_min_mm={m['border_min_mm']}")
    m["hard"] = hard
    pen = {
        "wiring": min(25, 60 * max(0.0, EX.WIRED_LOCAL_MIN - m["wired_local_ratio"])),
        "label_density": min(12, 6 * max(0.0, m["labels_per_part"] - EX.LABELS_PER_PART["p50"]) / EX.LABEL_IQR),
        "idiom_coverage": min(8, 20 * max(0.0, 0.6 - m["idiom_coverage"])),
        # (class D) a cap that hangs on the wire or rail bar of the supply pin it serves is where it belongs however
        # long the bar is: only the caps that a power symbol alone ties to their IC are judged by their distance
        "decap_distance": min(10, 0.8 * max(0.0, m.get("decap_dist_score_mm", m["decap_dist_mean_mm"]) - EX.DECAP_DIST_MM)),
        "satellite_distance": min(6, 0.4 * max(0.0, m["sat_dist_mean_mm"] - EX.SAT_DIST_MM)),
        "flow": min(9, 3 * (m["flow_violations"] + m["led_against_flow"])),
        "connector_edges": min(8, 8 * (1 - m["connector_edge_ratio"])),
        "crossings": min(6, 1.5 * m["wire_crossings"]),
        "bends": min(6, 4 * max(0.0, m["bends_per_wire"] - 1.0)),
        "staggered": min(4, 1.0 * m["staggered_pairs"]),
        "fill": min(8, 30 * max(0.0, EX.FILL_MIN - m["sheet_fill"])),
        "frames": min(5, 1.0 * m["frame_ragged"] + 20 * max(0.0, m["off_centre"] - 0.05)),
        # (fix round 5) the rails line of the title block is a design note too: it replaced the free NOTES block
        "notes_title": min(5, (3 if m["notes_count"] + m["title_rails"] < 2 else 0) + (2 if m["title_fields"] < 3 else 0)),
        "gnd_combs": min(4, 1.0 * max(0, m["max_junctions_on_one_power_net"] - EX.MAX_POWER_JUNCTIONS)),
        "hard": min(30, 5 * len(hard)),
    }
    m["penalties"] = {k: round(v, 1) for k, v in pen.items() if v > 0.04}
    m["score_v1"] = m["score"]
    m["score"] = round(max(0.0, min(100.0, 100 - sum(pen.values()))), 1)


_SUPPLY = re.compile(r"^(VIN|IN|VI|VCC|VDD|VDDA|VDDIO|AVDD|DVDD|VBAT|VBUS|V\+|VS|VM|PVIN|AVIN|VCCA|VCCB|3V3|5V)\d*$", re.I)


def decap_metrics(D: Design, syms, grp_of: dict, pos: dict, pin_xy: dict, fr_map: dict, groups_all=None) -> dict:
    """Class D (decoupling): does the drawing say which pin a capacitor serves?
      decap_detached        decoupling caps (rail - ground, counted to an IC that has a pin on that rail) whose rail
                            pin is joined to NO pin of that IC by wires - a power symbol of the same name is all that
                            says where they belong (`decap_detached_list` names them, `decaps` = how many were looked at)
      decap_groups_detached the wire groups those caps form (a detached row of three is one group)
      decap_rows_above      the most cap rows (distinct heights of the caps that stand above their IC) of one IC
      rail_symbol_repeats   supply symbols beyond the first of one rail in one frame (ground symbols are not counted:
                            one per pin group is the convention); `rail_symbols` = all supply symbols
      decap_rail_symbol_repeats   supply symbols beyond the first on the wires of one (IC, rail) and its decoupling
                            caps: every cap group on a symbol of its own and every pin on a symbol of its own
                            counts (`decap_rail_symbols` = all of them)"""
    det, groups, looked, rows = [], set(), 0, {}
    for s, r in sorted(D.role.items()):
        a, via = r.get("anchor"), r.get("via")
        po = D.pins_of.get(s, {})
        if r.get("kind") != "decap" or prefix(s) != "C" or len(po) != 2 or a not in D.pins_of or s not in pos:
            continue
        if not D.is_rail(via) or D.is_gnd(via) or not any(D.is_gnd(k) for k in po.values()):
            continue
        aps = [(a, p) for p, n in D.pins_of[a].items() if n == via]
        cp = next(((s, p) for p, n in po.items() if n == via), None)
        if not aps or cp is None:
            continue
        looked += 1
        g = grp_of.get(cp)
        if g is None or not any(grp_of.get(q) == g for q in aps):
            # ... unless it is wired into a circuit of its own (the output cap at the inductor of a buck stage)
            if g is not None and any(gq == g and q[0] != s and prefix(q[0]) != "C" for q, gq in grp_of.items()):
                continue
            det.append(s)
            groups.add(g if g is not None else s)
        ys = [pin_xy[q][1] for q in pin_xy if q[0] == a]
        if ys and pos[s][1] < min(ys) - 0.01:
            rows.setdefault(a, set()).add(round(pos[s][1], 1))

    def frame_at(at):
        for bid, f in fr_map.items():
            if f[0] <= at[0] <= f[2] and f[1] <= at[1] <= f[3]:
                return bid
        return None
    cnt: dict = {}
    for s in syms:
        if s.is_power and not O.is_flag(s) and not LAY.is_ground(str(s.value)):
            key = (frame_at(s.at), str(s.value))
            cnt[key] = cnt.get(key, 0) + 1
    # supply symbols that belong to the decoupling of one IC rail: on a wire group that holds a decoupling cap of
    # (IC, rail) or a pin of that IC on the rail. One is the drawing asked for; every further one is a repeat
    sym_grp = {}
    for gi, gr in enumerate(groups_all or []):
        for ps in gr.get("power") or []:
            if not LAY.is_ground(str(ps.value)):
                sym_grp[gi] = sym_grp.get(gi, 0) + 1
    per: dict = {}
    for s, r in D.role.items():
        a, via = r.get("anchor"), r.get("via")
        po = D.pins_of.get(s, {})
        if r.get("kind") != "decap" or prefix(s) != "C" or len(po) != 2 or a not in D.pins_of:
            continue
        if not D.is_rail(via) or D.is_gnd(via) or not any(n == via for n in D.pins_of[a].values()):
            continue
        gs = per.setdefault((a, via), set())
        # (the SUPPLY pins of the IC: an enable or reset input that is merely strapped to the rail on another side
        # of the part keeps a symbol of its own by convention)
        for q in [(s, p) for p, n in po.items() if n == via] + [(a, p) for p, n in D.pins_of[a].items() if n == via
                                                                 and ("power_in" in D.ptypes.get((a, p), "")
                                                                      or any(_SUPPLY.match(t) for t in
                                                                             str(D.pin_name.get((a, p), "")).split("/")))]:
            if grp_of.get(q) is not None:
                gs.add(grp_of[q])
    drs = {k: sum(sym_grp.get(g, 0) for g in gs) for k, gs in per.items()}
    return {"decaps": looked, "decap_detached": len(det), "decap_detached_list": det[:24],
            "decap_rail_symbols": sum(drs.values()), "decap_rail_symbol_repeats": sum(max(0, n - 1) for n in drs.values()),
            "decap_groups_detached": len(groups),
            "decap_rows_above": max((len(v) for v in rows.values()), default=0),
            "rail_symbols": sum(cnt.values()), "rail_symbol_repeats": sum(n - 1 for n in cnt.values())}

_CC_PIN = re.compile(r"^CC\d*$", re.I)
_SENSE_PIN = re.compile(r"^(FB|VFB|SENSE|VSENSE|VOS|VOUT_?S|OUT_?S|ADJ)$", re.I)
NEAR_PIN_MM = 12.7


def idiom_metrics(D: Design, wires, juncs, syms) -> dict:
    """Class I (idioms): crossings INSIDE the drawings that have one canonical form, measured on the finished sheet.
      crystal_crossings   wire crossings in which one wire belongs to a crystal: its two oscillator nets (through a
                          series resistor up to the IC pin) or the ground wires of the crystal's case pins / load caps
      crystal_grounds_max the most ground symbols one crystal with its load caps ends in (the pi drawing has one)
      usbc_crossings      crossings in which one wire is a ground / supply / CC wire of a USB receptacle (a connector
                          with CC pins), the return of its CC pull-downs included. usbc_pair_crossings = D+ over D- at
                          the doubled contacts of the receptacle (the pin order of the symbol; reported, not counted)
      power_x_signal      a supply / ground wire crossing a signal wire within NEAR_PIN_MM of a pin of a part with five
                          or more pins
      sense_hidden        a regulator's feedback / sense pin that sits on a supply rail and is drawn with a supply
                          symbol of its own instead of a wire to the node it senses
      idiom_crossings     crystal_crossings + usbc_crossings (the acceptance number of class I: 0)"""
    find, on = _wire_groups(wires, juncs)
    kind, pins_at, big_tips = {}, {}, []
    gnd_syms: dict = {}
    for s in syms:
        pw = s.pins_world()
        if s.is_power:
            if pw and not O.is_flag(s):
                k = _k(pw[0]["xy"])
                on(k)
                pins_at.setdefault(k, []).append(("#", "gnd" if LAY.is_ground(str(s.value)) else "rail"))
            continue
        npin = D.npins(s.ref) if s.ref in D.units else len(pw)
        for p in pw:
            k = _k(p["xy"])
            on(k)
            pins_at.setdefault(k, []).append((s.ref, p["number"]))
            if npin >= 5:
                big_tips.append(p["xy"])
    root_pins: dict = {}
    for k, lst in pins_at.items():
        r = find(k)
        for ref, no in lst:
            if ref == "#":
                kind[r] = no
                if no == "gnd":
                    gnd_syms[r] = gnd_syms.get(r, 0) + 1
            else:
                root_pins.setdefault(r, set()).add((ref, no))
    for r, ps in root_pins.items():
        if r not in kind:
            n = next((D.pins_of.get(ref, {}).get(no) for ref, no in sorted(ps, key=str)), None)
            kind[r] = ("gnd" if D.is_gnd(n) else "rail") if (n is not None and D.is_rail(n)) else "signal"
    root_of = {q: r for r, ps in root_pins.items() for q in ps}

    def two(r):
        return len(D.pins_of.get(r, {})) == 2 and len(D.units.get(r, ())) == 1
    host_refs = {r for r in D.units if not D.is_conn(r) and D.npins(r) >= 5}
    xtal_roots: dict = {}                     # root -> crystal ref
    xtal_gnd: dict = {}
    for y in D.units:
        po = D.pins_of.get(y, {})
        if prefix(y) not in ("Y", "X") or not 2 <= len(po) <= 4 or D.is_conn(y):
            continue
        sig = {k for k in po.values() if D.conn(k) and not D.is_rail(k)}
        if len(sig) != 2:
            continue
        mine, gr = set(), set()
        for p in po:
            if (y, p) in root_of:
                mine.add(root_of[(y, p)])
                if D.is_gnd(po[p]):
                    gr.add(root_of[(y, p)])
        for k in sig:
            for ref, no in D.view.nets[k].pins:
                if (ref, no) in root_of:
                    mine.add(root_of[(ref, no)])
                if ref != y and two(ref) and prefix(ref) in ("C", "R"):
                    for p2, k2 in D.pins_of[ref].items():
                        if k2 == k or (ref, p2) not in root_of:
                            continue
                        if prefix(ref) == "C" and D.is_gnd(k2):
                            mine.add(root_of[(ref, p2)])
                            gr.add(root_of[(ref, p2)])
                        elif prefix(ref) == "R" and D.conn(k2) and not D.is_rail(k2) \
                                and any(r2 in host_refs for r2, _p in D.view.nets[k2].pins):
                            mine.add(root_of[(ref, p2)])
        for r in mine:
            xtal_roots[r] = y
        xtal_gnd[y] = sum(gnd_syms.get(r, 0) for r in gr)
    usb_roots: dict = {}
    usb_pair: dict = {}
    for j in D.units:
        if not D.is_conn(j):
            continue
        po = D.pins_of.get(j, {})
        cc = [p for p in po if _CC_PIN.match(str(D.pin_name.get((j, p), "")))]
        if len(cc) < 2:
            continue
        for p, k in po.items():
            if (j, p) not in root_of or not D.conn(k):
                continue
            if D.is_rail(k) or p in cc:
                usb_roots[root_of[(j, p)]] = j
            else:
                usb_pair[root_of[(j, p)]] = j
        for p in cc:
            k = po[p]
            if not D.conn(k) or D.is_rail(k):
                continue
            for ref, no in D.view.nets[k].pins:
                if (ref, no) in root_of:
                    usb_roots[root_of[(ref, no)]] = j
                if ref != j and two(ref):
                    for p2 in D.pins_of[ref]:
                        if (ref, p2) in root_of and D.is_rail(D.pins_of[ref][p2]):
                            usb_roots[root_of[(ref, p2)]] = j
    H = [(min(a[0], b[0]), max(a[0], b[0]), a[1], find(_k(a))) for a, b in wires if abs(a[1] - b[1]) < 1e-6]
    V = [(min(a[1], b[1]), max(a[1], b[1]), a[0], find(_k(a))) for a, b in wires if abs(a[0] - b[0]) < 1e-6]
    xc = uc = up = pxs = 0
    lst = []
    for x0, x1, y, rh in H:
        for y0, y1, x, rv in V:
            if not (x0 + 0.01 < x < x1 - 0.01 and y0 + 0.01 < y < y1 - 0.01) or (round(x, 2), round(y, 2)) in juncs:
                continue
            kh, kv = kind.get(rh, "signal"), kind.get(rv, "signal")
            tags = []
            if rh in xtal_roots or rv in xtal_roots:
                xc += 1
                tags.append("crystal " + (xtal_roots.get(rh) or xtal_roots.get(rv)))
            if rh in usb_roots or rv in usb_roots:
                uc += 1
                tags.append("usbc " + (usb_roots.get(rh) or usb_roots.get(rv)))
            elif rh in usb_pair and rv in usb_pair:
                up += 1
                tags.append("usbc pair " + usb_pair[rh])
            if (kh == "signal") != (kv == "signal") and any(max(abs(t[0] - x), abs(t[1] - y)) <= NEAR_PIN_MM for t in big_tips):
                pxs += 1
                tags.append("power_x_signal")
            if tags:
                lst.append(f"{' + '.join(tags)} @ {round(x, 2)},{round(y, 2)} ({kh} x {kv})")
    hidden = []
    for (ref, no), nm in sorted(D.pin_name.items(), key=lambda t: (str(t[0][0]), str(t[0][1]))):
        k = D.pins_of.get(ref, {}).get(no)
        if not _SENSE_PIN.match(str(nm)) or k is None or not D.is_rail(k) or D.is_gnd(k) or (ref, no) not in root_of:
            continue
        if not any(q[0] != ref for q in root_pins.get(root_of[(ref, no)], ())):
            hidden.append(f"{ref}.{no}")
    return {"crystal_crossings": xc, "crystals": len(xtal_gnd), "crystal_grounds_max": max(xtal_gnd.values(), default=0),
            "usbc_crossings": uc, "usbc_pair_crossings": up, "power_x_signal": pxs, "sense_hidden": len(hidden),
            "sense_hidden_list": hidden[:12], "idiom_crossings": xc + uc, "idiom_crossing_list": lst[:40]}


def measure(sch: Schematic, nl: dict, design: Design | None = None, idioms: float | None = None) -> dict:
    """Readability metrics. `score` is score v2 (exemplar-calibrated, hard violations listed in `hard`); the earlier
    composite is kept as `score_v1`. `idioms` = share of parts the engine drew from a proven template (the engine
    passes it); without it the share of parts the matcher CAN claim is reported (idiom_coverage_kind)."""
    D = design or Design(sch, nl)
    wires = [(tuple(w["a"]), tuple(w["b"])) for w in sch.wires()]
    juncs = set()
    for j in sch.items("junction"):
        a = sexpr.find(j, "at")
        juncs.add((round(float(a[1]), 2), round(float(a[2]), 2)))
    H = [(min(a[0], b[0]), max(a[0], b[0]), a[1]) for a, b in wires if abs(a[1] - b[1]) < 1e-6]
    V = [(min(a[1], b[1]), max(a[1], b[1]), a[0]) for a, b in wires if abs(a[0] - b[0]) < 1e-6]
    crossings = 0
    for x0, x1, y in H:
        for y0, y1, x in V:
            if x0 + 0.01 < x < x1 - 0.01 and y0 + 0.01 < y < y1 - 0.01 and (round(x, 2), round(y, 2)) not in juncs:
                crossings += 1
    wl = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in wires)
    syms = sch.symbols()
    bodies, texts = [], []
    for s in syms:
        bodies.append((s.ref, graphic_box(sch, s)))
        for fb in s.field_boxes():
            texts.append((s.ref, fb))
    labels = sch.labels()
    for l in labels:
        texts.append(("label:" + l["net"], LAY.label_box(l)))
    ov = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if _hit(texts[i][1], texts[j][1]):
                ov.append(["text-text", texts[i][0], texts[j][0]])
    for tr, tb in texts:
        for br, bb in bodies:
            if tr != br and _hit(tb, bb):
                ov.append(["text-body", tr, br])
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            if _hit(bodies[i][1], bodies[j][1]):
                ov.append(["body-body", bodies[i][0], bodies[j][0]])
    wire_hits = 0
    # a wire that STARTS on a pin tip of a part and runs on in that pin's direction leaves the part - it is not a
    # wire through it, even where the symbol's graphics (LED arrows, a wide diode bar) reach beyond the pin tip
    leaves = set()
    for s in syms:
        if not s.is_power:
            for p in s.pins_world():
                leaves.add((s.ref, round(p["xy"][0], 2), round(p["xy"][1], 2), int(p["out"][0]), int(p["out"][1])))

    def _leaves(ref, a, b):
        for p, q in ((a, b), (b, a)):
            dx, dy = q[0] - p[0], q[1] - p[1]
            o = (0 if abs(dx) < 1e-6 else (1 if dx > 0 else -1), 0 if abs(dy) < 1e-6 else (1 if dy > 0 else -1))
            if (ref, round(p[0], 2), round(p[1], 2), o[0], o[1]) in leaves:
                return True
        return False
    for a, b in wires:
        wb = [min(a[0], b[0]) - 0.05, min(a[1], b[1]) - 0.05, max(a[0], b[0]) + 0.05, max(a[1], b[1]) + 0.05]
        for tr, tb in texts:
            if _hit(wb, tb, 0.2):
                wire_hits += 1
        for br, bb in bodies:
            if not br.startswith("#") and _hit(wb, bb, 0.3) and not _leaves(br, a, b):
                wire_hits += 1
    grp_of = {}
    geo_ = O.geo_groups(sch)
    for gi, g in enumerate(geo_):
        for p in g["pins"]:
            grp_of[p] = gi
    lp = lw = 0
    nsig = 0
    for k, ln in D.view.nets.items():
        if k in D.rails or not D.conn(k):
            continue
        nsig += 1
        ps = ln.pins
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                if D.block_of.get(ps[i][0]) == D.block_of.get(ps[j][0]):
                    lp += 1
                    if grp_of.get(ps[i]) is not None and grp_of.get(ps[i]) == grp_of.get(ps[j]):
                        lw += 1
    # class L (label-only links): two parts drawn in ONE frame whose shared signal net is joined by nothing but its
    # name. label_only_links = wires missing inside frames (per net and frame: wire groups - 1);
    # label_only_part_links = the same without the links between two pins of ONE part (the acceptance number);
    # label_only_pairs = pairs of parts of one frame that share signal nets of which not one is wired between them
    fr_map = D._frames()

    def _frame_of(r):
        us = D.units.get(r) or []
        at = us[0].at if us else None
        if at is not None:
            for bid, f in fr_map.items():
                if f[0] <= at[0] <= f[2] and f[1] <= at[1] <= f[3]:
                    return bid
        return D.block_of.get(r)
    lo_links, lo_pair, lo_nets, lo_part = 0, {}, [], 0
    for k, ln in D.view.nets.items():
        if k in D.rails or not D.conn(k):
            continue
        by: dict = {}
        for q in ln.pins:
            by.setdefault(_frame_of(q[0]), []).append(q)
        for _fid, qs in by.items():
            groups: dict = {}
            for q in qs:
                groups.setdefault(grp_of.get(q, ("solo", q)), set()).add(q[0])
            gl = list(groups.values())
            lo_links += len(gl) - 1
            # ... of which between DIFFERENT parts: a group that is just another pin of a part that is already on
            # another group (the far side of a pass-through TVS array, the second drain of a dual FET) is no link
            # between two parts - no placement of the parts changes it
            real = [g for i, g in enumerate(gl)
                    if not any(j != i and g <= h and (g < h or j < i) for j, h in enumerate(gl))]
            lo_part += max(0, len(real) - 1)
            if len(gl) > 1:
                lo_nets.append(f"{D.text(k)}({'|'.join(','.join(sorted(g)) for g in gl)})")
            for i in range(len(gl)):
                for ra in gl[i]:
                    for rb in gl[i]:
                        if ra < rb:
                            lo_pair.setdefault((ra, rb), [0, 0])[1] += 1
                    for j in range(i + 1, len(gl)):
                        for rb in gl[j]:
                            if ra != rb and rb not in gl[i]:
                                lo_pair.setdefault((min(ra, rb), max(ra, rb)), [0, 0])[0] += 1
    lo_only = sorted((a, b, n[0]) for (a, b), n in lo_pair.items() if n[0] and not n[1])
    pos, pin_xy = {}, {}
    for s in syms:
        if s.ref.startswith("#"):
            continue
        pos.setdefault(s.ref, s.at)
        for p in s.pins_world():
            pin_xy[(s.ref, p["number"])] = p["xy"]
    dists, decap, decap_s = [], [], []
    anchors_ = {a for b in D.blocks for a in b.anchors}
    for s, r in D.role.items():
        cand = [pin_xy[(r["anchor"], p)] for p, n in D.pins_of[r["anchor"]].items()
                if n == r["via"] and (r["anchor"], p) in pin_xy]
        if r.get("kind") in ("pullup", "pulldown") and r["via"] not in D.rails:
            # a pull-up serves its NET: drawn at the pin of another IC / connector on that net (engine._rehome: the
            # I2C pull-ups at the sensor when the MCU's rows are taken) it stands where it belongs, too
            cand += [pin_xy[q] for q in D.view.nets[r["via"]].pins if q[0] in anchors_ and q in pin_xy]
        if not cand and r.get("chain") in pos:
            cand = [pos[r["chain"]]]
        if not cand or s not in pos:
            continue
        d = min(abs(pos[s][0] - c[0]) + abs(pos[s][1] - c[1]) for c in cand)
        dists.append(d)
        if r["kind"] == "decap":
            decap.append(d)
            g_ = next((grp_of.get((s, p)) for p, n in D.pins_of.get(s, {}).items() if n == r["via"]), None)
            wired = g_ is not None and any(grp_of.get((r["anchor"], p)) == g_
                                           for p, n in D.pins_of[r["anchor"]].items() if n == r["via"])
            decap_s.append(min(d, EX.DECAP_DIST_MM) if wired else d)
    dm = decap_metrics(D, syms, grp_of, pos, pin_xy, fr_map, geo_)
    w, h = sch.page_size()
    boxes = [b for _, b in bodies] + [b for _, b in texts]
    used = (max(b[2] for b in boxes) - min(b[0] for b in boxes)) * (max(b[3] for b in boxes) - min(b[1] for b in boxes)) if boxes else 0
    offgrid = sum(1 for s in syms for p in s.pins_world()
                  if abs(p["xy"][0] / U - round(p["xy"][0] / U)) > 0.01 or abs(p["xy"][1] / U - round(p["xy"][1] / U)) > 0.01)
    pw = [s for s in syms if s.is_power and not O.is_flag(s)]
    upright = 0
    for s in pw:
        pins = s.pins_world()
        bb = s.body_bbox()
        if pins:
            dy = (bb[1] + bb[3]) / 2 - pins[0]["xy"][1]
            dx = (bb[0] + bb[2]) / 2 - pins[0]["xy"][0]
            gnd = LAY.is_ground(s.value)
            if abs(dy) >= abs(dx) and ((dy > 0) == gnd):
                upright += 1
    vertical_labels = sum(1 for l in labels if int(round(float(l.get("rot", 0) or 0))) % 180 == 90)
    fr = list(D._frames().values())
    if fr:
        cx = (min(f[0] for f in fr) + max(f[2] for f in fr)) / 2
        cy = (min(f[1] for f in fr) + max(f[3] for f in fr)) / 2
        frame_fill = (max(f[2] for f in fr) - min(f[0] for f in fr)) * (max(f[3] for f in fr) - min(f[1] for f in fr))             / max(1.0, (w - 25) * (h - 25))
        off_centre = round(max(abs(cx - w / 2) / w, abs(cy - h / 2) / h), 3)
    else:
        frame_fill, off_centre = 0.0, 0.0
    m = {"wire_crossings": crossings, "labels": len(labels), "labels_per_signal_net": round(len(labels) / max(1, nsig), 2),
         "power_symbols": len(pw), "power_upright": upright,
         "overlaps": len(ov), "overlap_list": ov[:12], "wire_through_text_or_body": wire_hits,
         "wire_length_mm": round(wl, 1), "wire_segments": len(wires),
         "local_pairs": lp, "local_pairs_wired": lw, "wired_local_ratio": round(lw / lp, 3) if lp else 1.0,
         "label_only_links": lo_links, "label_only_part_links": lo_part, "label_only_pairs": len(lo_only),
         "label_only_pair_list": [f"{a}-{b}:{n}" for a, b, n in lo_only[:24]],
         "label_only_link_list": sorted(lo_nets)[:40],
         "sat_dist_mean_mm": round(sum(dists) / len(dists), 1) if dists else 0.0,
         "sat_dist_max_mm": round(max(dists), 1) if dists else 0.0,
         "decap_dist_mean_mm": round(sum(decap) / len(decap), 1) if decap else 0.0,
         "decap_dist_score_mm": round(sum(decap_s) / len(decap_s), 1) if decap_s else 0.0,
         "sheet_fill": round(used / max(1.0, (w - 25) * (h - 25)), 3), "paper": _paper_name(sch), "offgrid_pins": offgrid,
         "frames": frame_count(sch), "blocks": len(D.blocks),
         "power_sideways": len(pw) - upright, "labels_vertical": vertical_labels,
         "frame_fill": round(frame_fill, 3), "off_centre": off_centre}
    m.update(dm)
    pen = (min(30, 5 * m["overlaps"]) + min(10, 2 * m["wire_through_text_or_body"]) + min(15, 2 * crossings)
           + min(20, 20 * (1 - m["wired_local_ratio"])) + min(15, 0.5 * max(0.0, m["sat_dist_mean_mm"] - 15))
           + min(10, 4 * max(0.0, m["labels_per_signal_net"] - 1.0)) + min(5, offgrid)
           + (5 if m["sheet_fill"] < 0.25 else 0) + (5 if m["frames"] < min(2, m["blocks"]) else 0)
           # what a reviewer sees first: supply arrows lying on their side, names to be read with a tilted head,
           # a drawing pushed into one corner of the sheet
           + min(5, 0.5 * m["power_sideways"]) + min(3, 0.5 * vertical_labels)
           + (3 if fr and off_centre > 0.12 else 0))
    m["score"] = round(100 - pen, 1)
    m.update(power_ways(wires, juncs, syms))
    m.update(idiom_metrics(D, wires, juncs, syms))
    tc = TB.collisions(sch)
    m.update(text_collisions=tc["count"], text_collision_kinds=tc["by_kind"], text_collision_list=tc["list"][:12],
             # positioned by the library symbol and KiCad's drawing rule alone, not by the layout (not in the count):
             lib_pin_text_collisions=tc["lib"], nc_cross_on_own_pin_number=tc["nc_on_number"])
    _v2_metrics(sch, D, m, wires, juncs, H, V, syms, labels, bodies, texts, idioms)
    return m


def _paper_name(sch) -> str:
    from . import page
    return page.paper_of(sch)
