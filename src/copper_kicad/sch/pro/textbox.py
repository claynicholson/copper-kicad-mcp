"""Rendered text boxes of a sheet and the collisions between them ("text hygiene", metric `text_collisions`).

Everything KiCad prints on a sheet is modelled as an INK box - no air added - from KiCad's own drawing rules and the
stroke font's measured advance widths (model.text_width, checked by bench/sch/textcheck.py; the boxes themselves are
checked against a rendering by bench/sch/textboxes.py):

* fields (Reference / Value / any other visible property) of parts and of supply symbols,
* pin numbers and pin names (size, `pin_names (offset ..)`, hidden flags and the inside / outside rule of the
  library symbol),
* net labels, free text (frame titles, notes),
* and what a text may not stand on: wires, junction dots, no-connect crosses, the graphic lines of symbols (pin
  lines included) and frame rectangles.

collisions(sch) lists every pair that touches. Two classes are kept apart and are NOT part of `text_collisions`,
because both members of the pair are positioned by the library symbol and by KiCad's drawing rule alone - nothing the
layout does can move one against the other, and the owner rule forbids redrawing a library symbol:

* `lib`: a pin text on another pin text / on the graphic of the SAME symbol ("GND" over "OD" inside the DW01A,
  stacked USB-C pin numbers),
* `nc_on_number`: a no-connect cross at a pin tip on that same pin's number (the cross is 1.22 mm wide and stands on
  the tip, the number is centred on the pin: on a 2.54 mm pin every number of two or more characters reaches it).

Everything else counts: a text over a text, two texts on one line closer than a word space (they read as one
string), a text on a wire, a junction, a cross, a symbol or a frame border.
"""
from __future__ import annotations

import math
import re

from ... import sexpr
from .. import symbols as G
from ..model import FONT, LABEL_OFFSET, Schematic, _is_hidden, _justify, text_width

PIN_TEXT_GAP = 0.28       # pin line to the number's / outside name's baseline (4 mil margin + pen)
NC_HALF = 0.61            # half width of a no-connect cross (48 mil)
DOT_R = 0.46              # junction dot radius (36 mil diameter)
AIR_TITLE = 2.4           # ... a block title keeps this much air (a part's name right under it reads as a subtitle)
TITLE_SIZE = 1.8          # sheet text of this size or larger is a title
AIR_DOT = 0.15            # ... to a junction dot / a no-connect cross (their own boxes are already square)
AIR_FRAME = 0.8           # ... and this far from a frame border
RUN_GAP = 1.4             # two texts on one line closer than this read as one string (a blank is 0.93 mm wide)
SHRINK = 0.08             # (run-together test) slack on "the two boxes just meet"
AIR_TEXT = 0.6            # two texts closer than this (ink to ink) touch for the eye
AIR_LINE = 0.25           # a text closer than this to a wire / symbol line / frame border / dot / cross stands on it
                          # (KiCad itself draws a label 0.35 mm and a pin number 0.28 mm above their own line)
LINE_K = 1.62             # line pitch of multi-line text in units of its size


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _yes(node, key) -> bool:
    for e in node[1:]:
        if e == key:
            return True
        if isinstance(e, list) and e and e[0] == key:
            return len(e) == 1 or str(e[1]) == "yes"
    return False


def _size(node, default=FONT) -> float:
    eff = sexpr.find(node, "effects") if node is not None else None
    font = sexpr.find(eff, "font") if eff is not None else None
    sz = sexpr.find(font, "size") if font is not None else None
    return _f(sz[1], default) if sz is not None and len(sz) > 1 else default


def _plain(s: str) -> str:
    return re.sub(r"~\{([^}]*)\}", lambda m: m.group(1), str(s))


def field_ink(p, s) -> list:
    """Ink box of one visible field of a placed symbol (sheet coordinates)."""
    a = sexpr.find(p, "at")
    x, y = _f(a[1]), _f(a[2])
    ang = int(round(_f(a[3]))) % 180 if len(a) > 3 else 0
    vertical = (ang == 90) != (s.rot in (90, 270))
    size = _size(p)
    w, h = text_width(str(p[2]), size), size
    just = _justify(p)
    # a mirrored / half-turned symbol flips the side its fields are justified to (KiCad draws the text upright and
    # keeps the anchor)
    m = s.matrix
    flip_h = False and m
    if flip_h:
        just = just.replace("left", "\0").replace("right", "left").replace("\0", "right")
    if vertical:
        x0, x1 = x - h / 2, x + h / 2
        y0, y1 = (y - w, y) if "left" in just else (y, y + w) if "right" in just else (y - w / 2, y + w / 2)
    else:
        x0, x1 = (x, x + w) if "left" in just else (x - w, x) if "right" in just else (x - w / 2, x + w / 2)
        y0, y1 = (y - h, y) if "bottom" in just else (y, y + h) if "top" in just else (y - h / 2, y + h / 2)
    return [x0, y0, x1, y1]


def _lib_text_rules(ls) -> dict:
    pn = sexpr.find(ls, "pin_names") if ls is not None else None
    pnum = sexpr.find(ls, "pin_numbers") if ls is not None else None
    off = 0.508
    if pn is not None:
        o = sexpr.find(pn, "offset")
        if o is not None and len(o) > 1:
            off = _f(o[1], 0.508)
    return {"offset": off, "names_hidden": pn is not None and _yes(pn, "hide"),
            "numbers_hidden": pnum is not None and _yes(pnum, "hide")}


def _pin_nodes(ls, unit, style) -> dict:
    """{number: (name size, number size)} for the pins of one unit."""
    out = {}
    for sub in sexpr.find_all(ls, "symbol"):
        m = re.match(r".*_(\d+)_(\d+)$", str(sub[1]))
        u, st = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        if u not in (0, unit) or st not in (0, style):
            continue
        for p in sexpr.find_all(sub, "pin"):
            nm, nu = sexpr.find(p, "name"), sexpr.find(p, "number")
            if nu is None or len(nu) < 2:
                continue
            out.setdefault(str(nu[1]), []).append((_size(nm), _size(nu)))
    return out


def pin_texts(s) -> list:
    """[(kind 'pinnum' | 'pinname', pin number, text, box)] for the visible pin texts of a placed symbol."""
    ls = s.sch.lib_symbol(s.lib_name)
    if ls is None:
        return []
    rules = _lib_text_rules(ls)
    sizes = _pin_nodes(ls, s.unit, s.body_style)
    seen: dict = {}
    out = []
    for p in s.pins_world():
        if p.get("hidden"):
            continue
        k = seen.get(p["number"], 0)
        seen[p["number"]] = k + 1
        lst = sizes.get(p["number"]) or [(FONT, FONT)]
        ns, us = lst[min(k, len(lst) - 1)]
        ln = _f(p.get("length", 2.54), 2.54)
        ox, oy = p["out"]
        tx, ty = p["xy"]
        ix, iy = tx - ox * ln, ty - oy * ln
        mx, my = (tx + ix) / 2, (ty + iy) / 2
        name = _plain(p.get("name") or "")
        show_name = bool(name) and name != "~" and not rules["names_hidden"] and ns > 0
        show_num = bool(p["number"]) and not rules["numbers_hidden"] and us > 0
        inside = rules["offset"] > 0
        if show_num:
            w = text_width(str(p["number"]), us)
            if oy == 0:
                if inside or not show_name:
                    b = [mx - w / 2, ty - PIN_TEXT_GAP - us, mx + w / 2, ty - PIN_TEXT_GAP]
                else:
                    b = [mx - w / 2, ty + PIN_TEXT_GAP, mx + w / 2, ty + PIN_TEXT_GAP + us]
            else:
                if inside or not show_name:
                    b = [tx - PIN_TEXT_GAP - us, my - w / 2, tx - PIN_TEXT_GAP, my + w / 2]
                else:
                    b = [tx + PIN_TEXT_GAP, my - w / 2, tx + PIN_TEXT_GAP + us, my + w / 2]
            if ln > 0.01:
                out.append(("pinnum", p["number"], str(p["number"]), b))
        if show_name:
            w = text_width(name, ns)
            if inside:
                sx, sy = ix - ox * rules["offset"], iy - oy * rules["offset"]
                ex, ey = sx - ox * w, sy - oy * w
                if oy == 0:
                    b = [min(sx, ex), sy - ns / 2, max(sx, ex), sy + ns / 2]
                else:
                    b = [sx - ns / 2, min(sy, ey), sx + ns / 2, max(sy, ey)]
            elif oy == 0:
                b = [mx - w / 2, ty - PIN_TEXT_GAP - ns, mx + w / 2, ty - PIN_TEXT_GAP]
            else:
                b = [tx - PIN_TEXT_GAP - ns, my - w / 2, tx - PIN_TEXT_GAP, my + w / 2]
            out.append(("pinname", p["number"], name, b))
    return out


def _arc_pts(a, m_, b, n=8):
    ax, ay = a
    bx, by = m_
    cx, cy = b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [a, b]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    t0, t1, t2 = (math.atan2(p[1] - uy, p[0] - ux) for p in (a, m_, b))

    def between(x, lo, hi):
        return (x - lo) % (2 * math.pi) <= (hi - lo) % (2 * math.pi)
    if not between(t1, t0, t2):
        t0, t2 = t2, t0
    span = (t2 - t0) % (2 * math.pi)
    return [(ux + r * math.cos(t0 + span * i / n), uy + r * math.sin(t0 + span * i / n)) for i in range(n + 1)]


def graphic_segments(s) -> list:
    """The drawn lines of a placed symbol in sheet coordinates: [(x0, y0, x1, y1)] - outline segments, circles and
    arcs as polygons, and the pin lines."""
    ls = s.sch.lib_symbol(s.lib_name)
    out = []
    if ls is None:
        return out
    at, m = s.at, s.matrix

    def W(p):
        return tuple(G.to_world(at, m, p))

    def poly(pts, close=False):
        pts = [W(p) for p in pts]
        if close and pts:
            pts.append(pts[0])
        for a, b in zip(pts, pts[1:]):
            out.append((a[0], a[1], b[0], b[1]))
    for sub in sexpr.find_all(ls, "symbol"):
        mm = re.match(r".*_(\d+)_(\d+)$", str(sub[1]))
        u, st = (int(mm.group(1)), int(mm.group(2))) if mm else (0, 0)
        if u not in (0, s.unit) or st not in (0, s.body_style):
            continue
        for g in sub[2:]:
            if not isinstance(g, list) or not g:
                continue
            h = g[0]
            if h == "rectangle":
                a, b = sexpr.find(g, "start"), sexpr.find(g, "end")
                if a is not None and b is not None:
                    x0, y0, x1, y1 = _f(a[1]), _f(a[2]), _f(b[1]), _f(b[2])
                    poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], True)
            elif h in ("polyline", "bezier"):
                pts = [(_f(xy[1]), _f(xy[2])) for xy in sexpr.find_all(sexpr.find(g, "pts") or [], "xy")]
                poly(pts)
            elif h == "circle":
                c = sexpr.find(g, "center")
                r = _f(sexpr.value(g, "radius", 0))
                if c is not None and r > 0:
                    poly([(_f(c[1]) + r * math.cos(i * math.pi / 6), _f(c[2]) + r * math.sin(i * math.pi / 6))
                          for i in range(12)], True)
            elif h == "arc":
                pts = [sexpr.find(g, k) for k in ("start", "mid", "end")]
                if all(p is not None for p in pts):
                    poly(_arc_pts(*[(_f(p[1]), _f(p[2])) for p in pts]))
    for p in s.pins_world():
        if p.get("hidden"):
            continue
        ln = _f(p.get("length", 2.54), 2.54)
        tx, ty = p["xy"]
        out.append((tx, ty, tx - p["out"][0] * ln, ty - p["out"][1] * ln))
    return out


def label_ink(l: dict) -> list:
    x, y = l["at"]
    size = _f(l.get("size"), FONT) or FONT
    w = text_width(l["net"], size)
    over = 0.45 if "~{" in str(l["net"]) else 0.0
    r = int(round(_f(l.get("rot", 0)))) % 360
    if l.get("kind", "local") != "local":
        w += 2.6
        lo, hi = -size / 2 - 0.4, size / 2 + 0.4
    else:
        lo, hi = -LABEL_OFFSET - size - over, -LABEL_OFFSET
    a0, a1 = LABEL_OFFSET, LABEL_OFFSET + w
    if r == 0:
        return [x + a0, y + lo, x + a1, y + hi]
    if r == 180:
        return [x - a1, y + lo, x - a0, y + hi]
    if r == 90:
        return [x + lo, y - a1, x + hi, y - a0]
    return [x + lo, y + a0, x + hi, y + a1]


def text_ink(t) -> list | None:
    a = sexpr.find(t, "at")
    if a is None:
        return None
    size = _size(t)
    lines = str(t[1]).split("\n")
    w = max(text_width(x, size) for x in lines)
    h = size * (1 + LINE_K * (len(lines) - 1))
    x, y = _f(a[1]), _f(a[2])
    just = _justify(t)
    w *= 1.04                                             # sentences: blanks are drawn wider than the label model says
    x0 = x + 0.1 if "left" in just else x - w - 0.1 if "right" in just else x - w / 2
    # KiCad lifts bottom-justified sheet text off its anchor by the text offset (measured: 0.45 mm at 1.27 .. 2 mm)
    y0 = y - h - 0.45 if "bottom" in just else y + 0.45 if "top" in just else y - h / 2
    return [x0, y0, x0 + w, y0 + h]


def scene(sch: Schematic) -> dict:
    """-> {'texts': [{'kind', 'owner', 'pin', 'text', 'box', 'node'}], 'wires': [(x0, y0, x1, y1)], 'dots': [(x, y)],
    'ncs': [{'at', 'owner', 'pin'}], 'segs': [(owner, seg)], 'frames': [box]}"""
    sc = {"texts": [], "wires": [], "dots": [], "ncs": [], "segs": [], "frames": []}
    tips = {}
    for s in sch.symbols():
        power = s.is_power
        owner = s.ref if not power else "#" + s.uuid
        for i, p in enumerate(sexpr.find_all(s.node, "property")):
            if len(p) < 3 or _is_hidden(p) or not str(p[2]) or sexpr.find(p, "at") is None:
                continue
            sc["texts"].append({"kind": "power" if power else "field", "owner": owner, "pin": None, "text": str(p[2]),
                                "box": field_ink(p, s), "node": p, "sym": s, "name": str(p[1])})
        for kind, pnum, text, box in pin_texts(s):
            sc["texts"].append({"kind": kind, "owner": owner, "pin": pnum, "text": text, "box": box, "node": None, "sym": s})
        for seg in graphic_segments(s):
            sc["segs"].append((owner, seg))
        if not power:
            for p in s.pins_world():
                tips[(round(p["xy"][0], 2), round(p["xy"][1], 2))] = (owner, p["number"])
    for w in sch.wires():
        sc["wires"].append((w["a"][0], w["a"][1], w["b"][0], w["b"][1]))
    for j in sch.items("junction"):
        a = sexpr.find(j, "at")
        sc["dots"].append((_f(a[1]), _f(a[2])))
    for n in sch.items("no_connect"):
        a = sexpr.find(n, "at")
        k = (round(_f(a[1]), 2), round(_f(a[2]), 2))
        o = tips.get(k)
        if o is None:                                     # the cross at the end of a lead: it belongs to the lead's pin
            for w in sc["wires"]:
                a_, b_ = (round(w[0], 2), round(w[1], 2)), (round(w[2], 2), round(w[3], 2))
                if k in (a_, b_) and tips.get(b_ if k == a_ else a_):
                    o = tips[b_ if k == a_ else a_]
                    break
        o = o or (None, None)
        sc["ncs"].append({"at": (_f(a[1]), _f(a[2])), "owner": o[0], "pin": o[1]})
    for i, l in enumerate(sch.labels()):
        sc["texts"].append({"kind": "label", "owner": f"label{i}", "pin": None, "text": str(l["net"]), "box": label_ink(l),
                            "node": l.get("node"), "label": l})
    for i, t in enumerate(sch.items("text")):
        b = text_ink(t)
        if b is not None and str(t[1]).strip():
            sc["texts"].append({"kind": "note", "owner": f"text{i}", "pin": None, "text": str(t[1]), "box": b, "node": t,
                                "title": _size(t) >= TITLE_SIZE})
    for r in sch.items("rectangle"):
        a, b = sexpr.find(r, "start"), sexpr.find(r, "end")
        if a is not None and b is not None:
            sc["frames"].append([min(_f(a[1]), _f(b[1])), min(_f(a[2]), _f(b[2])), max(_f(a[1]), _f(b[1])), max(_f(a[2]), _f(b[2]))])
    return sc


# ───────────────────────── tests ─────────────────────────
def hit(a, b, s=-AIR_TEXT / 2) -> bool:
    return a[0] + s < b[2] - s and b[0] + s < a[2] - s and a[1] + s < b[3] - s and b[1] + s < a[3] - s


def seg_hits_box(seg, b, s=-AIR_LINE) -> bool:
    """Does the segment pass through the box (shrunk by s)?  Liang-Barsky."""
    x0, y0, x1, y1 = seg
    bx0, by0, bx1, by1 = b[0] + s, b[1] + s, b[2] - s, b[3] - s
    if bx0 >= bx1 or by0 >= by1:
        return False
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - bx0), (dx, bx1 - x0), (-dy, y0 - by0), (dy, by1 - y0)):
        if abs(p) < 1e-12:
            if q <= 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 < t1


def run_together(a, b) -> bool:
    """Two horizontal texts on one line with less than a word space between them."""
    ha, hb = a[3] - a[1], b[3] - b[1]
    ov = min(a[3], b[3]) - max(a[1], b[1])
    if ov < 0.35 * min(ha, hb):
        return False
    gap = max(a[0], b[0]) - min(a[2], b[2])
    return -SHRINK * 2 <= gap < RUN_GAP


_LIB_KINDS = ("pinnum", "pinname")
_FREE_KINDS = ("field", "power", "label", "note")


def _horizontal(t) -> bool:
    b = t["box"]
    return (b[2] - b[0]) >= (b[3] - b[1]) or len(t["text"]) <= 1


def text_hits(t, sc, others=None, air: float = 0.0, skip=()) -> list:
    """Everything the text `t` touches: [(what, detail)]. air > 0 asks for that much clearance on top (used when a
    NEW place is chosen; the metric itself uses air = 0). `others` = the texts to test against (default: all)."""
    out = []
    b = t["box"] if not air else [t["box"][0] - air, t["box"][1] - air, t["box"][2] + air, t["box"][3] + air]
    kind, owner = t["kind"], t["owner"]
    for o in (sc["texts"] if others is None else others):
        if o is t or id(o) in skip:
            continue
        same = o["owner"] == owner
        if same and kind in _LIB_KINDS and o["kind"] in _LIB_KINDS:
            continue                                      # the library's own pin texts against each other: class 'lib'
        if hit(b, o["box"], -AIR_TITLE / 2 if (t.get("title") or o.get("title")) else -AIR_TEXT / 2):
            out.append(("text", o))
        elif kind in _FREE_KINDS and o["kind"] in _FREE_KINDS and not same and _horizontal(t) and _horizontal(o) \
                and run_together(b, o["box"]):
            out.append(("run", o))
    if kind in _LIB_KINDS:
        # a pin text is where the library put it; wires, dots and crosses ON it are the layout's doing
        own_segs = False
    else:
        own_segs = kind == "field"
    for w in sc["wires"]:
        if seg_hits_box(w, b):
            out.append(("wire", w))
    for d in sc["dots"]:
        if hit(b, [d[0] - DOT_R, d[1] - DOT_R, d[0] + DOT_R, d[1] + DOT_R], -AIR_DOT / 2):
            out.append(("junction", d))
    for n in sc["ncs"]:
        if kind == "pinnum" and n["owner"] == owner and n["pin"] == t["pin"]:
            continue                                      # class 'nc_on_number'
        x, y = n["at"]
        if hit(b, [x - NC_HALF, y - NC_HALF, x + NC_HALF, y + NC_HALF], -AIR_DOT / 2):
            out.append(("no_connect", n["at"]))
    for o, seg in sc["segs"]:
        if o == owner and not own_segs:
            continue
        if seg_hits_box(seg, b):
            out.append(("symbol", o))
            break
    if kind != "note" or not t.get("frame_title"):
        for f in sc["frames"]:
            for e in ((f[0], f[1], f[2], f[1]), (f[0], f[3], f[2], f[3]), (f[0], f[1], f[0], f[3]), (f[2], f[1], f[2], f[3])):
                if seg_hits_box(e, b, -AIR_FRAME):
                    out.append(("frame", f))
                    break
    return out


def collisions(sch: Schematic, sc: dict | None = None) -> dict:
    """-> {'count', 'by_kind': {...}, 'list': [readable strings], 'lib': n, 'nc_on_number': n}"""
    sc = sc or scene(sch)
    seen = set()
    rows = []
    by: dict = {}

    def name(t):
        if t["kind"] in ("field", "power"):
            return f"{t['kind']} '{t['text']}'" + (f" of {t['owner']}" if t["kind"] == "field" else "")
        if t["kind"] in _LIB_KINDS:
            return f"{t['kind']} {t['owner']}.{t['pin']}"
        return f"{t['kind']} '{t['text'][:24]}'"
    for t in sc["texts"]:
        for what, o in text_hits(t, sc):
            if what in ("text", "run"):
                key = (what, *sorted((id(t), id(o))))
                if key in seen:
                    continue
                seen.add(key)
                rows.append((what, f"{name(t)} {'on' if what == 'text' else 'runs into'} {name(o)}", t["box"]))
            else:
                key = (what, id(t), str(o))
                if key in seen:
                    continue
                seen.add(key)
                rows.append((what, f"{name(t)} on {what}" + (f" of {o}" if what == "symbol" and not str(o).startswith('#') else ""), t["box"]))
    for what, _s, _b in rows:
        by[what] = by.get(what, 0) + 1
    lib = 0
    pt = [t for t in sc["texts"] if t["kind"] in _LIB_KINDS]
    by_owner: dict = {}
    for t in pt:
        by_owner.setdefault(t["owner"], []).append(t)
    for lst in by_owner.values():
        for i, a in enumerate(lst):
            for b in lst[i + 1:]:
                if hit(a["box"], b["box"]):
                    lib += 1
    ncn = 0
    for n in sc["ncs"]:
        x, y = n["at"]
        for t in by_owner.get(n["owner"], ()):
            if t["kind"] == "pinnum" and t["pin"] == n["pin"] and hit(t["box"], [x - NC_HALF, y - NC_HALF, x + NC_HALF, y + NC_HALF], 0.0):
                ncn += 1
    return {"count": len(rows), "by_kind": by, "list": [s for _w, s, _b in rows][:40],
            "boxes": [[round(v, 2) for v in b] for _w, _s, b in rows][:40], "lib": lib, "nc_on_number": ncn}
