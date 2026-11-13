"""S0 classify: net roles, widths, buses, decoupling pairs, connections in Steiner order (design §4.3).

Pure functions over ``guided.model.Board`` + ``guided.plan.Config`` (+ the optional v2 hints ``x``); the only
side effects are in ``run``: it fills ``ctx.conns``, ``ctx.x.auto`` and ``ctx.history["classify"]`` and raises
the oracle clearance of nets whose plan clearance is above their class.

Role of a net, first match (§4.3):
    plan ``role`` > has a pour (``plane``) > ground / power name, power net class or a power-output pin
    (``power``) > declared differential pair (``pair``) > bus member (``bus``) > ``keep_short`` (``critical``)
    > ``signal``.
A ground without a pour (GNDA ...) is ``power``. The plan's ``skip`` is honoured whoever set it: the built-in
"leave a ground net with 6+ pads and no pour for pcb_pour" (what the tool promises with no plan, reported in
``skipped``), a plan entry, or the session's net selection (``nets=[...]``: reason 'not selected'). Such a ground is
routed, late and as power, only when the plan says so: ``plan.nets[<net>].skip = false`` (``auto.unskipped``).

What ``x`` (planx.V2Cfg) is read for, all optional and looked up defensively (``_hint`` / ``_net_hint``, so a
plain dict or None works too):
    per net  ``x.nets[name].role``, ``.tree`` ('star' | 'chain' | 'mst')
    global   ``x.power_width_factor`` (default 2.0), ``x.buses`` ([{nets, order?, layer?, spacing?}])

No clock, no randomness; every order is canonical (net name, cluster id, pad name).
"""
from __future__ import annotations

import math
import re

from ..guided import plan as P
from .types import ROLE_ORDER, ROLES, Budget, ConnV2, Ctx, StageReport, Terminal, q

POWER_WIDTH_FACTOR = 2.0      # power width = class width x this ...
POWER_WIDTH_MAX = 1.0         # ... but never above 1.0 mm ...
POWER_PAD_SHARE = 0.8         # ... nor above 0.8 x the smallest pad minor dimension on the net
BUS_MIN_NETS = 4
BUS_STEP_TOL = 0.05           # mm: two pad-to-pad steps are "the same" within this
DECAP_RADIUS = 3.0            # mm: a decoupling capacitor serves an IC pin of its power net within this distance
DECAP_IC_PADS = 4             # a footprint with at least this many pads counts as an IC
TREES = ("steiner", "mst", "star", "chain")
_SQ2M1 = 0.41421356237309515

_ANALOG_NAME = re.compile(r"^/?(A(GND|VDD|VCC|VSS)\w*|(GND|VDD|VCC|VSS)A\w*|VREF\w*|V?REF[+-]?\w*)$", re.I)
_CAP_REF = re.compile(r"^C\d")
_NUM_CHUNK = re.compile(r"(\d+)")


# ── reading the v2 hints (planx.V2Cfg is written by another work package) ──
def _hint(x, name, default=None):
    """A global v2 hint: ``x.<name>``, ``x[name]`` or ``x.glob[name]`` (also ``globals`` / ``g``)."""
    if x is None:
        return default
    if isinstance(x, dict):
        v = x.get(name)
        if v is None and isinstance(x.get("global"), dict):
            v = x["global"].get(name)
        return default if v is None else v
    v = getattr(x, name, None)
    if v is None:
        for holder in ("glob", "globals", "g"):
            d = getattr(x, holder, None)
            if isinstance(d, dict) and d.get(name) is not None:
                return d[name]
        return default
    return v


def _net_hint(x, net: str, field: str):
    """A per-net v2 hint: ``x.<field>s[net]``, ``x.<field>[net]``, ``x.nets[net][field]`` or ``x.nets[net].<field>``."""
    if x is None:
        return None
    for holder in (field + "s", field):
        d = _hint(x, holder)
        if isinstance(d, dict) and net in d:
            return d[net]
    nets = _hint(x, "nets")
    if isinstance(nets, dict) and net in nets:
        e = nets[net]
        return e.get(field) if isinstance(e, dict) else getattr(e, field, None)
    return None


# ── net facts ─────────────────────────────────────────────────────────────
def net_kind(board, name: str) -> str:
    """'ground' | 'power' | '' from the name, the net class and the pin types (the rules of ``plan.classify``)."""
    net = board.nets[name]
    s = P.short(name)
    cls = (net.cls or "").lower()
    if P._GROUND.match(s) or "gnd" in cls or "ground" in cls:
        return "ground"
    if P._POWER.match(s) or P._POWER.match(s.rsplit(".", 1)[-1]) or "power" in cls or "pwr" in cls:
        return "power"
    for p in net.pads:
        if p.pt == "power_out":
            return "power"
    return ""


def is_analog(board, name: str) -> bool:
    """Analog supply / reference / sense net (AGND, GNDA, AVDD, VREF, ADC*, *_SENSE ...): reported, not a role."""
    s = P.short(name)
    return bool(_ANALOG_NAME.match(s) or P._ANALOG.search(s))


def _routable(board, cfg) -> list[str]:
    return sorted(n for n, net in board.nets.items() if len(net.pads) >= 2)


def _natkey(s: str) -> tuple:
    return tuple((0, int(c), "") if c.isdigit() else (1, 0, c) for c in _NUM_CHUNK.split(s or "") if c != "")


def _pad_key(p) -> tuple:
    return (_natkey(p.ref), _natkey(p.num), p.x, p.y)


def pad_minor(pad) -> float:
    """Smaller copper dimension of a pad (mm): the widest track that still looks right entering it."""
    best = 0.0
    for shs in pad.shapes.values():
        for s in shs:
            if s.kind == "box":
                m = 2.0 * (min(s.g[4], s.g[5]) + s.g[6])
            else:
                bb = s.bbox
                m = min(bb[2] - bb[0], bb[3] - bb[1])
            if m > best:
                best = m
    return best


# ── buses ─────────────────────────────────────────────────────────────────
def _row_neighbours(pads: list) -> dict:
    """{pad index: [pad index, ...]}: mutual nearest neighbours inside one footprint (within 5 % of the
    nearest-neighbour distance of both pads) — the pads next to it in its row / column."""
    n = len(pads)
    near = []
    for i in range(n):
        pi = pads[i]
        ds = []
        for j in range(n):
            if j != i:
                ds.append((math.hypot(pads[j].x - pi.x, pads[j].y - pi.y), j))
        ds.sort()
        if not ds or ds[0][0] <= 1e-6:
            near.append(set())
            continue
        lim = ds[0][0] * 1.05 + 1e-6
        near.append({j for d, j in ds if d <= lim})
    return {i: sorted(j for j in near[i] if i in near[j]) for i in range(n)}


def detect_buses(board) -> list[dict]:
    """Conservative bus detection (§4.3): >= 4 two-pad nets between the same two footprints whose pads are
    consecutive (row neighbours at one constant step) on both, in the same or exactly reversed order.
    Ground / power / poured nets and nets with a pad anywhere else never take part.

    -> [{'nets': [...], 'order': [...] (both in pad order on A = the lateral order), 'refs': [A, B],
         'pad_order': 'same'|'reversed' (pad numbering on B against A), 'pads': [[A pads], [B pads]],
         'pitch': [mm on A, mm on B], 'auto': True}], longest first.
    """
    by_ref = {}
    for p in board.pads:
        if p.layers and p.num:
            by_ref.setdefault(p.ref, []).append(p)
    pairs = {}
    for name in sorted(board.nets):
        net = board.nets[name]
        if len(net.pads) != 2 or net.has_pour or net_kind(board, name):
            continue
        a, b = net.pads
        if a.ref == b.ref or not a.layers or not b.layers or not a.num or not b.num:
            continue
        if (_natkey(b.ref), b.ref) < (_natkey(a.ref), a.ref):
            a, b = b, a
        pairs.setdefault((a.ref, b.ref), []).append((name, a, b))
    found = []
    neigh_cache = {}

    def neigh(ref):
        if ref not in neigh_cache:
            pads = by_ref.get(ref, [])
            idx = {id(p): i for i, p in enumerate(pads)}
            neigh_cache[ref] = (pads, idx, _row_neighbours(pads))
        return neigh_cache[ref]

    for (ra, rb), members in sorted(pairs.items()):
        if len(members) < BUS_MIN_NETS:
            continue
        pa, ia, na = neigh(ra)
        pb, ib, nb = neigh(rb)
        net_of_a = {ia[id(a)]: k for k, (_n, a, _b) in enumerate(members) if id(a) in ia}
        if len(net_of_a) != len(members):
            continue                                    # two bus pads on one position: not a clean bus
        nxt = {}                                        # member k -> [(k2, stepA, stepB)]
        for k, (_n, a, b) in enumerate(members):
            if id(b) not in ib:
                continue
            bn = set(nb[ib[id(b)]])
            for j in na[ia[id(a)]]:
                k2 = net_of_a.get(j)
                if k2 is None:
                    continue
                a2, b2 = members[k2][1], members[k2][2]
                if ib.get(id(b2)) not in bn:
                    continue
                nxt.setdefault(k, []).append((k2, (a2.x - a.x, a2.y - a.y), (b2.x - b.x, b2.y - b.y)))

        def same(u, v):
            return abs(u[0] - v[0]) <= BUS_STEP_TOL and abs(u[1] - v[1]) <= BUS_STEP_TOL

        def follow(k, sa, sb):
            for k2, ta, tb in nxt.get(k, ()):
                if same(sa, ta) and same(sb, tb):
                    return k2
            return None

        prev = {}                                       # member k -> [(stepA, stepB)] of the steps arriving at k
        for kk in sorted(nxt):
            for k2, ta, tb in nxt[kk]:
                prev.setdefault(k2, []).append((kk, ta, tb))
        seen = set()
        for k in range(len(members)):
            for k2, sa, sb in nxt.get(k, ()):
                # a chain starts where nothing arrives at k with the same two steps
                if any(kk != k2 and same(sa, ta) and same(sb, tb) for kk, ta, tb in prev.get(k, ())):
                    continue
                chain = [k, k2]
                guard = {k, k2}
                cur = k2
                while True:
                    k3 = follow(cur, sa, sb)
                    if k3 is None or k3 in guard:
                        break
                    chain.append(k3)
                    guard.add(k3)
                    cur = k3
                if len(chain) < BUS_MIN_NETS:
                    continue
                apads = [members[c][1] for c in chain]
                bpads = [members[c][2] for c in chain]
                if _natkey(apads[0].num) > _natkey(apads[-1].num):
                    chain.reverse()
                    apads.reverse()
                    bpads.reverse()
                key = tuple(members[c][0] for c in chain)
                if key in seen:
                    continue
                seen.add(key)
                asc_b = _natkey(bpads[0].num) <= _natkey(bpads[-1].num)
                found.append({"nets": list(key), "order": list(key), "refs": [ra, rb],
                              "pad_order": "same" if asc_b else "reversed",
                              "pads": [[p.name for p in apads], [p.name for p in bpads]],
                              "pitch": [round(math.hypot(*sa), 4), round(math.hypot(*sb), 4)], "auto": True})
    found.sort(key=lambda d: (-len(d["nets"]), d["refs"], d["nets"]))
    out, used = [], set()
    for d in found:
        if used.isdisjoint(d["nets"]):
            out.append(d)
            used.update(d["nets"])
    return out


def _plan_buses(board, x) -> list[dict]:
    out = []
    for d in _hint(x, "buses") or []:
        if not isinstance(d, dict):
            continue
        nets = [n for n in (d.get("order") or d.get("nets") or []) if n in board.nets]
        if len(nets) >= 2:
            e = {"nets": nets, "auto": False}
            for k in ("layer", "spacing"):
                if d.get(k) is not None:
                    e[k] = d[k]
            out.append(e)
    return out


def buses(board, x=None) -> list[dict]:
    """Declared buses (plan ``buses``) first, then auto-detected ones that share no net with a declared bus."""
    out = _plan_buses(board, x)
    used = {n for d in out for n in d["nets"]}
    for d in detect_buses(board):
        if used.isdisjoint(d["nets"]):
            out.append(d)
    return out


# ── roles and widths ──────────────────────────────────────────────────────
def _roles(board, cfg, x, bus_list) -> tuple[dict, dict]:
    """-> ({net: role}, {net: why}) for every net with >= 2 pads; why in plan|pour|name|pair|bus|keep_short|default."""
    member = {n for d in bus_list for n in d["nets"]}
    out, why = {}, {}
    for n in _routable(board, cfg):
        net = board.nets[n]
        nc = cfg.nets.get(n) if cfg is not None else None
        r = _net_hint(x, n, "role")
        if r in ROLES:
            out[n], why[n] = r, "plan"
        elif net.has_pour:
            out[n], why[n] = "plane", "pour"
        elif net_kind(board, n):
            out[n], why[n] = "power", "name"
        elif nc is not None and nc.dp_partner:
            out[n], why[n] = "pair", "pair"
        elif n in member:
            out[n], why[n] = "bus", "bus"
        elif nc is not None and nc.keep_short:
            out[n], why[n] = "critical", "keep_short"
        else:
            out[n], why[n] = "signal", "default"
    return out, why


def roles(board, cfg, x) -> dict[str, str]:
    """{net: role} for every net with >= 2 pads (§4.3 precedence)."""
    return _roles(board, cfg, x, buses(board, x))[0]


def _plan_net(cfg, n: str) -> dict:
    return (((getattr(cfg, "used", None) or {}).get("nets") or {}).get(n) or {}) if cfg is not None else {}


def widths(board, cfg, x, roles: dict) -> dict[str, tuple[float, float]]:
    """{net: (width, neck)}. Plan ``width`` wins; ``power``: min(class x power_width_factor, 0.8 x the smallest
    pad minor dimension on the net, 1.0 mm), floored to 0.01 mm and never below the class width; everything
    else: the class (or pair) width. ``neck`` is the narrowest width a segment of the net may fall back to where
    the full width does not fit: min(width, class width, Default-class width) — v1's ``neck_widths`` rule, so a
    0.4 mm power class can still leave a 0.5 mm-pitch pad; == width for pairs and with plan ``neck: false``."""
    try:
        factor = float(_hint(x, "power_width_factor", POWER_WIDTH_FACTOR) or POWER_WIDTH_FACTOR)
    except (TypeError, ValueError):
        factor = POWER_WIDTH_FACTOR
    out = {}
    floor_w = board.rules["track_width"]
    df_w = max((board.rules.get("default") or {}).get("width") or 0.2, floor_w)
    cons = x.get("cons") if isinstance(x, dict) else getattr(x, "cons", None)
    rules = (getattr(cons, "widths", None) or {}) if getattr(cons, "mode", None) in ("auto", "plan") else {}
    for n in sorted(roles):
        net = board.nets[n]
        nc = cfg.nets.get(n) if cfg is not None else None
        d = _plan_net(cfg, n)
        class_w = net.width
        role = roles[n]
        if d.get("width") is not None and nc is not None:
            w = nc.width
        elif role == "power":
            minors = [m for m in (pad_minor(p) for p in net.pads) if m > 0]
            cap = POWER_PAD_SHARE * min(minors) if minors else POWER_WIDTH_MAX
            w = min(class_w * max(1.0, factor), cap, POWER_WIDTH_MAX)
            w = math.floor(w * 100.0 + 1e-6) / 100.0
            w = max(w, class_w)
        elif nc is not None:
            w = nc.width                                 # class width, or the pair width of a declared pair
        else:
            w = class_w
        neck = w if (role == "pair" or d.get("neck", True) is False) else max(min(w, class_w, df_w), floor_w)
        rule = rules.get(n)
        if rule is not None and not rule.neck:
            # a width rule (plan nets[..].widths / z, a KiCad track_width rule): the search runs at the widest
            # of its layers (detail.layer_width then lays each layer's own) and the net is never necked below it
            w = max([rule.width or w] + [v for _l, v in rule.by_layer])
            neck = w
        out[n] = (q(w), q(neck))
    return out


def net_table(board, cfg, x=None) -> dict[str, dict]:
    """Everything S0 decides per net: {net: {role, why, kind, analog, width, neck, clearance, via, skip}}."""
    bl = buses(board, x)
    r, why = _roles(board, cfg, x, bl)
    w = widths(board, cfg, x, r)
    out = {}
    for n in sorted(r):
        net = board.nets[n]
        nc = cfg.nets.get(n) if cfg is not None else None
        skip = bool(nc is not None and nc.skip)
        out[n] = {"role": r[n], "why": why[n], "kind": net_kind(board, n), "analog": is_analog(board, n),
                  "width": w[n][0], "neck": w[n][1],
                  "clearance": nc.clearance if nc is not None else net.clearance,
                  "via": (nc.via, nc.drill) if nc is not None else (net.via, net.drill),
                  "skip": skip, "unskipped": bool(nc is not None and not nc.skip and nc.explicit and not net.has_pour
                                                 and P._GROUND.match(n))}
    return out


# ── decoupling ────────────────────────────────────────────────────────────
def decoupling(board, roles: dict) -> list[tuple[str, str, str]]:
    """[(power net, capacitor pad, IC pad)]: a 2-pad capacitor with one pad on a ``power`` net and the other on
    a plane / ground net, whose power pad is within 3 mm of an IC pin (footprint with >= 4 pads) of the same
    net on a shared layer. One entry per capacitor, the nearest pin; canonical order."""
    by_ref = {}
    for p in board.pads:
        by_ref.setdefault(p.ref, []).append(p)
    out = []
    for ref in sorted(by_ref, key=lambda r: (_natkey(r), r)):
        pads = by_ref[ref]
        if len(pads) != 2 or not _CAP_REF.match(ref):
            continue
        for cap, other in ((pads[0], pads[1]), (pads[1], pads[0])):
            if not cap.net or not other.net or cap.net == other.net:
                continue
            n = board.net_by_id[cap.net].name
            on = board.net_by_id[other.net].name
            if roles.get(n) != "power":
                continue
            if not (roles.get(on) == "plane" or net_kind(board, on) == "ground"):
                continue
            best = None
            for ic in board.nets[n].pads:
                if ic.ref == ref or len(by_ref.get(ic.ref, ())) < DECAP_IC_PADS:
                    continue
                if not set(ic.layers) & set(cap.layers):
                    continue
                d = math.hypot(ic.x - cap.x, ic.y - cap.y)
                if d <= DECAP_RADIUS and (best is None or (round(d, 6), _pad_key(ic)) < best[0]):
                    best = ((round(d, 6), _pad_key(ic)), ic)
            if best:
                out.append((n, cap.name, best[1].name))
                break
    return out


# ── connections ───────────────────────────────────────────────────────────
def _octile(dx: float, dy: float) -> float:
    dx, dy = abs(dx), abs(dy)
    return (dx + _SQ2M1 * dy) if dx > dy else (dy + _SQ2M1 * dx)


def _terminal(p) -> Terminal:
    return Terminal(x=q(p.x), y=q(p.y), layers=tuple(p.layers), pad=p.name, cluster=p.cluster)


def _dogleg(ax, ay, tx, ty):
    """The octile path a -> t as <= 2 segments (diagonal first)."""
    dx, dy = tx - ax, ty - ay
    m = min(abs(dx), abs(dy))
    if m < 1e-9 or abs(abs(dx) - abs(dy)) < 1e-9:
        return [(ax, ay, tx, ty)]
    mx = ax + math.copysign(m, dx)
    my = ay + math.copysign(m, dy)
    return [(ax, ay, mx, my), (mx, my, tx, ty)]


class _Tree:
    """Incremental nearest-point bookkeeping for one net: every pad outside the tree knows its nearest tree
    point (pads, kept copper and the planned doglegs) and its nearest tree pad."""

    def __init__(self, groups: dict, steiner: bool):
        self.steiner = steiner
        self.out = []                       # [x, y, gid, pad, d_tree, tx, ty, d_pad, tree pad]
        for gid in sorted(groups):
            for p in groups[gid]["pads"]:
                self.out.append([p.x, p.y, gid, p, math.inf, 0.0, 0.0, math.inf, None])

    def drop(self, gid):
        self.out = [o for o in self.out if o[2] != gid]

    def reset(self):
        for o in self.out:
            o[4] = o[7] = math.inf
            o[8] = None

    def grow(self, pads, segs):
        """Add tree pads and (Steiner mode only) tree segments."""
        out = self.out
        for t in pads:
            tx, ty = t.x, t.y
            for o in out:
                dx = o[0] - tx
                dy = o[1] - ty
                if dx < 0:
                    dx = -dx
                if dy < 0:
                    dy = -dy
                d = (dx + _SQ2M1 * dy) if dx > dy else (dy + _SQ2M1 * dx)
                if d < o[7] - 1e-9:
                    o[7] = d
                    o[8] = t
                if d < o[4] - 1e-9:
                    o[4] = d
                    o[5] = tx
                    o[6] = ty
        if not self.steiner:
            return
        for x1, y1, x2, y2 in segs:
            ex, ey = x2 - x1, y2 - y1
            L = ex * ex + ey * ey
            for o in out:
                px, py = o[0], o[1]
                if L <= 0:
                    nx, ny = x1, y1
                else:
                    t = ((px - x1) * ex + (py - y1) * ey) / L
                    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                    nx, ny = x1 + t * ex, y1 + t * ey
                dx = px - nx
                dy = py - ny
                if dx < 0:
                    dx = -dx
                if dy < 0:
                    dy = -dy
                d = (dx + _SQ2M1 * dy) if dx > dy else (dy + _SQ2M1 * dx)
                if d < o[4] - 1e-9:
                    o[4] = d
                    o[5] = nx
                    o[6] = ny

    def nearest(self):
        best = None
        for o in self.out:
            if o[8] is None:
                continue
            k = (round(o[4], 6), o[2], _pad_key(o[3]))
            if best is None or k < best[0]:
                best = (k, o)
        return best[1] if best else None


def _kept_segs(c: dict) -> list:
    segs = [(t[2], t[3], t[4], t[5]) for t in c["tracks"]]
    segs += [(v[1], v[2], v[1], v[2]) for v in c["vias"]]
    return segs


def _weight(c: dict) -> tuple:
    area = 0.0
    for _l, pts in c["islands"]:
        n = len(pts)
        a = 0.0
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            a += x1 * y2 - x2 * y1
        area += abs(a) / 2
    length = sum(math.hypot(t[4] - t[2], t[5] - t[3]) for t in c["tracks"])
    return (round(area, 3), len(c["pads"]), round(length, 3))


def _plane_edges(cl: dict) -> list:
    """Poured net: every cluster but the main one (largest island area, then most pads) connects to the nearest
    pad of any other cluster — the pour does the rest. -> [(pad a, pad b, est, tree_target)]"""
    cids = [c for c in sorted(cl) if cl[c]["pads"]]
    if len(cids) < 2:
        return []
    main = max(cids, key=lambda c: (_weight(cl[c]), -c))
    anchors = [(p.x, p.y, c, p) for c in cids for p in cl[c]["pads"]]
    out = []
    for c in cids:
        if c == main:
            continue
        best = None
        for p in sorted(cl[c]["pads"], key=_pad_key):
            for x, y, c2, p2 in anchors:
                if c2 != c:
                    k = (round(_octile(p.x - x, p.y - y), 6), _pad_key(p2))
                    if best is None or k < best[0]:
                        best = (k, p, p2)
        if best:
            out.append((best[1], best[2], best[0][0], True))
    return out


def _tree_edges(cl: dict, mode: str, forced: list) -> list:
    """Decompose one unpoured net into connections. ``forced`` = [(pad a, pad b)] connections that come first
    (decoupling capacitor -> its IC pin). -> [(pad a, pad b, est, tree_target, forced?)]"""
    cids = [c for c in sorted(cl) if cl[c]["pads"]]
    if len(cids) < 2:
        return []
    parent = {c: c for c in cids}

    def find(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    edges = []
    extra = {}                                   # root cluster -> dogleg segments of its forced connections
    for a, b in forced:
        if a.cluster not in parent or b.cluster not in parent:
            continue
        ra, rb = find(a.cluster), find(b.cluster)
        if ra == rb:
            continue
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        parent[hi] = lo
        extra.setdefault(lo, []).extend(extra.pop(hi, []) + _dogleg(a.x, a.y, b.x, b.y))
        edges.append((a, b, _octile(a.x - b.x, a.y - b.y), False, True))
    groups = {}
    for c in cids:
        g = groups.setdefault(find(c), {"pads": [], "segs": [], "w": (0.0, 0, 0.0)})
        g["pads"] += cl[c]["pads"]
        g["segs"] += _kept_segs(cl[c])
        w = _weight(cl[c])
        g["w"] = (g["w"][0] + w[0], g["w"][1] + w[1], g["w"][2] + w[2])
    for gid, segs in extra.items():
        groups[find(gid)]["segs"] += segs
    if len(groups) < 2:
        return edges
    for g in groups.values():
        g["pads"].sort(key=_pad_key)
    root = max(groups, key=lambda gid: (groups[gid]["w"], -gid))
    steiner = mode == "steiner"
    tree = _Tree(groups, steiner)
    tree.drop(root)
    tree.grow(groups[root]["pads"], groups[root]["segs"])
    while tree.out:
        o = tree.nearest()
        if o is None:
            break
        px, py, gid, pad, d, tx, ty, d_pad, tpad = o
        est = d if steiner else d_pad
        edges.append((pad, tpad, est, mode in ("steiner", "mst"), False))
        tree.drop(gid)
        g = groups[gid]
        if mode == "star":
            continue
        if mode == "chain":
            tree.reset()
            tree.grow(g["pads"], ())
            continue
        tree.grow(g["pads"], g["segs"] + _dogleg(px, py, tx, ty) if steiner else ())
    return edges


TUNED_PRIORITY = 70.0     # below clock (85) and high-speed (80), above ordinary signals (50)


GROUP_BIAS = "long+prio"  # how members of a tuned group are ordered: 'long' (one block, longest member first),
#                           'prio' (priority TUNED_PRIORITY), 'long+prio', 'none'. Measured on the constraint bench
#                           boards (docs/internal/v2-interface-notes.md, "Electrical constraints, step B")


def _group_bias() -> str:
    import os
    return os.environ.get("COPPER_V2_GROUP_BIAS") or GROUP_BIAS


def _tuner_built() -> bool:
    """Is the length tuner (v2/tune.py, design §14.7) there? Routing the members of a tuned group early only pays
    when something adds the missing length afterwards; until then the order stays exactly what it was (measured
    without a tuner on bench board cons_bus_match: one member 10 mm longer and 2 vias more, the spread unchanged)."""
    try:
        from . import tune
    except Exception:  # noqa: BLE001 — not built yet
        return False
    return not getattr(tune, "STUB", False)


def _connections(board, cfg, x, table: dict) -> tuple[list[ConnV2], dict]:
    conns = []
    skipped = []
    decaps = []
    r = {n: t["role"] for n, t in table.items()}
    pad_by_name = {}
    for p in board.pads:
        pad_by_name.setdefault(p.name, p)
    forced_by_net = {}
    for n, cap, ic in decoupling(board, r):
        forced_by_net.setdefault(n, []).append((pad_by_name[cap], pad_by_name[ic]))
    for name in sorted(board.clusters):
        cl = board.clusters[name]
        if len(cl) < 2 or name not in table:
            continue
        t = table[name]
        nc = cfg.nets.get(name) if cfg is not None else None
        if t["skip"]:
            skipped.append({"net": name, "connections": len(cl) - 1,
                            "reason": (nc.skip_reason if nc is not None else None) or "plan: skip"})
            continue
        if board.nets[name].has_pour:
            edges = [e + (False,) for e in _plane_edges(cl)]
        else:
            mode = _net_hint(x, name, "tree")
            mode = mode if mode in TREES else "steiner"
            edges = _tree_edges(cl, mode, forced_by_net.get(name, ()))
        open_b = board.nets[name].has_pour
        for a, b, est, tree_target, forced in edges:
            # a poured net: the target is "anything else of this net" (pieces.is_open: no pad, no cluster, no layers),
            # placed at the nearest pad of another cluster
            tb = Terminal(x=q(b.x), y=q(b.y), layers=()) if open_b else _terminal(b)
            c = ConnV2(idx=len(conns), net=name, a=_terminal(a), b=tb, est=round(est, 6), role=t["role"],
                       width=t["width"], neck=t["neck"], clearance=t["clearance"], via=tuple(t["via"]),
                       tree_target=tree_target)
            conns.append(c)
            if forced:
                decaps.append(c.idx)
    # global order key: (plan rank, -priority, role order, est, net name, idx); the N net of a pair follows its P net
    by_net = {}
    for c in conns:
        by_net.setdefault(c.net, []).append(c)
    # members of a length group that will be tuned go early (direct paths need less added length, design §14.6);
    # empty on every board without a plan- or rule-backed group, and while the tuner is not built
    cons = x.get("cons") if isinstance(x, dict) else getattr(x, "cons", None)
    tuned = {n for g in cons.tuned() for n in g.nets} if (
        cons is not None and hasattr(cons, "tuned") and _tuner_built()) else set()
    bias = _group_bias()
    # keep-short nets the plan did not already make 'critical' (a KiCad `length (max ..)` rule, plan max_length, a
    # switch node, an antenna feed): routed with the critical ones, before anything can stand in their way
    short = set(getattr(cons, "short", None) or ()) if getattr(cons, "mode", None) in ("auto", "plan") else set()
    lead = {}                               # net -> (est of its group's shortest member, group name)
    if tuned and "long" in bias:
        for g in cons.tuned():
            ests = [max(c.est for c in by_net[n]) for n in g.nets if n in by_net]
            for n in g.nets:
                if ests and n in by_net:
                    lead[n] = (round(min(ests), 3), g.name)
    for c in conns:
        nc = cfg.nets.get(c.net) if cfg is not None else None
        rank, prio, name, est = (nc.rank, nc.priority, c.net, c.est) if nc is not None else (0, 50.0, c.net, c.est)
        if nc is not None and nc.dp_partner and nc.dp_role == "n" and nc.dp_partner in cfg.nets:
            pc = cfg.nets[nc.dp_partner]
            rank, prio, name = pc.rank, pc.priority, nc.dp_partner + "\x7f"
            mine, theirs = by_net[c.net], by_net.get(nc.dp_partner, [])
            k = mine.index(c)
            if k < len(theirs):
                est = theirs[k].est
        if c.net in tuned and "prio" in bias:
            prio = max(prio, TUNED_PRIORITY)
        est_key = round(est, 3)
        if c.net in lead and not (nc is not None and nc.dp_partner):
            # a tuned group is routed as one block, where its shortest member would have come, LONGEST member
            # first: the longest one sets the group's target, so a detour on it is paid by every other member
            est_key, gname = lead[c.net]
            name = f"\x00{gname}\x00{1e6 - max(k.est for k in by_net[c.net]):013.3f}\x00{c.net}"
        ro = ROLE_ORDER.get(c.role, ROLE_ORDER["signal"])
        if c.net in short and c.role in ("signal", "bus"):
            ro = ROLE_ORDER["critical"]
        c.rank = (rank, -prio, ro, est_key, name, c.idx)
    return conns, {"skipped": skipped, "decaps": decaps}


def connections(board, cfg, x, roles: dict) -> list[ConnV2]:
    """Every connection still to be made, ``conns[i].idx == i``; route in ``sorted(conns, key=lambda c: c.rank)``.

    Per unpoured net, incremental Steiner order: start from the largest cluster, repeatedly attach the cluster
    nearest to the tree built so far (its pads, kept copper and the doglegs of the connections already planned);
    ``a`` is the attaching pad, ``b`` the nearest pad of the tree, ``est`` the octile distance to the nearest tree
    point, ``tree_target=True``. Plan ``tree``: 'mst' (pad-to-pad Prim), 'star' (all to the largest cluster) and
    'chain' (each to the previous one) — the last two with ``tree_target=False``. Decoupling pairs come first
    (capacitor pad -> IC pin, ``tree_target=False``). Poured nets: one connection per cluster outside the main
    one; ``b`` is an *open* terminal (no pad, no cluster, no layers: "anything else of this net") located at the
    nearest pad of another cluster (role 'plane' unless the plan says otherwise, ranked last). ``roles`` overrides the role of the
    nets it names."""
    table = net_table(board, cfg, x)
    if roles:
        changed = {n: r for n, r in roles.items() if n in table and table[n]["role"] != r and r in ROLES}
        if changed:
            for n, r in changed.items():
                table[n]["role"] = r
            w = widths(board, cfg, x, {n: t["role"] for n, t in table.items()})
            for n in table:
                table[n]["width"], table[n]["neck"] = w[n]
    return _connections(board, cfg, x, table)[0]


def auto_report(table: dict, bus_list: list, decaps: list) -> dict:
    """The ``auto{}`` block: everything S0 decided on its own, for veto by the plan."""
    return {
        "roles": {n: t["role"] for n, t in table.items() if t["why"] != "plan" and t["role"] != "signal"},
        "buses": [dict(d) for d in bus_list if d.get("auto")],
        "widths": {n: t["width"] for n, t in table.items() if t["role"] == "power" and t["width"] > t["neck"] + 1e-9},
        "analog": sorted(n for n, t in table.items() if t["analog"]),
        "unskipped": sorted(n for n, t in table.items() if t["unskipped"]),
        "decoupling": [list(d) for d in decaps],
    }


def run(ctx: Ctx, budget: Budget) -> StageReport:
    """Fills ctx.conns, ctx.x.auto and ctx.history['classify'] = {nets, buses, decaps, skipped}."""
    board, cfg, x = ctx.board, ctx.cfg, ctx.x
    table = net_table(board, cfg, x)
    bl = buses(board, x)
    conns, info = _connections(board, cfg, x, table)
    ctx.conns[:] = conns                  # in place: a RouteState built earlier holds this very list
    r = {n: t["role"] for n, t in table.items()}
    pairs = decoupling(board, r)
    auto = auto_report(table, bl, pairs)
    ax = getattr(x, "auto", None) if x is not None else None
    if isinstance(ax, dict):
        ax.update(auto)
    elif x is not None and not isinstance(x, dict):
        try:
            x.auto = dict(auto)
        except AttributeError:
            pass
    ctx.history["classify"] = {"nets": table, "buses": bl, "decaps": list(info["decaps"]),
                               "skipped": info["skipped"], "auto": auto}
    if ctx.oracle is not None:
        for n, t in table.items():
            if t["clearance"] > board.nets[n].clearance + 1e-9:
                ctx.oracle.set_clearance(n, t["clearance"])
    count = {}
    for c in conns:
        count[c.role] = count.get(c.role, 0) + 1
    notes = [f"{len(conns)} connections on {len({c.net for c in conns})} nets: "
             + ", ".join(f"{count[k]} {k}" for k in ROLES if k in count)]
    if auto["buses"]:
        notes.append(f"{len(auto['buses'])} bus(es) auto-detected "
                     f"({', '.join(str(len(d['nets'])) + ' nets ' + '-'.join(d['refs']) for d in auto['buses'][:6])})")
    if auto["unskipped"]:
        notes.append(f"ground without a pour routed as power (the plan names it): {', '.join(auto['unskipped'][:8])}")
    if info["skipped"]:
        notes.append(f"{len(info['skipped'])} net(s) skipped by the plan")
    if info["decaps"]:
        notes.append(f"{len(info['decaps'])} decoupling connection(s) queued for the power stage")
    budget.charge(len(conns))
    rep = StageReport(name="classify", routed=0, failed=0, work=len(conns), notes=notes,
                      data={"roles": r, "auto": auto, "skipped": info["skipped"], "decaps": list(info["decaps"]),
                            "connections": len(conns), "by_role": count})
    return rep
