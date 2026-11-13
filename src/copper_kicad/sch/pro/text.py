"""Text placement for "pro2" (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 7): a candidate solver for the Reference /
Value texts of the parts, run on the finished drawing.

The block builders put every field at its home slot (right of a vertical 2-pin part, above a horizontal one, at
the free corner of an IC). solve() looks at the sheet as it will be written - bodies, pin strips, wires,
junctions, labels, power symbols, frame titles, notes - and re-seats only the fields that touch something:

* candidates per part: the home slot, the mirrored side, above, below, each also one step (1.27 mm) further out;
  Reference and Value move as a stacked pair first, then one by one;
* greedy, most-constrained first (the part with the fewest free candidates chooses first); hard constraint: no
  overlap with anything; soft cost: distance from home;
* one grow iteration: what found no place gets a second ring of candidates one grid step (2.54 mm) further out;
  what is still unplaced stays at home and is reported.

Only field positions change (G0 ignores them); nothing electrical is touched. Text extents come from
model.text_width. Checked against kicad-cli renderings (bench/sch/textcheck.py, KiCad 10.0.6): net names, values
and references are within -0.25 .. +0.15 mm; long part numbers with several hyphens come out up to 0.55 mm wider
in KiCad than the model says (ESP32-C3-MINI-1-N4: +0.53 mm), sentences up to 3.6 % wider. The field boxes carry
0.4 mm of air and every test here asks for 0.15 - 0.2 mm more, so a long value can come 0.1 mm closer to its
neighbour than intended; it cannot reach it.
"""
from __future__ import annotations

from ... import sexpr
from ...kicad import libs as LIBS
from .. import layout as LAY
from .. import symbols as G
from ..model import FONT, Schematic, _is_hidden, _set_justify, num, text_width

PAD = 0.3
WORD_AIR = 0.5            # free space between two texts on one line (the boxes carry 0.2 mm each already)
LINE = 1.9                # line pitch of a Reference / Value pair
WIRE_AIR = 0.35           # between a text line and a horizontal wire that runs along it
NC_HALF = 1.1             # half size of the box a no-connect cross keeps free (the cross itself is 1.27 mm wide)
OWN_PINS = 6              # parts up to this many pins: a field may not stand on the part's own pins / graphic either
SYM_AIR = 0.6             # a text beside the graphic of a supply / ground symbol, on its line


def _hit(a, b, s=0.0):
    return a[0] + s < b[2] and b[0] + s < a[2] and a[1] + s < b[3] and b[1] + s < a[3]


def _words_touch(a, b, air=None):
    """Two texts on one line closer than `air`: they read as one word ("PWR_FLAG330n")."""
    air = WORD_AIR if air is None else air
    return a[0] - air < b[2] and b[0] - air < a[2] and a[1] + 0.3 < b[3] and b[1] + 0.3 < a[3]


def _grow(b, d):
    return [b[0] - d, b[1] - d, b[2] + d, b[3] + d]


def graphic_box(sch: Schematic, s):
    ls = sch.lib_symbol(s.lib_name)
    if ls is None:
        return s.body_bbox()
    return G.bbox_world(s.at, s.matrix, LIBS.unit_bbox(ls, s.unit, s.body_style, include_pins=False))


def visible_fields(s) -> list:
    return [p for p in sexpr.find_all(s.node, "property") if len(p) > 2 and not _is_hidden(p) and str(p[2])
            and sexpr.find(p, "at") is not None]


def field_size(p) -> tuple:
    return text_width(str(p[2])) + 0.4, FONT + 0.4


def field_box(p, s) -> list:
    """Sheet box of one visible field as SymInst.field_boxes() computes it."""
    a = sexpr.find(p, "at")
    x, y = float(a[1]), float(a[2])
    ang = int(round(float(a[3]))) % 180 if len(a) > 3 else 0
    vertical = (ang == 90) != (s.rot in (90, 270))
    w, h = field_size(p)
    if vertical:
        w, h = h, w
    eff = sexpr.find(p, "effects")
    j = sexpr.find(eff, "justify") if eff is not None else None
    just = " ".join(str(x_) for x_ in j[1:]) if j is not None else ""
    if vertical:
        x0, x1 = x - w / 2, x + w / 2
        y0, y1 = (y - h, y) if "left" in just else (y, y + h) if "right" in just else (y - h / 2, y + h / 2)
    else:
        x0, x1 = (x, x + w) if "left" in just else (x - w, x) if "right" in just else (x - w / 2, x + w / 2)
        y0, y1 = (y - h, y) if "bottom" in just else (y, y + h) if "top" in just else (y - h / 2, y + h / 2)
    return [x0, y0, x1, y1]


def pin_boxes(s) -> list:
    """The pin lines of a placed symbol with the strip their numbers are printed in."""
    out = []
    ls = s.sch.lib_symbol(s.lib_name)
    pn = sexpr.find(ls, "pin_numbers") if ls is not None else None
    no_numbers = pn is not None and any(e == "hide" or (isinstance(e, list) and e and e[0] == "hide"
                                                        and (len(e) == 1 or str(e[1]) == "yes")) for e in pn[1:])
    for p in s.pins_world():
        ln = float(p.get("length", 2.54) or 2.54)
        ox, oy = p["out"]
        tip = p["xy"]
        inner = (tip[0] - ox * ln, tip[1] - oy * ln)
        b = [min(tip[0], inner[0]), min(tip[1], inner[1]), max(tip[0], inner[0]), max(tip[1], inner[1])]
        if not p.get("hidden") and not no_numbers:
            tw = text_width(str(p["number"]))
            if oy == 0:
                mx = (tip[0] + inner[0]) / 2
                b = [min(b[0], mx - tw / 2), b[1] - 1.6, max(b[2], mx + tw / 2), b[3] + 0.15]
            else:
                my = (tip[1] + inner[1]) / 2
                b = [b[0] - 1.6, min(b[1], my - tw / 2), b[2] + 0.15, max(b[3], my + tw / 2)]
        out.append(b)
    return out


def scene(sch: Schematic, extra: list | None = None) -> dict:
    """Everything a text may not touch, by kind: {'body': [(ref, box)], 'pin': [(ref, box)], 'wire': [box],
    'label': [box], 'power': [box], 'note': [box], 'field': [(ref, index, box)]}."""
    sc = {"body": [], "pin": [], "wire": [], "label": [], "power": [], "note": list(extra or []), "field": [], "ptext": [],
          "pbody": []}
    for s in sch.symbols():
        if s.is_power:
            sc["power"].append(s.body_bbox())
            sc["pbody"].append(s.body_bbox())
            sc["power"] += s.field_boxes()
            sc["ptext"] += s.field_boxes()
            continue
        sc["body"].append((s.ref, graphic_box(sch, s)))
        for b in pin_boxes(s):
            sc["pin"].append((s.ref, b))
        for i, p in enumerate(visible_fields(s)):
            sc["field"].append((s.ref, i, field_box(p, s)))
    for w in sch.wires():
        a, b = w["a"], w["b"]
        sc["wire"].append([min(a[0], b[0]) - 0.05, min(a[1], b[1]) - 0.05, max(a[0], b[0]) + 0.05, max(a[1], b[1]) + 0.05])
    for j in sch.items("junction"):
        a = sexpr.find(j, "at")
        sc["wire"].append([float(a[1]) - 0.5, float(a[2]) - 0.5, float(a[1]) + 0.5, float(a[2]) + 0.5])
    for n in sch.items("no_connect"):
        a = sexpr.find(n, "at")
        sc["wire"].append([float(a[1]) - NC_HALF, float(a[2]) - NC_HALF, float(a[1]) + NC_HALF, float(a[2]) + NC_HALF])
    for l in sch.labels():
        b = LAY.label_box(l)
        if "~{" in str(l.get("net", "")) and int(round(float(l.get("rot", 0) or 0))) % 180 == 0:
            b = [b[0], b[1] - 0.6, b[2], b[3]]            # the overbar stands above the letters
        sc["label"].append(b)
    for t in sch.items("text"):
        a = sexpr.find(t, "at")
        if a is None:
            continue
        size = FONT
        font = sexpr.find(sexpr.find(t, "effects") or [], "font")
        sz = sexpr.find(font, "size") if font else None
        if sz and len(sz) > 1:
            size = float(sz[1])
        lines = str(t[1]).split("\n")
        w = max(text_width(x, size) for x in lines)
        h = size * 1.62 * len(lines)
        sc["note"].append([float(a[1]), float(a[2]) - h, float(a[1]) + w, float(a[2]) + 0.2])
    return sc


def flat(sc: dict) -> list:
    """All obstacle boxes of a scene except the part fields (notes and frame titles are placed against these)."""
    return ([b for _r, b in sc["body"]] + [b for _r, b in sc["pin"]] + sc["wire"] + sc["label"] + sc["power"]
            + sc["note"] + [b for _r, _i, b in sc["field"]])


def _blocked(box, ref, sc, placed, skip_fields=(), own: bool = True) -> bool:
    """Does a field box of part `ref` touch anything? own=False leaves the part's own body and pins out: that is
    the test for "this field must move" (a value printed inside its own IC body is where its library put it); a
    NEW place for a field must be clear of its own part as well."""
    for r, b in sc["body"]:
        if (own or r != ref) and _hit(box, b, 0.15):
            return True
    for r, b in sc["pin"]:
        if (own or r != ref) and _hit(box, b, 0.2 if r == ref else 0.1):
            return True
    for b in sc["wire"]:
        if _hit(box, b, 0.2):
            return True
        # a wire that runs ALONG a text right under / over it underlines it ("LM4040DBZ-2.5" resting on VREF)
        if b[2] - b[0] > 1.0 and min(box[2], b[2]) - max(box[0], b[0]) > 1.5 and                 box[1] - WIRE_AIR < b[3] and b[1] < box[3] + WIRE_AIR:
            return True
    for b in sc["label"]:
        if _hit(box, b, -0.25):                           # a net name needs air: a value right under it reads as one word
            return True
    for key in ("power", "note"):
        for b in sc[key]:
            if _hit(box, b, 0.15):
                return True
    for b in sc.get("ptext", ()):
        if _words_touch(box, b):
            return True
    for b in sc.get("pbody", ()):                         # "TS-1187" running into the ground triangle next to it
        if _words_touch(box, b, SYM_AIR):
            return True
    for r, i, b in sc["field"]:
        if (r, i) in skip_fields:
            continue
        if _hit(box, placed.get((r, i), b), 0.15) or (r != ref and _words_touch(box, placed.get((r, i), b))):
            return True
    return False


CORNERS = True            # fix round 6: a field pair may stand at a corner of its part (left / right aligned with the body)
IN_FRAME = True           # fix round 6: a Reference / Value never leaves the frame its part is drawn in
FRAME_AIR = 0.9           # ... and keeps this far from the frame's lines
PAIR_STAYS = True         # fix round 6: the exact pass moves a Reference / Value pair that stands together as a pair


BOTTOM_PIN_AIR = 2.0      # fix round 7: a large part's Reference / Value under the body closer than this to a bottom pin's lead


def _at_bottom_pin(s, gb, boxes) -> bool:
    """A part of more than OWN_PINS pins whose fields all stand under its body, one of them right beside the lead
    of a pin that leaves the bottom edge ("ATmega328P-A" a millimetre from the ground lead): the pair is re-seated."""
    pw = s.pins_world()
    if BOTTOM_PIN_AIR <= 0 or len(pw) <= OWN_PINS or len(boxes) < 2 or s.rot in (90, 270):
        return False
    if not all(b[1] >= gb[3] - 0.2 for b in boxes):
        return False
    xs = [q["xy"][0] for q in pw if q["out"][1] > 0 and q["xy"][1] >= gb[3] - 0.2]
    return any(b[0] - BOTTOM_PIN_AIR < x < b[2] + BOTTOM_PIN_AIR for b in boxes for x in xs)


def _home_frame(gb, frames):
    """The frame a part is drawn in: the smallest one that holds the middle of its graphic."""
    cx, cy = (gb[0] + gb[2]) / 2, (gb[1] + gb[3]) / 2
    fs = [f for f in (frames or ()) if f[0] <= cx <= f[2] and f[1] <= cy <= f[3]]
    return min(fs, key=lambda f: (f[2] - f[0]) * (f[3] - f[1])) if fs else None


def _inside(box, fr, air=None) -> bool:
    a = FRAME_AIR if air is None else air
    return fr is None or (fr[0] + a <= box[0] and box[2] <= fr[2] - a and fr[1] + a <= box[1] and box[3] <= fr[3] - a)


def _candidates(gb, sizes, ring: int, wide: bool = False) -> list:
    """Centre points for a stack of fields (sizes = [(w, h)]) around the body box `gb`, nearest slots first.
    ring 0: touching distance and one step out; ring 1 (the grow iteration): one grid step further."""
    n = len(sizes)
    wmax = max(w for w, _h in sizes)
    total = LINE * n
    cx, cy = (gb[0] + gb[2]) / 2, (gb[1] + gb[3]) / 2
    outs = (0.9, 2.17) if ring == 0 else (3.44, 4.71, 5.98)
    cands = []
    for d in outs:
        ys = [cy - total / 2 + LINE * (i + 0.5) for i in range(n)]
        for sh in (0.0, -1.27, 1.27, -2.54, 2.54):
            cands.append([(gb[2] + d + sizes[i][0] / 2, ys[i] + sh) for i in range(n)])          # right, left-aligned
            cands.append([(gb[0] - d - sizes[i][0] / 2, ys[i] + sh) for i in range(n)])          # left, right-aligned
        for sh in (0.0, -1.27, 1.27, -2.54, 2.54, -(wmax / 2 + 1), wmax / 2 + 1):
            cands.append([(cx + sh, gb[1] - d - 0.4 - LINE * (n - 1 - i)) for i in range(n)])    # above
            cands.append([(cx + sh, gb[3] + d + 0.6 + LINE * i) for i in range(n)])              # below
        if CORNERS:
            # (fix round 6) the corners of a large part: both lines start on the body's left edge / end on its right
            # edge - where a Reference / Value pair stands when pins leave the middle of the top and bottom edge
            for xa, sg in ((gb[0], 1), (gb[2], -1)):
                # (wide: a pair that leaves the lead of a bottom pin) ... or runs out over the corner, clear of the
                # leads of the pins on the top edge
                for sh in (0.0, 1.27 * sg, 2.54 * sg) + ((-2.54 * sg, -5.08 * sg, -7.62 * sg, -10.16 * sg) if wide else ()):
                    cands.append([(xa + sh + sg * sizes[i][0] / 2, gb[1] - d - 0.4 - LINE * (n - 1 - i)) for i in range(n)])
                    cands.append([(xa + sh + sg * sizes[i][0] / 2, gb[3] + d + 0.6 + LINE * i) for i in range(n)])
    return cands


def _box_at(c, size):
    return [c[0] - size[0] / 2, c[1] - size[1] / 2, c[0] + size[0] / 2, c[1] + size[1] / 2]


def solve(sch: Schematic, extra: list | None = None, refs: set | None = None, frames: list | None = None) -> dict:
    """Re-seat the part fields that touch something. extra = more obstacle boxes (frame title strips, notes).
    -> {'conflicts': fields that touched something, 'moved': fields re-seated, 'grown': of those, placed in the
    second ring, 'unplaced': [ref] that stay where they were}."""
    sc = scene(sch, extra)
    syms = {}
    for s in sch.symbols(include_power=False):
        syms.setdefault(s.ref, []).append(s)
    todo = []
    low: set = set()
    placed: dict = {}
    for ref, lst in syms.items():
        if refs is not None and ref not in refs:
            continue
        # a multi-unit part: every unit carries its own copy of the fields; index the units' fields in scene order
        k = 0
        for s in lst:
            fs = visible_fields(s)
            idx = list(range(k, k + len(fs)))
            k += len(fs)
            gb = graphic_box(sch, s)
            bad = []
            for i, p in zip(idx, fs):
                fb = field_box(p, s)
                # a text INSIDE its part's body is where the library put it (the value of an IC); one that merely
                # touches its own body or stands on its own pin (the Reference on a switch's contact bar) moves
                inside = gb[0] - 0.2 <= fb[0] and fb[2] <= gb[2] + 0.2 and gb[1] - 0.2 <= fb[1] and fb[3] <= gb[3] + 0.2
                small = len(s.pins_world()) <= OWN_PINS
                if _blocked(fb, ref, sc, placed, {(ref, i)}, own=small and not inside)                         or (IN_FRAME and not _inside(fb, _home_frame(gb, frames), 0.0)):
                    bad.append(i)
            if not bad and _at_bottom_pin(s, gb, [field_box(p, s) for p in fs]):
                bad = list(idx)                           # (fix round 7) Reference / Value squeezed beside a bottom pin's lead
                low.add(ref)
            if bad:
                todo.append((ref, s, fs, idx, bad))
    res = {"conflicts": sum(len(t[4]) for t in todo), "moved": 0, "grown": 0, "unplaced": []}
    if not todo:
        return res

    def options(ref, s, fs, idx, ring):
        gb = graphic_box(sch, s)
        # the pins widen the box the texts must stay clear of on the sides they leave from
        sizes = [field_size(p) for p in fs]
        skip = {(ref, i) for i in idx}
        home = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in (field_box(p, s) for p in fs)]
        fr = _home_frame(gb, frames)
        pw_ = s.pins_world()
        big = len(pw_) > OWN_PINS
        sides_ = {"R" if q["out"][0] > 0 else "L" for q in pw_ if q["out"][1] == 0}
        out = []
        botx = [q["xy"][0] for q in pw_ if q["out"][1] > 0 and q["xy"][1] >= gb[3] - 0.2]             if (BOTTOM_PIN_AIR > 0 and big and len(fs) > 1 and s.rot not in (90, 270)) else []
        for cand in _candidates(gb, sizes, ring, bool(botx)):
            boxes = [_box_at(c, sz) for c, sz in zip(cand, sizes)]
            if any(_blocked(b, ref, sc, placed, skip) for b in boxes):
                continue
            if IN_FRAME and not all(_inside(b, fr) for b in boxes):
                continue
            cost = sum(abs(c[0] - h[0]) + abs(c[1] - h[1]) for c, h in zip(cand, home))
            if CORNERS and big and len(fs) > 1:
                # a large part: the pair stands over / under the body (a corner) before it stands out beyond the
                # pin rows of a side, among the names of that side's nets
                bx = boxes[0]
                if bx[3] > gb[1] and bx[1] < gb[3] and (("R" in sides_ and bx[0] >= gb[2]) or ("L" in sides_ and bx[2] <= gb[0])):
                    cost += 1000.0
            if botx and all(b[1] >= gb[3] - 0.2 for b in boxes) and                     any(b[0] - BOTTOM_PIN_AIR < x < b[2] + BOTTOM_PIN_AIR for b in boxes for x in botx):
                cost += 500.0                             # ... goes over the body (a corner) when there is room
            out.append((round(cost, 3), cand))
        out.sort(key=lambda t: t[0])
        return out

    def commit(ref, s, fs, idx, cand):
        fang = 90 if s.rot in (90, 270) else 0
        for p, i, c in zip(fs, idx, cand):
            a = sexpr.find(p, "at")
            a[1:] = [num(round(c[0], 3)), num(round(c[1], 3)), fang]
            _set_justify(p, None)
            placed[(ref, i)] = _box_at(c, field_size(p))
            res["moved"] += 1

    for ring in (0, 1):
        scored = []
        for t in todo:
            ref, s, fs, idx, bad = t
            scored.append((len(options(ref, s, fs, idx, ring)), ref, t))
        scored.sort(key=lambda x: (x[0], x[1]))            # most constrained first
        left = []
        for _n, _ref, t in scored:
            ref, s, fs, idx, bad = t
            opts = options(ref, s, fs, idx, ring)          # re-checked against what was placed meanwhile
            if opts:
                commit(ref, s, fs, idx, opts[0][1])
                if ring:
                    res["grown"] += len(fs)
                continue
            # the pair finds no place: the fields that are fine stay, the others move one by one
            done = True
            for p, i in zip(fs, idx):
                if i not in bad:
                    continue
                o1 = options(ref, s, [p], [i], ring)
                if o1:
                    commit(ref, s, [p], [i], o1[0][1])
                    if ring:
                        res["grown"] += 1
                else:
                    done = False
            if not done:
                left.append(t)
        todo = left
        if not todo:
            break
    res["unplaced"] = sorted({t[0] for t in todo})
    return res


# ───────────────────────── exact pass (text hygiene) ─────────────────────────
# solve() works on reserved boxes with air; refine() is the second opinion on the FINISHED sheet: it asks
# textbox.text_hits - the same test the metric `text_collisions` counts with, on ink boxes built from KiCad's drawing
# rules - and moves what still touches: part fields, the value of a supply symbol, and net labels (along their own
# wire, or turned to its other side). Positions and angles of texts only; no wire, pin or symbol moves.
REFINE = True
REF_AIR = 0.15            # a new place keeps this much more air than the metric asks for
GAP0 = 0.9                # nearest a field's ink comes to its part's graphic
STEP = 0.635
WORD = 0.95               # blank between Reference and Value on one line
LABEL_AIR = 0.05
SLIDE = 8                 # a label may move this many grid steps (1.27 mm) along its wire


def _is_connector(s) -> bool:
    lib = str(s.lib_id).split(":", 1)[0].lower()
    return lib.startswith("connector") or str(s.ref).rstrip("0123456789").upper() in ("J", "P", "CN", "X", "CON")


def _pin_axis(s) -> str | None:
    """'h' when every pin of a 2-pin part leaves sideways, 'v' when up / down."""
    pins = s.pins_world()
    if len(pins) != 2:
        return None
    if all(p["out"][1] == 0 for p in pins):
        return "h"
    if all(p["out"][0] == 0 for p in pins):
        return "v"
    return None


def _arrangements(gb, sizes, klass: str, vpins=()) -> list:
    """[(rank, step, variant, [boxes])] for one or two fields around the graphic box gb. Two fields: on one line
    ('row') or stacked. The rank orders the sides the way they are picked by hand for this kind of part."""
    n = len(sizes)
    h = sizes[0][1]
    cx, cy = (gb[0] + gb[2]) / 2, (gb[1] + gb[3]) / 2
    roww = sum(w for w, _h in sizes) + WORD * (n - 1)
    out = []

    def row(x0, y0):
        bs, x = [], x0
        for w, hh in sizes:
            bs.append([x, y0, x + w, y0 + hh])
            x += w + WORD
        return bs

    def stack(xa, y0, align):
        bs = []
        for i, (w, hh) in enumerate(sizes):
            x0 = xa if align == "l" else xa - w if align == "r" else xa - w / 2
            bs.append([x0, y0 + i * LINE, x0 + w, y0 + i * LINE + hh])
        return bs
    stackh = LINE * (n - 1) + h
    order = {"conn": ("above_row", "above_stack", "below_row", "below_stack", "right", "left"),
             "h": ("above_row", "below_row", "above_stack", "below_stack", "right", "left"),
             "v": ("right", "left", "above_row", "below_row", "above_stack", "below_stack"),
             "any": ("right", "above_row", "left", "below_row", "above_stack", "below_stack")}[klass]
    rank = {k: i for i, k in enumerate(order)}
    for k in range(0, 12):
        d = GAP0 + k * STEP
        xs_row = [cx - roww / 2 + sh for sh in (0.0, -1.27, 1.27, -2.54, 2.54)] + [gb[0], gb[2] - roww]
        for j, x0 in enumerate(xs_row):
            out.append((rank["above_row"], k, j, row(x0, gb[1] - d - h)))
            out.append((rank["below_row"], k, j, row(x0, gb[3] + d)))
        if n > 1:
            anchors = [(cx, "c"), (gb[0], "l"), (gb[2], "r"), (cx - 1.27, "c"), (cx + 1.27, "c")]
            if vpins and CORNERS:
                # (fix round 6) beside the pins that leave the top / bottom edge: the pair ends left of the first
                # one or starts right of the last one (a long Value does not fit between a corner and those pins)
                anchors += [(min(vpins) - 1.9, "r"), (max(vpins) + 1.9, "l")]
            for j, (xa, al) in enumerate(anchors):
                out.append((rank["above_stack"], k, j, stack(xa, gb[1] - d - stackh, al)))
                out.append((rank["below_stack"], k, j, stack(xa, gb[3] + d, al)))
        for j, sh in enumerate((0.0, -1.27, 1.27, -2.54, 2.54)):
            out.append((rank["right"], k, j, stack(gb[2] + d, cy - stackh / 2 + sh, "l")))
            out.append((rank["left"], k, j, stack(gb[0] - d, cy - stackh / 2 + sh, "r")))
    return out


def _put_field(t, box):
    p, s = t["node"], t["sym"]
    a = sexpr.find(p, "at")
    a[1:] = [num(round((box[0] + box[2]) / 2, 3)), num(round((box[1] + box[3]) / 2, 3)), 90 if s.rot in (90, 270) else 0]
    _set_justify(p, None)
    t["box"] = list(box)


def _label_spots(l, wires, pin_pts) -> list:
    """Places a label may take without changing what it names: its own anchor turned round, and the grid points of
    the straight wire runs through its anchor - along the text and across it (a horizontal name beside a vertical
    wire). Never a point another wire touches, never a pin: the name stays on its own copper."""
    x, y = l["at"]
    rot = int(l.get("rot", 0)) % 360

    def on(w, px, py):
        return min(w[0], w[2]) - 0.01 <= px <= max(w[0], w[2]) + 0.01 and min(w[1], w[3]) - 0.01 <= py <= max(w[1], w[3]) + 0.01

    def chain(horiz):
        """The collinear wires through the anchor, followed through joints."""
        line = [w for w in wires if ((abs(w[1] - w[3]) < 0.01 and abs(w[1] - y) < 0.01) if horiz
                                     else (abs(w[0] - w[2]) < 0.01 and abs(w[0] - x) < 0.01))]
        run = [w for w in line if on(w, x, y)]
        grew = True
        while grew:
            grew = False
            for w in line:
                if w in run:
                    continue
                if any(on(r, w[0], w[1]) or on(r, w[2], w[3]) for r in run):
                    run.append(w)
                    grew = True
        return run
    spots = [((x, y), (rot + 180) % 360)]
    # a name that closes the open end of a wire stays there: moved inward it would leave a dangling wire end (ERC)
    ends = [w for w in wires if (abs(w[0] - x) < 0.01 and abs(w[1] - y) < 0.01) or (abs(w[2] - x) < 0.01 and abs(w[3] - y) < 0.01)]
    through = [w for w in wires if on(w, x, y) and w not in ends]
    if len(ends) == 1 and not through and (round(x, 2), round(y, 2)) not in pin_pts:
        return spots
    for horiz in (rot in (0, 180), rot not in (0, 180)):
        run = chain(horiz)
        if not run:
            continue
        for k in range(1, SLIDE + 1):
            for sgn in (1, -1):
                q = (round(x + sgn * k * 1.27, 4), y) if horiz else (x, round(y + sgn * k * 1.27, 4))
                if not any(on(w, q[0], q[1]) for w in run):
                    continue
                others = [w for w in wires if w not in run and on(w, q[0], q[1])]
                if others or (round(q[0], 2), round(q[1], 2)) in pin_pts:
                    continue
                for r in (rot, (rot + 180) % 360):
                    spots.append((q, r))
    return spots


NEAR_AIR = 0.8            # a part's Reference / Value closer than this to a net name or another part's text is re-seated


def refine(sch: Schematic, frames: list | None = None) -> dict:
    """Move the texts that still touch something on the finished sheet. -> {'before': collisions counted by the
    metric, 'after', 'moved': {'field': n, 'power': n, 'label': n}, 'left': [readable strings]}"""
    from . import emit as E
    from . import textbox as TB
    sc = TB.scene(sch)
    res = {"before": TB.collisions(sch, sc)["count"], "moved": {"field": 0, "power": 0, "label": 0}}

    def near(t, box=None, skip=()):
        """(fix round 5) a Reference / Value that all but touches a net name or another part's text ("4.7k" resting
        on "I2C_SCL"): closer than NEAR_AIR ink to ink. Not a collision for the metric, but read as one."""
        if not NEAR_AIR or t["kind"] != "field":
            return False
        b = box or t["box"]
        g = [b[0] - NEAR_AIR, b[1] - NEAR_AIR, b[2] + NEAR_AIR, b[3] + NEAR_AIR]
        return any(o["kind"] in ("label", "field") and o["owner"] != t["owner"] and id(o) not in skip
                   and _hit(g, o["box"]) for o in sc["texts"])
    def outside(t, box=None):
        if not IN_FRAME or t["kind"] != "field" or not frames:
            return False
        return not _inside(box or t["box"], _home_frame(graphic_box(sch, t["sym"]), frames), 0.0 if box is None else None)
    if not REFINE or not (res["before"] or any(near(t) or outside(t) for t in sc["texts"])):
        res["after"] = res["before"]
        res["left"] = []
        return res
    pin_pts = set()
    for s in sch.symbols():
        for p in s.pins_world():
            pin_pts.add((round(p["xy"][0], 2), round(p["xy"][1], 2)))

    def bad(t):
        return bool(TB.text_hits(t, sc)) or near(t) or outside(t)

    def free(t, box, skip):
        probe = dict(t)
        probe["box"] = box
        return not TB.text_hits(probe, sc, air=REF_AIR, skip=skip | {id(t)}) and not near(t, box, skip)             and not outside(t, box)

    for _round in range(3):
        moved_any = False
        # 1. part fields
        by_sym: dict = {}
        for t in sc["texts"]:
            if t["kind"] == "field":
                by_sym.setdefault(id(t["sym"]), []).append(t)
        for lst in by_sym.values():
            s = lst[0]["sym"]
            hits = [t for t in lst if bad(t)]
            if not hits:
                continue
            small = len(s.pins_world()) <= OWN_PINS or _is_connector(s)
            klass = "conn" if _is_connector(s) else (_pin_axis(s) or "any")
            gb = graphic_box(sch, s)
            pair = [t for t in lst if t.get("name") in ("Reference", "Value")]
            groups = [pair] if small and len(pair) == 2 and len(lst) == 2 else [[t] for t in hits]
            if PAIR_STAYS and not small and len(pair) == 2 and len(lst) == 2:
                # a pair that stands together outside its part (two lines, one above the other) stays a pair
                ba, bb_ = pair[0]["box"], pair[1]["box"]
                out_ = all(b_[2] < gb[0] or b_[0] > gb[2] or b_[3] < gb[1] or b_[1] > gb[3] for b_ in (ba, bb_))
                if out_ and abs((ba[1] + ba[3]) / 2 - (bb_[1] + bb_[3]) / 2) < 2.6 and ba[0] < bb_[2] and bb_[0] < ba[2]:
                    groups = [pair]
            for grp in groups:
                grp = sorted(grp, key=lambda t: t.get("name") != "Reference")
                sizes = []
                for t in grp:
                    bw, bh = t["box"][2] - t["box"][0], t["box"][3] - t["box"][1]
                    sizes.append((max(bw, bh), min(bw, bh)) if len(t["text"]) > 1 else (bw, bh))
                home = [((t["box"][0] + t["box"][2]) / 2, (t["box"][1] + t["box"][3]) / 2) for t in grp]
                skip = {id(t) for t in grp}
                best = None
                vp = [q["xy"][0] for q in s.pins_world() if q["out"][0] == 0] if not small else ()
                for rank, k, j, boxes in _arrangements(gb, sizes, klass, vp):
                    dist = sum(abs((b[0] + b[2]) / 2 - h_[0]) + abs((b[1] + b[3]) / 2 - h_[1]) for b, h_ in zip(boxes, home))
                    cost = (rank, k, j, dist) if klass != "any" else (0, 0, 0, dist)
                    if CORNERS and klass == "any" and not small and len(grp) > 1:
                        # (fix round 6) a large part: over / under the body before out beyond a side's pin rows
                        b0 = boxes[0]
                        if b0[3] > gb[1] and b0[1] < gb[3] and any(
                                q["out"][1] == 0 and ((q["out"][0] > 0) == (b0[0] >= gb[2])) for q in s.pins_world()):
                            cost = (1, 0, 0, dist)
                    if best is not None and cost >= best[0]:
                        continue
                    if all(free(t, b, skip) for t, b in zip(grp, boxes)):
                        best = (cost, boxes)
                if best:
                    for t, b in zip(grp, best[1]):
                        _put_field(t, b)
                        res["moved"]["field"] += 1
                    moved_any = True
        # 2. net labels: along their wire / to its other side
        for t in sc["texts"]:
            if t["kind"] != "label" or not bad(t):
                continue
            l = t["label"]
            if l.get("kind", "local") != "local":
                continue
            spots = _label_spots(l, sc["wires"], pin_pts)
            # second chance (first round only): a place where nothing but a part's Reference / Value is in the
            # way - a field has many places, a name only its wire; the field gives way in the next round
            fields = {id(o) for o in sc["texts"] if o["kind"] == "field"}
            for skip in (({id(t)},) if _round else ({id(t)}, {id(t)} | fields)):
                done = False
                for q, r in spots:
                    l2 = dict(l, at=[q[0], q[1]], rot=r)
                    probe = dict(t, box=TB.label_ink(l2))
                    # a name keeps KiCad's own 0.35 mm over its wire: only a little extra air is asked for
                    if not TB.text_hits(probe, sc, air=LABEL_AIR, skip=skip):
                        E.move_label(l["node"], (q[0], q[1]), r)
                        l["at"], l["rot"] = [q[0], q[1]], r
                        t["box"] = TB.label_ink(l)
                        res["moved"]["label"] += 1
                        moved_any = done = True
                        break
                if done:
                    break
        # 3. the value of a supply symbol: one line further from the symbol, or beside it
        for t in sc["texts"]:
            if t["kind"] != "power" or not bad(t):
                continue
            b = t["box"]
            if (b[2] - b[0]) < (b[3] - b[1]):
                continue                                  # a rotated value: leave
            s = t["sym"]
            gb = s.body_bbox()
            w, h = b[2] - b[0], b[3] - b[1]
            below = (b[1] + b[3]) / 2 >= (gb[1] + gb[3]) / 2
            cy = (gb[1] + gb[3]) / 2
            cands = []
            for dy in (1.27, 1.905, 2.54):
                cands.append([b[0], b[1] + (dy if below else -dy), b[2], b[3] + (dy if below else -dy)])
            cands += [[gb[0] - 0.8 - w, cy - h / 2, gb[0] - 0.8, cy + h / 2], [gb[2] + 0.8, cy - h / 2, gb[2] + 0.8 + w, cy + h / 2]]
            for c in cands:
                if free(t, c, set()):
                    _put_field(t, c)
                    res["moved"]["power"] += 1
                    moved_any = True
                    break
        if not moved_any:
            break
    c = TB.collisions(sch, sc)
    res["after"] = c["count"]
    res["left"] = c["list"][:12]
    return res
