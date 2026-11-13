"""Automatic, readable schematic placement.

Strategy (what a human engineer does on a one-page design):

1. **Classify** every new part: main IC (MCU / most pins / ``group:"mcu"``),
   regulators (``Regulator*`` libs / ``group:"power"``), connectors, other ICs,
   and *small* parts (R, C, L, D, LED, FB, crystals, transistors, ≤4 pins).
2. **Attach** every small part to an *anchor* (a non-small part): by the signal
   net it shares with the anchor, else for decoupling caps by the rail it
   decouples (bulk ≥1 µF → the regulator / connector feeding the rail,
   100 nF-class → the IC consuming it), else transitively through other small
   parts (LED → its series resistor → MCU pin). ``near`` overrides.
3. **Cluster** = anchor + its small parts arranged in cells on the side of the
   anchor pin they connect to, sorted to line up with those pins. Two-pin parts
   are drawn vertically with the supply on top and ground at the bottom.
4. **Pack** clusters onto the sheet in signal-flow order (input connectors →
   power → MCU → other ICs → output connectors) with first-fit placement that
   avoids every existing symbol/label/wire and the title block, using real
   bounding boxes (body graphics + pins + fields + the stub/label/power symbol
   each connected pin will get). The page grows A4 → A3 → A2 → A1 if needed.

Everything lands on the 1.27 mm grid.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import sexpr
from . import symbols as G
from .model import Schematic, SymInst, text_width, PAPER, LABEL_OFFSET

CLEAR = 2.54          # minimum clearance between boxes
CLUSTER_GAP = 5.08    # extra air between functional blocks
STUB = 2.54
PAGES = ["A4", "A3", "A2", "A1", "A0"]
MARGIN = 12.7


def is_ground(net: str) -> bool:
    n = net.upper().lstrip("/")
    return n.startswith("GND") or n.startswith("VSS") or n in ("0V", "AGND", "DGND", "PGND", "SGND", "EARTH", "CHASSIS")


def is_supply(net: str, power_names: set[str]) -> bool:
    if is_ground(net):
        return False
    n = net.lstrip("/")
    return n in power_names or bool(re.match(r"^\+?\d+V\d*$|^\+?\d+\.\d+V$|^V(CC|DD|BUS|BAT|IN|SYS)", n.upper()))


def cap_farads(value: str) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([pnuµm]?)(?:F)?", value or "", re.I)
    if not m:
        m2 = re.match(r"^\s*(\d+)([pnuµ])(\d+)", value or "", re.I)  # 4u7
        if not m2:
            return None
        v = float(f"{m2.group(1)}.{m2.group(3)}")
        unit = m2.group(2).lower()
    else:
        v = float(m.group(1))
        unit = m.group(2).lower()
    mul = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0}.get(unit, 1.0)
    if unit == "" and v > 1:  # "100" with no unit → assume pF-ish; treat as small
        mul = 1e-12
    return v * mul


@dataclass
class Item:
    key: str                      # reference (unique per item+unit)
    libsym: object                # kicad.libs.LibSymbol
    unit: int = 1
    rot: int | None = None
    mirror: str | None = None
    group: str | None = None
    near: str | None = None
    value: str = ""
    ref: str = ""
    at: list | None = None
    nets: dict = field(default_factory=dict)      # pin number -> net name
    nc: set = field(default_factory=set)          # pin numbers that get no_connect
    # computed
    kind: str = ""
    box: list | None = None       # relative to origin, current rot
    anchor: str | None = None
    via_pin: dict | None = None   # anchor pin the item hangs off


def _rect_hit(a, b, clr=0.0):
    return not (a[2] + clr <= b[0] or b[2] + clr <= a[0] or a[3] + clr <= b[1] or b[3] + clr <= a[1])


def _union(boxes):
    boxes = [b for b in boxes if b]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _shift(b, dx, dy):
    return [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]


# ── measuring ──────────────────────────────────────────────────────────
_scratch: dict = {}


def _scratch_inst(item: Item, rot: int, mirror):
    key = (item.libsym.lib_id, item.unit, rot, mirror, item.value, len(item.ref or "U1"))
    s = _scratch.get(key)
    if s is None:
        sch = Schematic.new(None, "scratch")
        k = sch.embed(item.libsym)
        si = sch.add_symbol(k, item.libsym.lib_id, item.ref or "U1", [0, 0], rot, mirror, item.unit,
                            value=item.value or None)
        s = si
        if len(_scratch) > 500:
            _scratch.clear()
        _scratch[key] = s
    return s


def connection_reach(net: str | None, power_names: set[str], label_kind: str = "local",
                     horizontal: bool = True) -> tuple[float, float]:
    """(length along the pin's outward direction, half-width across it) for what Copper attaches."""
    if net is None:
        return 1.27, 1.0
    if net == "__NC__":
        return 1.0, 1.0
    if net.startswith("__W_"):
        return 2.54, 1.0
    if net.lstrip("/") in power_names:
        tw = text_width(net.lstrip("/"))
        if horizontal:   # rotated power symbol (+ room for a shared rail riser): graphic + value text continue outward
            return STUB * 2 + 2.6 + 0.8 + tw, 1.6
        return STUB * 2 + 2.6 + 2.2, max(2.0, tw / 2 + 0.5)   # room for a shared rail + riser
    extra = 3.2 if label_kind == "global" else 0.9
    return STUB + text_width(net) + extra, 1.2


def measure(item: Item, rot: int, mirror, power_names: set[str]) -> list[float]:
    """Bounding box relative to the symbol origin, incl. pins, fields and attached labels."""
    si = _scratch_inst(item, rot, mirror)
    boxes = [si.body_bbox()] + si.field_boxes()
    pins = si.pins_world()

    def is_label(n):
        return bool(n) and not n.startswith("__") and n.lstrip("/") not in power_names
    for p in pins:
        net = item.nets.get(p["number"])
        if net is None and p["number"] in item.nc:
            net = "__NC__"
        if p.get("hidden") and net is None:
            continue
        L, w = connection_reach(net, power_names, horizontal=bool(p["out"][0]))
        if net and not net.startswith("__") and net.lstrip("/") in power_names:
            # a power symbol beside a labelled pin is pushed out past that label (ops.declutter): keep room for it
            al = 1 if p["out"][0] else 0
            near = [text_width(item.nets[o["number"]]) for o in pins
                    if o is not p and tuple(o["out"]) == tuple(p["out"]) and is_label(item.nets.get(o["number"]))
                    and abs(o["xy"][1 - al] - p["xy"][1 - al]) < 0.01 and abs(o["xy"][al] - p["xy"][al]) < 2.54 + 0.01]
            if near:
                L += -(-(LABEL_OFFSET + max(near) + 0.6) // 2.54) * 2.54
        x, y = p["xy"]
        ox, oy = p["out"]
        ex, ey = x + ox * L, y + oy * L
        if ox:
            boxes.append([min(x, ex), y - w, max(x, ex), y + w])
        else:
            boxes.append([x - w, min(y, ey), x + w, max(y, ey)])
    return _union(boxes)


def pin_sides(item: Item, rot: int, mirror) -> dict:
    si = _scratch_inst(item, rot, mirror)
    return {p["number"]: p for p in si.pins_world()}


# ── classification ───────────────────────────────────────────────────────
_SMALL_PREFIX = {"R", "C", "L", "D", "LED", "FB", "F", "Y", "X", "Q", "TP", "JP", "SW", "BZ", "RV", "TH", "NTC", "VR"}


def _prefix(item: Item) -> str:
    r = item.libsym.properties.get("Reference", "") or re.sub(r"\d+$", "", item.ref)
    return r.rstrip("?").upper()


def classify(item: Item) -> str:
    g = (item.group or "").lower()
    if g in ("mcu", "main", "cpu", "soc"):
        return "main"
    if g in ("power", "regulator", "pmic", "ldo", "buck"):
        return "power"
    if g in ("connector", "conn", "io"):
        return "connector"
    if g in ("passive", "decap", "pullup", "pulldown", "series", "led", "small"):
        return "small"
    lib = item.libsym.lib_id
    pref = _prefix(item)
    npins = len({p["number"] for p in item.libsym.unit_pins(item.unit)})
    if lib.startswith(("Connector", "power:")) or pref in ("J", "P", "CN", "USB"):
        return "connector"
    if npins <= 4 and (pref in _SMALL_PREFIX or lib.startswith(("Device:", "LED:", "Diode:", "Transistor"))):
        return "small"
    if lib.startswith(("Regulator", "Power_Management", "Converter")) or g == "power":
        return "power"
    if lib.startswith(("MCU", "RF_Module", "FPGA", "CPU")) or re.match(r"^(MCU|RF_)", lib):
        return "main"
    return "ic"


def choose_rotation(item: Item, power_names: set[str]) -> int:
    """Two-pin parts: vertical, supply on top, ground at the bottom, anode on top."""
    if item.rot is not None:
        return item.rot
    pins = item.libsym.unit_pins(item.unit)
    nums = []
    for p in pins:
        if p["number"] not in nums:
            nums.append(p["number"])
    if len(nums) != 2 or item.kind != "small":
        return 0
    top = None
    bottom = None
    for n in nums:
        net = item.nets.get(n)
        if net and is_ground(net):
            bottom = n
        elif net and is_supply(net, power_names):
            top = n
    if top is None and bottom is None:
        named = {p["number"]: p["name"].upper() for p in pins}
        for n in nums:
            if named.get(n) in ("A", "ANODE", "+"):
                top = n
        if top is None and bottom is None:
            top = nums[0]
    want_pin, want_dir = (top, (0, -1)) if top is not None else (bottom, (0, 1))
    for r in (0, 90, 270, 180):
        ps = pin_sides(item, r, item.mirror)
        if ps.get(want_pin) and tuple(ps[want_pin]["out"]) == want_dir:
            return r
    return 0


# ── occupancy of an existing sheet ───────────────────────────────────────
def label_box(l: dict, pad: float = 0.0) -> list[float]:
    """Sheet box of a label's text as KiCad draws it: starting LABEL_OFFSET past the anchor, running outward along
    the label angle, and sitting ABOVE the wire (left of it for vertical labels); global/hierarchical labels are
    centred on the wire inside their flag shape."""
    x, y = l["at"]
    local = l["kind"] == "local"
    length = LABEL_OFFSET + text_width(l["net"]) + (0.3 if local else 3.0) + pad
    lo, hi = (-1.75 - pad, -0.2 + pad) if local else (-1.3 - pad, 1.3 + pad)
    r = l["rot"] % 360
    if r == 0:
        return [x, y + lo, x + length, y + hi]
    if r == 180:
        return [x - length, y + lo, x, y + hi]
    if r == 90:
        return [x + lo, y - length, x + hi, y]
    return [x + lo, y, x + hi, y + length]


def occupied(sch: Schematic) -> list[list[float]]:
    boxes = []
    for s in sch.symbols():
        boxes.append(s.body_bbox())
        boxes += s.field_boxes()
    for l in sch.labels():
        boxes.append(label_box(l, pad=0.5))
    for w in sch.wires():
        (x0, y0), (x1, y1) = w["a"], w["b"]
        boxes.append([min(x0, x1) - 0.3, min(y0, y1) - 0.3, max(x0, x1) + 0.3, max(y0, y1) + 0.3])
    for n in sch.items("text"):
        a = sexpr.find(n, "at")
        x, y = float(a[1]), float(a[2])
        lines = str(n[1]).split("\n")
        boxes.append([x, y - 1.6 * len(lines), x + max(text_width(t) for t in lines), y + 0.4])
    for n in sch.items("sheet"):
        a = sexpr.find(n, "at")
        sz = sexpr.find(n, "size")
        if a is not None and sz is not None:
            # sheet pins sit on the edge and carry their names outside it: pad the box
            boxes.append([float(a[1]) - 8, float(a[2]) - 3, float(a[1]) + float(sz[1]) + 8, float(a[2]) + float(sz[2]) + 3])
    # buses, graphic lines/boxes, text boxes, images, directive labels drawn by hand in KiCad
    for kind in ("bus", "polyline", "bus_entry", "rectangle", "circle", "arc", "text_box", "image",
                 "directive_label", "netclass_flag", "table", "rule_area"):
        for n in sch.items(kind):
            pts = []
            for xy in sexpr.find_all(sexpr.find(n, "pts") or [], "xy"):
                pts.append((float(xy[1]), float(xy[2])))
            for k in ("start", "end", "mid", "at", "center"):
                e = sexpr.find(n, k)
                if e is not None and len(e) > 2:
                    pts.append((float(e[1]), float(e[2])))
            if not pts:
                continue
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            bb = [min(xs) - 0.5, min(ys) - 0.5, max(xs) + 0.5, max(ys) + 0.5]
            sz = sexpr.find(n, "size")
            if sz is not None and len(sz) > 2:
                bb[2], bb[3] = max(bb[2], bb[0] + float(sz[1]) + 1), max(bb[3], bb[1] + float(sz[2]) + 1)
            if kind == "circle":
                r = float(sexpr.value(n, "radius", 0) or 0)
                c = sexpr.find(n, "center")
                if c is not None:
                    bb = [float(c[1]) - r, float(c[2]) - r, float(c[1]) + r, float(c[2]) + r]
            if kind == "image":       # size unknown without decoding the bitmap — assume a logo-sized block
                bb = [bb[0] - 15, bb[1] - 15, bb[2] + 15, bb[3] + 15]
            if kind in ("directive_label", "netclass_flag"):
                bb = [bb[0] - 4, bb[1] - 8, bb[2] + 12, bb[3] + 1]
            boxes.append(bb)
    return boxes


def usable(sch: Schematic, paper: str | None = None) -> tuple[list[float], list[float]]:
    """(usable rect, title-block rect) in sheet coordinates."""
    w, h = PAPER.get(paper or sch.paper, sch.page_size()) if paper else sch.page_size()
    area = [MARGIN + 2.54, MARGIN + 2.54, w - MARGIN - 2.54, h - MARGIN - 2.54]
    title = [w - 10 - 112, h - 10 - 36, w, h]
    return area, title


# ── clustering ───────────────────────────────────────────────────────────
@dataclass
class Cluster:
    anchor: Item | None
    members: list = field(default_factory=list)   # (item, dx, dy) relative to cluster origin
    box: list | None = None
    rank: int = 5
    inline: list = field(default_factory=list)


def _net_kind(net, power_names):
    if is_ground(net):
        return "gnd"
    if is_supply(net, power_names):
        return "rail"
    return "sig"


def build_clusters(items: list[Item], power_names: set[str], existing_anchor_pins: dict,
                   existing_refs: set | None = None) -> tuple[list[Cluster], dict]:
    """Returns (clusters, satellites-of-existing-parts {existing_ref: [items]})."""
    existing_refs = set(existing_refs or ()) | {r for lst in existing_anchor_pins.values() for r, _ in lst}
    for it in items:
        it.kind = classify(it)
    anchors = [it for it in items if it.kind != "small"]
    smalls = [it for it in items if it.kind == "small"]
    if anchors and not any(a.kind == "main" for a in anchors):
        # the IC with most pins becomes main (connectors never)
        ics = [a for a in anchors if a.kind in ("ic", "power")]
        if ics:
            m = max(ics, key=lambda a: len(a.libsym.unit_pins(a.unit)))
            if len(m.libsym.unit_pins(m.unit)) >= 8 and m.kind == "ic":
                m.kind = "main"
    by_key = {it.key: it for it in items}
    # net -> [(item, pin)]
    net_pins: dict[str, list] = {}
    for it in items:
        for pn, net in it.nets.items():
            net_pins.setdefault(net, []).append((it, pn))

    def pin_type(it, pn):
        for p in it.libsym.unit_pins(it.unit):
            if p["number"] == pn:
                return p["type"]
        return ""

    assigned: dict[str, tuple] = {}   # small key -> (anchor key or existing ref, anchor pin number, how)
    ext_sat: dict[str, list] = {}
    # explicit near
    near_small = []   # near → another small part: join whatever cluster that part ends up in
    for s in smalls:
        if s.near:
            if s.near in by_key and by_key[s.near].kind == "small":
                near_small.append((s, by_key[s.near]))
            elif s.near in by_key:
                tgt = by_key[s.near]
                # hang it off the ANCHOR's pin it shares a net with: signal first, then rail, ground last
                shared = [(net, opn) for pn, net in s.nets.items() for (o, opn) in net_pins.get(net, []) if o is tgt]
                order = {"sig": 0, "rail": 1, "gnd": 2}
                shared.sort(key=lambda c: order[_net_kind(c[0], power_names)])
                how = "near"
                if shared and _net_kind(shared[0][0], power_names) == "rail" and _prefix(s) == "C":
                    farad = cap_farads(s.value)
                    how = "bulk" if farad is not None and farad >= 1e-6 else "decap"
                assigned[s.key] = (tgt.key, shared[0][1] if shared else None, how)
            elif s.near in existing_refs:
                ext_sat.setdefault(s.near, []).append(s)
                assigned[s.key] = (s.near, None, "near-existing")
    # signal nets shared with an anchor
    for s in smalls:
        if s.key in assigned:
            continue
        best = None
        for pn, net in s.nets.items():
            if _net_kind(net, power_names) != "sig":
                continue
            for (o, opn) in net_pins.get(net, []):
                if o is s or o.kind == "small":
                    continue
                sc = 10 + (5 if o.kind == "main" else 0)
                if best is None or sc > best[0]:
                    best = (sc, o.key, opn)
            for (eref, epn) in existing_anchor_pins.get(net, []):
                if best is None or 9 > best[0]:
                    best = (9, eref, epn)
        if best:
            if best[1] in by_key:
                assigned[s.key] = (best[1], best[2], "signal")
            else:
                ext_sat.setdefault(best[1], []).append(s)
                assigned[s.key] = (best[1], best[2], "signal-existing")
    # decoupling / bulk caps: rail shared with anchors
    for s in smalls:
        if s.key in assigned:
            continue
        rails = [net for net in s.nets.values() if _net_kind(net, power_names) == "rail"]
        if not rails:
            continue
        rail = rails[0]
        farad = cap_farads(s.value) if _prefix(s) == "C" else None
        bulk = farad is not None and farad >= 1e-6
        cands = []
        for (o, opn) in net_pins.get(rail, []):
            if o.kind == "small":
                continue
            t = pin_type(o, opn)
            if bulk:
                sc = 10 if t == "power_out" else 6 if o.kind in ("power", "connector") else 3
            else:
                sc = 10 if t == "power_in" and o.kind in ("main", "ic") else 7 if t == "power_in" else 4 if o.kind == "power" else 2
            cands.append((sc, o.key, opn))
        if cands:
            cands.sort(key=lambda c: -c[0])
            # spread several 100nF caps over all consumer ICs on that rail
            top = [c for c in cands if c[0] == cands[0][0]]
            load = {}
            for k2, v2 in assigned.items():
                load[v2[0]] = load.get(v2[0], 0) + 1
            top.sort(key=lambda c: load.get(c[1], 0))
            assigned[s.key] = (top[0][1], top[0][2], "bulk" if bulk else "decap")
        else:
            for (eref, epn) in existing_anchor_pins.get(rail, []):
                ext_sat.setdefault(eref, []).append(s)
                assigned[s.key] = (eref, epn, "decap-existing")
                break
    # chains through other small parts (LED → R → MCU)
    changed = True
    while changed:
        changed = False
        for s in smalls:
            if s.key in assigned:
                continue
            for pn, net in s.nets.items():
                if _net_kind(net, power_names) != "sig":
                    continue
                for (o, opn) in net_pins.get(net, []):
                    if o is not s and o.kind == "small" and o.key in assigned:
                        a = assigned[o.key]
                        assigned[s.key] = (a[0], a[1], "chain:" + o.key)
                        if a[0] not in by_key:
                            ext_sat.setdefault(a[0], []).append(s)
                        changed = True
                        break
                if s.key in assigned:
                    break
    for s, tgt in near_small:
        if s.key not in assigned and tgt.key in assigned:
            a = assigned[tgt.key]
            assigned[s.key] = (a[0], a[1], "near:" + tgt.key)
            if a[0] not in by_key:
                ext_sat.setdefault(a[0], []).append(s)
    # never leave a part pointing at something that is not a cluster anchor
    for k in [k for k, v in assigned.items() if v[0] in by_key and by_key[v[0]].kind == "small"]:
        del assigned[k]
    clusters = []
    rank = {"connector-in": 0, "power": 1, "main": 2, "ic": 3, "connector": 4, "small": 6}
    for a in anchors:
        sats = [s for s in smalls if assigned.get(s.key, (None,))[0] == a.key]
        c = Cluster(a)
        kind = a.kind
        if kind == "connector":
            # connectors carrying a rail into the design (USB, barrel jack, battery) go first
            rails_in = [net for net in a.nets.values() if _net_kind(net, power_names) == "rail"]
            kind = "connector-in" if rails_in and not any(
                pin_type(o, opn) == "power_out" and o is not a and o.kind != "connector"
                for net in rails_in for (o, opn) in net_pins.get(net, [])) else "connector"
        c.rank = rank.get(kind, 5)
        _layout_cluster(c, a, sats, assigned, by_key, power_names)
        clusters.append(c)
    loose = [s for s in smalls if s.key not in assigned]
    if loose:
        c = Cluster(None, rank=6)
        _layout_cells_row(c, _make_cells(loose, assigned, power_names))
        clusters.append(c)
    clusters.sort(key=lambda c: (c.rank, -(len(c.anchor.libsym.pins) if c.anchor else 0)))
    return clusters, ext_sat


def _item_box(it: Item, power_names):
    it.rot = choose_rotation(it, power_names) if it.rot is None else it.rot
    it.box = measure(it, it.rot, it.mirror, power_names)
    return it.box


CHAIN_GAP = 5.08   # wire length between stacked parts (label sits at its midpoint)


def _two_pins(it: Item) -> list[str] | None:
    nums = []
    for p in it.libsym.unit_pins(it.unit):
        if p["number"] not in nums:
            nums.append(p["number"])
    return nums if len(nums) == 2 else None


def _rot_with_top(it: Item, top_pin: str) -> int | None:
    for r in (0, 90, 270, 180):
        ps = pin_sides(it, r, it.mirror)
        if ps.get(top_pin) and tuple(ps[top_pin]["out"]) == (0, -1):
            return r
    return None


def _chains(sats: list[Item], power_names) -> list[list]:
    """Paths of two-pin parts joined by a signal net that only those two small parts share
    (R → LED, pull-up R → reset C). Returns [[(item, top_pin, bottom_pin, net_below)], ...]."""
    two = {s.key: _two_pins(s) for s in sats}
    users: dict[str, list] = {}
    for s in sats:
        if not two[s.key] or s.rot is not None:
            continue
        for pn, net in s.nets.items():
            if _net_kind(net, power_names) == "sig" and not net.startswith("__"):
                users.setdefault(net, []).append((s, pn))
    adj: dict[str, list] = {s.key: [] for s in sats}
    for net, us in users.items():
        if len(us) == 2 and us[0][0] is not us[1][0]:
            (a, pa), (b, pb) = us
            adj[a.key].append((b, pa, pb, net))
            adj[b.key].append((a, pb, pa, net))
    seen = set()
    out = []
    starts = [s for s in sats if len(adj[s.key]) == 1] + [s for s in sats if len(adj[s.key]) > 1]
    for s in starts:
        if s.key in seen or not adj[s.key]:
            continue
        path = [s]
        links = []
        seen.add(s.key)
        cur = s
        while True:
            nxt = [e for e in adj[cur.key] if e[0].key not in seen]
            if not nxt:
                break
            o, pc, po, net = nxt[0]
            links.append((pc, po, net))
            path.append(o)
            seen.add(o.key)
            cur = o
        if len(path) < 2:
            continue
        top_outer = [p for p in two[path[0].key] if p != links[0][0]][0]
        bot_outer = [p for p in two[path[-1].key] if p != links[-1][1]][0]
        tn = path[0].nets.get(top_outer, "")
        bn = path[-1].nets.get(bot_outer, "")
        rev = bool((tn and is_ground(tn)) or (bn and is_supply(bn, power_names) and not (tn and is_supply(tn, power_names))))
        if rev:
            path = path[::-1]
            links = [(b, a, n) for (a, b, n) in links[::-1]]
            top_outer = bot_outer
        chain = []
        for i, it in enumerate(path):
            top = top_outer if i == 0 else links[i - 1][1]
            bottom = links[i][0] if i < len(links) else [p for p in two[it.key] if p != top][0]
            chain.append((it, top, bottom, links[i][2] if i < len(links) else None))
        rots = [_rot_with_top(it, top) for it, top, _, _ in chain]
        if any(r is None for r in rots):
            continue
        for (it, top, bottom, net), r in zip(chain, rots):
            it.rot = r
        out.append(chain)
    return out


def _make_cells(sats: list[Item], assigned: dict, power_names) -> list[dict]:
    """Cells = vertical stacks (wired chains) or single parts, each with a box + members."""
    cells = []
    in_chain = set()
    for chain in _chains(sats, power_names):
        members, boxes, inline = [], [], []
        prev_bottom = None
        for i, (it, top, bottom, net) in enumerate(chain):
            saved = dict(it.nets)
            if net:
                it.nets[bottom] = "__W_" + net      # wired links reserve no label room
            if i:
                it.nets[top] = "__W_" + chain[i - 1][3]
            it.box = measure(it, it.rot, it.mirror, power_names)
            it.nets = saved
            ps = pin_sides(it, it.rot, it.mirror)
            ox = oy = 0.0
            if prev_bottom is not None:
                tx, ty = ps[top]["xy"]
                ox = prev_bottom[0] - tx
                oy = prev_bottom[1] + CHAIN_GAP - ty
                net_above = chain[i - 1][3]
                mid = [prev_bottom[0], prev_bottom[1] + CHAIN_GAP / 2]
                tw = text_width(net_above)
                boxes.append([mid[0] - tw - 1.2, mid[1] - 1.6, mid[0], mid[1] + 0.6])
                inline.append({"net": net_above, "a": (chain[i - 1][0].key, chain[i - 1][2]), "b": (it.key, top)})
            members.append((it, ox, oy))
            boxes.append(_shift(it.box, ox, oy))
            bx, by_ = ps[bottom]["xy"]
            prev_bottom = [ox + bx, oy + by_]
            in_chain.add(it.key)
        root = next((it for it, *_ in chain if not assigned.get(it.key, ("", "", "chain:"))[2].startswith("chain:")),
                    chain[0][0])
        cells.append({"members": members, "box": _union(boxes), "root": root, "inline": inline})
    for s in sats:
        if s.key in in_chain:
            continue
        _item_box(s, power_names)
        cells.append({"members": [(s, 0.0, 0.0)], "box": s.box, "root": s, "inline": []})
    return cells


def _place_cell(c: "Cluster", cell: dict, x0: float, y0: float, boxes: list):
    """Put a cell so its box's top-left corner is at (x0, y0)."""
    dx = x0 - cell["box"][0]
    dy = y0 - cell["box"][1]
    for it, mx, my in cell["members"]:
        c.members.append((it, mx + dx, my + dy))
    c.inline += cell["inline"]
    boxes.append(_shift(cell["box"], dx, dy))


def _layout_cluster(c: Cluster, a: Item, sats: list[Item], assigned, by_key, power_names):
    a.rot = 0 if a.rot is None else a.rot
    abox = _item_box(a, power_names)
    c.members.append((a, 0.0, 0.0))
    apins = pin_sides(a, a.rot, a.mirror)
    cells = _make_cells(sats, assigned, power_names)
    sides: dict[str, list] = {"left": [], "right": [], "top": [], "bottom": []}
    deferred = []
    for cell in cells:
        root = cell["root"]
        info = assigned.get(root.key, (None, None, ""))
        p = apins.get(info[1]) if info[1] else None
        if p is None:
            sides["bottom"].append(((abox[0] + abox[2]) / 2, cell))
            continue
        side = G.side_of(p["out"])
        if info[2] in ("decap", "bulk") and side in ("top", "bottom"):
            deferred.append((p, cell))       # decaps go beside the IC, level with its supply pin
            continue
        pos = p["xy"][1] if side in ("left", "right") else p["xy"][0]
        sides[side].append((pos, cell))
    for p, cell in deferred:
        lh = sum(cl["box"][3] - cl["box"][1] for _, cl in sides["left"])
        rh = sum(cl["box"][3] - cl["box"][1] for _, cl in sides["right"])
        side = "right" if rh <= lh else "left"
        ch = cell["box"][3] - cell["box"][1]
        pos = (abox[1] + ch / 2) if p["out"][1] < 0 else (abox[3] - ch / 2)
        sides[side].append((pos, cell))
    boxes = [abox]
    for side, entries in sides.items():
        if not entries:
            continue
        entries.sort(key=lambda e: e[0])
        if side in ("left", "right"):
            cursor = None
            col_x = (abox[0] - CLEAR) if side == "left" else (abox[2] + CLEAR)
            col_w = 0.0
            for pos, cell in entries:
                bw = cell["box"][2] - cell["box"][0]
                h = cell["box"][3] - cell["box"][1]
                y0 = pos - h / 2
                if cursor is not None and y0 < cursor + CLEAR:
                    y0 = cursor + CLEAR
                if cursor is not None and y0 + h > abox[3] + 40:
                    col_x = (col_x - col_w - CLEAR) if side == "left" else (col_x + col_w + CLEAR)
                    col_w = 0.0
                    y0 = max(abox[1], pos - h / 2)
                cursor = y0 + h
                col_w = max(col_w, bw)
                x0 = col_x - bw if side == "left" else col_x
                _place_cell(c, cell, x0, y0, boxes)
        else:
            maxw = max(abox[2] - abox[0] + 40, 60)
            rows, row, rw = [], [], 0.0
            for pos, cell in entries:
                w = cell["box"][2] - cell["box"][0]
                if row and rw + CLEAR + w > maxw:
                    rows.append((row, rw))
                    row, rw = [], 0.0
                row.append(cell)
                rw += (CLEAR if rw else 0) + w
            if row:
                rows.append((row, rw))
            y_edge = abox[3] + CLEAR if side == "bottom" else abox[1] - CLEAR
            cx = (abox[0] + abox[2]) / 2
            for row, rw in rows:
                h = max(cl["box"][3] - cl["box"][1] for cl in row)
                x = cx - rw / 2
                y0 = y_edge if side == "bottom" else y_edge - h
                for cl in row:
                    ch = cl["box"][3] - cl["box"][1]
                    _place_cell(c, cl, x, y0 if side == "bottom" else y0 + h - ch, boxes)
                    x += cl["box"][2] - cl["box"][0] + CLEAR
                y_edge = (y0 + h + CLEAR) if side == "bottom" else (y0 - CLEAR)
    c.box = _union(boxes)


def _layout_cells_row(c: Cluster, cells: list[dict], maxw: float = 120):
    """Unanchored parts in rows; wired chains (R→LED) stay stacked as one cell."""
    x = y = rowh = 0.0
    boxes = []
    for cell in cells:
        w, h = cell["box"][2] - cell["box"][0], cell["box"][3] - cell["box"][1]
        if x > 0 and x + w > maxw:
            x = 0
            y += rowh + CLEAR
            rowh = 0
        _place_cell(c, cell, x, y, boxes)
        x += w + CLEAR * 2
        rowh = max(rowh, h)
    c.box = _union(boxes)


def _layout_row(c: Cluster, items: list[Item], power_names, maxw: float = 120):
    x = y = 0.0
    rowh = 0.0
    boxes = []
    for it in items:
        _item_box(it, power_names)
        w, h = it.box[2] - it.box[0], it.box[3] - it.box[1]
        if x > 0 and x + w > maxw:
            x = 0
            y += rowh + CLEAR
            rowh = 0
        ox, oy = x - it.box[0], y - it.box[1]
        c.members.append((it, ox, oy))
        boxes.append(_shift(it.box, ox, oy))
        x += w + CLEAR * 2
        rowh = max(rowh, h)
    c.box = _union(boxes)


# ── packing ──────────────────────────────────────────────────────────────
def _fits(box, area, obstacles):
    if box[0] < area[0] or box[1] < area[1] or box[2] > area[2] or box[3] > area[3]:
        return None
    for o in obstacles:
        if _rect_hit(box, o, CLEAR):
            return o
    return False


def find_spot(size_box, area, obstacles, start=None, step: float = 2.54, prefer: str = "rows"):
    """First-fit top-left scan for a box (relative coords) → offset (dx, dy) or None."""
    w = size_box[2] - size_box[0]
    h = size_box[3] - size_box[1]
    y = area[1] if start is None else start[1]
    while y + h <= area[3]:
        x = area[0] if start is None else start[0]
        while x + w <= area[2]:
            cand = [x, y, x + w, y + h]
            hit = _fits(cand, area, obstacles)
            if hit is False:
                return (x - size_box[0], y - size_box[1])
            if hit is None:
                break
            x = max(x + step, hit[2] + CLEAR)
            x = area[0] + round((x - area[0]) / 1.27 + 0.4999) * 1.27
        y += step
    return None


def find_spot_near(size_box, target, area, obstacles, max_r: float = 120):
    """Nearest free spot (spiral search) whose box centre is close to ``target``."""
    w = size_box[2] - size_box[0]
    h = size_box[3] - size_box[1]
    best = None
    step = 2.54
    r = 0.0
    while r <= max_r and best is None:
        n = max(1, int(r / step) * 8) if r else 1
        cands = []
        if r == 0:
            cands = [(0, 0)]
        else:
            k = int(r / step)
            for i in range(-k, k + 1):
                cands += [(i * step, -r), (i * step, r), (-r, i * step), (r, i * step)]
        for dx, dy in cands:
            cx, cy = target[0] + dx, target[1] + dy
            box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
            if _fits(box, area, obstacles) is False:
                d = dx * dx + dy * dy
                if best is None or d < best[0]:
                    best = (d, box)
        r += step
    if best is None:
        return None
    box = best[1]
    return (box[0] - size_box[0], box[1] - size_box[1])


def arrange(sch: Schematic, items: list[Item], power_names: set[str], existing_net_pins: dict | None = None,
            reserved: list | None = None) -> dict:
    """Assign at/rot to every item lacking ``at``. Returns {key: {'at':[x,y], 'rot':r}, 'paper': name?}."""
    out = {}
    if not items:
        return out
    existing_net_pins = existing_net_pins or {}
    obstacles = occupied(sch) + list(reserved or [])
    existing = {s.ref: s for s in sch.symbols(include_power=False)}
    clusters, ext_sat = build_clusters(items, power_names, existing_net_pins, set(existing))
    ext_cells = {eref: _make_cells(sats, {}, power_names) for eref, sats in ext_sat.items()}   # once: chains fix rot
    paper0 = sch.paper
    order = [p for p in PAGES if p in PAPER]
    start_i = order.index(paper0) if paper0 in order else 0

    for pi in range(start_i, len(order)):
        paper = order[pi]
        w, h = PAPER[paper]
        area = [MARGIN + 2.54, MARGIN + 2.54, w - MARGIN - 2.54, h - MARGIN - 2.54]
        title = [w - 10 - 112, h - 10 - 36, w, h]
        obs = obstacles + [title]
        res = {}
        ok = True
        ext_inline = []
        # satellites of parts that already exist on the sheet — R→LED chains stay stacked + wired here too
        for eref, cells in ext_cells.items():
            anchor = existing.get(eref)
            for cell in cells:
                nets = {n for it, _, _ in cell["members"] for n in it.nets.values()}
                target = None
                if anchor is not None:
                    bb = anchor.body_bbox()
                    target = [bb[2] + 15, (bb[1] + bb[3]) / 2]
                    mine = {pn for net, lst in existing_net_pins.items() for (r_, pn) in lst if r_ == eref and net in nets}
                    for p in anchor.pins_world():
                        if p["number"] in mine:
                            target = [p["xy"][0] + p["out"][0] * 20, p["xy"][1] + p["out"][1] * 12]
                            break
                off = find_spot_near(cell["box"], target, area, obs) if target else find_spot(cell["box"], area, obs)
                if off is None:
                    ok = False
                    break
                for it, mx, my in cell["members"]:
                    res[it.key] = (off[0] + mx, off[1] + my)
                obs.append(_shift(cell["box"], *off))
                ext_inline += cell["inline"]
            if not ok:
                break
        if not ok:
            continue
        placed_boxes = []
        for c in clusters:
            gbox = [c.box[0] - CLUSTER_GAP, c.box[1] - CLUSTER_GAP, c.box[2] + CLUSTER_GAP, c.box[3] + CLUSTER_GAP]
            if c.anchor is not None and c.anchor.near and c.anchor.near in existing:
                bb = existing[c.anchor.near].body_bbox()
                off = find_spot_near(gbox, [bb[2] + (c.box[2] - c.box[0]) / 2 + 10, (bb[1] + bb[3]) / 2], area, obs)
            else:
                off = find_spot(gbox, area, obs)
                if off is None:
                    off = find_spot(c.box, area, obs)
            if off is None:
                ok = False
                break
            dx, dy = off
            for it, mx, my in c.members:
                res[it.key] = (dx + mx, dy + my)
            obs.append(_shift(c.box, dx, dy))
            placed_boxes.append(_shift(c.box, dx, dy))
        if ok and placed_boxes and not obstacles and not ext_sat:
            # a fresh sheet: centre the drawing in the free area above the title block
            u = _union(placed_boxes)
            free = [area[0], area[1], area[2], title[1] - CLEAR]
            sx = max(0.0, ((free[2] - free[0]) - (u[2] - u[0])) / 2 - (u[0] - free[0]))
            sy = max(0.0, ((free[3] - free[1]) - (u[3] - u[1])) / 2 - (u[1] - free[1]))
            sx, sy = G.snap(sx, 2.54), G.snap(sy * 0.6, 2.54)
            if u[3] + sy > free[3]:
                sy = 0.0
            res = {k: (v[0] + sx, v[1] + sy) for k, v in res.items()}
        if ok:
            for it in items:
                if it.key in res:
                    dx, dy = res[it.key]
                    out[it.key] = {"at": G.snap_pt([dx, dy]), "rot": it.rot or 0}
            if paper != paper0:
                out["__paper__"] = paper
            refs = {it.key: it.ref for it in items}
            out["__inline__"] = [{"net": il["net"], "a": (refs[il["a"][0]], il["a"][1]), "b": (refs[il["b"][0]], il["b"][1])}
                                 for il in [x for c in clusters for x in c.inline] + ext_inline]
            return out
    raise RuntimeError("layout: the design does not fit on an A0 sheet — split it into hierarchical sheets")


def tidy(pr, sheet: str | None = None, dry_run: bool = False, while_open: str = "refuse") -> dict:
    """Re-layout an existing sheet keeping connectivity (implementation in sch.ops.tidy)."""
    from .ops import tidy as _tidy
    return _tidy(pr, sheet, dry_run, while_open)
