"""Connectivity-driven automatic placement (pure Python, no pcbnew).

Input is ``board.read(pr, what=['geom'])`` — footprints with their local
(unrotated, front-side) courtyard box and pads. Output is a list of
``{ref, x, y, rot, side}`` moves for the worker's ``place`` op.

Strategy (what a careful human does first):
  1. roles: connectors, main MCU/IC, regulators, crystals, decoupling caps
     (attached to the IC power pin they serve), crystal load caps,
     pull-ups / series parts (attached to their IC pin), everything else;
  2. connectors go on the board edge, mouth facing out, power input first;
  3. the main IC goes to the centre of the free area, other ICs and
     regulators by wirelength;
  4. attached parts hug the pin they serve; the rest are placed greedily by
     wirelength to what is already placed; then two refinement passes;
  5. courtyard-legal everywhere (no overlaps, inside the outline with edge
     clearance, around mounting holes), 0.5 mm grid, rotations 0/90/180/270.

Score = total ratsnest length (sum of per-net minimum spanning trees).
"""
from __future__ import annotations

import math
import re

from . import holes as _holes

GROUND_RE = re.compile(r"^/?(.*/)?(GND\w*|\w*GND|VSS\w*|0V|EARTH|COM)$", re.I)
POWER_RE = re.compile(r"^/?(.*/)?(\+.*|.*VCC.*|.*VDD.*|.*VBUS.*|.*VIN.*|.*VBAT.*|.*VSYS.*|.*3V3.*|.*5V.*|.*12V.*|"
                      r".*1V8.*|.*VOUT.*|.*VREG.*|.*VMOT.*|.*PWR.*)$", re.I)
REG_RE = re.compile(r"(1117|LDO|78\d\d|79\d\d|LM317|AP21\d\d|XC62\d\d|MCP17\d\d|TLV7\d|RT9\d|ME6211|HT7\d|"
                    r"REG|BUCK|BOOST|MP1584|MP2\d\d\d|TPS\d|LM2596|SY8\d|AP3\d\d\d|LP29\d\d|NCP1\d\d)", re.I)


def is_ground(net: str) -> bool:
    return bool(net) and bool(GROUND_RE.match(net))


def is_power(net: str) -> bool:
    return bool(net) and not is_ground(net) and bool(POWER_RE.match(net))


def cap_farads(v: str) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([pnuµm]?)(\d*)\s*F?", v or "", re.I)
    if not m:
        return None
    num = float(m.group(1) + ("." + m.group(3) if m.group(3) else ""))
    mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1e-6}[m.group(2).lower() if m.group(2) else ""]
    return num * mult


def tf(lx, ly, rot):
    """local → board offset for KiCad rotation (deg, CCW on screen, y down)."""
    r = int(round(rot)) % 360
    if r == 0:
        return lx, ly
    if r == 90:
        return ly, -lx
    if r == 180:
        return -lx, -ly
    if r == 270:
        return -ly, lx
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    return lx * c + ly * s, -lx * s + ly * c


def snap(v, g=0.5):
    return round(round(v / g) * g, 4)


class Part:
    def __init__(self, d):
        self.ref = d["ref"]
        self.d = d
        self.value = d.get("value", "")
        self.fpid = d.get("footprint", "")
        lb = d.get("lbbox") or d.get("lbody") or [-1, -1, 1, 1]
        if lb[2] - lb[0] <= 0 or lb[3] - lb[1] <= 0:
            lb = [-0.5, -0.5, 0.5, 0.5]
        self.lbox = lb
        self.pads = [p for p in d.get("pad_list", []) if "lx" in p]
        self.x, self.y, self.rot = d["x"], d["y"], d.get("rot", 0)
        self.side = d.get("side", "top")
        self.fixed = bool(d.get("locked")) or "board_only" in (d.get("flags") or [])
        self.role = "other"
        self.anchor = None          # (Part, pad dict) this part should hug
        self.nets = {p["net"] for p in self.pads if p.get("net")}
        self.placed = False
        self.edge_hint = d.get("edge_hint")   # local [x,y] of the footprint's "PCB Edge" marker
        self.overhang = None                  # board edge this part may overhang (connector mouths)
        self.antenna = d.get("antenna")       # RF module antenna region {'side','rect'} (local mm)

    def prefix(self):
        return re.sub(r"\d.*$", "", self.ref).upper()

    def rect(self, x=None, y=None, rot=None):
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        x0, y0, x1, y1 = self.lbox
        # all four corners: two opposite corners are only enough for right angles (a locked part
        # at 30° was modelled too small and the placer put parts on top of its pads)
        pts = [self.ltf(cx, cy, rot) for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (x + min(xs), y + min(ys), x + max(xs), y + max(ys))

    def pad_pos(self, p, x=None, y=None, rot=None):
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        dx, dy = self.ltf(p["lx"], p["ly"], rot)
        return x + dx, y + dy

    def ltf(self, lx, ly, rot):
        """Local (front-side, unrotated) offset → board offset. A bottom-side footprint is the
        front geometry mirrored in local y, then rotated (checked against pcbnew pad positions)."""
        return tf(lx, -ly if self.side == "bottom" else ly, rot)

    def area(self):
        return (self.lbox[2] - self.lbox[0]) * (self.lbox[3] - self.lbox[1])


def mst_length(points) -> float:
    n = len(points)
    if n < 2:
        return 0.0
    inf = float("inf")
    dist = [inf] * n
    used = [False] * n
    dist[0] = 0
    total = 0.0
    for _ in range(n):
        u = min((i for i in range(n) if not used[i]), key=lambda i: dist[i])
        used[u] = True
        total += dist[u]
        ux, uy = points[u]
        for v in range(n):
            if not used[v]:
                d = math.hypot(points[v][0] - ux, points[v][1] - uy)
                if d < dist[v]:
                    dist[v] = d
    return total


def ratsnest(parts, only_placed=False, skip_nets=()) -> float:
    nets = {}
    for p in parts:
        if only_placed and not p.placed:
            continue
        for pad in p.pads:
            n = pad.get("net")
            if n and n not in skip_nets and not n.startswith("unconnected-"):
                nets.setdefault(n, []).append(p.pad_pos(pad))
    return round(sum(mst_length(v) for v in nets.values()), 1)


# ── role analysis ─────────────────────────────────────────────────────────
ESD_RE = re.compile(r"(ESD|TVS|USBLC|PRTR\d|TPD\d|SRV05|SP05\d|IP42\d\d|PESD|RCLAMP|SMF\d|SMBJ|SMAJ|ULC\d|"
                    r"LXES|CDSOT|NUP\d|PUSB)", re.I)
XTAL_FN = re.compile(r"(X(TAL)?_?(IN|OUT|I|O)\b|XI\b|XO\b|OSC|XTAL|HSE|LSE|XIN|XOUT|CLKIN)", re.I)


def classify(parts: list[Part]):
    """Roles: connector / crystal / ic / mcu / regulator / esd / switch, and the anchor (part + pin) that
    decoupling caps, crystal load caps, pull-ups, series parts and ESD arrays should hug. Fixed parts
    (locked, keep=[...], parts not in refs=[...]) get their real role too, so a new decap still finds
    the existing IC it belongs to."""
    net_parts = {}
    for p in parts:
        for pad in p.pads:
            if pad.get("net"):
                net_parts.setdefault(pad["net"], []).append((p, pad))
    for p in parts:
        pre = p.prefix()
        fpl = p.fpid.lower()
        text = p.value + " " + p.fpid
        if p.ref.startswith("#OBST") or _holes.is_hole(p.fpid) or pre in ("H", "MH"):
            p.role = "fixed"
            p.fixed = True
        elif pre in ("J", "P", "CN", "USB", "X") and len(p.pads) >= 2 and ("conn" in fpl or "usb" in fpl or "header" in fpl
                                                                         or "jst" in fpl or "terminal" in fpl or pre in ("J", "CN", "USB")):
            p.role = "connector"
        elif pre in ("Y",) or "crystal" in fpl or "oscillator" in fpl or "resonator" in fpl:
            p.role = "crystal"
        elif pre in ("U", "IC", "D") and len(p.pads) >= 3 and ESD_RE.search(text):
            p.role = "esd"
        elif pre in ("U", "IC") and len(p.pads) >= 3:
            p.role = "regulator" if REG_RE.search(text) else "ic"
        elif pre in ("SW", "S", "BTN"):
            p.role = "switch"
        elif pre in ("BT",):
            p.role = "connector"
        elif p.fixed:
            p.role = "fixed"
    ics = [p for p in parts if p.role in ("ic", "regulator")]
    mcu = max((p for p in ics if p.role == "ic"), key=lambda p: len(p.pads), default=None)
    if mcu is not None and len(mcu.pads) >= 6:
        mcu.role = "mcu"
    crystals = [p for p in parts if p.role == "crystal"]
    conns = [p for p in parts if p.role == "connector"]
    caps = []
    # two-terminal parts
    for p in parts:
        if p.role != "other" or len(p.pads) not in (2, 3) or p.fixed:
            continue
        nets = [pad.get("net") for pad in p.pads]
        pre = p.prefix()
        gnd = [n for n in nets if is_ground(n)]
        oth = [n for n in nets if n and not is_ground(n)]
        if pre == "C" and gnd and oth:
            n = oth[0]
            xt = [c for c in crystals if n in c.nets]
            if xt:
                p.role, p.anchor = "xtal_cap", (xt[0], next(pd for pd in xt[0].pads if pd.get("net") == n))
                continue
            if any(q.role in ("ic", "mcu", "regulator") for q, _ in net_parts.get(n, [])):
                caps.append((p, n))
                continue
        if pre in ("R", "C", "L", "FB", "D") and oth:
            # pull-ups / series parts → attach to the IC signal pin they touch
            best = None
            for n in oth:
                if is_power(n):
                    continue
                for q, pad in net_parts.get(n, []):
                    if q is not p and q.role in ("mcu", "ic", "regulator", "crystal"):
                        score = len(net_parts.get(n, []))
                        if best is None or score < best[0]:
                            best = (score, q, pad)
            if best:
                p.role, p.anchor = "attached", (best[1], best[2])
    _assign_decaps(caps, net_parts)
    # ESD arrays sit at the connector whose signal lines they clamp
    for e in [p for p in parts if p.role == "esd"]:
        best = None
        for c in conns:
            shared = [pd for pd in c.pads if pd.get("net") in e.nets and not is_ground(pd["net"]) and not is_power(pd["net"])]
            if shared and (best is None or len(shared) > best[0]):
                best = (len(shared), c, shared[0])
        if best:
            e.anchor = (best[1], best[2])
    # crystals hug the MCU's oscillator pins (a shared GND/TESTEN pin is not an anchor)
    for c in crystals:
        if mcu is not None:
            sig = [pd for pd in mcu.pads if pd.get("net") in c.nets and not is_ground(pd["net"]) and not is_power(pd["net"])]
            sig.sort(key=lambda pd: 0 if XTAL_FN.search(pd.get("fn") or "") else 1)
            if sig:
                c.anchor = (mcu, sig[0])
    return mcu


def _assign_decaps(caps, net_parts):
    """Spread decoupling caps over the ICs on their supply net. Cost per (cap, IC): schematic
    distance when both symbols are on one sheet (the designer drew the cap next to its IC), plus the
    IC's load (caps already given / its pins on that net) so every IC gets one before any gets a
    second, plus a role bias (small caps at ICs, one bulk cap at a regulator)."""
    load, reg_bulk, pin_load = {}, set(), {}
    caps = sorted(caps, key=lambda cn: (cap_farads(cn[0].value) or 1e-7, cn[0].ref))
    for p, n in caps:
        f = cap_farads(p.value) or 1e-7
        best = None
        for q in sorted({q for q, _ in net_parts.get(n, []) if q.role in ("ic", "mcu", "regulator")}, key=lambda q: q.ref):
            pins = [pd for pd in q.pads if pd.get("net") == n]
            if not pins:
                continue
            cost = 20.0 * load.get((q.ref, n), 0) / len(pins)
            cost += _sch_cost(p, q)
            if q.role == "regulator":
                cost += -8.0 if (f >= 1e-6 and (q.ref, n) not in reg_bulk) else 12.0
            if best is None or cost < best[0]:
                best = (cost, q, pins)
        if best is None:
            continue
        _, q, pins = best
        pin = min(pins, key=lambda pd: (pin_load.get((q.ref, pd.get("num")), 0), str(pd.get("num"))))
        pin_load[(q.ref, pin.get("num"))] = pin_load.get((q.ref, pin.get("num")), 0) + 1
        load[(q.ref, n)] = load.get((q.ref, n), 0) + 1
        if q.role == "regulator" and f >= 1e-6:
            reg_bulk.add((q.ref, n))
        p.role, p.anchor = "decap", (q, pin)


def _sch_cost(a: Part, b: Part) -> float:
    """Schematic affinity: 0..20 when both symbols sit on the same sheet instance (closer = lower),
    30 on different sheets (another instance of a multi-instance sheet is a different IC), 10 unknown."""
    sa, sb = a.d.get("sch"), b.d.get("sch")
    if not sa or not sb:
        return 10.0
    if sa[0] != sb[0]:
        return 30.0
    return min(math.hypot(sa[1] - sb[1], sa[2] - sb[2]), 80.0) / 4.0


# ── the placer ────────────────────────────────────────────────────────────
class Placer:
    def __init__(self, parts, outline, spacing=None, edge=0.5, grid=0.5, ground_weight=0.25, tracks=None):
        self.parts = parts
        self.mcu = classify(parts)   # also marks mounting holes etc. as fixed obstacles
        self.by_ref = {p.ref: p for p in parts}
        self.ol = outline
        x0, y0, x1, y1 = outline["bbox"]
        self.box = (x0 + edge, y0 + edge, x1 - edge, y1 - edge)
        self.poly = outline.get("polygon")
        self.rect_outline = self._is_rect(self.poly, outline["bbox"]) if self.poly else True
        self.grid = grid
        self.edge = edge
        self.gw = ground_weight
        free = (x1 - x0) * (y1 - y0)
        # every part on the board counts (placing 6 new parts on a full board is still a full board)
        used = sum(p.area() for p in parts if not p.ref.startswith("#OBST") and
                   (not p.fixed or (x0 - 1 <= p.x <= x1 + 1 and y0 - 1 <= p.y <= y1 + 1))) or 1
        fill = used / max(free, 1)
        self.fill = fill
        self.spacing = spacing if spacing is not None else (1.5 if fill < 0.2 else 1.0 if fill < 0.35 else 0.6 if fill < 0.5 else 0.3)
        self.placed_rects = {}
        self.unplaced_refs = []
        self.tracks = tracks or []   # existing copper [x1,y1,x2,y2,half_width,layer] new parts must not land on
        for p in parts:
            if p.fixed:
                p.placed = True
                self.placed_rects[p.ref] = p.rect()

    @staticmethod
    def _is_rect(poly, bbox):
        if not poly:
            return True
        x0, y0, x1, y1 = bbox
        for x, y in poly:
            if not (abs(x - x0) < 0.01 or abs(x - x1) < 0.01) and not (abs(y - y0) < 0.01 or abs(y - y1) < 0.01):
                return False
        return True

    def _inside(self, r):
        b = self.box
        if r[0] < b[0] - 1e-6 or r[1] < b[1] - 1e-6 or r[2] > b[2] + 1e-6 or r[3] > b[3] + 1e-6:
            return False
        if not self.rect_outline and self.poly:
            for (x, y) in ((r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])):
                if not _pip(self.poly, x, y):
                    return False
        return True

    def _gap(self, a, b):
        if (a.anchor and a.anchor[0] is b) or (b.anchor and b.anchor[0] is a):
            return 0.25
        if a.role in ("decap", "xtal_cap", "attached", "esd") and b.role in ("decap", "xtal_cap", "attached", "esd"):
            return min(self.spacing, 0.4)
        return self.spacing

    def legal(self, p, x, y, rot, ignore=None):
        r = p.rect(x, y, rot)
        if p.overhang:
            # a connector mouth may stick out past its own edge; clip that side for the inside test
            r2 = list(r)
            b = self.box
            k = {"left": 0, "top": 1, "right": 2, "bottom": 3}[p.overhang]
            r2[k] = max(r2[k], b[k]) if k < 2 else min(r2[k], b[k])
            if not self._inside(tuple(r2)):
                return False
        elif not self._inside(r):
            return False
        for ref, q in self.placed_rects.items():
            if ref == p.ref or ref == ignore:
                continue
            g = self._gap(p, self.by_ref[ref])
            if r[0] < q[2] + g and r[2] + g > q[0] and r[1] < q[3] + g and r[3] + g > q[1]:
                return False
        if self.tracks:
            tht = any(pd.get("type") in ("tht", "npth") for pd in p.pads)
            for x1, y1, x2, y2, hw, lay in self.tracks:
                if (tht or lay == "F.Cu" or lay == "*") and _seg_hits_rect(x1, y1, x2, y2, hw + 0.2, r):
                    return False
        return True

    def cost(self, p, x, y, rot):
        c = 0.0
        for pad in p.pads:
            n = pad.get("net")
            if not n or n.startswith("unconnected-"):
                continue
            px, py = p.pad_pos(pad, x, y, rot)
            best = None
            for q in self.parts:
                if q is p or not q.placed or n not in q.nets:
                    continue
                for qp in q.pads:
                    if qp.get("net") == n:
                        qx, qy = q.pad_pos(qp)
                        d = abs(qx - px) + abs(qy - py)
                        if best is None or d < best:
                            best = d
            if best is not None:
                c += best * (self.gw if is_ground(n) else 1.0)
        if p.anchor and p.anchor[0].placed:
            q, qp = p.anchor
            qx, qy = q.pad_pos(qp)
            # distance from the anchor pin to this part's pad on the same net (else its centre)
            mine = [pd for pd in p.pads if pd.get("net") and pd.get("net") == qp.get("net")]
            if mine:
                px, py = p.pad_pos(mine[0], x, y, rot)
            else:
                px, py = x, y
            c += 4.0 * math.hypot(qx - px, qy - py)
        return c

    def target(self, p):
        pts = []
        if p.anchor and p.anchor[0].placed:
            return p.anchor[0].pad_pos(p.anchor[1])
        for pad in p.pads:
            n = pad.get("net")
            if not n or is_ground(n):
                continue
            for q in self.parts:
                if q is p or not q.placed or n not in q.nets:
                    continue
                for qp in q.pads:
                    if qp.get("net") == n:
                        pts.append(q.pad_pos(qp))
        if not pts:
            b = self.box
            return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))

    def rotations(self, p):
        if len(p.pads) <= 1:
            return [0]
        return [0, 90, 180, 270]

    def search(self, p, rots=None, max_legal=60, ignore=None):
        tx, ty = self.target(p)
        g = self.grid
        b = self.box
        step = max(g, round(math.sqrt((b[2] - b[0]) * (b[3] - b[1])) / 70 / g) * g)
        cands = []
        xs = _frange(b[0], b[2], step)
        ys = _frange(b[1], b[3], step)
        for x in xs:
            for y in ys:
                cands.append((abs(x - tx) + abs(y - ty), x, y))
        cands.sort()
        rots = rots or self.rotations(p)
        best = None
        legal_found = 0
        for d, x, y in cands:
            if best is not None and d > best[0] + 6 * step + 4 and legal_found >= 8:
                break
            for rot in rots:
                # align the courtyard to the grid rather than the origin
                cx, cy = self._grid_pos(p, x, y, rot)
                if not self.legal(p, cx, cy, rot, ignore):
                    continue
                legal_found += 1
                c = self.cost(p, cx, cy, rot) + 0.02 * d
                if best is None or c < best[1]:
                    best = (d, c, cx, cy, rot)
            if legal_found >= max_legal:
                break
        if best is None:
            return None
        # local refinement on the fine grid
        _, c0, bx, by, br = best
        if step > g:
            for dx in _frange(-step, step, g):
                for dy in _frange(-step, step, g):
                    for rot in rots:
                        cx, cy = snap(bx + dx, g), snap(by + dy, g)
                        if self.legal(p, cx, cy, rot, ignore):
                            c = self.cost(p, cx, cy, rot)
                            if c < c0:
                                c0, bx, by, br = c, cx, cy, rot
        return bx, by, br, c0

    def _grid_pos(self, p, x, y, rot):
        return snap(x, self.grid), snap(y, self.grid)

    def pin_in_place(self, p):
        """No legal spot: leave the part where it is, but as an obstacle for everything placed after it."""
        p.p_unplaced = True
        p.fixed = True
        p.placed = True
        p.overhang = None
        self.placed_rects[p.ref] = p.rect()
        self.unplaced_refs.append(p.ref)

    def commit(self, p, x, y, rot):
        p.x, p.y, p.rot = x, y, rot
        p.side = "top"
        p.placed = True
        self.placed_rects[p.ref] = p.rect()

    # ── connectors on the edges ────────────────────────────────────────
    def place_connectors(self, conns, power_in):
        b = self.box
        order = sorted(conns, key=lambda p: (0 if p in power_in else 1, -len(p.pads), p.ref))
        edges = ["left", "right", "top", "bottom"]
        slots = {e: [] for e in edges}
        for i, p in enumerate(order):
            e = edges[0] if p in power_in else edges[(i if not power_in else i) % 4]
            if p not in power_in and e == "left" and power_in:
                e = "right"
            slots[e].append(p)
        for e, lst in slots.items():
            n = len(lst)
            for k, p in enumerate(lst):
                rot = self._edge_rot(p, e)
                frac = (k + 1) / (n + 1)
                r0 = p.rect(0, 0, rot)
                w, h = r0[2] - r0[0], r0[3] - r0[1]
                if e == "left":
                    x = b[0] - r0[0]
                    y = b[1] + frac * (b[3] - b[1]) - (r0[1] + h / 2)
                elif e == "right":
                    x = b[2] - r0[2]
                    y = b[1] + frac * (b[3] - b[1]) - (r0[1] + h / 2)
                elif e == "top":
                    y = b[1] - r0[1]
                    x = b[0] + frac * (b[2] - b[0]) - (r0[0] + w / 2)
                else:
                    y = b[3] - r0[3]
                    x = b[0] + frac * (b[2] - b[0]) - (r0[0] + w / 2)
                if p.edge_hint:
                    # put the footprint's "PCB Edge" marker on the real board edge (mouth flush / overhanging)
                    hx, hy = tf(p.edge_hint[0], p.edge_hint[1], rot)
                    ob = self.ol["bbox"]
                    if e == "left":
                        x = ob[0] - hx
                    elif e == "right":
                        x = ob[2] - hx
                    elif e == "top":
                        y = ob[1] - hy
                    else:
                        y = ob[3] - hy
                    p.overhang = e
                # snap inward so we never cross the edge
                g = self.grid
                if e in ("left", "right"):
                    if not p.edge_hint:   # edge-marked connectors stay exactly on the edge
                        x = math.ceil(x / g) * g if e == "left" else math.floor(x / g) * g
                    y = snap(y, g)
                else:
                    if not p.edge_hint:
                        y = math.ceil(y / g) * g if e == "top" else math.floor(y / g) * g
                    x = snap(x, g)
                if self.legal(p, x, y, rot):
                    self.commit(p, round(x, 4), round(y, 4), rot)
                    p.edge = e
                else:
                    # slide along the edge until legal
                    done = False
                    for off in _offsets(max(b[2] - b[0], b[3] - b[1]), self.grid):
                        xx, yy = (x, y + off) if e in ("left", "right") else (x + off, y)
                        if self.legal(p, xx, yy, rot):
                            self.commit(p, round(xx, 4), round(yy, 4), rot)
                            p.edge = e
                            done = True
                            break
                    if not done and not p.edge_hint:
                        # doesn't fit along this edge (e.g. a long header vs. a short edge between holes):
                        # try the other edges before giving up on edge placement
                        for e2 in [x for x in ("top", "bottom", "right", "left") if x != e]:
                            if self._place_on_edge(p, e2):
                                done = True
                                break
                    if not done:
                        r = self.search(p)
                        if r:
                            self.commit(p, r[0], r[1], r[2])
                        else:
                            self.pin_in_place(p)

    def _place_antenna(self, p) -> bool:
        """Put the antenna strip flush with a board edge, facing out; pick the cheapest legal edge."""
        out_vec = {"top": (0, -1), "bottom": (0, 1), "left": (-1, 0), "right": (1, 0)}
        lv = out_vec[p.antenna["side"]]
        b, g = self.box, self.grid
        best = None
        for e, ev in out_vec.items():
            rot = next((r for r in (0, 90, 180, 270)
                        if tuple(round(v) for v in p.ltf(lv[0], lv[1], r)) == ev), None)
            if rot is None:
                continue
            r0 = p.rect(0, 0, rot)
            w, h = r0[2] - r0[0], r0[3] - r0[1]
            if e == "left":
                x, y = math.ceil((b[0] - r0[0]) / g) * g, snap((b[1] + b[3]) / 2 - (r0[1] + h / 2), g)
            elif e == "right":
                x, y = math.floor((b[2] - r0[2]) / g) * g, snap((b[1] + b[3]) / 2 - (r0[1] + h / 2), g)
            elif e == "top":
                x, y = snap((b[0] + b[2]) / 2 - (r0[0] + w / 2), g), math.ceil((b[1] - r0[1]) / g) * g
            else:
                x, y = snap((b[0] + b[2]) / 2 - (r0[0] + w / 2), g), math.floor((b[3] - r0[3]) / g) * g
            for off in _offsets(max(b[2] - b[0], b[3] - b[1]) / 2, g):
                xx, yy = (x, y + off) if e in ("left", "right") else (x + off, y)
                if self.legal(p, xx, yy, rot):
                    c = self.cost(p, xx, yy, rot) + 0.5 * abs(off)
                    if best is None or c < best[0]:
                        best = (c, xx, yy, rot, e)
                    break
        if best is None:
            return False
        self.commit(p, round(best[1], 4), round(best[2], 4), best[3])
        p.edge = best[4]
        return True

    def _place_on_edge(self, p, e) -> bool:
        b, g = self.box, self.grid
        rot = self._edge_rot(p, e)
        r0 = p.rect(0, 0, rot)
        w, h = r0[2] - r0[0], r0[3] - r0[1]
        if e == "left":
            x, y = math.ceil((b[0] - r0[0]) / g) * g, snap((b[1] + b[3]) / 2 - (r0[1] + h / 2), g)
        elif e == "right":
            x, y = math.floor((b[2] - r0[2]) / g) * g, snap((b[1] + b[3]) / 2 - (r0[1] + h / 2), g)
        elif e == "top":
            x, y = snap((b[0] + b[2]) / 2 - (r0[0] + w / 2), g), math.ceil((b[1] - r0[1]) / g) * g
        else:
            x, y = snap((b[0] + b[2]) / 2 - (r0[0] + w / 2), g), math.floor((b[3] - r0[3]) / g) * g
        for off in _offsets(max(b[2] - b[0], b[3] - b[1]), g):
            xx, yy = (x, y + off) if e in ("left", "right") else (x + off, y)
            if self.legal(p, xx, yy, rot):
                self.commit(p, round(xx, 4), round(yy, 4), rot)
                p.edge = e
                return True
        return False

    def _edge_rot(self, p, edge):
        lb = p.lbox
        cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        if p.pads:
            px = sum(pd["lx"] for pd in p.pads) / len(p.pads) - cx
            py = sum(pd["ly"] for pd in p.pads) / len(p.pads) - cy
        else:
            px = py = 0
        w, h = lb[2] - lb[0], lb[3] - lb[1]
        out = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[edge]
        if p.edge_hint:
            mx, my = p.edge_hint[0] - cx, p.edge_hint[1] - cy
            if abs(mx) > abs(my):
                my = 0
            else:
                mx = 0
            return max((0, 90, 180, 270), key=lambda r: tf(mx, my, r)[0] * out[0] + tf(mx, my, r)[1] * out[1])
        # side-entry connector: mouth is opposite the pads (pads sit at the back)
        if math.hypot(px, py) > 0.18 * max(w, h):
            mx, my = -px, -py
            best = max((0, 90, 180, 270), key=lambda r: tf(mx, my, r)[0] * out[0] + tf(mx, my, r)[1] * out[1])
            return best
        # symmetric header: long axis along the edge
        along_x = edge in ("top", "bottom")
        return 0 if (w >= h) == along_x else 90

    def run(self):
        parts = [p for p in self.parts if not p.fixed]
        mcu = self.mcu
        conns = [p for p in parts if p.role == "connector"]
        regs = [p for p in parts if p.role == "regulator"]
        reg_in_nets = set()
        for r in regs:
            for pad in r.pads:
                fn = (pad.get("fn") or "").upper()
                if pad.get("net") and not is_ground(pad["net"]) and (fn in ("VI", "VIN", "IN", "VCC") or pad.get("pt") == "power_in"):
                    reg_in_nets.add(pad["net"])
        power_in = [c for c in conns if c.nets & reg_in_nets] or \
                   [c for c in conns if any(is_power(n) for n in c.nets) and len(c.pads) <= 4][:1]
        self.place_connectors(conns, power_in)
        # RF modules: antenna on a board edge, pointing out (before anything else takes the edges)
        for p in sorted([q for q in parts if q.antenna and not q.placed], key=lambda q: -q.area()):
            self._place_antenna(p)
        order = []
        if mcu is not None and not mcu.fixed and not mcu.placed:
            b = self.box
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            best = None
            for rot in (0, 90):
                r0 = mcu.rect(0, 0, rot)
                for off in _offsets(max(b[2] - b[0], b[3] - b[1]) / 2, self.grid):
                    for dx, dy in ((off, 0), (0, off)):
                        x = snap(cx - (r0[0] + r0[2]) / 2 + dx)
                        y = snap(cy - (r0[1] + r0[3]) / 2 + dy)
                        if self.legal(mcu, x, y, rot):
                            c = self.cost(mcu, x, y, rot) + abs(dx) + abs(dy)
                            if best is None or c < best[0]:
                                best = (c, x, y, rot)
                    if best is not None and abs(off) > 6:
                        break
            if best:
                self.commit(mcu, best[1], best[2], best[3])
            else:
                r = self.search(mcu, rots=(0, 90, 180, 270), max_legal=400)
                if r:
                    self.commit(mcu, r[0], r[1], r[2])
        rest = [p for p in parts if not p.placed]
        # anchors first (ICs/regulators/crystals), each followed by the parts that hug it
        prio = {"mcu": 0, "regulator": 1, "ic": 2, "crystal": 3, "switch": 5, "other": 6,
                "decap": 4, "xtal_cap": 4, "attached": 4, "esd": 2}
        bx = self.box
        big_area = 0.04 * (bx[2] - bx[0]) * (bx[3] - bx[1])
        for p in rest:
            if p.role in ("other", "switch") and p.area() > big_area and not p.anchor:
                p.role = "big"      # sockets, modules, displays… hard to fit: place them early
        prio["big"] = 0.5
        anchors = sorted([p for p in rest if p.role in ("regulator", "ic", "crystal", "big", "esd") and
                          not (p.role == "esd" and p.anchor)],
                         key=lambda p: (prio[p.role], -p.area() if p.role == "big" else -len(p.pads)))
        followers = {}
        for p in rest:
            if p.anchor and p.role in ("decap", "xtal_cap", "attached", "esd"):
                followers.setdefault(p.anchor[0].ref, []).append(p)
        seq = []
        for c in [q for q in self.parts if q.role == "connector"]:
            seq += sorted(followers.get(c.ref, []), key=lambda p: p.ref)
        if mcu is not None:
            seq += sorted(followers.get(mcu.ref, []), key=lambda p: (p.role != "decap", p.ref))
        for a in anchors:
            seq.append(a)
            seq += sorted(followers.get(a.ref, []), key=lambda p: (p.role != "xtal_cap", p.role != "decap", p.ref))
        for p in rest:
            if p not in seq and not (p.anchor and p.anchor[0].ref in followers and p in followers[p.anchor[0].ref]):
                seq.append(p)
        for p in rest:
            if p not in seq:
                seq.append(p)
        # generic parts: most-connected-to-placed first
        fixed_seq = [p for p in seq if p.role != "other" and p.role != "switch"]
        generic = [p for p in seq if p.role in ("other", "switch")]
        unplaced = []
        for p in fixed_seq:
            r = self.search(p)
            if r:
                self.commit(p, r[0], r[1], r[2])
            else:
                unplaced.append(p.ref)
                self.pin_in_place(p)
        b = self.box
        big_area = 0.04 * (b[2] - b[0]) * (b[3] - b[1])
        while generic:
            # big parts first (they are the hard ones to fit), then most-connected-to-what-is-placed
            generic.sort(key=lambda p: (0 if p.area() > big_area else 1, -p.area() if p.area() > big_area else 0,
                                        -sum(1 for n in p.nets if any(q.placed and n in q.nets for q in self.parts if q is not p))))
            p = generic.pop(0)
            r = self.search(p)
            if r:
                self.commit(p, r[0], r[1], r[2])
            else:
                unplaced.append(p.ref)
                self.pin_in_place(p)
        # refinement: re-place each movable non-connector part with the others fixed
        for _ in range(2):
            improved = 0
            for p in parts:
                if not p.placed or p.fixed or p.role in ("connector", "fixed", "mcu") or p.antenna:
                    continue
                cur = self.cost(p, p.x, p.y, p.rot)
                del self.placed_rects[p.ref]
                p.placed = False
                r = self.search(p)
                if r and r[3] < cur - 0.5:
                    self.commit(p, r[0], r[1], r[2])
                    improved += 1
                else:
                    self.commit(p, p.x, p.y, p.rot)
            if not improved:
                break
        return unplaced


def _seg_hits_rect(x1, y1, x2, y2, pad, r):
    """Segment (x1,y1)-(x2,y2) comes within ``pad`` of rect r (Liang-Barsky clip on the grown rect)."""
    rx0, ry0, rx1, ry1 = r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p_, q_ in ((-dx, x1 - rx0), (dx, rx1 - x1), (-dy, y1 - ry0), (dy, ry1 - y1)):
        if abs(p_) < 1e-12:
            if q_ < 0:
                return False
            continue
        t = q_ / p_
        if p_ < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _frange(a, b, step):
    out = []
    n = int(math.floor((b - a) / step + 1e-9))
    for i in range(n + 1):
        out.append(round(a + i * step, 4))
    return out


def _offsets(maxd, step):
    yield 0.0
    k = 1
    while k * step <= maxd:
        yield k * step
        yield -k * step
        k += 1


def _pip(poly, x, y):
    c = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            c = not c
    return c


def auto_place(snapshot: dict, refs: list[str] | None = None, keep: list[str] | None = None,
               spacing: float | None = None, edge_clearance: float = 0.5, grid: float = 0.5) -> dict:
    """Compute an automatic placement. Returns {'moves': [...], 'score_before', 'score_after', ...}."""
    ol = snapshot.get("outline")
    if not ol:
        raise ValueError("no outline")
    parts = [Part(d) for d in snapshot["footprints"]]
    for i, o in enumerate(snapshot.get("obstacles") or []):
        x0, y0, x1, y1 = o["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        ob = Part({"ref": f"#OBST{i + 1}", "x": cx, "y": cy, "rot": 0, "locked": True, "pad_list": [],
                   "lbbox": [x0 - cx, y0 - cy, x1 - cx, y1 - cy], "footprint": o.get("kind", "")})
        ob.role = "fixed"
        parts.append(ob)
    keep = set(keep or [])
    if refs:
        keep |= {p.ref for p in parts if p.ref not in set(refs)}
    for p in parts:
        if p.ref in keep:
            p.fixed = True
        if p.side == "bottom" and not p.fixed:
            p.fixed = True   # bottom-side parts are left alone by the auto placer
    before = ratsnest(parts)
    pl = Placer(parts, ol, spacing=spacing, edge=edge_clearance, grid=grid,
                tracks=snapshot.get("track_segs") if refs else None)   # only a partial placement keeps the routing
    pl.run()
    unplaced = sorted(set(pl.unplaced_refs))
    after = ratsnest(parts)
    moves = [{"ref": p.ref, "x": p.x, "y": p.y, "rot": p.rot % 360, "side": "top"}
             for p in parts if not p.fixed and p.placed and not p.ref.startswith("#OBST")]
    roles = {}
    for p in parts:
        if p.ref.startswith("#OBST"):
            continue
        roles.setdefault(p.role, []).append(p.ref + (f"→{p.anchor[0].ref}.{p.anchor[1]['num']}" if p.anchor else ""))
    return {"moves": moves, "score_before": before, "score_after": after, "unplaced": unplaced,
            "spacing": pl.spacing, "fill_ratio": round(pl.fill, 3), "roles": roles}


def auto_skipped(snapshot: dict, moved, refs=None, keep=None, unplaced=()) -> list[dict]:
    """Every part an auto placement was asked to place but left where it was, with the reason and a ready
    fix: [{ref, skipped, fix?}]. ``moved`` = refs the placement produced a position for. With refs=[...]
    only those are in scope (the rest were never asked for); otherwise every footprint is."""
    moved, keep, unplaced = set(moved), set(keep or []), set(unplaced or [])
    by = {f["ref"]: f for f in snapshot.get("footprints") or []}
    out = []
    for ref in (list(dict.fromkeys(refs)) if refs else list(by)):
        f = by.get(ref)
        if f is None:
            out.append({"ref": ref, "skipped": "not on the board",
                        "fix": "pcb_read(what=['footprints']) lists the footprints; pcb_sync() pulls new parts in"})
        elif ref in moved:
            continue
        elif "board_only" in (f.get("flags") or []):
            out.append({"ref": ref, "skipped": "board-only footprint (mounting hole): a fixed obstacle",
                        "fix": "pcb_setup(holes=...) re-seats mounting holes"})
        elif f.get("locked"):
            out.append({"ref": ref, "skipped": "locked", "fix": f"pcb_place(auto=true, moves=[{{\"ref\":\"{ref}\",\"unlock\":true}}])"})
        elif ref in keep:
            out.append({"ref": ref, "skipped": "in keep=[...]"})
        elif f.get("side") == "bottom":
            out.append({"ref": ref, "skipped": "on the bottom side (auto placement only places top-side parts)",
                        "fix": f"pcb_place(moves=[{{\"ref\":\"{ref}\",\"side\":\"top\"}}]) first, or give it x/y in moves=[...]"})
        elif ref in unplaced:
            out.append({"ref": ref, "skipped": "no legal spot",
                        "fix": "a bigger board (pcb_setup(width=..., height=...)) or pcb_place(auto=true, spacing=0.3)"})
        else:
            out.append({"ref": ref, "skipped": "not moved"})
    return out


def score(snapshot: dict) -> float:
    return ratsnest([Part(d) for d in snapshot["footprints"]])


def overlaps(snapshot: dict, gap: float = 0.0) -> list[list[str]]:
    parts = [Part(d) for d in snapshot["footprints"]]
    out = []
    for i, a in enumerate(parts):
        ra = a.rect()
        for b in parts[i + 1:]:
            if a.side != b.side and not (a.fixed or b.fixed):
                continue
            rb = b.rect()
            if ra[0] < rb[2] + gap and ra[2] + gap > rb[0] and ra[1] < rb[3] + gap and ra[3] + gap > rb[1]:
                out.append([a.ref, b.ref])
    return out


def antenna_polygon(p: Part) -> list[list[float]] | None:
    """Absolute board polygon of a placed RF module's antenna keepout."""
    if not p.antenna:
        return None
    x0, y0, x1, y1 = p.antenna["rect"]
    return [[round(p.x + dx, 4), round(p.y + dy, 4)]
            for dx, dy in (p.ltf(cx, cy, p.rot) for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))]


def antenna_keepouts(snapshot: dict) -> list[dict]:
    out = []
    for d in snapshot.get("footprints", []):
        p = Part(d)
        poly = antenna_polygon(p)
        if poly:
            out.append({"name": f"copper-antenna-{p.ref}", "polygon": poly, "ref": p.ref, "edge_side": p.antenna["side"]})
    return out
