"""Block-local construction: an anchor part with its support parts drawn at the pins they serve.

* stems: pull-ups above the pin wire, pull-downs / RC caps / switches below it, series parts in line (LED chains)
* rail groups: neighbouring pins of one rail share a rail wire and ONE power symbol
* decoupling banks: the caps of one supply under one supply symbol and over one ground symbol, above their IC
* cells: every other part is posed as a small anchor of its own and docked on the side of the pin it serves

Everything here is constructive (no search over the sheet): nothing is emitted that overlaps something already
placed in the block, so different nets never touch. All coordinates are mm on the 1.27 grid.
"""
from __future__ import annotations

import math
import re

from ... import sexpr
from ...sexpr import Sym
from .. import layout as LAY
from .. import ops as O
from .. import symbols as G
from ..model import _is_hidden, _set_justify, autoplace_fields, num, text_width
from .blocks import Design, _refkey
from .score import graphic_box

U = 1.27
GAP = 2 * U
BANK_ROW = 6            # decoupling caps side by side under one supply symbol
STUB = 2 * U
NC_LEAD = True          # pro2: a no-connect cross that would cover its pin's number stands one step out on a lead
_SUPPLY_PIN = re.compile(r"^(VIN|IN|VI|VCC|VDD|VDDA|VDDIO|AVDD|DVDD|VBAT|VBUS|V\+|VS|VM|PVIN|AVIN|VCCA|VCCB|3V3|5V)\d*$", re.I)


def _prefix(ref: str) -> str:
    i = 0
    while i < len(ref) and not ref[i].isdigit():
        i += 1
    return ref[:i].upper()


def snap(v, g=U):
    return round(round(v / g) * g, 4)


def hit(a, b, s=0.12):
    return a[0] + s < b[2] and b[0] + s < a[2] and a[1] + s < b[3] and b[1] + s < a[3]


def wbox(a, b, t=0.25):
    return [min(a[0], b[0]) - t, min(a[1], b[1]) - t, max(a[0], b[0]) + t, max(a[1], b[1]) + t]


def set_pose(si, x, y, rot, mirror="keep"):
    a = sexpr.find(si.node, "at")
    old = si.at
    a[1:] = [num(x), num(y), int(rot) % 360]
    O._shift_fields(si.node, x - old[0], y - old[1])
    if mirror != "keep":
        for e in list(si.node):
            if isinstance(e, list) and e and e[0] == "mirror":
                si.node.remove(e)
        if mirror:
            si.node.insert(si.node.index(a) + 1, [Sym("mirror"), Sym(mirror)])


def pins(si):
    return {p["number"]: p for p in si.pins_world()}


def inline_fields(si):
    """Horizontal 2-pin part: Reference and Value side by side ABOVE the body (the next pin row is 2.54 below)."""
    bb = graphic_box(si.sch, si)
    cx = (bb[0] + bb[2]) / 2
    vis = [p for p in sexpr.find_all(si.node, "property") if len(p) > 2 and not _is_hidden(p) and str(p[2])]
    ws = [text_width(str(p[2])) for p in vis]
    total = sum(ws) + 1.0 * (len(vis) - 1)
    x = cx - total / 2
    fang = 90 if si.rot in (90, 270) else 0
    for p, w in zip(vis, ws):
        a = sexpr.find(p, "at")
        a[1:] = [num(round(x + w / 2, 3)), num(round(bb[1] - 1.2, 3)), fang]
        _set_justify(p, None)
        x += w + 1.0
    for p in sexpr.find_all(si.node, "property"):
        if len(p) > 2 and (_is_hidden(p) or not str(p[2])):
            a = sexpr.find(p, "at")
            if a is not None:
                a[1:] = [num(si.at[0]), num(si.at[1]), fang]


def side_fields(si):
    """Vertical 2-pin part: Reference over Value, left-aligned just right of the body."""
    bb = graphic_box(si.sch, si)
    vis = [p for p in sexpr.find_all(si.node, "property") if len(p) > 2 and not _is_hidden(p) and str(p[2])]
    fang = 90 if si.rot in (90, 270) else 0
    y = (bb[1] + bb[3]) / 2 - (len(vis) - 1) * 0.95
    for p in vis:
        a = sexpr.find(p, "at")
        a[1:] = [num(round(bb[2] + 1.0 + text_width(str(p[2])) / 2, 3)), num(round(y, 3)), fang]
        _set_justify(p, None)
        y += 1.9
    for p in sexpr.find_all(si.node, "property"):
        if len(p) > 2 and (_is_hidden(p) or not str(p[2])):
            a = sexpr.find(p, "at")
            if a is not None:
                a[1:] = [num(si.at[0]), num(si.at[1]), fang]


def place_fields(si):
    ps = si.pins_world()
    nums = {p["number"] for p in ps}
    if len(nums) == 2 and all(p["out"][1] == 0 for p in ps):
        inline_fields(si)
    elif len(nums) == 2 and all(p["out"][0] == 0 for p in ps):
        side_fields(si)
    else:
        autoplace_fields(si)


PATH_SHUNTS = True         # a TVS / cap to ground behind a fuse or series diode hangs on the rail wire of that path
FIELDS_TOGETHER = True     # a part with pins on every side: Reference and Value stand together, not at two corners


def together_fields(si):
    """The autoplacer gives a part with pins on every side its Reference above one corner and its Value below the
    opposite one - on a large IC the two then stand a hand's width apart. Here the Value takes the line right above
    the body and the Reference the line above that one, both starting at the same x. Called by the engine on the
    FINISHED drawing (the two lines take no room from the placement); the text solver re-seats what then touches."""
    bb = graphic_box(si.sch, si)
    vis = [p for p in sexpr.find_all(si.node, "property") if len(p) > 2 and not _is_hidden(p) and str(p[2])]
    ref = next((p for p in vis if str(p[1]) == "Reference"), None)
    val = next((p for p in vis if str(p[1]) == "Value"), None)
    if ref is None or val is None or si.rot in (90, 270):
        return
    ar, av = sexpr.find(ref, "at"), sexpr.find(val, "at")
    if ar is None or av is None or not (float(ar[2]) < bb[1] and float(av[2]) > bb[3]):
        return
    x0 = float(ar[1]) - text_width(str(ref[2])) / 2
    y = float(ar[2])
    av[1:] = [num(G.snap(x0 + text_width(str(val[2])) / 2, 0.0254)), num(G.snap(y, 0.0254)), ar[3] if len(ar) > 3 else 0]
    ar[2] = num(G.snap(y - 1.9, 0.0254))
    _set_justify(val, None)


def pose_pin(si, pin_no, target, out, mirror=None):
    """Rotate a part so that `pin_no` points along `out`, and move it so that the pin tip sits on `target`."""
    for rot in (0, 90, 180, 270):
        set_pose(si, 0, 0, rot, mirror)
        p = pins(si).get(pin_no)
        if p is not None and tuple(p["out"]) == tuple(out):
            set_pose(si, snap(target[0] - p["xy"][0]), snap(target[1] - p["xy"][1]), rot, mirror)
            place_fields(si)
            return True
    return False


# air kept free around the value text of a power symbol / PWR_FLAG (both sides together): two such texts on one
# line must read as two words ("GND VBUS", "+5V PWR_FLAG" stood half a letter apart with 0.6)
TEXT_AIR = 1.8
NAME_GAP = 3.0             # fix round 6: between the value text of a supply / ground symbol and a net name on its line
WORD_GAP = 1.0             # more air (beyond the 0.3 mm each box carries) between two power value texts on one line


def sym_boxes(si):
    return [si.body_bbox()] + si.field_boxes()


def power_box(name, pt, orient):
    tw = max(2.6, text_width(name) + 0.6)
    x, y = pt
    if orient == "up":
        return [x - tw / 2, y - 5.9, x + tw / 2, y]
    if orient == "down":
        return [x - tw / 2, y, x + tw / 2, y + 5.9]
    if orient == "right":
        return [x, y - 1.4, x + 3.2 + tw, y + 1.4]
    return [x - 3.2 - tw, y - 1.4, x, y + 1.4]


def power_boxes(name, pt, orient):
    """Tighter than power_box: the symbol graphic and its value text as two boxes (upright symbols only)."""
    if orient not in ("up", "down"):
        return [power_box(name, pt, orient)]
    tw = max(2.6, text_width(name) + 0.6)
    x, y = pt
    sg = -1 if orient == "up" else 1
    g = [x - 1.4, min(y, y + sg * 2.7), x + 1.4, max(y, y + sg * 2.7)]
    t = [x - tw / 2, min(y + sg * 2.7, y + sg * 5.9), x + tw / 2, max(y + sg * 2.7, y + sg * 5.9)]
    return [g, t]


def lbl_box(text, pt, out, kind="local"):
    return LAY.label_box({"at": list(pt), "kind": kind, "net": text, "rot": G.label_angle(out)}, pad=0.3)


# pro2: the in / out pins of a pass-through part (TVS array) are joined by a bar over / under the part, so the net
# is one wire with one name (Blk._bridges). False = both sides keep a name of their own (the state after fix round 1
# of docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 15; wired_local_ratio then drops below 0.85 on the USB designs).
# OFF since fix round 2 (section 16): three critics read the bar as a frame around the part ("worst recurring
# geometry defect"); the data lines now run straight through the part - a wire on one side, the name on the other.
FLAG_ON_RAIL_WIRE = True     # pro2: a PWR_FLAG of a rail stands on the rail's own wire when one has a free spot
PASS_THROUGH_BAR = False
CONTACT_BAR = True           # fix round 4: the doubled signal contact of a 4-pin part is joined by a bar over the part
BANK_ONE_ROW = True          # pro2: the cap groups that stand above a part (no room at the pins) share one row
BANK_ROW_MAX = 110.0         # mm; a wider row wraps
NODE_REACH = 64              # grid steps a pin's wire may run on before its pull / filter parts hang on it
CONTACT_LINK = True          # package B: doubled contacts are joined by a straight link along their pin row (no bar, no ring)
RAIL_PAIR_BAR = False        # fix round 3: no bar under a switch's doubled ground contact (it closed a box around the pins)
RAIL_LANE = 14               # fix round 3: cells kept free in front of a waiting side supply pin while a stage is drawn
ARCH_LAST = False            # fix round 3: a square crossing of ONE wire is tried before an arch ("hook") to a symbol
# package A (power conventions): the supply / ground symbol of a SIDE pin is reached by a straight drop / rise or ONE
# L - never by an arch that leads a ground wire up and over (or a supply down and under). In this order: a free L
# (also a short one that tucks the symbol under its own stub), a bar to the next pin of the same net on that side,
# an L that crosses at most SINGLE_L_CROSS wires squarely, and only then a sideways symbol.
FLAG_ON_END = True           # fix round 6: a signal net's PWR_FLAG may stand on the open end of the net's own pin stub
SPAN = True                  # fix round 6: a two-pin part across two neighbouring pins is drawn across the pair
SPAN_PARTS = ("R", "C")      # (not L / FB: an inductor between SW and the sense pin next to it is the power path)
SPAN_TEXT = 8                # grid steps kept free right of such a part for its Reference / Value
SINGLE_L = True
FLIP_PINS = 10               # a part of up to so many pins may be flipped top to bottom for its supply pins
RAIL_BUS = True              # class D: the supply pins on top of a part and ALL caps of their rail on one rail bar
RAIL_BUS_TIER = 14           # grid steps between the bars of two rails when one has to run out over the other
RAIL_BUS_GROW = 2.6          # mm a row of caps may reach out past the part's drawing before a tier higher is preferred
RAIL_BUS_KS = (8, 10, 12, 14, 16, 20, 24, 28, 32)   # grid steps from the pin tips to the bar
DECAP_ON_WIRE = True         # the caps of a side supply pin hang on that pin's wire, beyond the rise to its symbol
FLAG_APART = 3 * 2 * U       # a PWR_FLAG on a wire keeps this far from the supply / ground symbol of that wire
SINGLE_L_CROSS = 2
AHEAD = 8                    # cells in front of an open wire end in which no such symbol may stand
SINGLE_L_LANE = 6            # cells of its row a side supply pin that still waits keeps free (stub + one crossing)
ROW_LANE = 44                # cells of its row a pin keeps free whose net is wired to a peer of the block afterwards


class Cell:
    """A rigid piece of drawing: symbols + wires + power symbols + terminals, with its occupancy boxes."""

    def __init__(self, D: Design, info, used: set | None = None):
        self.D, self.info = D, info
        self.boxes, self.resv = [], []            # hard boxes; label reservations [(net, box)]
        self.wires, self.powers, self.ncs, self.terms = [], [], [], []
        self.labels, self.flags = [], []          # fixed labels (net, name entry, pt, angle); PWR_FLAG points
        self.syms, self.fallback = [], []
        self.used = used if used is not None else set()
        self.sat_refs = list(info.sats) if info is not None else []
        self.v2 = False                           # "pro2": idiom templates, upright power symbols, grouped grounds
        self.im = None                            # idioms.match.match_block() of this block
        self.sm = None                            # idioms.stage.match_block() of this block
        self.stage_claim = set()
        self.stats = None                         # {'matches': [idiom], 'dropped': [(idiom, why)], 'claimed': set}
        # pro2: other wire ends a doubled pin pair offers (net, first end, alternative end, wires to draw when it
        # is used, direction) - see Blk.place / dock.dock_anchor; bars over a pass-through part that a supply pin
        # may cross squarely (net, y, x0, x1, box)
        self.alts, self.bars = [], []
        # pro2 (bundle.py): where the name of a net that is wired between two parts should stand - at the host's
        # end of the wire, so the names of a bundle form one column: (net, open end, direction of the wire there)
        self.name_at = []
        self.runs = []                            # (net, host end, end of the first straight piece, direction, turns there)

    # ── occupancy ──
    def occ(self):
        return self.boxes + [b for _, b in self.resv]

    def free(self, boxes, extra=()):
        occ = self.occ() + list(extra)
        return not any(hit(b, o) for b in boxes for o in occ)

    def words_apart(self, name, pt, orient) -> bool:
        """pro2: would the value text of an upright power symbol at `pt` stand on one line with the value text of
        another one of this block, closer than WORD_GAP? ("GND VBUS" read as one word: a ground symbol hanging
        beside a supply arrow has its text exactly on the arrow's text line.)"""
        if not self.v2 or orient not in ("up", "down"):
            return True
        t = power_boxes(name, pt, orient)[1]
        for _n, q, o, nm in self.powers:
            if o not in ("up", "down") or (nm == name and o == orient):
                continue
            u = power_boxes(nm, q, o)[1]
            if t[1] + 0.3 < u[3] and u[1] + 0.3 < t[3] and t[0] - WORD_GAP < u[2] and u[0] - WORD_GAP < t[2]:
                return False
        if NAME_GAP:
            # (fix round 6) ... nor on one line with a net name next to it: "GND" beside "MISO" in the name column of
            # a header reads as the name of the next pin
            for _n, u in self.resv:
                if t[1] + 0.3 < u[3] and u[1] + 0.3 < t[3] and t[0] - NAME_GAP < u[2] and u[0] - NAME_GAP < t[2]:
                    return False
        return True

    def bbox(self):
        bs = self.occ()
        if not bs:
            return [0.0, 0.0, 0.0, 0.0]
        return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]

    def shift(self, dx, dy):
        if not dx and not dy:
            return
        sh = lambda p: (snap(p[0] + dx), snap(p[1] + dy))   # noqa: E731
        for si in self.syms:
            set_pose(si, snap(si.at[0] + dx), snap(si.at[1] + dy), si.rot)
        bar_ids = {id(bx) for _n, _y, _x0, _x1, bx in self.bars}
        self.boxes = [b if id(b) in bar_ids else [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy] for b in self.boxes]
        self.resv = [(n, [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]) for n, b in self.resv]
        self.wires = [(n, sh(a), sh(b)) for n, a, b in self.wires]
        self.powers = [(n, sh(p), o, v) for n, p, o, v in self.powers]
        self.ncs = [sh(p) for p in self.ncs]
        self.terms = [(n, sh(p), o) for n, p, o in self.terms]
        self.labels = [(n, e, sh(p), a) for n, e, p, a in self.labels]
        self.flags = [(n, sh(p), o) for n, p, o in self.flags]
        self.alts = [(n, sh(f), sh(t), [(n2, sh(a), sh(b)) for n2, a, b in ws], o) for n, f, t, ws, o in self.alts]
        self.name_at = [(n, sh(p), o) for n, p, o in self.name_at]
        self.runs = [(n, sh(a), sh(b), o, t) for n, a, b, o, t in self.runs]
        for i, (n, y, x0, x1, bx) in enumerate(self.bars):
            nb = [bx[0] + dx, bx[1] + dy, bx[2] + dx, bx[3] + dy]
            for j, b in enumerate(self.boxes):
                if b is bx:
                    self.boxes[j] = nb
            self.bars[i] = (n, snap(y + dy), snap(x0 + dx), snap(x1 + dx), nb)

    def absorb(self, o: "Cell"):
        self.boxes += o.boxes
        self.resv += o.resv
        self.wires += o.wires
        self.powers += o.powers
        self.ncs += o.ncs
        self.terms += o.terms
        self.labels += o.labels
        self.flags += o.flags
        self.syms += o.syms
        self.fallback += o.fallback
        self.alts += o.alts
        self.bars += o.bars
        self.name_at += o.name_at
        self.runs += o.runs

    def attach(self, sub: "Cell", side: str, pref=None, port=None, gap=4 * U, clr=2.2, slide=True, grow_weight=1.0):
        """Dock `sub` on one side of this cell, as close to `pref` as it fits without touching anything."""
        if not self.occ():
            self.absorb(sub)
            return
        pb, sb = self.bbox(), sub.bbox()
        port = port or ((sb[0] + sb[2]) / 2, (sb[1] + sb[3]) / 2)
        pref = pref or ((pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2)
        sboxes = [[b[0] - clr, b[1] - clr, b[2] + clr, b[3] + clr] for b in sub.occ()]
        occ = self.occ()
        horiz = side in ("L", "R")
        G2 = 2 * U
        best = None
        for i in (0, 1, -1, 2, -2, 3, -3, 4, -4, 6, -6, 8, -8, 11, -11, 14, -14, 18, -18, 24, -24, 32, -32):
            off = i * G2
            if horiz:
                dy = snap(pref[1] - port[1] + off, G2)
                dx = snap((pb[0] - gap - sb[2]) if side == "L" else (pb[2] + gap - sb[0]), G2)
                dx += -G2 if side == "L" else G2
                step = (G2, 0.0) if side == "L" else (-G2, 0.0)
            else:
                dx = snap(pref[0] - port[0] + off, G2)
                dy = snap((pb[1] - gap - sb[3]) if side == "T" else (pb[3] + gap - sb[1]), G2)
                dy += -G2 if side == "T" else G2
                step = (0.0, G2) if side == "T" else (0.0, -G2)
            # corridor the cell sweeps while it slides in
            lo = [sb[0] + dx - clr, sb[1] + dy - clr, sb[2] + dx + clr, sb[3] + dy + clr]
            cor = [min(lo[0], lo[0] + 60 * step[0]), min(lo[1], lo[1] + 60 * step[1]),
                   max(lo[2], lo[2] + 60 * step[0]), max(lo[3], lo[3] + 60 * step[1])]
            near = [o for o in occ if hit(o, cor, 0)]
            n = 0
            while slide and n < 60:
                tx, ty = dx + step[0], dy + step[1]
                if any(hit([b[0] + tx, b[1] + ty, b[2] + tx, b[3] + ty], o, 0.05) for b in sboxes for o in near):
                    break
                dx, dy = tx, ty
                n += 1
            nb = [min(pb[0], sb[0] + dx), min(pb[1], sb[1] + dy), max(pb[2], sb[2] + dx), max(pb[3], sb[3] + dy)]
            grow = (nb[2] - nb[0]) + (nb[3] - nb[1]) - (pb[2] - pb[0]) - (pb[3] - pb[1])
            cost = abs(port[0] + dx - pref[0]) + abs(port[1] + dy - pref[1]) + grow_weight * grow
            if best is None or cost < best[0] - 0.01:
                best = (cost, dx, dy)
        sub.shift(best[1], best[2])
        self.absorb(sub)


class Blk(Cell):
    """One functional block: primary anchor with stems, banks and docked cells."""

    # ── "pro2": idiom templates ──
    def enable_v2(self):
        from .idioms import match as IM
        self.v2 = True
        from .idioms import stage as ST
        taken: set = set()
        self.sm = ST.match_block(self.D, self.info, taken)        # stage idioms claim first
        self.stage_claim = set(taken)
        self.used |= taken                                        # ... so that no stem picks their parts up
        self.im = IM.match_block(self.D, self.info, taken)
        self.stats = {"matches": [], "dropped": [], "claimed": set()}
        for a, m in self.im["modgnd"].items():
            self.stats["matches"].append(m.idiom)

    def _child(self) -> "Blk":
        c = Blk(self.D, self.info, self.used)
        c.sat_refs = self.sat_refs
        c.v2, c.im, c.stats = self.v2, self.im, self.stats
        c.sm, c.stage_claim = self.sm, self.stage_claim
        return c

    def _ctx_pins(self) -> dict:
        D = self.D
        out = {}
        for s in self.syms:
            for p in s.pins_world():
                out[(snap(p["xy"][0]), snap(p["xy"][1]))] = D.pins_of.get(s.ref, {}).get(p["number"])
        return out

    def _commit(self, piece: dict, m):
        self.wires += piece["wires"]
        self.powers += piece["powers"]
        self.terms += piece["terms"]
        self.boxes += piece["boxes"] + [wbox(a, b) for _, a, b in piece["wires"]]
        self.resv += piece["resv"]
        self.used |= set(piece["used"])
        self.syms += piece["syms"]
        self.stats["matches"].append(m.idiom)
        self.stats["claimed"] |= set(piece["used"])

    def _fits(self, piece: dict, lanes=()) -> bool:
        boxes = piece["boxes"] + [wbox(a, b, 0.02) for _, a, b in piece["wires"]] + [b for _, b in piece["resv"]]
        return self.free(boxes, lanes)

    def try_node(self, p, n) -> bool:
        """The idiom hanging on signal net n, docked on side pin p: template -> proof -> first offset that fits."""
        from .idioms import match as IM
        from .idioms import passive as IP
        from .idioms.model import IdiomError, instantiate
        D = self.D
        m = self.im["node"].get(n)
        if m is not None and any(r in self.used for r in m.refs()):
            m = None
        if m is None:
            ref = next((s.ref for s in self.syms if any(tuple(q["xy"]) == tuple(p["xy"]) for q in s.pins_world())), None)
            m = IM.node_match(D, self.info, n, self.used, (ref, p["number"]))
        if m is None or not m.roles or any(r not in self.sat_refs for r in m.refs()):
            return False
        make = IP.led_template if m.idiom == "led" else IP.node_template
        ctx = self._ctx_pins()
        why = "no room at the pin"
        if m.idiom == "pull" and set(m.roles) == {"down0"} and p["out"][1] == 0:
            # the pins right below this one carry signals: the pull-down lies in its row instead of hanging through
            # theirs (IP.pull_inline_template)
            host = next((s for s in self.syms if any(tuple(q["xy"]) == tuple(p["xy"]) for q in s.pins_world())), None)
            rows = 0
            if host is not None and D.npins(host.ref) >= 6:
                below = {round((q["xy"][1] - p["xy"][1]) / (2 * U)) for q in host.pins_world()
                         if tuple(q["out"]) == tuple(p["out"]) and abs(q["xy"][0] - p["xy"][0]) < 0.01
                         and D.conn(D.pins_of[host.ref].get(q["number"])) and not D.is_rail(D.pins_of[host.ref].get(q["number"]))
                         and D.pins_of[host.ref].get(q["number"]) != n}
                while rows + 1 in below:
                    rows += 1
            if 1 <= rows <= 3:
                for k in range(4, 40, 2):
                    try:
                        t = IP.pull_inline_template(D, m, tuple(p["xy"]), p["out"][0], k, rows)
                        if t is None:
                            continue
                        piece = instantiate(D, m, t, ctx)
                    except IdiomError as e:
                        why = str(e)
                        continue
                    if self._fits(piece):
                        self._commit(piece, m)
                        return True
        for k in range(4, NODE_REACH, 2):
            try:
                t = make(D, m, tuple(p["xy"]), p["out"][0], k)
                if t is None:
                    continue
                piece = instantiate(D, m, t, ctx)
            except IdiomError as e:
                why = str(e)
                continue
            if self._fits(piece):
                self._commit(piece, m)
                return True
        self.stats["dropped"].append((m.idiom, why))
        return False

    def _decap_roles(self, ref, n) -> list:
        m = self.im["decap"].get((ref, n)) if self.im else None
        return [r for r in sorted(m.roles, key=lambda r: int(r[1:])) if m.roles[r] not in self.used] if m else []

    def try_decap(self, ref, n, ps, lanes, sides, share: int = 1) -> bool:
        """Decoupling caps of `ref` on rail n drawn at the pin group `ps`. `share` = how many pin groups of this
        rail still want caps (the caps are spread evenly)."""
        from .idioms import passive as IP
        from .idioms.model import IdiomError, Match, instantiate
        D = self.D
        avail = self._decap_roles(ref, n)
        if not avail or not sides:
            return False
        m = self.im["decap"][(ref, n)]
        o = tuple(ps[0]["out"])
        cap_max = 3 if len(ps) <= 2 else max(1, 5 - len(ps))      # keeps a rail's junctions at four or fewer
        if RAIL_BUS:
            cap_max = BANK_ROW       # (class D) every cap at the pins: a bar with its caps on it is no comb (score.py)
        take = min(cap_max, math.ceil(len(avail) / max(1, share)))
        pts = [(snap(q["xy"][0]), snap(q["xy"][1])) for q in ps]
        ks = (2, 4, 6, 8, 10, 12, 16, 20) if o[1] == 0 else (8, 10, 12, 14, 16, 20, 24) if o == (0, -1) else (3, 4, 6, 8)
        if o[1] == 0:
            sides = ("R",)
        ctx = self._ctx_pins()
        why = "no room at the pin"
        for nn in range(take, 0, -1):
            sub = Match("decap", {r: m.roles[r] for r in avail[:nn]}, dict(m.nets), (ref, ps[0]["number"]), m.score)
            for k in ks:
                for side in sides:
                    try:
                        piece = instantiate(D, sub, IP.decap_template(D, sub, pts, o, k, side), ctx)
                    except IdiomError as e:
                        why = str(e)
                        continue
                    if self._fits(piece, lanes):
                        self._commit(piece, sub)
                        return True
        self.stats["dropped"].append(("decap", why))
        return False

    def _is_supply(self, ref, p) -> bool:
        D = self.D
        return "power_in" in D.ptypes.get((ref, p["number"]), "") or bool(
            _SUPPLY_PIN.match(D.pin_name.get((ref, p["number"]), "")))

    def rail_buses(self, ref, si, plist, groups: list, pending: set, hostpins: set) -> set:
        """Class D: the supply pins on TOP of `ref` and all their decoupling caps, one rail bar per rail
        (passive.rail_bus_template). The bars of the part are placed together (`solve`): one height for all, the
        rail whose pins are leftmost runs its caps out to the left, the others to the right, and no cap stands over
        the pins of another bar. A bar with no free side at that height - or whose row would make the part's
        drawing wider than it is - stands RAIL_BUS_TIER higher, its caps on the line of the row above.
        -> numbers of the pins that are drawn (they need no symbol of their own any more).

        A rail takes part with its longest run of neighbouring top pins (no connected pin of another net between
        them) that holds a supply pin; pins of the rail outside that run keep their own symbol. Nothing is drawn
        for a rail whose bar finds no room - its caps go the old way (try_decap, pin_bank, bank above the part)."""
        from .idioms import passive as IP
        from .idioms.model import IdiomError, Match, instantiate
        D = self.D
        up = (0, -1)

        def net_of(p):
            return D.pins_of[ref].get(p["number"])
        waiting = {p["number"]: p for g in groups for p in g["pins"] if tuple(p["out"]) == up}
        if not waiting or not self.im:
            return set()
        tops = sorted((p for p in plist if tuple(p["out"]) == up and D.conn(net_of(p))), key=lambda p: (p["xy"][0], p["number"]))
        fed = {net_of(p) for p in plist if D.is_rail(net_of(p)) and self._is_supply(ref, p)}
        jobs = []
        for n in sorted({net_of(p) for p in waiting.values()}, key=lambda k: D.text(k)):
            roles = self._decap_roles(ref, n)
            if D.is_gnd(n) or not roles:
                continue
            runs, cur = [], []
            for p in tops:
                if net_of(p) == n and p["number"] in waiting:
                    if not any(abs(q["xy"][0] - p["xy"][0]) < 0.01 for q in cur):
                        cur.append(p)
                elif cur and abs(p["xy"][0] - cur[-1]["xy"][0]) > 0.01:
                    runs.append(cur)
                    cur = []
            if cur:
                runs.append(cur)
            runs = [r for r in runs if len({round(p["xy"][1], 2) for p in r}) == 1
                    and (n not in fed or any(self._is_supply(ref, p) for p in r))]
            if not runs:
                continue
            run = max(runs, key=lambda r: (sum(1 for p in r if self._is_supply(ref, p)), len(r), -r[0]["xy"][0]))
            jobs.append({"net": n, "pins": run, "roles": roles, "x": (run[0]["xy"][0] + run[-1]["xy"][0]) / 2})
        if not jobs:
            return set()
        jobs.sort(key=lambda j: j["x"])
        for i, j in enumerate(jobs):
            # one rail: to the right first (the reading direction); several: the leftmost one to the left
            first = "L" if (i == 0 and len(jobs) > 1) else "R"
            other = "L" if first == "R" else "R"
            j["sides"] = [first, other] + ([first + other] if len(j["roles"]) >= 2 else [])
            # one row before two: more caps than one row holds go half to either side when both sides are free
            j["sides"].sort(key=lambda sd: max(sum(1 for s_, _r in IP.bus_rows(j["roles"], sd) if s_ == d_) for d_ in (1, -1)))
            j["pts"] = [(snap(p["xy"][0]), snap(p["xy"][1])) for p in j["pins"]]
            j["m"] = self.im["decap"][(ref, j["net"])]
        mine = {p["number"] for j in jobs for p in j["pins"]}
        # room kept for what is drawn later: the lanes of the pins that carry stems, the stub and the upright symbol
        # of every other supply / ground pin that still waits
        lanes = [wbox(p["xy"], (p["xy"][0] + p["out"][0] * 30 * U, p["xy"][1] + p["out"][1] * 30 * U))
                 for p in plist if p["number"] in hostpins]
        lanes += [wbox(p["xy"], (p["xy"][0] + p["out"][0] * 3 * U, p["xy"][1] + p["out"][1] * 3 * U))
                  if p["out"][1] == 0 else
                  [p["xy"][0] - 1.4, min(p["xy"][1], p["xy"][1] + p["out"][1] * 7 * U),
                   p["xy"][0] + 1.4, max(p["xy"][1], p["xy"][1] + p["out"][1] * 7 * U)]
                  for p in plist if p["number"] not in mine and p["number"] in pending]
        ctx = self._ctx_pins()

        def variant(j, k, side, taken, k0=None):
            """Job j at height k, caps to `side`: (match, piece) when it is free of the drawing, the lanes, `taken`
            and the risers of the bars that are not placed yet (they rise to k0)."""
            sub = Match("decap", {r: j["m"].roles[r] for r in j["roles"]}, dict(j["m"].nets),
                        (ref, j["pins"][0]["number"]), j["m"].score)
            try:
                piece = instantiate(D, sub, IP.rail_bus_template(D, sub, j["pts"], k, IP.bus_rows(j["roles"], side)), ctx)
            except IdiomError:
                return None
            keep = [[q[0] - 1.4, q[1] - ((k if k0 is None else k0) + 2) * U, q[0] + 1.4, q[1]]
                    for j2 in jobs[jobs.index(j) + 1:] for q in j2["pts"]]
            return (sub, piece) if self._fits(piece, lanes + keep + taken) else None

        def fit(j, k, taken):
            """The first variant of job j at height k that is free of the drawing, the lanes and `taken`."""
            for side in j["sides"]:
                r = variant(j, k, side, taken)
                if r is not None:
                    return r
            return None

        def boxes_of(piece):
            return piece["boxes"] + [wbox(a, b, 0.3) for _, a, b in piece["wires"]]

        def solve(i, k, taken):
            """Variants for jobs[i:] at height k that are free of each other (first combination in order)."""
            if i == len(jobs):
                return []
            # a bar that lies between two others has no free side at their height: it stands one or two tiers
            # higher and runs out over its neighbour. A bar whose row would make the part's drawing WIDER than it is
            # (out past the names of the side pins) stands a tier higher as well when its caps then fit over the
            # part: a sheet runs out of width before it runs out of height
            x0 = min([b_[0] for b_ in self.occ() + taken] or [0.0])
            x1 = max([b_[2] for b_ in self.occ() + taken] or [0.0])
            opts = []
            for tier in (0, 1, 2) if len(jobs) > 2 else (0, 1) if len(jobs) > 1 else (0,):
                for si_, side in enumerate(jobs[i]["sides"]):
                    kk = k + tier * RAIL_BUS_TIER
                    r = variant(jobs[i], kk, side, taken, k)
                    if r is None:
                        continue
                    bx = boxes_of(r[1])
                    grow = max(0.0, x0 - min(b_[0] for b_ in bx)) + max(0.0, max(b_[2] for b_ in bx) - x1)
                    opts.append((0 if (i == 0 or grow < RAIL_BUS_GROW) else 1, tier, si_, side, kk, bx))
            for _g, _t, _si, side, kk, bx in sorted(opts, key=lambda o_: o_[:3]):
                rest = solve(i + 1, k, taken + bx)
                if rest is not None:
                    return [(side, kk)] + rest
            return None
        done: set = set()

        def commit(j, sub, piece):
            self._commit(piece, sub)
            self.stats["matches"].append("railbus")
            tips = {(round(p["xy"][0], 2), round(p["xy"][1], 2)) for p in j["pins"]}
            for p in plist:
                if net_of(p) == j["net"] and (round(p["xy"][0], 2), round(p["xy"][1], 2)) in tips:
                    done.add(p["number"])
        for k in RAIL_BUS_KS:                               # one height for every bar of this side
            got = solve(0, k, [])
            if got is not None:
                taken = []
                for j, (side, kk) in zip(jobs, got):
                    # (the caps were posed again by the variants that were tried after the kept one)
                    r = variant(j, kk, side, taken, k)
                    if r is None:
                        break
                    commit(j, *r)
                    taken = taken + boxes_of(r[1])
                return done
        taken = []
        for j in jobs:                                      # no common height: every bar at the first height it fits
            for k in RAIL_BUS_KS:
                r = fit(j, k, taken)
                if r is not None:
                    commit(j, *r)
                    taken = taken + boxes_of(r[1])
                    break
            else:
                self.stats["dropped"].append(("railbus", "no room for the rail bar"))
        return done

    def _wire_decaps(self, ref, n, pin, corner, o, lanes=()) -> bool:
        """Decoupling caps of `ref` on rail n hung on the wire of the side supply pin `pin`, beyond `corner` (where
        the wire turns up to its supply symbol):   pin ────┬──────┬───┐
                                                          ↑ +V   [C] [C]
        At most three, one ground symbol; nothing is drawn when the room under the wire is taken."""
        from .idioms import passive as IP
        from .idioms.model import IdiomError, Match, Template, instantiate
        D = self.D
        avail = self._decap_roles(ref, n)
        if not avail:
            return False
        m = self.im["decap"][(ref, n)]
        d = o[0]
        tip = (snap(pin["xy"][0]), snap(pin["xy"][1]))
        ctx = self._ctx_pins()
        for nn in range(min(BANK_ROW if RAIL_BUS else 3, len(avail)), 0, -1):
            sub = Match("decap", {r: m.roles[r] for r in avail[:nn]}, dict(m.nets), (ref, pin["number"]), m.score)
            for k in (4, 6, 8, 10):
                T = Template()
                x, xs, fars = snap(corner[0] + d * k * U), [], []
                try:
                    for role in IP._roles(sub, "c"):
                        far, bx, _ = IP._hang(T, D, role, sub.roles[role], sub.nets["RAIL"], x, tip[1], 1, lead=0,
                                              slot="right" if d > 0 else "left")
                        xs.append(x)
                        fars.append(far)
                        x = snap(x + d * IP._pitch(bx, x, d))
                    T.wires.append(("RAIL", [tip, (xs[-1], tip[1])]))
                    IP._grounds(T, fars)
                    piece = instantiate(D, sub, T, ctx)
                except IdiomError:
                    continue
                if self._fits(piece, lanes):
                    self._commit(piece, sub)
                    return True
        return False

    def _regroup(self, ref, groups: list) -> list:
        """pro2 pin groups: at most four neighbouring pins share one power symbol (no ground combs); the outer pins
        of a row of supply pins on top carry the decoupling caps, one group to the left, one to the right."""
        D = self.D
        out = []

        def supply(p):
            return "power_in" in D.ptypes.get((ref, p["number"]), "") or bool(
                _SUPPLY_PIN.match(D.pin_name.get((ref, p["number"]), "")))
        # (package A) decoupling caps belong to the SUPPLY pins of a rail. A pin that is merely strapped to the rail
        # (a reset or enable input tied high) takes none - it used to be given its share, found no room, and the
        # cap ended up as an island above the part
        fed = {g["net"] for g in groups if any(supply(p) for p in g["pins"])} if DECAP_ON_WIRE else set()
        for g in groups:
            n = g["net"]
            ps = []                                   # stacked pins (several numbers on one tip) are one pin here
            for p in g["pins"]:
                if not any(tuple(q["xy"]) == tuple(p["xy"]) for q in ps):
                    ps.append(p)
            o = tuple(ps[0]["out"])
            caps = bool(self._decap_roles(ref, n)) and not D.is_gnd(n) and (n not in fed or any(supply(p) for p in ps))
            if caps and o == (0, -1) and len(ps) >= 3:
                h = (len(ps) + 1) // 2
                out.append(dict(g, pins=ps[:h], sides=("L",)))
                out.append(dict(g, pins=ps[h:], sides=("R",)))
                continue
            if caps and o == (0, -1) and len(ps) == 2:
                out.append(dict(g, pins=ps, sides=("R", "L")))
                continue
            sides = () if not caps else ("LR", "R", "L") if o == (0, -1) else ("R", "L") if o == (0, 1) else ("R",)
            # the short chunk comes first: the symbol of a chunk hangs into the rows of the next one, which leaves
            # by its last row - four rows further down, clear of that symbol
            r0 = len(ps) % 4
            chunks = ([ps[:r0]] if r0 else []) + [ps[i:i + 4] for i in range(r0, len(ps), 4)]
            for ci, ch in enumerate(chunks):
                out.append(dict(g, pins=ch, sides=sides if (ci == len(chunks) - 1 and (o[1] == 0 or len(ch) == 1)) else ()))
        return out

    # ── a PWR_FLAG on the net itself: beside the rail symbol / on the wire it marks ──
    def add_flag(self, net) -> bool:
        """Put one PWR_FLAG on `net` where the net is drawn in this block: on a free spot of one of its horizontal
        wires, else on a short side wire next to its supply / ground symbol. False when nothing here is free.
        pro2: first with FLAG_APART (not beside a supply / ground symbol, not on a corner or junction of its wire);
        only a flag that finds no such place may stand closer."""
        if self.v2 and FLAG_APART and self._add_flag(net, True):
            return True
        return self._add_flag(net, False)

    def _add_flag(self, net, apart: bool) -> bool:
        D = self.D
        name = D.text(net)
        ftw = max(2.6, text_width("PWR_FLAG") + TEXT_AIR)
        taken = {(n, p) for n, p, _o in self.flags}

        def fbox(p, o="up"):
            return [p[0] - ftw / 2, p[1] - 5.2, p[0] + ftw / 2, p[1] - 0.45] if o == "up" else                 [p[0] - ftw / 2, p[1] + 0.45, p[0] + ftw / 2, p[1] + 5.2]

        def clear(p, o="up", extra=()):
            if (net, p) in taken:
                return False
            return self.free([fbox(p, o)] + list(extra))
        mine = [(a, b) for n, a, b in self.wires if n == net]
        if not D.is_rail(net) or (self.v2 and FLAG_ON_RAIL_WIRE and getattr(self, "flag_on_wire", True)):
            # (pro2, a rail too: the flag stands upright ON the wire that leads to the supply symbol - a side wire
            # of its own beside the symbol is the last resort, docs section 6)
            for o in (("up",) if D.is_rail(net) else ("up", "down")):
                for a, b in sorted(mine, key=lambda w: (-abs(w[0][0] - w[1][0]), w[0][1], w[0][0])):
                    if a[1] != b[1] or abs(a[0] - b[0]) < 4 * U - 0.01:
                        continue
                    x0, x1 = sorted((a[0], b[0]))
                    n_ = int(round((x1 - x0) / U))
                    for i in sorted(range(1, n_), key=lambda i: (abs(i - n_ / 2), i)):
                        p = (snap(x0 + i * U), a[1])
                        if apart and (
                                (n_ >= 4 and not 2 <= i <= n_ - 2)
                                or any(abs(q_[0] - p[0]) < FLAG_APART - 0.01 and abs(q_[1] - p[1]) < 3 * U + 0.01
                                       for _n2, q_, _o2, _v2 in self.powers)):
                            continue                      # (never beside a supply / ground symbol, never on a corner)
                        if clear(p, o):
                            self.flags.append((net, p, o))
                            self.boxes.append(fbox(p, o))
                            return True
        pts = [(q, o) for n, q, o, v in self.powers if n == net and o in ("up", "down")]
        pts += [(e, None) for w in mine for e in w]
        seen = set()
        if self.v2 and FLAG_ON_END and not apart and not D.is_rail(net):
            # (fix round 6) before a side wire of its own: the flag stands on an open END of the net's wire here (the
            # top of the stub that leaves a pin upwards) - the wire that goes on from that end carries it, and nothing
            # is added that the router would have to get around
            tips_ = self._ctx_pins()
            for n_, q, o_ in self.terms:
                if n_ != net or q in tips_:
                    continue
                for fo, sg in (("up", -1), ("down", 1)):
                    if tuple(o_) == (0, sg) or any((a == q and (b[1] - q[1]) * sg > 0.01) or (b == q and (a[1] - q[1]) * sg > 0.01)
                                                   for a, b in mine):
                        continue
                    if not any((a == q and abs(b[0] - q[0]) < 0.01) or (b == q and abs(a[0] - q[0]) < 0.01) for a, b in mine):
                        continue                          # only the end of a vertical stub: the flag continues its line
                    # (the net's own name at that end is no obstacle: the flag's text stands a line above it)
                    own_ = [x for x in self.resv if x[0] == net and x[1][0] - 0.4 <= q[0] <= x[1][2] + 0.4
                            and x[1][1] - 0.4 <= q[1] <= x[1][3] + 0.4]
                    keep_ = self.resv
                    self.resv = [x for x in keep_ if not any(x is y_ for y_ in own_)]
                    ok_ = clear(q, fo)
                    self.resv = keep_
                    if ok_:
                        self.flags.append((net, q, fo))
                        self.boxes.append([q[0] - 0.9, min(q[1], q[1] + (-5.2 if fo == "up" else 5.2)),
                                           q[0] + 0.9, max(q[1], q[1] + (-5.2 if fo == "up" else 5.2))])
                        self.boxes.append([q[0] - ftw / 2, q[1] - 5.2, q[0] + ftw / 2, q[1] - 2.2] if fo == "up" else
                                          [q[0] - ftw / 2, q[1] + 2.2, q[0] + ftw / 2, q[1] + 5.2])
                        return True
        for q, o in pts:
            if q in seen:
                continue
            seen.add(q)
            stw = max(2.6, text_width(name) + 0.6) if o == "up" else 2.6
            d0 = max(3, math.ceil(((ftw + stw) / 2 + 0.6) / (2 * U))) * 2 * U
            for fo in (("up",) if o is not None else ("up", "down")):
                for sg in (1, -1):
                    for d in (d0, d0 + 2 * U, d0 + 4 * U):
                        p = (snap(q[0] + sg * d), q[1])
                        w = wbox((q[0] + sg * U, q[1]), p, 0.02)
                        if self.v2 and o is not None:
                            # (not along the outline of a part: the edge of a box wider than a two-pin part)
                            edge = wbox((q[0] + sg * 2 * U, q[1]), p, 0.3)
                            if any(hit(edge, b_) and b_[2] - b_[0] > 6.0 for b_ in self.occ()):
                                continue
                        if clear(p, fo, [w]):
                            self.wires.append((net, q, p))
                            self.flags.append((net, p, fo))
                            self.boxes += [fbox(p, fo), wbox(q, p)]
                            return True
        if self.v2:
            # pro2: a flag never stands on a strip of its own (docs section 6). When the wire of its net has no free
            # spot, a short stub leaves the wire - upright, from any point of it - and carries the flag
            cand = []
            for wa, wb in mine:
                hz = wa[1] == wb[1]
                n_ = int(round((abs(wa[0] - wb[0]) + abs(wa[1] - wb[1])) / U))
                for i in range(n_ + 1):
                    t = i / n_ if n_ else 0.0
                    q = (snap(wa[0] + (wb[0] - wa[0]) * t), snap(wa[1] + (wb[1] - wa[1]) * t))
                    for d in ((0, -1), (0, 1)) if hz else ((1, 0), (-1, 0)):
                        cand.append((abs(i - n_ / 2), q, d))
            pins_here = self._ctx_pins()
            for _m, q, d in sorted(cand, key=lambda c: (c[0], c[1][1], c[1][0], c[2])):
                if q in pins_here:
                    continue                              # a wire may not leave a pin tip sideways
                for ln in (2 * U, 4 * U, 6 * U):
                    p = (snap(q[0] + d[0] * ln), snap(q[1] + d[1] * ln))
                    fo = "down" if d == (0, 1) else "up"
                    w = wbox((q[0] + d[0] * U, q[1] + d[1] * U), p, 0.3)
                    if apart and any(abs(q_[0] - p[0]) < FLAG_APART - 0.01 and abs(q_[1] - p[1]) < 5 * 2 * U
                                     for _n2, q_, _o2, _v2 in self.powers):
                        continue                          # (not on a tall stem right beside a supply symbol)
                    if clear(p, fo, [w]):
                        self.wires.append((net, q, p))
                        self.flags.append((net, p, fo))
                        self.boxes += [fbox(p, fo), wbox(q, p)]
                        return True
        return False

    # ── elements on a net: 2-pin parts of this block that are not placed yet ──
    def elems_on(self, net):
        D = self.D
        out = []
        for r in self.sat_refs:
            if r in self.used or len(D.units.get(r, ())) != 1:
                continue
            po = D.pins_of[r]
            if len(po) != 2 or net not in po.values():
                continue
            (p1, n1), (p2, n2) = sorted(po.items())
            if n1 == n2:
                continue
            pn_, pO, M = (p1, p2, n2) if n1 == net else (p2, p1, n1)
            out.append((D.units[r][0], pn_, pO, M, r))
        return out

    def twins_on(self, net) -> list:
        """(package B) the unused parts with doubled contacts (4-pin tactile switch) that hang on `net`: the node
        templates draw them like the two-terminal parts they are."""
        if not self.v2:
            return []
        from .idioms.match import node_part
        D = self.D
        return [r for r in self.sat_refs if r not in self.used and len(D.pins_of.get(r, {})) > 2
                and net in D.pins_of[r].values() and node_part(D, r)]

    def stem(self, tip, d, N, elems, k, _chain=((), ())):
        """Parts hanging on the wire that leaves pin `tip` in direction d (±1): columns of up/down parts, then at
        most one series part (which recurses on the net behind it). Returns the drawing or None.
        `_chain` = (refs, nets) already drawn further up this stem: a ring of series parts (two resistors in
        parallel, a filter loop) must end in a label instead of recursing for ever."""
        D = self.D
        px, py = tip
        ups = [e for e in elems if D.is_rail(e[3]) and not D.is_gnd(e[3])]
        downs = [e for e in elems if D.is_gnd(e[3])]
        series = [e for e in elems if not D.is_rail(e[3]) and D.conn(e[3])]
        if D.is_rail(N):
            series, ups = [], []
        elif not series:
            # an inductor / ferrite / fuse / diode towards a supply rail is part of the power path: it lies in
            # line with the pin (SW - L - +5V), it is not a pull-up
            path = sorted((e for e in ups if _prefix(e[4]) in ("L", "FB", "F", "D")),
                          key=lambda e: (("D", "L", "FB", "F").index(_prefix(e[4])), _refkey(e[4])))
            if len(path) > 1:
                path = [e for e in path if _prefix(e[4]) == _prefix(path[0][4])][:1]
            if path and (len(ups) == 1 or _prefix(path[0][4]) != "D" or True):
                series = path[:1]
                ups = [e for e in ups if e is not series[0]]
        res = {"wires": [], "powers": [], "boxes": [], "terms": [], "resv": [], "used": [], "syms": []}
        x = snap(px + d * k * U)
        lastx = None
        for i in range(max(len(ups), len(downs))):
            for attempt in range(14):
                col, cw, cp, cu, cs = [], [], [], [], []
                for e, sg in ((ups[i] if i < len(ups) else None, -1), (downs[i] if i < len(downs) else None, 1)):
                    if e is None:
                        continue
                    si, pn_, pO, M, r = e
                    if not pose_pin(si, pn_, (x, py + sg * GAP), (0, -sg)):
                        return None
                    far = pins(si)[pO]["xy"]
                    orient = "up" if sg < 0 else "down"
                    cw += [(N, (x, py), (x, snap(py + sg * GAP))), (M, tuple(far), (far[0], snap(far[1] + sg * U)))]
                    cp.append((M, (far[0], snap(far[1] + sg * U)), orient, D.text(M)))
                    col += sym_boxes(si) + [power_box(D.text(M), (far[0], far[1] + sg * U), orient)]
                    cu.append(r)
                    cs.append(si)
                if not any(hit(a, b, -0.6) for a in col for b in res["boxes"]):
                    break
                x = snap(x + d * 2 * U)
            res["wires"] += cw
            res["powers"] += cp
            res["used"] += cu
            res["syms"] += cs
            res["boxes"] += col
            lastx = x
            x = snap(x + d * 3 * 2 * U)
        if D.is_rail(N):
            res["wires"] += [(N, (px, py), (lastx, py)), (N, (lastx, py), (lastx, snap(py - GAP)))]
            res["powers"].append((N, (lastx, snap(py - GAP)), "up", D.text(N)))
            res["boxes"].append(power_box(D.text(N), (lastx, py - GAP), "up"))
        elif series:
            si, pn_, pO, M, r = series[0]
            if not pose_pin(si, pn_, (x, py), (-d, 0)):
                return None
            bx0 = sym_boxes(si)
            edge = min(b[0] for b in bx0) if d > 0 else max(b[2] for b in bx0)
            front = px
            need = front + d * (U + (text_width(D.text(N)) + 2.4 if D.named(N) else 0.8))
            if (need - edge) * d > 0.01:
                x = snap(x + d * math.ceil((need - edge) * d / (2 * U)) * 2 * U)
                pose_pin(si, pn_, (x, py), (-d, 0))
            far = tuple(pins(si)[pO]["xy"])
            res["boxes"] += sym_boxes(si)
            res["used"].append(r)
            res["syms"].append(si)
            res["wires"].append((N, (px, py), (x, py)))
            if D.named(N):
                lp = (snap(px + d * U), py)
                res["terms"].append((N, lp, (d, 0)))
                res["resv"].append((N, lbl_box(D.text(N), lp, (d, 0), D.label_kind(N))))
            held = (set(res["used"]) | set(_chain[0])) - self.used
            self.used |= held
            sub = [] if (M in _chain[1] or M == N) else self.elems_on(M)
            self.used -= held
            kk = max(4, 2 * math.ceil((text_width(D.text(M)) + 3.0) / (2 * U))) if D.named(M) else 4
            # (fix round 7) ... and behind a fuse / series diode the input's protection: a TVS or bulk cap from the
            # rail to ground hangs on the rail's wire, not beside it on supply symbols of its own
            shunt = ("C", "D") if PATH_SHUNTS else ("C",)
            caps_m = [e for e in sub if D.is_gnd(e[3]) and _prefix(e[4]) in shunt
                      and "LED" not in (D.comps[e[4]]["lib_id"] + " " + D.comps[e[4]]["value"]).upper()][:3]                 if D.is_rail(M) and not D.is_gnd(M) and _prefix(r) in (("L", "FB", "F", "D") if PATH_SHUNTS else ("L", "FB")) else []
            if caps_m:
                # the power path ends on its rail (SW - L - +5V): the output caps hang right there
                r2 = self.stem(far, d, M, caps_m, 4, (tuple(_chain[0]) + tuple(res["used"]), tuple(_chain[1]) + (N,)))
                if r2 is None:
                    return None
                for key in res:
                    res[key] += r2[key]
            elif D.is_rail(M):
                orient = "down" if D.is_gnd(M) else "up"
                end = (snap(far[0] + d * STUB), far[1])
                q = (end[0], snap(end[1] + (GAP if orient == "down" else -GAP)))
                res["wires"] += [(M, far, end), (M, end, q)]
                res["powers"].append((M, q, orient, D.text(M)))
                res["boxes"].append(power_box(D.text(M), q, orient))
            elif sub:
                r2 = self.stem(far, d, M, sub, kk, (tuple(_chain[0]) + tuple(res["used"]), tuple(_chain[1]) + (N,)))
                if r2 is None:
                    return None
                for key in res:
                    res[key] += r2[key]
            else:
                end = (snap(far[0] + d * STUB), far[1])
                res["wires"].append((M, far, end))
                res["terms"].append((M, end, (d, 0)))
                res["resv"].append((M, lbl_box(D.text(M), end, (d, 0), D.label_kind(M))))
        else:
            end = (snap((lastx if lastx is not None else px) + d * STUB), py)
            res["wires"].append((N, (px, py), end))
            res["terms"].append((N, end, (d, 0)))
            res["resv"].append((N, lbl_box(D.text(N), end, (d, 0), D.label_kind(N))))
        return res

    def try_stem(self, tip, d, N, elems):
        D = self.D
        k0 = 4
        if not D.is_rail(N) and D.named(N) and any(not D.is_rail(e[3]) or (
                not D.is_gnd(e[3]) and _prefix(e[4]) in ("L", "FB", "F", "D")) for e in elems):
            k0 = max(4, 2 * math.ceil((text_width(D.text(N)) + 4.0) / (2 * U)))
        for k in range(k0, 64, 2):
            r = self.stem(tip, d, N, elems, k)
            if r is None:
                return False
            boxes = r["boxes"] + [wbox(a, b, 0.02) for _, a, b in r["wires"]] + [b for _, b in r["resv"]]
            if self.free(boxes):
                self.wires += r["wires"]
                self.powers += r["powers"]
                self.terms += r["terms"]
                self.boxes += r["boxes"] + [wbox(a, b) for _, a, b in r["wires"]]
                self.resv += r["resv"]
                self.used |= set(r["used"])
                self.syms += r["syms"]
                return True
        return False

    # ── one anchor (or a small part treated as one): pose, stubs, rails, stems ──
    def place(self, si, at, stems=True, face=None, rot=None, mirror="keep", skip=(), hook=None):
        """`skip` = pin numbers a stage idiom draws itself (no stub, no stem, no terminal here); `hook()` is
        called once the plain signal pins are drawn and before the supply pins and stems take their room (the
        crystal and CC templates run there)"""
        D = self.D
        ref = si.ref
        set_pose(si, at[0], at[1], si.rot if rot is None else rot,
                 "keep" if rot is None else (None if mirror == "keep" else mirror))
        def net_of(p):
            return D.pins_of[ref].get(p["number"])
        if self.v2 and rot is None and mirror == "keep" and D.npins(ref) <= (FLIP_PINS if SINGLE_L else 8) \
                and len(D.units.get(ref, ())) == 1 and si.mirror in (None, "x"):
            # a small part whose ground pin points up and whose supply pin points down (a TVS array, a supervisor)
            # is flipped top to bottom: the symbols then stand straight on the pins instead of on hooks
            bad = good = 0
            pw_ = si.pins_world()
            for p in pw_:
                k = net_of(p)
                if p["out"][0] == 0 and D.conn(k) and D.is_rail(k):
                    if (p["out"][1] > 0) == D.is_gnd(k):
                        good += 1
                    else:
                        bad += 1
            if SINGLE_L:
                # (package A) the same question for the supply / ground pins on the SIDES: a ground pin wants no
                # wired signal pin below it on its side, a supply pin none above - else its symbol needs a crossing
                # or lies on its side. The pose with fewer such pins wins; a draw keeps the part as it is.
                # A signal pin whose parts hang down to ground (a filter cap, a pull-down) counts the same way: it
                # wants no connected pin below it, one with a pull-up none above.
                for p in pw_:
                    k = net_of(p)
                    if p["out"][1] != 0 or not D.conn(k):
                        continue
                    row = [q for q in pw_ if tuple(q["out"]) == tuple(p["out"]) and abs(q["xy"][0] - p["xy"][0]) < 0.01
                           and D.conn(net_of(q)) and q["number"] != p["number"]]
                    if D.is_rail(k):
                        sig = [q for q in row if not D.is_rail(net_of(q))]
                        below = any(q["xy"][1] > p["xy"][1] + 0.01 for q in sig)
                        above = any(q["xy"][1] < p["xy"][1] - 0.01 for q in sig)
                        bad += (below if D.is_gnd(k) else above)
                        good += (above if D.is_gnd(k) else below)
                        continue
                    el = self.elems_on(k) if stems else []
                    below = any(q["xy"][1] > p["xy"][1] + 0.01 for q in row)
                    above = any(q["xy"][1] < p["xy"][1] - 0.01 for q in row)
                    if any(D.is_gnd(e[3]) for e in el):
                        bad += below
                        good += above
                    if any(D.is_rail(e[3]) and not D.is_gnd(e[3]) for e in el):
                        bad += above
                        good += below
            if bad > good:
                set_pose(si, at[0], at[1], si.rot, "x" if si.mirror is None else None)
        mirror = "keep"
        if face:
            ps = [p for p in si.pins_world() if D.conn(net_of(p)) and not D.is_rail(net_of(p))]
            if not ps and self.v2 and D.is_conn(ref):
                # a supply connector has nothing but rail pins: they look into the sheet all the same
                ps = [p for p in si.pins_world() if D.conn(net_of(p))]
            l = sum(1 for p in ps if tuple(p["out"]) == (-1, 0))
            r = sum(1 for p in ps if tuple(p["out"]) == (1, 0))
            if (face == "right" and l > r) or (face == "left" and r > l):
                mirror = None if si.mirror == "y" else "y"
                if si.mirror in (None, "y"):
                    set_pose(si, at[0], at[1], si.rot, mirror)
        place_fields(si)
        self.syms.append(si)
        self.boxes += sym_boxes(si)
        pw = sorted(si.pins_world(), key=lambda p: (G.side_of(p["out"]), p["xy"][1], p["xy"][0], p["number"]))
        seen, plist = set(), []
        for p in pw:
            if p["number"] not in seen:
                seen.add(p["number"])
                plist.append(p)
        skip = set(skip)
        skip_lanes = []
        if skip:
            # the rows of the pins a stage idiom will draw stay free: no rail hook, no stem may run through them
            skip_lanes = [wbox(p["xy"], (p["xy"][0] + p["out"][0] * 24 * U, p["xy"][1] + p["out"][1] * 24 * U))
                          for p in plist if p["number"] in skip and D.conn(net_of(p))]
            self.boxes += skip_lanes
        stubs = {p["number"]: wbox(p["xy"], (p["xy"][0] + p["out"][0] * STUB, p["xy"][1] + p["out"][1] * STUB))
                 for p in plist if D.conn(net_of(p))}
        small_anchor = D.npins(ref) <= 8
        host = {}
        if stems:
            for p in plist:
                n = net_of(p)
                if not D.conn(n) or D.is_rail(n) or p["out"][1] != 0 or p["number"] in skip:
                    continue
                if n in getattr(self, "no_stem", ()):
                    continue          # (bundle reservation) this net is wired to a peer first: its parts hang on that wire
                if n not in host and (self.elems_on(n) or (self.twins_on(n) and not D.is_conn(ref))):
                    # (a push button is drawn at the IC it resets, or on the net's wire - never squeezed between
                    # the rows of the header that merely carries the same net)
                    host[n] = p["number"]
            for n in sorted({net_of(p) for p in plist if D.is_rail(net_of(p)) and not D.is_gnd(net_of(p))}):
                if self.v2:
                    break                                 # pro2: decoupling is drawn by the decap template
                caps = [e for e in self.elems_on(n) if D.is_gnd(e[3]) and D.role.get(e[4], {}).get("anchor") == ref]
                cand = [p for p in plist if net_of(p) == n and p["out"][1] == 0]
                if not caps or len(cand) != len([p for p in plist if net_of(p) == n]):
                    continue                              # a supply pin on top: its caps stand beside its rail wire
                busy = {G.side_of(p["out"]) for p in plist if p["number"] in host.values()}
                if any(G.side_of(p["out"]) in busy for p in cand):
                    continue                              # that side already carries a stem: no room for hanging caps
                # the caps hang on the SUPPLY pin - not on a strap pin that is merely tied to the rail
                pwr = [p for p in cand if "power_in" in D.ptypes.get((ref, p["number"]), "")
                       or _SUPPLY_PIN.match(D.pin_name.get((ref, p["number"]), ""))]
                if len(cand) == 1 and small_anchor and (pwr or len([e for e in D.pins_of[ref].values() if e == n]) == 1):
                    host[n] = cand[0]["number"]
                elif len(pwr) == 1 and len(caps) <= 3 and (small_anchor or D.npins(ref) <= 12):
                    host[n] = pwr[0]["number"]
        spanned = self._spans(si, plist, net_of, host, skip) if (self.v2 and stems and SPAN) else set()
        hostpins = set(host.values())
        groups, cur = [], None
        for p in plist:
            n = net_of(p)
            if not D.conn(n):
                continue
            if p["number"] in hostpins or not D.is_rail(n) or p["number"] in skip:
                cur = None
                continue
            side = G.side_of(p["out"])
            if cur and cur["net"] == n and cur["side"] == side and (
                    abs(p["xy"][0] - cur["pins"][-1]["xy"][0]) + abs(p["xy"][1] - cur["pins"][-1]["xy"][1])) <= 3 * 2 * U + 0.01:
                cur["pins"].append(p)
            else:
                cur = {"net": n, "side": side, "pins": [p]}
                groups.append(cur)
        if self.v2:
            groups = self._regroup(ref, groups)
            groups = self._rail_pairs(si, plist, groups)
        for p in plist:
            if (ref, p["number"]) in D.view.nc:
                self.ncs.append(tuple(p["xy"]))
                x_, y_ = p["xy"]
                self.boxes.append([x_ - 0.9, y_ - 0.9, x_ + 0.9, y_ + 0.9])
        tpts = {t[1] for t in self.terms}
        # neighbouring pins of ONE signal on one side (USB-C D+ on A6 / B6, a doubled output): a short jumper right
        # at the pins and one name - not two labels, not a wire looped around the other pair. Pairs that interleave
        # (D- between the D+ pins) get jumpers at different depths; a stub then crosses the other jumper squarely.
        comb, cgroups = {}, []
        byn: dict = {}
        for p in plist:
            n = net_of(p)
            if D.conn(n) and not D.is_rail(n) and p["number"] not in hostpins and p["out"][1] == 0 \
                    and p["number"] not in skip:
                byn.setdefault((n, tuple(p["out"])), []).append(p)
        for (n, o), ps in sorted(byn.items(), key=lambda t: (t[0][1], min(p["xy"][1] for p in t[1]))):
            ps.sort(key=lambda p: p["xy"][1])
            if len(ps) < 2 or len({p["xy"][0] for p in ps}) != 1 or ps[-1]["xy"][1] - ps[0]["xy"][1] > 4 * 2 * U + 0.01:
                continue
            y0_, y1_ = ps[0]["xy"][1], ps[-1]["xy"][1]
            lvl = sum(1 for o2, a2, b2, *_r in cgroups if o2 == o and a2 <= y1_ and y0_ <= b2)
            if lvl > 2:
                continue
            cgroups.append((o, y0_, y1_, lvl, n, ps))
            for i, p in enumerate(ps):
                comb[p["number"]] = (lvl, i == 0, i == len(ps) - 1, ps[0])
        if self.v2:
            # the wire of a doubled pair normally leaves at its first pin; it may leave at its last pin instead
            # (beyond the jumpers of the pairs it is interleaved with), which lets a part that is docked here keep
            # its ground pin down although its rows are the other way round
            for o, y0_, y1_, lvl, n, ps in cgroups:
                over = max(l2 for o2, a2, b2, l2, _n2, _ps2 in cgroups if o2 == o and a2 <= y1_ and y0_ <= b2)
                x_ = snap(ps[0]["xy"][0] + o[0] * STUB)
                first = (snap(x_ + o[0] * lvl * 2 * U), snap(ps[0]["xy"][1]))
                last = (first[0], snap(ps[-1]["xy"][1]))
                if over > lvl:
                    tip = (snap(x_ + o[0] * (over + 1) * 2 * U), last[1])
                    self.alts.append((n, first, tip, [(n, last, tip)], o))
                else:
                    self.alts.append((n, first, last, [], o))
        bridged = (self._bridges(si, plist, net_of, hostpins | skip | set(comb) | spanned) if self.v2 else set()) | spanned
        # pro2: the pin next to a pin that carries a stem of the SAME net (TRIG beside THRES on a 555): a short link
        # between the two stubs once the stem is drawn - not a wire around whatever hangs on the stem
        ties = []
        if self.v2:
            byno = {p["number"]: p for p in plist}
            for p in plist:
                n = net_of(p)
                hp = byno.get(host.get(n)) if D.conn(n) and not D.is_rail(n) else None
                if hp is None or hp is p or p["number"] in skip or p["number"] in comb or p["number"] in bridged                         or p["out"][1] != 0 or tuple(hp["out"]) != tuple(p["out"])                         or abs(hp["xy"][0] - p["xy"][0]) > 0.01 or abs(abs(hp["xy"][1] - p["xy"][1]) - 2 * U) > 0.01:
                    continue
                ties.append((p, hp, n))
        tied = {p["number"] for p, _hp, _n in ties}
        for p in plist:                       # plain signal pins: stub + terminal (+ label reservation)
            n = net_of(p)
            if not D.conn(n) or D.is_rail(n) or p["number"] in hostpins or p["number"] in skip or p["number"] in bridged:
                continue
            o = tuple(p["out"])
            end = (snap(p["xy"][0] + o[0] * STUB), snap(p["xy"][1] + o[1] * STUB))
            if p["number"] in tied:
                self.wires.append((n, tuple(p["xy"]), end))
                self.boxes.append(wbox(p["xy"], end))
                continue
            if p["number"] in comb:
                lvl, first, last, p0 = comb[p["number"]]
                end = (snap(end[0] + o[0] * lvl * 2 * U), end[1])
                self.wires.append((n, tuple(p["xy"]), end))
                self.boxes.append(wbox(p["xy"], end))
                if last:
                    top = (end[0], p0["xy"][1])
                    self.wires.append((n, top, end))
                    self.boxes.append(wbox(top, end))
                if not first:
                    continue
                tpts.add(end)
                self.terms.append((n, end, o))
                if D.named(n):
                    self.resv.append((n, lbl_box(D.text(n), end, o, D.label_kind(n))))
                continue
            self.wires.append((n, tuple(p["xy"]), end))
            self.boxes.append(wbox(p["xy"], end))
            if end not in tpts:
                tpts.add(end)
                if o[0] == 0 and D.named(n):
                    # a top / bottom pin: the name reads horizontally at the wire end when no neighbour is in the
                    # way (vertical text is the last resort)
                    tw = text_width(D.text(n)) + 3.0
                    for lo in ((1, 0), (-1, 0)):
                        lb = lbl_box(D.text(n), end, lo, D.label_kind(n))
                        near = [p2 for p2 in plist if p2 is not p and D.conn(net_of(p2)) and
                                hit(lb, wbox(p2["xy"], (p2["xy"][0] + p2["out"][0] * 4 * STUB,
                                                        p2["xy"][1] + p2["out"][1] * 4 * STUB), 1.0), 0)]
                        own = self.boxes.pop()            # the stub itself ends where the label starts
                        ok = not near and self.free([lb])
                        self.boxes.append(own)
                        if ok:
                            o = lo
                            break
                self.terms.append((n, end, o))
                if D.named(n):
                    self.resv.append((n, lbl_box(D.text(n), end, o, D.label_kind(n))))
        pending = {p["number"] for g in groups for p in g["pins"]}      # rail pins that still need a way out
        if self.v2:                                # decoupling first: the caps get the room next to their pins
            groups = [g for g in groups if g.get("sides")] + [g for g in groups if not g.get("sides")]
        def rail_group(g, only_decap=False, compact=False, probe=False):
            """Draw one group of rail pins. pro2 runs it in passes: (1) the decoupling caps, which get the room next
            to their pins; (2) `compact`: the symbols that fit right at the pin - straight, a short L or an arch -
            before the signal stems; (3) whatever is left after the stems, when it is known what is really there.
            Returns False when the pass drew nothing for this group."""
            o = tuple(g["pins"][0]["out"])
            n, ps = g["net"], g["pins"]
            if only_decap and not (stems and g.get("sides")):
                return False
            if self.v2 and stems and g.get("sides") and only_decap:
                mine_ = {p["number"] for p in ps}
                lanes_ = [wbox(p2["xy"], (p2["xy"][0] + p2["out"][0] * 30 * U, p2["xy"][1] + p2["out"][1] * 30 * U))
                          for p2 in plist if p2["number"] not in mine_ and p2["number"] in hostpins]
                # a supply pin on top / bottom that still waits needs its stub AND its upright symbol (7 steps)
                lanes_ += [wbox(p2["xy"], (p2["xy"][0] + p2["out"][0] * 3 * U, p2["xy"][1] + p2["out"][1] * 3 * U))
                           if p2["out"][1] == 0 else
                           [p2["xy"][0] - 1.4, min(p2["xy"][1], p2["xy"][1] + p2["out"][1] * 7 * U),
                            p2["xy"][0] + 1.4, max(p2["xy"][1], p2["xy"][1] + p2["out"][1] * 7 * U)]
                           for p2 in plist if p2["number"] not in mine_ and p2["number"] in pending]
                share = 1 + sum(1 for g2 in groups if g2 is not g and g2["net"] == n and g2.get("sides")
                                and any(p2["number"] in pending for p2 in g2["pins"]))
                if self.try_decap(ref, n, ps, lanes_, g["sides"], share):
                    pending.difference_update(p["number"] for p in ps)
                    return True
                return False
            pending.difference_update(p["number"] for p in ps)
            w0 = len(self.wires)
            dep_ = STUB + g.get("depth", 0) * U       # (a joined pair of pin groups leaves two steps further out)
            ends = [(snap(p["xy"][0] + o[0] * dep_), snap(p["xy"][1] + o[1] * dep_)) for p in ps]
            own = []
            for p, e in zip(ps, ends):
                self.wires.append((n, tuple(p["xy"]), e))
                own.append(wbox(p["xy"], e))
            if len(ends) > 1:
                self.wires.append((n, ends[0], ends[-1]))
                own.append(wbox(ends[0], ends[-1]))
            gnd = D.is_gnd(n)
            name = D.text(n)
            mine = {p["number"] for p in ps}
            if self.v2 and o[1] != 0 and len(ps) == 1:
                # under / over a pass-through bar (see _bridges): straight on through it, the symbol beyond
                bar = next((b for b in self.bars if b[2] < ends[0][0] < b[3]
                            and 0 < (b[1] - ends[0][1]) * o[1] <= 4 * U + 0.01), None)
                if bar is not None:
                    q = (ends[0][0], snap(bar[1] + o[1] * 2 * U))
                    nat_ = "down" if gnd else "up"
                    self.wires.append((n, ends[0], q))
                    self.powers.append((n, q, nat_, name))
                    self.boxes += own + power_boxes(name, q, nat_) + [wbox(ends[0], q)]
                    return True
            # lanes kept free: the pins that will carry stems (their extent is not known yet); a plain pin's stub
            # and name are already on the drawing
            other = [wbox(p2["xy"], (p2["xy"][0] + p2["out"][0] * 30 * U, p2["xy"][1] + p2["out"][1] * 30 * U))
                     for p2 in plist if p2["number"] not in mine and p2["number"] in hostpins
                     and (compact or not self.v2)]
            lane_ = SINGLE_L_LANE if (self.v2 and SINGLE_L and not compact) else 16
            other += [wbox(p2["xy"], (p2["xy"][0] + p2["out"][0] * (lane_ if p2["out"][1] == 0 else 16) * U,
                                      p2["xy"][1] + p2["out"][1] * 16 * U))
                      for p2 in plist if p2["number"] not in mine and p2["number"] in pending
                      and not (net_of(p2) == n and tuple(p2["out"]) == o)]
            if self.v2:                             # the next group of this very rail: its stubs and bar
                other += [wbox(p2["xy"], (p2["xy"][0] + p2["out"][0] * 3 * U, p2["xy"][1] + p2["out"][1] * 3 * U))
                          for p2 in plist if p2["number"] not in mine and p2["number"] in pending
                          and net_of(p2) == n and tuple(p2["out"]) == o]
            if stems and not gnd and o == (0, -1) and self.pin_bank(ref, n, ends[len(ends) // 2], other):
                self.boxes += own
                return True
            if stems and not gnd and o[1] == 0 and len(ends) == 1 and self.bank_caps(ref) and (
                    "power_in" in D.ptypes.get((ref, ps[0]["number"]), "")
                    or _SUPPLY_PIN.match(D.pin_name.get((ref, ps[0]["number"]), ""))):
                # a supply pin on the side: its wire steps out, turns up to the rail symbol, the caps stand beside it
                done = False
                for kk in (2, 4, 6, 8, 10, 12):
                    e2 = (snap(ends[0][0] + o[0] * kk * U), ends[0][1])
                    if not self.free([wbox(ends[0], e2, 0.02)], other):
                        break
                    if self.pin_bank(ref, n, e2, other, away=o[0],
                                     cross=SINGLE_L_CROSS if (self.v2 and SINGLE_L and DECAP_ON_WIRE and not compact) else 0):
                        self.wires.append((n, ends[0], e2))
                        self.boxes += own + [wbox(ends[0], e2)]
                        done = True
                        break
                if done:
                    return True
            cands = []
            nat = "down" if gnd else "up"
            dy = GAP if gnd else -GAP
            if o[1] == 0:
                e = ends[-1] if gnd else ends[0]            # ground leaves at the lowest pin, supply at the top one
                for kk in (0, 2, 4, 6, 8, 10, 12):        # upright: at the stub, else out past the neighbours' names
                    e2 = (snap(e[0] + o[0] * kk * U), e[1])
                    q = (e2[0], snap(e[1] + dy))
                    cands.append((q, nat, ([(n, e, e2)] if kk else []) + [(n, e2, q)]))
                mid = ends[len(ends) // 2]
                for kk in (1, 4, 6, 8, 10, 12, 14, 1):      # sideways only when nothing upright is free
                    q3 = (snap(mid[0] + o[0] * kk * U), mid[1])
                    cands.append((q3, "right" if o[0] > 0 else "left", [(n, mid, q3)]))
            else:
                mid = ends[len(ends) // 2]
                natural = (o[1] > 0) == gnd                 # ground on a bottom pin, supply on a top pin
                if natural:
                    for kk in (0, 2, 4, 6, 8, 0):           # straight on, further out if a neighbour's label is there
                        q4 = (mid[0], snap(mid[1] + o[1] * kk * U))
                        cands.append((q4, nat, [(n, mid, q4)] if kk else []))
                else:
                    # supply on a bottom pin / ground on a top pin: a hook to the side keeps the symbol upright
                    for kk in (0, 2, 4):
                        for sx in (1, -1):
                            for dd in (2, 4, 6):
                                a1 = (mid[0], snap(mid[1] + o[1] * kk * U))
                                a2 = (snap(a1[0] + sx * dd * U), a1[1])
                                q4 = (a2[0], snap(a2[1] + dy))
                                cands.append((q4, nat, ([(n, mid, a1)] if kk else []) + [(n, a1, a2), (n, a2, q4)]))
                    for kk in (0, 2, 4, 0):
                        q4 = (mid[0], snap(mid[1] + o[1] * kk * U))
                        for orient in ("right", "left"):
                            cands.append((q4, orient, [(n, mid, q4)] if kk else []))
            if self.v2 and SINGLE_L and o[1] == 0:
                e = ends[-1] if gnd else ends[0]
                sg = 1 if gnd else -1

                def commit(q, orient, ws):
                    if DECAP_ON_WIRE and stems and not gnd and len(ps) == 1 and orient == "up" and not g.get("depth"):
                        # the decoupling caps of this pin hang on its own wire, beyond the rise to the symbol
                        c_, q_ = ws[-1][1], ws[-1][2]
                        hung = self._wire_decaps(ref, n, ps[0], c_, o, list(other) + power_boxes(name, q_, orient)
                                                 + [wbox((c_[0], snap(c_[1] - U)), q_, 0.3)])
                        if RAIL_BUS and not hung and g.get("sides") and self._decap_roles(ref, n):
                            # (class D) no room under the wire: the rise goes on to a bar above the rows, the caps
                            # hang on that bar beside the rise and the supply symbol stands on it - wired to the
                            # pin, not a row of their own somewhere above the part
                            lead = ws[:-1]
                            if self.pin_bank(ref, n, c_, other, cross=SINGLE_L_CROSS, reach=True):
                                self.wires += lead
                                self.boxes += own + [wbox(a, b) for _, a, b in lead]
                                return True
                    self.wires += ws
                    self.powers.append((n, q, orient, name))
                    self.boxes += own + power_boxes(name, q, orient) + [wbox(a, b) for _, a, b in ws]
                    return True
                # (before the stems are drawn only the short ways count: a long one would step over the lane of a
                # neighbour that still waits)
                lone = bool(g.get("lying")) and not compact and not probe
                for kk in () if lone else (0, 2, 4, 6, 8, 10, 12) + (() if compact else (14, 16, 20)):
                    e2 = (snap(e[0] + o[0] * kk * U), e[1])
                    if kk and not self.free([wbox(e, e2, 0.02)], other):
                        break
                    for dy_ in (GAP, U):
                        q = (e2[0], snap(e[1] + sg * dy_))
                        ws = ([(n, e, e2)] if kk else []) + [(n, e2, q)]
                        if self.free(power_boxes(name, q, nat) + [wbox(e2, q, 0.02)], other) and self.words_apart(name, q, nat):
                            if probe:
                                del self.wires[w0:]
                                pending.update(mine)
                                return True
                            return commit(q, nat, ws)
                if compact or probe:
                    del self.wires[w0:]
                    pending.update(mine)
                    return False
                # the open ends of this part that a wire will leave later (the router, a docked part): a symbol
                # must not stand in their way - the wire would be led around it
                ahead = [wbox(t[1], (t[1][0] + t[2][0] * AHEAD * U, t[1][1] + t[2][1] * AHEAD * U), 0.3) for t in self.terms]
                ahead += [wbox(t_, (t_[0] + o_[0] * AHEAD * U, t_[1] + o_[1] * AHEAD * U), 0.3)
                          for _n, _f, t_, _ws, o_ in self.alts]      # (the other end a doubled pin pair offers)
                cx = None if lone else self._plan_l(n, name, e, o, gnd, other, SINGLE_L_CROSS, ahead)
                if cx is not None:
                    return commit(*cx)
                # nothing upright without an arch: the symbol lies in the pin's own row (counted and reported),
                # right at the stub or beyond ONE wire that it crosses squarely
                mid = ends[len(ends) // 2]
                side_ = []
                occ_ = self.occ() + list(other)
                wk_ = self._wkey()
                for kk in (1, 2, 4, 6, 8, 10, 12):
                    q3 = (snap(mid[0] + o[0] * kk * U), mid[1])
                    cand = (q3, "right" if o[0] > 0 else "left", [(n, mid, q3)])
                    side_.append(cand)
                    c_ = self._leg(n, mid, q3, occ_, wk_)
                    if c_ is not None and c_ <= 1 and not any(hit(power_box(name, q3, cand[1]), o_) for o_ in occ_ + ahead):
                        commit(*cand)
                        return "side"

                def touched(c):
                    bx = [power_box(name, c[0], c[1])] + [wbox(a, b, 0.02) for _, a, b in c[2]]
                    return sum(1 for o_ in occ_ if any(hit(b_, o_) for b_ in bx))
                commit(*min(side_, key=touched))
                return "side"
            if self.v2:
                # supplies point up, grounds down. A pin that faces sideways gets an L (out along the pin, then
                # vertical to the symbol); when the rows next to it are taken, an arch: out, three rows the other
                # way, over, and the symbol hangs / stands there upright. A sideways symbol is the very last resort.
                up_ = [c for c in cands if c[1] in ("up", "down")]
                n_legacy = len(up_)
                arches = []
                side_ = [c for c in cands if c[1] not in ("up", "down")]
                sg = 1 if gnd else -1
                if o[1] == 0:
                    e = ends[-1] if gnd else ends[0]
                    e_far = ends[0] if gnd else ends[-1]
                    for kk in (14, 16, 20):
                        e2 = (snap(e[0] + o[0] * kk * U), e[1])
                        up_.append(((e2[0], snap(e[1] + dy)), nat, [(n, e, e2), (n, e2, (e2[0], snap(e[1] + dy)))]))
                    for kk in (2, 4, 6, 8):
                        for w in (4, 6, 8, 10):
                            a1 = (snap(e_far[0] + o[0] * kk * U), e_far[1])
                            a2 = (a1[0], snap(e_far[1] - sg * 6 * U))
                            q4 = (snap(a2[0] + o[0] * w * U), a2[1])
                            up_.append((q4, nat, [(n, e_far, a1), (n, a1, a2), (n, a2, q4)]))
                            arches.append(up_[-1])
                    for kk in (24, 28, 32):
                        e2 = (snap(e[0] + o[0] * kk * U), e[1])
                        up_.append(((e2[0], snap(e[1] + dy)), nat, [(n, e, e2), (n, e2, (e2[0], snap(e[1] + dy)))]))
                else:
                    mid = ends[len(ends) // 2]
                    for kk in (0, 2, 4, 6, 8):
                        for sx in (1, -1):
                            for dd in (2, 4, 6, 8, 10):
                                a1 = (mid[0], snap(mid[1] + o[1] * kk * U))
                                a2 = (snap(a1[0] + sx * dd * U), a1[1])
                                q4 = (a2[0], snap(a2[1] + dy))
                                up_.append((q4, nat, ([(n, mid, a1)] if kk else []) + [(n, a1, a2), (n, a2, q4)]))
                    if SINGLE_L and not natural:
                        # (package A) ground on a top pin / supply on a bottom pin: a short stub, sideways from
                        # the OUTER pin of the group, then down (up) to the symbol - the lowest and shortest such
                        # way first, the symbol tucked close under its wire when that keeps it clear of the part
                        up_ = []
                        for kk in (0, 2, 4, 6, 8):
                            for dd in (4, 6, 8, 10, 2):
                                for sx in (1, -1):
                                    e_ = ends[-1] if sx > 0 else ends[0]
                                    for dy_ in (GAP, U):
                                        a1 = (e_[0], snap(e_[1] + o[1] * kk * U))
                                        a2 = (snap(a1[0] + sx * dd * U), a1[1])
                                        q4 = (a2[0], snap(a2[1] + (dy_ if gnd else -dy_)))
                                        up_.append((q4, nat, ([(n, e_, a1)] if kk else []) + [(n, a1, a2), (n, a2, q4)]))
                cands = up_ + (side_[:1] or up_[:1])
                if compact:
                    for q, orient, ws in (up_[:n_legacy] + ([] if ARCH_LAST else arches) if o[1] == 0 else up_):
                        if self.free(power_boxes(name, q, orient) + [wbox(a, b, 0.02) for _, a, b in ws], other)                                 and self.words_apart(name, q, orient):
                            self.wires += ws
                            self.powers.append((n, q, orient, name))
                            self.boxes += own + power_boxes(name, q, orient) + [wbox(a, b) for _, a, b in ws]
                            return True
                    del self.wires[w0:]
                    pending.update(mine)
                    return False
            last = len(cands) - 1
            if self.v2 and ARCH_LAST and o[1] == 0 and not any(
                    self.free(power_boxes(name, q, orient) + [wbox(a, b, 0.02) for _, a, b in ws], other)
                    and self.words_apart(name, q, orient) for q, orient, ws in cands[:n_legacy]):
                # no straight L is free: one square crossing of the neighbour's wire reads better than an arch
                cx = self._crossing_l(n, name, ends[-1] if gnd else ends[0], o, gnd, other, 1)
                if cx is not None:
                    q, orient, ws = cx
                    self.wires += ws
                    self.powers.append((n, q, orient, name))
                    self.boxes += own + power_boxes(name, q, orient) + [wbox(a, b) for _, a, b in ws]
                    return True
            if self.v2:
                # nothing is free: the candidate that touches the least (never blindly the last one - that put a
                # ground bar through the value text of a supply symbol)
                occ_ = self.occ() + list(other)

                def touched(ci):
                    q, orient, ws = cands[ci]
                    bx = power_boxes(name, q, orient) + [wbox(a, b, 0.02) for _, a, b in ws]
                    return sum(1 for o_ in occ_ if any(hit(b_, o_) for b_ in bx))                         + (0 if self.words_apart(name, q, orient) else 1)
                tl = [touched(ci) for ci in range(len(cands))]
                last = min(range(len(cands)), key=lambda ci: (tl[ci], ci))
                if o[1] == 0 and cands[last][1] not in ("up", "down"):
                    # only a sideways symbol is free: an L that crosses the neighbour's wire squarely (at most two
                    # wires, clear of every wire end and junction) keeps the symbol upright
                    cx = self._crossing_l(n, name, ends[-1] if gnd else ends[0], o, gnd, other)
                    if cx is not None:
                        q, orient, ws = cx
                        self.wires += ws
                        self.powers.append((n, q, orient, name))
                        self.boxes += own + power_boxes(name, q, orient) + [wbox(a, b) for _, a, b in ws]
                        return True
            for ci, (q, orient, ws) in enumerate(cands):
                pb = power_box(name, q, orient)
                if (ci >= last if self.v2 else (self.free([pb] + [wbox(a, b, 0.02) for _, a, b in ws], other)
                                                or ci == last)):
                    self.wires += ws
                    self.powers.append((n, q, orient, name))
                    self.boxes += own + (power_boxes(name, q, orient) if self.v2 else [pb]) + [wbox(a, b) for _, a, b in ws]
                    break
            return True
        if self.v2 and stems and RAIL_BUS:
            bused = self.rail_buses(ref, si, plist, groups, pending, hostpins)
            if bused:
                pending.difference_update(bused)
                groups = [dict(g, pins=[p for p in g["pins"] if p["number"] not in bused]) for g in groups]
                groups = [g for g in groups if g["pins"]]
        deferred = []
        for g in groups:
            if not self.v2:
                pending.difference_update(p["number"] for p in g["pins"])
                rail_group(g)
            elif not rail_group(g, True):
                deferred.append(g)
        deferred.sort(key=lambda g: min(plist.index(p) for p in g["pins"]))
        deferred = [g for g in deferred if not rail_group(g, False, True)]
        # (bundle reservation, dock.build_block) the rows of the nets this part shares with a peer of its block stay
        # free while its own parts are placed: no ground symbol of an LED chain, no crystal on a row that is wired
        # through to a header later
        row_lanes = [wbox((p["xy"][0] + p["out"][0] * (STUB + 0.6), p["xy"][1]),
                          (p["xy"][0] + p["out"][0] * ROW_LANE * U, p["xy"][1]), 0.6)
                     for p in plist if p["out"][1] == 0 and p["number"] not in skip
                     and net_of(p) in getattr(self, "row_lanes", ())]
        if hook is not None:
            # the supply pins that fit right at the part have their symbols; now the stage idioms on the reserved
            # rows (crystal, CC pull-downs), before the stems and the far-out symbols take the room around them
            self.boxes = [b for b in self.boxes if not any(b is t for t in skip_lanes)]
            skip_lanes = []
            # (fix round 3) the supply / ground pins of a side that still wait for their symbol: a stage that is
            # drawn now keeps the way out of those pins free, so the symbol finds room beyond the neighbours' names
            self.rail_lanes = [wbox(p["xy"], (p["xy"][0] + p["out"][0] * RAIL_LANE * U, p["xy"][1]))
                               for g in deferred for p in g["pins"] if p["out"][1] == 0]
            # ... and the rows that are wired through to a peer later (a stage idiom is tried with these lanes
            # first and without them second: the crystal at its pins comes before the header's wire)
            self.rail_lanes += row_lanes
            hook()
            self.rail_lanes = []
        self.boxes += row_lanes
        # the supply / ground pins that are drawn after the stems keep a lane free in front of them
        tmp_lanes = [wbox(p["xy"], (p["xy"][0] + p["out"][0] * 12 * U, p["xy"][1] + p["out"][1] * 12 * U))
                     for g in deferred for p in g["pins"]]
        self.boxes += tmp_lanes
        tmp_lanes = tmp_lanes + row_lanes                 # (both leave the block together, below)

        def skey(p):
            n = net_of(p)
            el = self.elems_on(n)
            up = any(D.is_rail(e[3]) and not D.is_gnd(e[3]) for e in el)
            dn = any(D.is_gnd(e[3]) for e in el) or bool(self.twins_on(n))
            # in-line parts first (they stay in their pin row), then everything that hangs down from the lowest
            # pin upwards and what stands up from the top pin downwards: later stems step outwards past the
            # earlier ones (a staircase) instead of dropping through a neighbour's row
            cls = 4 if D.is_rail(n) else 0 if not (up or dn) else 1 if dn else 2
            return (cls, -p["xy"][1] if cls == 1 else p["xy"][1])
        for p in sorted(plist, key=skey):
            n = net_of(p)
            if host.get(n) != p["number"]:
                continue
            el = self.elems_on(n)
            if D.is_rail(n):
                el = [e for e in el if D.is_gnd(e[3]) and D.role.get(e[4], {}).get("anchor") == ref]
            if (el or self.twins_on(n)) and self.v2 and not D.is_rail(n) and self.try_node(p, n):
                continue
            if not el or not self.try_stem(tuple(p["xy"]), p["out"][0], n, el):
                o = tuple(p["out"])                  # could not pack: plain pin, its parts become cells
                end = (snap(p["xy"][0] + o[0] * STUB), p["xy"][1])
                self.wires.append((n, tuple(p["xy"]), end))
                self.boxes.append(wbox(p["xy"], end))
                if D.is_rail(n):
                    side_o = "right" if o[0] > 0 else "left"
                    opts = []
                    for kk in (0, 2, 4, 6, 8, 10, 12):
                        e2 = (snap(end[0] + o[0] * kk * U), end[1])
                        q = (e2[0], snap(e2[1] - GAP))
                        opts.append((q, "up", [(n, end, e2), (n, e2, q)] if kk else [(n, end, q)]))
                    for kk in (0, 4, 6, 8, 10, 12, 14):
                        q = (snap(end[0] + o[0] * kk * U), end[1])
                        opts.append((q, side_o, [(n, end, q)] if kk else []))
                    opts.append((end, side_o, []))
                    if self.v2:
                        opts = [x for x in opts if x[1] == "up"]
                        opts.append(opts[0])
                    for oi, (q, orient, ws) in enumerate(opts):
                        pb = power_box(D.text(n), q, orient)
                        if self.free([pb] + [wbox(a, b, 0.02) for _, a, b in ws]) or oi == len(opts) - 1:
                            self.wires += ws
                            self.powers.append((n, q, orient, D.text(n)))
                            self.boxes += [pb] + [wbox(a, b) for _, a, b in ws]
                            break
                else:
                    if D.named(n):
                        # the name must not land on something a neighbour's stem drew: run the wire on past it
                        for kk in (0, -2, 2, 4, 6, 8, 10, 12, 16, 20):
                            e2 = (snap(end[0] + o[0] * kk * U), end[1])
                            own = self.boxes.pop()
                            ok = self.free([lbl_box(D.text(n), e2, o, D.label_kind(n))] + ([wbox(end, e2, 0.02)] if kk > 0 else []))
                            self.boxes.append(own)
                            if ok:
                                if kk < 0:
                                    end = e2                      # no room beyond the stub: the name sits on it
                                elif kk:
                                    self.wires.append((n, end, e2))
                                    self.boxes.append(wbox(end, e2))
                                    end = e2
                                break
                    self.terms.append((n, end, o))
                    if D.named(n):
                        self.resv.append((n, lbl_box(D.text(n), end, o, D.label_kind(n))))

        for p, hp, n in ties:
            o = tuple(p["out"])
            end = (snap(p["xy"][0] + o[0] * STUB), snap(p["xy"][1]))
            tgt = (end[0], snap(hp["xy"][1]))
            on = any(n2 == n and a[1] == b[1] == tgt[1] and min(a[0], b[0]) - 0.01 <= tgt[0] <= max(a[0], b[0]) + 0.01
                     for n2, a, b in self.wires)
            if on:
                self.wires.append((n, end, tgt))
                self.boxes.append(wbox(end, tgt))
                continue
            self.terms.append((n, end, o))                # the stem did not come this way: an ordinary open end
            if D.named(n):
                self.resv.append((n, lbl_box(D.text(n), end, o, D.label_kind(n))))
        self.boxes = [b for b in self.boxes if not any(b is t for t in tmp_lanes)]
        deferred.sort(key=lambda g: min(plist.index(p) for p in g["pins"]))
        if self.v2 and SINGLE_L:
            deferred = self._join_side(ref, deferred, lambda g: rail_group(g, False, False, True))
            # a supply pin that carries decoupling caps first: its rise needs the room next to the pin
            deferred.sort(key=lambda g: 0 if (g.get("sides") and g["pins"][0]["out"][1] == 0) else 1)
        if self.v2 and SINGLE_L and len(deferred) > 1:
            # a pin that ends with a sideways symbol is drawn FIRST in a second go: the symbol then lies right at
            # its pin, and the rises / drops of its neighbours are planned around it instead of through its text
            keep = [len(x_) for x_ in (self.wires, self.powers, self.boxes, self.resv, self.terms, self.syms,
                                       self.flags, self.ncs, self.labels, self.alts, self.bars, self.fallback)]
            used0, pend0 = set(self.used), set(pending)
            st0 = (len(self.stats["matches"]), set(self.stats["claimed"])) if self.stats else None
            stuck = [g for g in deferred if rail_group(g) == "side"]
            if stuck:
                if st0 is not None:
                    del self.stats["matches"][st0[0]:]
                    self.stats["claimed"].intersection_update(st0[1])
                for x_, k_ in zip((self.wires, self.powers, self.boxes, self.resv, self.terms, self.syms,
                                   self.flags, self.ncs, self.labels, self.alts, self.bars, self.fallback), keep):
                    del x_[k_:]
                self.used.intersection_update(used0)
                pending.clear()
                pending.update(pend0)
                for g in stuck:
                    g["lying"] = True
                for g in stuck + [g for g in deferred if not any(g is s_ for s_ in stuck)]:
                    rail_group(g)
        else:
            for g in deferred:
                rail_group(g)
        if skip_lanes:
            self.boxes = [b for b in self.boxes if not any(b is t for t in skip_lanes)]

    def _rail_pairs(self, si, plist, groups: list) -> list:
        """pro2: the doubled contact of a small part (pins 3 / 4 of a tactile switch: one on the left, one on the
        right of one row, both on ground or both on one supply): ONE bar under (ground) or over (supply) the part
        and ONE symbol in its middle - not a symbol at each end. -> the groups that are still to be drawn."""
        D = self.D
        ref = si.ref
        if not (RAIL_PAIR_BAR or CONTACT_LINK) or D.npins(ref) > 4 or len(D.units.get(ref, ())) != 1:
            return groups
        if CONTACT_LINK:
            # (package B) the shortest straight link: tip to tip along the pin row, on the line the symbol draws
            # between its two contacts, and ONE symbol straight below (ground) / above (supply) one tip
            out = list(groups)
            for ga in groups:
                for gb_ in groups:
                    if ga not in out or gb_ not in out or ga is gb_ or ga["net"] != gb_["net"] or ga.get("sides")                             or gb_.get("sides") or len(ga["pins"]) != 1 or len(gb_["pins"]) != 1:
                        continue
                    pl, pr = ga["pins"][0], gb_["pins"][0]
                    if tuple(pl["out"]) != (-1, 0) or tuple(pr["out"]) != (1, 0) or abs(pl["xy"][1] - pr["xy"][1]) > 0.01:
                        continue
                    n = ga["net"]
                    gnd = D.is_gnd(n)
                    nat = "down" if gnd else "up"
                    name = D.text(n)
                    a, b = (snap(pl["xy"][0]), snap(pl["xy"][1])), (snap(pr["xy"][0]), snap(pr["xy"][1]))
                    fb_old = si.field_boxes()
                    occ = [o for o in self.occ() if o not in fb_old]
                    for tip in (b, a):
                        q = (tip[0], snap(tip[1] + (U if gnd else -U)))
                        bx = [wbox(tip, q, 0.02)] + power_boxes(name, q, nat)
                        if any(hit(x_, o) for x_ in bx for o in occ):
                            continue
                        self.wires += [(n, a, b), (n, tip, q)]
                        self.powers.append((n, q, nat, name))
                        self.boxes += [wbox(a, b), wbox(tip, q)] + power_boxes(name, q, nat)
                        out = [g for g in out if g is not ga and g is not gb_]
                        break
            return out
        gb = si.body_bbox()
        fb_old = si.field_boxes()
        ys = [p["xy"][1] for p in plist]
        out = list(groups)
        for ga in groups:
            for gb_ in groups:
                if ga not in out or gb_ not in out or ga is gb_ or ga["net"] != gb_["net"] or ga.get("sides") or gb_.get("sides"):
                    continue
                if len(ga["pins"]) != 1 or len(gb_["pins"]) != 1:
                    continue
                pl, pr = ga["pins"][0], gb_["pins"][0]
                if tuple(pl["out"]) != (-1, 0) or tuple(pr["out"]) != (1, 0) or abs(pl["xy"][1] - pr["xy"][1]) > 0.01:
                    continue
                n = ga["net"]
                gnd = D.is_gnd(n)
                sg = 1 if gnd else -1
                y = snap(pl["xy"][1])
                if any((yy - y) * sg > 0.01 for yy in ys):
                    continue                              # another pin row lies on that side of the part
                edge = gb[3] if gnd else gb[1]
                k = 1
                while (y + sg * k * 2 * U - edge) * sg < U - 0.01:
                    k += 1
                yb = snap(y + sg * k * 2 * U)
                el = (snap(pl["xy"][0] - STUB), y)
                er = (snap(pr["xy"][0] + STUB), y)
                a, b = (el[0], yb), (er[0], yb)
                mid = (snap((el[0] + er[0]) / 2), yb)
                if not (el[0] + U < mid[0] < er[0] - U):
                    continue
                name = D.text(n)
                nat = "down" if gnd else "up"
                ws = [(n, tuple(pl["xy"]), el), (n, tuple(pr["xy"]), er), (n, el, a), (n, er, b), (n, a, mid), (n, mid, b)]
                occ = [o for o in self.occ() if o not in fb_old]
                bx = [wbox(p_, q_, 0.02) for _n, p_, q_ in ws[2:]] + power_boxes(name, mid, nat)
                if any(hit(x_, o) for x_ in bx for o in occ):
                    continue
                self.wires += ws
                self.powers.append((n, mid, nat, name))
                self.boxes += [wbox(p_, q_) for _n, p_, q_ in ws] + power_boxes(name, mid, nat)
                out = [g for g in out if g is not ga and g is not gb_]
        return out

    def _spans(self, si, plist, net_of, host, skip) -> set:
        """(fix round 6) A two-pin part that lies ACROSS two neighbouring pins of this part (the termination
        resistor between the A and B outputs of a bus transceiver, a damping part across a differential pair) is
        drawn across the pair, not in line with one of its wires - in line it reads as a series part:

            pin a ──┐  ┌─────┬──────── a          the outer wire steps two rows away from the pair, the part
                    └──┘    [R]  ┌──── b          stands upright between the two wires, and the inner wire then
            pin b ───────────┴───┘                steps back so that both leave on neighbouring rows, a over b

        Only when nothing else hangs on the two nets here, each net has one pin on this part, and the rows the step
        needs carry no other pin of that side. -> pin numbers drawn here (their nets leave `host`)."""
        D = self.D
        ref = si.ref
        done: set = set()
        for r in list(self.sat_refs):
            if r in self.used or len(D.units.get(r, ())) != 1 or _prefix(r) not in SPAN_PARTS                     or r in (getattr(self, "stage_claim", None) or ()):
                continue
            po = D.pins_of[r]
            if len(po) != 2:
                continue
            (p1, n1), (p2, n2) = sorted(po.items())
            if n1 == n2 or any(D.is_rail(n) or not D.conn(n) for n in (n1, n2)):
                continue
            ps = []
            for n in (n1, n2):
                mine = [p for p in plist if net_of(p) == n]
                if len(mine) == 1 and len([1 for k in D.pins_of[ref].values() if k == n]) == 1:
                    ps.append(mine[0])
            if len(ps) != 2 or any(p["number"] in skip or p["number"] in done or p["out"][1] != 0 for p in ps):
                continue
            if tuple(ps[0]["out"]) != tuple(ps[1]["out"]) or abs(ps[0]["xy"][0] - ps[1]["xy"][0]) > 0.01                     or round(abs(ps[0]["xy"][1] - ps[1]["xy"][1]) / (2 * U), 2) not in (1.0, 2.0, 3.0):
                continue
            self.used.add(r)
            lone = not any(self.elems_on(n) or self.twins_on(n) for n in (n1, n2))
            self.used.discard(r)
            # ... nor when another two-pin part of the block is on one of the two nets, drawn or not (the inductor of
            # a buck on SW, next to the bootstrap cap between BOOT and SW: the switch pin's row is the power path's)
            if not lone or any(r2 != r and len(D.pins_of.get(r2, {})) == 2 and (n1 in D.pins_of[r2].values() or n2 in D.pins_of[r2].values())
                               for r2 in self.sat_refs):
                continue
            pu, pl = sorted(ps, key=lambda p: p["xy"][1])
            nu, nl = net_of(pu), net_of(pl)
            d = int(pu["out"][0])
            tu, tl = (snap(pu["xy"][0]), snap(pu["xy"][1])), (snap(pl["xy"][0]), snap(pl["xy"][1]))
            sr = D.units[r][0]
            side_rows = {round(q["xy"][1], 2) for q in plist if tuple(q["out"]) == tuple(pu["out"])
                         and q is not pu and q is not pl}
            x0 = snap(tu[0] + d * STUB)
            xr = snap(x0 + d * (2 * U if d > 0 else SPAN_TEXT * U))       # the part's column; its texts stand right of it
            xb = snap(xr + d * (SPAN_TEXT * U if d > 0 else 2 * U))
            xe = snap(xb + d * 2 * U)
            between = {y_ for y_ in side_rows if tu[1] + 0.01 < y_ < tl[1] - 0.01}
            for up in (True, False):
                if up:
                    ya = snap(tl[1] - 6 * U)                 # rows the two nets leave on: a over b, neighbours
                    yb = snap(ya + 2 * U)
                    ws = ([(nu, tu, (x0, tu[1])), (nu, (x0, tu[1]), (x0, ya)), (nu, (x0, ya), (xr, ya))]
                          if abs(ya - tu[1]) > 0.01 else [(nu, tu, (xr, ya))]) + [(nu, (xr, ya), (xe, ya))]
                    ws += [(nl, tl, (xr, tl[1])), (nl, (xr, tl[1]), (xb, tl[1])), (nl, (xb, tl[1]), (xb, yb)), (nl, (xb, yb), (xe, yb))]
                    top, bot = (xr, ya), (xr, tl[1])
                else:
                    yb = snap(tu[1] + 6 * U)
                    ya = snap(yb - 2 * U)
                    ws = ([(nl, tl, (x0, tl[1])), (nl, (x0, tl[1]), (x0, yb)), (nl, (x0, yb), (xr, yb))]
                          if abs(yb - tl[1]) > 0.01 else [(nl, tl, (xr, yb))]) + [(nl, (xr, yb), (xe, yb))]
                    ws += [(nu, tu, (xr, tu[1])), (nu, (xr, tu[1]), (xb, tu[1])), (nu, (xb, tu[1]), (xb, ya)), (nu, (xb, ya), (xe, ya))]
                    top, bot = (xr, tu[1]), (xr, yb)
                if between:
                    break
                if {round(ya, 2), round(yb, 2)} & side_rows:
                    continue
                pn_top = p1 if n1 == nu else p2
                pn_bot = p2 if pn_top == p1 else p1
                if not pose_pin(sr, pn_top, top, (0, -1)):
                    break
                far = pins(sr)[pn_bot]["xy"]
                if abs(far[0] - bot[0]) > 0.01 or abs(far[1] - bot[1]) > 0.01:
                    break                                    # not a part of three rows' length
                terms = [(nu, (xe, ya), (d, 0)), (nl, (xe, yb), (d, 0))]
                resv = [(n, lbl_box(D.text(n), e, o, D.label_kind(n))) for n, e, o in terms if D.named(n)]
                bx = sym_boxes(sr)
                if not self.free(bx + [wbox(a, b, 0.02) for _n, a, b in ws] + [b for _n, b in resv]):
                    continue
                self.wires += ws
                self.boxes += bx + [wbox(a, b) for _n, a, b in ws]
                self.terms += terms
                self.resv += resv
                self.syms.append(sr)
                self.used.add(r)
                done |= {pu["number"], pl["number"]}
                for n in (nu, nl):
                    host.pop(n, None)
                if self.stats is not None:
                    self.stats["matches"].append("span")
                    self.stats["claimed"].add(r)
                break
        return done

    def _bridges(self, si, plist, net_of, taken) -> set:
        """pro2: a small part that one signal passes THROUGH (pin on the left and pin on the right of one row on one
        net: the in / out pins of a TVS array, a common-mode choke's ends tied together): both pins are joined by a
        bar over (top row) or under (bottom row) the part, so the net is ONE wire and carries ONE name. A supply /
        ground pin between the legs crosses the bar squarely and its symbol stands straight beyond it; when such a
        pin points the wrong way (ground up) nothing is bridged and the two sides keep their names.
        -> pin numbers drawn here."""
        D = self.D
        ref = si.ref
        if not ((PASS_THROUGH_BAR and 5 <= D.npins(ref) <= 8) or (CONTACT_BAR and D.npins(ref) == 4))                 or len(D.units.get(ref, ())) != 1:
            return set()
        # (fix round 4) the doubled contact of a 4-pin part (pins 1 / 2 of a tactile switch, both on RUN): the same
        # bar over the part - ONE wire, ONE name, instead of the name printed on both sides of the switch. The part
        # keeps its texts where they are; the bar clears them
        small = D.npins(ref) == 4
        conn = [p for p in plist if D.conn(net_of(p))]
        horiz = [p for p in conn if p["out"][1] == 0]
        byn: dict = {}
        for p in horiz:
            if not D.is_rail(net_of(p)):
                byn.setdefault(net_of(p), []).append(p)
        gb = si.body_bbox()
        done: set = set()
        used_sides: set = set()
        fb_old = si.field_boxes()
        occ = [b for b in self.occ() if b not in fb_old]     # the Reference / Value texts move out of the way below
        bar_y = {}
        for n, ps in sorted(byn.items(), key=lambda t: t[1][0]["xy"][1]):
            if len(ps) != 2 or len([1 for k in D.pins_of[ref].values() if k == n]) != 2 or any(p["number"] in taken for p in ps):
                continue
            pl, pr = sorted(ps, key=lambda p: p["xy"][0])
            if tuple(pl["out"]) != (-1, 0) or tuple(pr["out"]) != (1, 0) or abs(pl["xy"][1] - pr["xy"][1]) > 0.01:
                continue
            y = snap(pl["xy"][1])
            if small and CONTACT_LINK:
                # (package B) the doubled contact of a 4-pin part: the shortest straight link, tip to tip along
                # the pin row (the line the symbol draws between the two pins). The net goes on from either tip.
                a, b = (snap(pl["xy"][0]), y), (snap(pr["xy"][0]), y)
                self.wires.append((n, a, b))
                self.boxes.append(wbox(a, b))
                for e_, o_ in ((a, (-1, 0)), (b, (1, 0))):
                    self.terms.append((n, e_, o_))
                    if D.named(n):
                        self.resv.append((n, lbl_box(D.text(n), e_, o_, D.label_kind(n))))
                done |= {pl["number"], pr["number"]}
                continue
            el = (snap(pl["xy"][0] - STUB), y)
            er = (snap(pr["xy"][0] + STUB), y)
            for sg in (-1, 1):                               # over the top, under the bottom
                if sg in used_sides:
                    continue
                if any((p["xy"][1] - y) * sg > 0.01 for p in horiz if p is not pl and p is not pr):
                    continue                                 # another pin row lies between this row and that edge
                vert = [p for p in conn if p["out"][0] == 0 and p["out"][1] == sg and el[0] < p["xy"][0] < er[0]]
                if any(not D.is_rail(net_of(p)) or (sg > 0) != D.is_gnd(net_of(p)) or p["number"] in taken
                       or min(p["xy"][0] - el[0], er[0] - p["xy"][0]) < 4 * U - 0.01 for p in vert):
                    continue
                if len({round(p["xy"][1], 2) for p in vert}) > 1:
                    continue
                edge = (gb[1] if sg < 0 else gb[3])
                if vert:
                    edge = vert[0]["xy"][1] + sg * STUB
                k = 1
                while (y + sg * k * 2 * U - edge) * sg < 2 * U - 0.01:
                    k += 1
                if small:
                    fbs = [f for f in fb_old if f[2] > el[0] and f[0] < er[0]]
                    while k < 6 and any(hit(wbox((el[0], y + sg * k * 2 * U), (er[0], y + sg * k * 2 * U), 0.9), f) for f in fbs):
                        k += 1
                yb = snap(y + sg * k * 2 * U)
                a, b = (el[0], yb), (er[0], yb)
                ws = [(n, tuple(pl["xy"]), el), (n, tuple(pr["xy"]), er), (n, el, a), (n, a, b), (n, b, er)]
                if any(hit(leg, o_) for leg in (wbox(el, a, 0.02), wbox(b, er, 0.02)) for o_ in occ):
                    continue
                if small and any(hit(leg, o_) for leg in (wbox(el, a, 0.02), wbox(b, er, 0.02), wbox(a, b, 0.02))
                                 for o_ in list(occ) + fb_old):
                    continue
                self.wires += ws
                bar = wbox(a, b)
                self.boxes += [wbox(p_, q_) for _n, p_, q_ in ws[:3]] + [wbox(b, er), bar]
                self.bars.append((n, yb, el[0], er[0], bar))
                for e_, o_ in ((el, (-1, 0)), (er, (1, 0))):
                    self.terms.append((n, e_, o_))
                    if D.named(n):
                        self.resv.append((n, lbl_box(D.text(n), e_, o_, D.label_kind(n))))
                done |= {pl["number"], pr["number"]}
                used_sides.add(sg)
                bar_y[sg] = (yb, er[0], y)
                break
        if done and not small:
            # Reference and Value stand right of the legs, level with the bars (above / below the rows the names
            # of the signals need)
            xr = max(v[1] for v in bar_y.values())
            rows = [v[2] for v in bar_y.values()]
            y_of = {"Reference": bar_y[-1][0] if -1 in bar_y else min(rows) - 4 * U,
                    "Value": bar_y[1][0] if 1 in bar_y else max(rows) + 4 * U}
            for p in sexpr.find_all(si.node, "property"):
                if len(p) > 2 and not _is_hidden(p) and str(p[2]) and str(p[1]) in y_of:
                    at = sexpr.find(p, "at")
                    at[1:] = [num(round(xr + 1.2 + text_width(str(p[2])) / 2, 3)), num(round(y_of[str(p[1])], 3)), 0]
                    _set_justify(p, None)
            self.boxes = [b for b in self.boxes if b not in fb_old] + si.field_boxes()
        return done

    def _cross_row(self, x, y, n):
        """May a vertical wire of net `n` cross row `y` at `x`? None = nothing runs there; True = exactly one
        other net's horizontal wire, and no wire end, junction, terminal or symbol within 2.54 mm; else False."""
        on = [w for w in self.wires if abs(w[1][1] - y) < 0.01 and abs(w[2][1] - y) < 0.01
              and min(w[1][0], w[2][0]) - 0.01 <= x <= max(w[1][0], w[2][0]) + 0.01]
        if not on:
            return None
        if any(w[0] == n for w in on) or len({w[0] for w in on}) != 1:
            return False

        def near(p):
            return max(abs(p[0] - x), abs(p[1] - y)) < 2.53
        for w in self.wires:
            for p in (w[1], w[2]):
                if not near(p):
                    continue
                at = [w2 for w2 in self.wires if any(abs(e[0] - p[0]) < 0.01 and abs(e[1] - p[1]) < 0.01 for e in (w2[1], w2[2]))]
                if not (abs(p[1] - y) < 0.01 and len(at) == 2 and all(abs(w2[1][1] - w2[2][1]) < 0.01 for w2 in at)):
                    return False
        if any(near(t[1]) for t in self.terms) or any(near(p[1]) for p in self.powers) or any(near(f[1]) for f in self.flags):
            return False
        return True

    def _xing(self, x, y, n, vertical: bool = True):
        """May a wire of net `n` cross the wire that runs through (x, y)? `vertical` = the crossing leg is vertical
        (it crosses a row), else horizontal (it crosses a column). None = nothing runs there; True = exactly one
        other net's wire, square, and no wire end, junction, terminal, symbol or no-connect within 2.54 mm."""
        i, j = (1, 0) if vertical else (0, 1)
        pt = (x, y)
        on = [w for w in self.wires if abs(w[1][i] - pt[i]) < 0.01 and abs(w[2][i] - pt[i]) < 0.01
              and min(w[1][j], w[2][j]) - 0.01 <= pt[j] <= max(w[1][j], w[2][j]) + 0.01]
        if not on:
            return None
        if any(w[0] == n for w in on) or len({w[0] for w in on}) != 1:
            return False

        def near(p):
            return max(abs(p[0] - x), abs(p[1] - y)) < 2.53
        for w in self.wires:
            for p in (w[1], w[2]):
                if not near(p):
                    continue
                at = [w2 for w2 in self.wires if any(abs(e[0] - p[0]) < 0.01 and abs(e[1] - p[1]) < 0.01 for e in (w2[1], w2[2]))]
                if not (abs(p[i] - pt[i]) < 0.01 and len(at) == 2 and all(abs(w2[1][i] - w2[2][i]) < 0.01 for w2 in at)):
                    return False
        if any(near(t[1]) for t in self.terms) or any(near(p[1]) for p in self.powers) \
                or any(near(f[1]) for f in self.flags) or any(near(c) for c in self.ncs):
            return False
        return True

    def _leg(self, n, a, b, occ, wkey):
        """A straight wire a - b of net n over the drawing: -> number of other wires it crosses squarely, or None
        when it touches anything else (a body, a text, a wire end, a wire of its own net, a wire along its way)."""
        box = wbox(a, b, 0.02)
        vertical = abs(a[0] - b[0]) < 0.01
        seen = set()
        for bx in occ:
            if not hit(box, bx):
                continue
            w = wkey.get(tuple(round(v_, 3) for v_ in bx))
            if w is None or w[0] == n:
                return None
            wv = abs(w[1][0] - w[2][0]) < 0.01
            if wv == vertical or w[1] == w[2]:
                return None
            pt = (a[0], w[1][1]) if vertical else (w[1][0], a[1])
            if pt in seen:
                continue
            if self._xing(pt[0], pt[1], n, vertical) is not True:
                return None
            if min(abs(pt[0] - a[0]) + abs(pt[1] - a[1]), abs(pt[0] - b[0]) + abs(pt[1] - b[1])) < 2.53:
                return None
            seen.add(pt)
        return len(seen)

    def _wkey(self) -> dict:
        return {tuple(round(v_, 3) for v_ in wbox(a, b)): (nn, a, b) for nn, a, b in self.wires}

    def _plan_l(self, n, name, e, o, gnd: bool, other, most: int = 2, ahead=()):
        """Upright power symbol for a side pin, reached by ONE L from the stub end `e`: out along the pin (the leg
        may cross a vertical wire squarely), then down (ground) / up (supply) across at most `most` wires in all.
        -> (point, 'up'|'down', wires) or None."""
        sg = 1 if gnd else -1
        nat = "down" if gnd else "up"
        occ = self.occ() + list(other)
        wkey = self._wkey()
        best = None
        blocked = None
        for kk in (2, 4, 6, 0, 8, 10, 12, 14, 16, 20, 24):
            if blocked is not None and kk >= blocked:
                continue
            e2 = (snap(e[0] + o[0] * kk * U), e[1])
            c1 = self._leg(n, e, e2, occ, wkey) if kk else 0
            if c1 is None:
                # (a leg that ENDS on a crossing is refused but a longer one may pass: only a real obstacle ends it)
                if not any(self._leg(n, e, (snap(e[0] + o[0] * k2 * U), e[1]), occ, wkey) is not None
                           for k2 in (kk + 2, kk + 4)):
                    blocked = kk
                continue
            if c1 > most:
                continue
            for j in (0, 1, 2, 3):
                for dy_ in ((GAP, U) if j == 0 else (GAP,)):
                    q = (e2[0], snap(e[1] + sg * (j * 2 * U + dy_)))
                    c2 = self._leg(n, e2, q, occ, wkey)
                    if c2 is None or c1 + c2 > most:
                        continue
                    if not self.free(power_boxes(name, q, nat), list(other) + list(ahead)) or not self.words_apart(name, q, nat):
                        continue
                    key = (c1 + c2, kk + 2 * j + (3 if kk < 2 else 0), dy_ == U)
                    if best is None or key < best[0]:
                        best = (key, (q, nat, ([(n, e, e2)] if kk else []) + [(n, e2, q)]))
        return best[1] if best else None

    def _join_side(self, ref, deferred: list, probe) -> list:
        """Side pin groups of one supply / ground that find no free way to a symbol of their own are joined to the
        next group of that net on the same side that lies in their direction (a ground pin to the ground pin
        below it, a supply pin to the supply pin above): ONE bar beside the pins, crossing at most
        SINGLE_L_CROSS wires squarely, and ONE symbol. -> the groups that are still to be drawn."""
        D = self.D
        out = list(deferred)
        for g in list(deferred):
            if g not in out or g.get("sides"):
                continue
            o = tuple(g["pins"][0]["out"])
            n = g["net"]
            if o[1] != 0:
                continue
            gnd = D.is_gnd(n)
            if not gnd and any(k[0] == n for k in self.bank_caps(ref)):
                continue
            if probe(g):
                continue
            tx = g["pins"][0]["xy"][0]
            ys = sorted(p["xy"][1] for p in g["pins"])
            best = None
            for h in out:
                if h is g or h["net"] != n or h.get("sides") or h.get("depth") or tuple(h["pins"][0]["out"]) != o \
                        or abs(h["pins"][0]["xy"][0] - tx) > 0.01:
                    continue
                hy = sorted(p["xy"][1] for p in h["pins"])
                if gnd and hy[0] > ys[-1]:
                    gap = (ys[-1], hy[0])
                elif not gnd and hy[-1] < ys[0]:
                    gap = (hy[-1], ys[0])
                else:
                    continue
                rows = int(round((gap[1] - gap[0]) / (2 * U)))
                if rows > 4 or (best is not None and rows >= best[0]):
                    continue
                wkey = self._wkey()
                occ = self.occ()
                for depth in (2, 4, 6, 0):               # (beyond the names that stand on the rows in between)
                    x = snap(tx + o[0] * (STUB + depth * U))
                    ext = [wbox((snap(p["xy"][0] + o[0] * STUB), p["xy"][1]), (x, p["xy"][1]), 0.02)
                           for p in g["pins"] + h["pins"]] if depth else []
                    if ext and not self.free(ext):
                        continue
                    c = self._leg(n, (x, snap(gap[0])), (x, snap(gap[1])), occ, wkey)
                    if c is None or c > SINGLE_L_CROSS:
                        continue
                    best = (rows, h, depth)
                    break
            if best is not None:
                _rows, h, depth = best
                h["pins"] = sorted(g["pins"] + h["pins"], key=lambda p: p["xy"][1])
                h["depth"] = depth
                out = [x_ for x_ in out if x_ is not g]
        return out

    def _crossing_l(self, n, name, e, o, gnd: bool, other, most: int = 2):
        """Upright power symbol for a side pin whose neighbouring rows are wired: out along the pin, then a
        vertical leg across one or two of those wires. -> (point, 'up'|'down', wires) or None."""
        sg = 1 if gnd else -1
        nat = "down" if gnd else "up"
        rows = {}
        for w in self.wires:
            if abs(w[1][1] - w[2][1]) < 0.01:
                rows[tuple(round(v_, 3) for v_ in wbox(w[1], w[2]))] = w
        best = None
        for kk in (0, 2, 4, 6):
            e2 = (snap(e[0] + o[0] * kk * U), e[1])
            if kk and not self.free([wbox(e, e2, 0.02)], other):
                break
            for j in (1, 2, 3):
                q = (e2[0], snap(e[1] + sg * (j * 2 * U + GAP)))
                crossed, ok = 0, True
                for i in range(1, j + 1):
                    c = self._cross_row(e2[0], snap(e[1] + sg * i * 2 * U), n)
                    if c is False:
                        ok = False
                        break
                    crossed += 1 if c else 0
                if not ok:
                    break
                if not crossed or crossed > most:
                    continue
                leg = wbox(e2, q, 0.02)
                hits = [b for b in self.occ() + list(other) if hit(leg, b)]
                if any(tuple(round(v_, 3) for v_ in b) not in rows for b in hits):
                    continue
                if not self.free(power_boxes(name, q, nat), other):
                    continue
                key = (crossed, kk, j)
                if best is None or key < best[0]:
                    best = (key, (q, nat, ([(n, e, e2)] if kk else []) + [(n, e2, q)]))
        return best[1] if best else None

    # ── decoupling bank: caps of one supply under one symbol, above their IC ──
    def bank_caps(self, anchor):
        D = self.D
        by: dict = {}
        for r in self.sat_refs:
            if r in self.used or len(D.units.get(r, ())) != 1 or D.role.get(r, {}).get("anchor") != anchor:
                continue
            po = D.pins_of[r]
            if len(po) != 2:
                continue
            (p1, n1), (p2, n2) = sorted(po.items())
            for pa, na, pb_, nb in ((p1, n1, p2, n2), (p2, n2, p1, n1)):
                if D.is_rail(na) and not D.is_gnd(na) and D.is_gnd(nb):
                    by.setdefault((na, nb), []).append((D.units[r][0], pa, pb_, r))
                    break
        return by

    def bank(self, rail, gnd, caps, plain: bool = False) -> Cell:
        D = self.D
        c = Cell(D, None, self.used)
        if self.v2 and not plain:
            from .idioms import passive as IP
            from .idioms.model import IdiomError, Match, instantiate
            m = Match("decap", {f"c{i}": e[3] for i, e in enumerate(caps)}, {"RAIL": rail, "GND": gnd})
            try:
                piece = instantiate(D, m, IP.bank_template(D, m))
            except IdiomError as e:
                self.stats["dropped"].append(("decap", str(e)))
            else:
                c.wires, c.powers, c.syms = piece["wires"], piece["powers"], piece["syms"]
                c.boxes = piece["boxes"] + [wbox(a, b) for _, a, b in piece["wires"]]
                self.used |= set(piece["used"])
                self.stats["matches"].append("decap")
                self.stats["claimed"] |= set(piece["used"])
                return c
        x, y0 = 0.0, 0.0
        xs, yb = [], None
        caps = sorted(caps, key=lambda e: (-(LAY.cap_farads(e[0].value) or 0), _refkey(e[3])))
        for si, pa, pb_, r in caps:
            if not pose_pin(si, pa, (x, y0 + U), (0, -1)):
                continue
            far = pins(si)[pb_]["xy"]
            bx = sym_boxes(si)
            yb = snap(far[1] + U)
            c.wires += [(rail, (x, y0), (x, snap(y0 + U))), (gnd, tuple(far), (far[0], yb))]
            c.boxes += bx + [wbox((x, y0), (x, y0 + U)), wbox(far, (far[0], yb))]
            c.syms.append(si)
            self.used.add(r)
            xs.append(x)
            ext = max(b[2] for b in bx) - x + 1.2
            x = snap(x + max(3, math.ceil(ext / (2 * U)) + 1) * 2 * U)
        if not xs:
            return c
        if len(xs) > 1:
            c.wires += [(rail, (xs[0], y0), (xs[-1], y0)), (gnd, (xs[0], yb), (xs[-1], yb))]
            c.boxes += [wbox((xs[0], y0), (xs[-1], y0)), wbox((xs[0], yb), (xs[-1], yb))]
        c.powers += [(rail, (xs[0], y0), "up", D.text(rail)), (gnd, (xs[0], yb), "down", D.text(gnd))]
        c.boxes += [power_box(D.text(rail), (xs[0], y0), "up"), power_box(D.text(gnd), (xs[0], yb), "down")]
        return c

    def pin_bank(self, ref, rail, mid, lanes=(), away=0, cross=0, reach=False) -> bool:
        """The decoupling caps of `ref` on `rail`, drawn AT its supply pin: the pin's wire runs up to the rail
        symbol, the caps stand beside it between that rail wire and one ground symbol. `mid` = end of the pin's
        stub. False (nothing drawn) when there is no room - the caps then form a bank above the part."""
        D = self.D
        by = self.bank_caps(ref)
        keys = [k for k in by if k[0] == rail]
        if len(keys) != 1 or len(by[keys[0]]) > 12:
            return False
        row = sorted(by[keys[0]], key=lambda e: (-(LAY.cap_farads(e[0].value) or 0), _refkey(e[3])))[:BANK_ROW]
        refs = [e[3] for e in row]
        c = self.bank(rail, keys[0][1], row, plain=True)
        if not c.syms:
            self.used.difference_update(refs)
            return False
        name = D.text(rail)
        own_sym = power_box(name, (0.0, 0.0), "up")
        c.powers = [p for p in c.powers if p[0] != rail]
        c.boxes = [b for b in c.boxes if b != own_sym]
        xs = sorted({a[0] for n, a, b in c.wires if n == rail and a[0] == b[0]})
        for h in (14, 16, 18, 22) + ((26, 30, 34) if reach else ()):
            ry = snap(mid[1] - h * U)
            for sg in ((away,) if away else (1, -1)):
                for d in (4, 6, 8, 10, 12):
                    dx = snap((mid[0] + d * U - xs[0]) if sg > 0 else (mid[0] - d * U - xs[-1]))
                    far = (snap((xs[-1] if sg > 0 else xs[0]) + dx), ry)
                    near = (snap((xs[0] if sg > 0 else xs[-1]) + dx), ry)
                    ws = [(rail, mid, (mid[0], ry)), (rail, (mid[0], ry), near)]
                    boxes = [[b[0] + dx, b[1] + ry, b[2] + dx, b[3] + ry] for b in c.occ()]
                    pb = power_box(name, (mid[0], ry), "up")
                    riser = wbox((mid[0], mid[1] - U), (mid[0], ry), 0.6)
                    if any(hit(riser, b, 0) for b in boxes):
                        continue
                    if cross:
                        # (package A) the rise may cross `cross` wires squarely: the caps of a side supply pin
                        # whose upper neighbours are wired stand above those rows, wired to the pin
                        occ_ = self.occ() + list(lanes)
                        wk_ = self._wkey()
                        c_ = self._leg(rail, mid, (mid[0], ry), occ_, wk_)
                        fits = c_ is not None and c_ <= cross and self.free(boxes + [pb, wbox((mid[0], ry), near, 0.02)], lanes)                             and all(tuple(round(v_, 3) for v_ in b) in wk_ for b in occ_ if hit(riser, b))
                    else:
                        fits = self.free(boxes + [pb, riser, wbox((mid[0], ry), near, 0.02)], lanes)
                    if fits:
                        c.shift(dx, ry)
                        self.absorb(c)
                        self.wires += ws
                        self.powers.append((rail, (mid[0], ry), "up", name))
                        self.boxes += [pb] + [wbox(a, b) for _, a, b in ws]
                        return True
        self.used.difference_update(refs)
        return False

    def _power_unit(self, a):
        """Index of the unit of a multi-unit part that carries only its supply pins (op-amp unit C), or None."""
        D = self.D
        us = D.units[a]
        for ui in range(1, len(us)):
            nets = [D.pins_of[a].get(p["number"]) for p in us[ui].pins_world()]
            nets = [k for k in nets if D.conn(k)]
            if nets and all(D.is_rail(k) for k in nets):
                return ui
        return None

    def add_banks(self, cell: Cell, anchor, unit=0, side="T"):
        D = self.D
        by = self.bank_caps(anchor)
        if not by:
            return
        si = D.units[anchor][unit]
        bb = si.body_bbox()
        x = None
        row = None
        for (rail, gnd) in sorted(by, key=lambda k: (-len(by[k]), D.text(k[0]))):
            caps_all = sorted(by[(rail, gnd)], key=lambda e: (-(LAY.cap_farads(e[0].value) or 0), _refkey(e[3])))
            nrow = math.ceil(len(caps_all) / (3 if self.v2 else BANK_ROW))
            per = math.ceil(len(caps_all) / nrow)
            for ri in range(nrow):                          # a long bank wraps into rows instead of one 15 cm ladder
                b = self.bank(rail, gnd, caps_all[ri * per:(ri + 1) * per])
                if not b.syms:
                    continue
                sb = b.bbox()
                if self.v2 and side == "T" and BANK_ONE_ROW:
                    # the groups that found no room at their pins stand in ONE row above the part (their supply
                    # symbols on one line), not each where the slide search happened to drop it
                    if row is None:
                        row = b
                    else:
                        rb = row.bbox()
                        if rb[2] - rb[0] + (sb[2] - sb[0]) > BANK_ROW_MAX:
                            rb0 = row.bbox()
                            cell.attach(row, "T", pref=((bb[0] + bb[2]) / 2, bb[1]), port=((rb0[0] + rb0[2]) / 2, rb0[3]),
                                        gap=3 * U, grow_weight=3.0)
                            row = b
                        else:
                            ry = min(q[1] for _n, q, o, _v in row.powers if o == "up") if any(o == "up" for *_x, o, _v in row.powers) else rb[1]
                            by_ = min(q[1] for _n, q, o, _v in b.powers if o == "up") if any(o == "up" for *_x, o, _v in b.powers) else sb[1]
                            b.shift(snap(rb[2] - sb[0] + 2 * U, 2 * U), snap(ry - by_, 2 * U))
                            row.absorb(b)
                    continue
                if side == "R":
                    cell.attach(b, "R", pref=(bb[2], (bb[1] + bb[3]) / 2), port=(sb[0], (sb[1] + sb[3]) / 2), gap=4 * U,
                                grow_weight=3.0)
                else:
                    cell.attach(b, "T", pref=((bb[0] + bb[2]) / 2, bb[1]), port=((sb[0] + sb[2]) / 2, sb[3]), gap=3 * U,
                                grow_weight=3.0)
        if row is not None:
            rb0 = row.bbox()
            cell.attach(row, "T", pref=((bb[0] + bb[2]) / 2, bb[1]), port=((rb0[0] + rb0[2]) / 2, rb0[3]), gap=3 * U,
                        grow_weight=3.0)

    # ── a part that is not on a stem: its own little anchor, docked where it is needed ──
    def sub_cell(self, r, unit_index=0, face=None, port=None, flat=False) -> Cell:
        """`port` = (net, direction away from the host): a lone 2-pin part turns its pin on that net to the host.
        flat: ... also a part whose other pin is on a supply / ground (it lies in line with the wire it hangs on,
        its supply symbol upright beyond it) - by default such a part stands upright."""
        D = self.D
        si = D.units[r][unit_index]
        c = self._child()
        rot = None
        mirror = "keep"
        po = D.pins_of[r]
        if len(po) == 2 and len(D.units[r]) == 1:
            (p1, n1), (p2, n2) = sorted(po.items())
            up = p1 if (D.is_rail(n1) and not D.is_gnd(n1)) or D.is_gnd(n2) else \
                p2 if (D.is_rail(n2) and not D.is_gnd(n2)) or D.is_gnd(n1) else None
            other = n2 if up == p1 else n1
            follows = any(e[4] != r for e in self.elems_on(other))
            if flat and port is not None and port[0] in (n1, n2) and n1 != n2:
                pose_pin(si, p1 if n1 == port[0] else p2, (0, 0), (-port[1][0], -port[1][1]))
            elif up is not None and not D.is_rail(other) and D.conn(other) and follows:
                # head of a chain that starts on a rail (+5V - R - LED - GND): lie flat with the rail end on the
                # left, so the parts behind it continue in line instead of being docked somewhere else
                pose_pin(si, up, (0, 0), (-1, 0))
            elif up is not None and not (D.is_gnd(n1) and D.is_gnd(n2)):
                pose_pin(si, up, (0, 0), (0, -1))
            elif port is not None and port[0] in (n1, n2) and n1 != n2:
                pose_pin(si, p1 if n1 == port[0] else p2, (0, 0), (-port[1][0], -port[1][1]))
            else:
                pose_pin(si, p1, (0, 0), (-1, 0))
            rot = si.rot
        elif self.v2 and unit_index == 0:
            from .idioms import stage as ST
            tp = ST.transistor_pose(D, r)
            if tp is not None:                            # control pin left, source / emitter towards its rail
                set_pose(si, 0.0, 0.0, tp[0], tp[1])
                rot, mirror, face = tp[0], tp[1], None
                self.stats["matches"].append("tswitch")
                self.stats["claimed"].add(r)
        self.used.add(r)
        c.place(si, (0.0, 0.0) if rot is None else tuple(si.at), stems=True, rot=rot, face=face, mirror=mirror)
        if unit_index == 0:
            self.add_banks(c, r)
        return c

    def dock_target(self, r):
        """(side, preferred point, net) for a leftover part: at the anchor pin (or chain peer) it serves."""
        D = self.D
        role = D.role.get(r, {})
        placed = {s.ref: s for s in self.syms}
        nets = [role.get("via")] + [k for k in D.pins_of[r].values() if k != role.get("via")]
        for k in nets:
            if k is None or not D.conn(k) or D.is_rail(k):
                continue
            cands = [role.get("anchor"), role.get("chain")] + sorted(
                (p[0] for p in D.view.nets[k].pins if p[0] != r), key=_refkey)
            for a in cands:
                if a in placed:
                    for u in D.units[a]:
                        if not any(u is s for s in self.syms):
                            continue
                        for p in u.pins_world():
                            if D.pins_of[a].get(p["number"]) == k:
                                side = {(-1, 0): "L", (1, 0): "R", (0, -1): "T", (0, 1): "B"}[tuple(p["out"])]
                                return side, (p["xy"][0] + p["out"][0] * STUB, p["xy"][1] + p["out"][1] * STUB), k
        return "B", None, None

    def build(self, origin=(0.0, 0.0)):
        """pro2: stage idioms, docking on pin rows, peers facing their host (dock.py); otherwise the legacy build."""
        if self.v2 and self.sm is not None:
            from . import dock
            return dock.build_block(self, origin)
        return self.build_legacy(origin)

    def plain_stub(self, p, n):
        """A pin a stage idiom gave back: the ordinary stub with its terminal (or its upright power symbol)."""
        D = self.D
        o = tuple(p["out"])
        tip = (snap(p["xy"][0]), snap(p["xy"][1]))
        end = (snap(tip[0] + o[0] * STUB), snap(tip[1] + o[1] * STUB))
        self.wires.append((n, tip, end))
        self.boxes.append(wbox(tip, end))
        if D.is_rail(n):
            gnd = D.is_gnd(n)
            orient = "down" if gnd else "up"
            if o[1] != 0 and (o[1] > 0) == gnd:
                q = end
            else:
                mid = end if o[1] == 0 else (snap(end[0] + 2 * U), end[1])
                q = (mid[0], snap(mid[1] + (GAP if gnd else -GAP)))
                self.wires += ([(n, end, mid)] if mid != end else []) + [(n, mid, q)]
                self.boxes += [wbox(end, mid), wbox(mid, q)]
            self.powers.append((n, q, orient, D.text(n)))
            self.boxes += power_boxes(D.text(n), q, orient)
            return
        self.terms.append((n, end, o))
        if D.named(n):
            self.resv.append((n, lbl_box(D.text(n), end, o, D.label_kind(n))))

    def build_legacy(self, origin=(0.0, 0.0)):
        D, info = self.D, self.info
        anchors = list(info.anchors)
        prim = info.id if info.id in anchors else None
        face = {"input": "right", "io": "left" if getattr(info, "stage", 90) >= 3 else "right"}.get(info.kind)
        if prim:
            self.used.add(prim)
            self.place(D.units[prim][0], origin, face=face)
            if self._power_unit(prim) is None:
                self.add_banks(self, prim)
        for a in anchors:
            if a == prim:
                continue
            shared = set(D.pins_of[a].values()) - D.rails
            pts = [(p["xy"], tuple(p["out"])) for p in (D.units[prim][0].pins_world() if prim else [])
                   if D.pins_of[prim].get(p["number"]) in shared and D.conn(D.pins_of[prim].get(p["number"]))]
            if pts:
                sides = [{(-1, 0): "L", (1, 0): "R", (0, -1): "T", (0, 1): "B"}[o] for _, o in pts]
                side = max(sorted(set(sides)), key=sides.count)
                sel = [xy for (xy, o), s in zip(pts, sides) if s == side]
                pref = (sum(p[0] for p in sel) / len(sel), sum(p[1] for p in sel) / len(sel))
            else:
                side, pref = "R", None
            if D.is_conn(a) and side in ("L", "R"):
                pb = self.bbox()
                if pb[2] - pb[0] > 110.0 and pb[2] - pb[0] > 1.3 * (pb[3] - pb[1]):
                    # the block is a wide strip already: the connector goes under the side it serves instead of
                    # pushing the sheet one paper size up
                    face_ = {"R": "left", "L": "right"}[side]
                    c = self.sub_cell(a, face=face_)
                    cb = c.bbox()
                    self.attach(c, "B", pref=(pref[0], pb[3]), port=((cb[0] + cb[2]) / 2, cb[1]), gap=4 * U, clr=4.0)
                    continue
            # a connector docked at the pins it serves looks at them
            c = self.sub_cell(a, face={"R": "left", "L": "right"}.get(side) if D.is_conn(a) else None)
            port = None
            if D.is_conn(a) and pts and side in ("L", "R"):
                # row on row: the first shared pin of the connector sits level with the pin it goes to
                for p in sorted(D.units[prim][0].pins_world(), key=lambda p: (p["xy"][1], p["xy"][0])):
                    k = D.pins_of[prim].get(p["number"])
                    t = next((t for t in c.terms if t[0] == k), None) if k in shared else None
                    if t is not None and {(-1, 0): "L", (1, 0): "R"}.get(tuple(p["out"])) == side:
                        pref, port = tuple(p["xy"]), t[1]
                        break
            self.attach(c, side, pref, port=port, gap=8 * U, clr=4.0)
        for a in anchors:                                 # further units of multi-unit parts
            pu = self._power_unit(a)
            for ui in range(1, len(D.units[a])):
                c = self._child()
                c.place(D.units[a][ui], (0.0, 0.0))
                if ui == pu and a == prim:
                    # the supply unit carries the part's decoupling caps: one tidy group beside the amplifiers
                    self.add_banks(c, a, unit=ui, side="R")
                    pb = self.bbox()
                    cb = c.bbox()
                    self.attach(c, "L", pref=(pb[0], pb[3]), port=(cb[2], cb[3]), gap=8 * U)
                else:
                    self.attach(c, "B", gap=6 * U)
        for r in list(self.sat_refs):                     # leftovers: multi-pin parts first (they host stems)
            pass
        left = [r for r in self.sat_refs if r not in self.used]

        def rail_head(r):
            # 2-pin part from a supply rail to a signal net: it starts a chain (+5V - R - LED - GND), draw it first
            ns = list(D.pins_of[r].values())
            return len(ns) == 2 and sum(1 for k in ns if D.is_rail(k) and not D.is_gnd(k)) == 1 and                 sum(1 for k in ns if not D.is_rail(k)) == 1

        def lkey(r):
            side, pref, _k = self.dock_target(r)
            return (pref is None, D.npins(r) == 2 and not rail_head(r), side, round(pref[1] if pref else 0, 1), round(pref[0] if pref else 0, 1), _refkey(r))
        left.sort(key=lkey)
        for r in left:
            if r in self.used:
                continue
            side, pref, k = self.dock_target(r)
            c = self.sub_cell(r)
            port = next((t[1] for t in c.terms if t[0] == k), None) if k else None
            if k is not None and pref is not None and D.npins(r) > 2:
                # dock at the open end of the wire the net already has (the far end of a pin's stem), not at the pin
                ts = [t[1] for t in self.terms if t[0] == k]
                if ts:
                    pref = min(ts, key=lambda q: {"L": q[0], "R": -q[0], "T": q[1], "B": -q[1]}[side])
            # a crystal / switch / transistor belongs at the wire it serves even if the block grows a little for it
            self.attach(c, side, pref, port=port, grow_weight=0.3 if D.npins(r) > 2 and pref is not None else 1.0)
            for ui in range(1, len(D.units[r])):
                c2 = self._child()
                c2.place(D.units[r][ui], (0.0, 0.0))
                self.attach(c2, "B", gap=6 * U)
            self.fallback.append(r)


def _self_hit(r) -> bool:
    """A stem whose own columns collide with each other (wide value texts) is not acceptable."""
    bx = r["boxes"]
    for i in range(len(bx)):
        for j in range(i + 1, len(bx)):
            if hit(bx[i], bx[j], 0.3):
                return True
    return False


def strip_cell(D: Design, items: list) -> Cell:
    """Name-keeping strips: one short wire per net that carries a PWR_FLAG or a second name.
    items: [{'net': key|None, 'first': ('power', value) | ('label', entry) | None,
             'power': [values], 'labels': [entries], 'flags': n}]"""
    c = Cell(D, None)
    x0 = 0.0
    y_row = 0.0
    P = 6 * U
    per_row = math.ceil(len(items) / math.ceil(len(items) / 4)) if items else 1
    for idx, it in enumerate(items):
        net = it["net"] if it["net"] is not None else f"\0orphan{len(c.wires)}"
        nodes = []
        if it.get("first"):
            nodes.append(it["first"])
        nodes += [("power", v) for v in it.get("power", [])]
        nodes += [("flag", None)] * it.get("flags", 0)
        nodes += [("label", e) for e in it.get("labels", [])]
        if len(nodes) < 2 and not (nodes and nodes[0][0] == "label"):
            nodes.append(("none", None))
        lead = 0.0
        if nodes[0][0] == "label":
            lead = text_width(nodes[0][1][0]) + (4.0 if nodes[0][1][1] != "local" else 1.5)
        x = snap(x0 + math.ceil(lead / (2 * U)) * 2 * U, 2 * U)
        y = y_row
        xa = x
        n = len(nodes)
        tail = 0.0
        for i, (kind, val) in enumerate(nodes):
            px = snap(x + i * P)
            if kind == "power":
                orient = "down" if LAY.is_ground(val) else "up"
                c.powers.append((net, (px, y), orient, val))
                c.boxes.append(power_box(val, (px, y), orient))
            elif kind == "flag":
                c.flags.append((net, (px, y), "up"))
                c.boxes.append(power_box("PWR_FLAG", (px, y), "up"))
            elif kind == "label":
                if i == 0:
                    c.labels.append((net, val, (px, y), 180))
                    c.boxes.append(lbl_box(val[0], (px, y), (-1, 0), val[1]))
                elif i == n - 1:
                    c.labels.append((net, val, (px, y), 0))
                    c.boxes.append(lbl_box(val[0], (px, y), (1, 0), val[1]))
                    tail = text_width(val[0]) + (4.0 if val[1] != "local" else 1.5)
                else:
                    q = (px, snap(y + 2 * U))
                    c.wires.append((net, (px, y), q))
                    c.labels.append((net, val, q, 270))
                    c.boxes += [lbl_box(val[0], q, (0, 1), val[1]), wbox((px, y), q)]
        xb = snap(x + (n - 1) * P)
        if xb > xa:
            c.wires.append((net, (xa, y), (xb, y)))
            c.boxes.append(wbox((xa, y), (xb, y)))
        x0 = snap(xb + tail + 7 * U, 2 * U)
        if (idx + 1) % per_row == 0:                    # rows of at most four strips, evenly filled
            x0, y_row = 0.0, y_row + 12 * U
    return c
