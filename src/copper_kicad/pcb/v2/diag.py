"""Diagnostics a model can act on (design §6.2, §6.3): stage table, congestion, cuts, failure
certificates, critique, ready plan patches, and overlay primitives + a Pillow painter.

Everything here is read-only on the routing state and deterministic (no clock, no randomness,
sorted iteration). Text is primary; pictures are the same facts drawn.

    stages(ctx)                      -> [{name, routed, failed, score, work, notes}]
    failure(ctx, conn, fail)         -> the failed[] entry (v1 keys + cut, static_share, mincut_nets, probe, tried)
    congestion(ctx)                  -> {tile_mm, cell_mm, origin, cols, rows, layers: {name: [[util %]]}, hot, ...}
    cuts(ctx, top=10)                -> [{a, b, layer, capacity_mm, demand_mm, nets, overflow_mm, at}]
    critique(ctx, reference=None)    -> [{kind, net, value, limit, where}]
    suggest(ctx, failed, crit)       -> [{why, patch}]      patch = plan fragment (planx.merge_patch) or {'move': ...}
    overlays(ctx, kind, conn=None)   -> line / rect / text primitives in board mm ('congestion' | 'sketch' | 'failure')
    overlay_set(ctx, failed)         -> {'congestion', 'sketch', 'failures'}: what the tool turns into pictures
    draw(img, to_px, prims)          -> paints primitives on a Pillow image
    render(board, prims, path, ...)  -> a PNG of the board with the primitives (crops to a 'view' primitive)
    build(ctx, fails=None, ...)      -> every §6.2 key in one dict (+ '_overlays'), for the engine

Imports from v2: types (tiles only through ctx.tiles, by its §9 interface). Where §9 gives no accessor
(tile origin, booked demand, the global paths) this module measures instead: the origin is read from
``tiles.x0 / y0`` when present, else probed through ``Tiles.tile_of``; demand is counted from the copper in
``ctx.state`` plus the planned tile paths of connections that have no copper yet (``ctx.history['global']``
as globalroute.py publishes it, or ``StageReport('global').data['paths']``).

Two rules keep the numbers honest once copper exists: copper that is there proves room for itself (a border's
capacity is never taken as less than the routed copper crossing it — the tile model does not see a track
entering its own pad), and a *cut* over capacity is only reported where a connection that is still unrouted
wants through (or where a stage / a certificate named one).
"""
from __future__ import annotations

import math
from typing import Callable

from .types import Blocker, ConnV2, Ctx, Cut  # noqa: F401

MAX_GRID = 40                 # congestion is reported on at most 40 x 40 cells per layer
DETOUR_LIMIT = 2.0            # routed length / estimate above this is a 'detour'
DETOUR_MIN_MM = 3.0           # ... for connections longer than this (short ones are noise)
NECKED_LIMIT = 0.25           # share of a net's length below its target width
ODD_ANGLE_LIMIT = 0.05        # share of segments that are neither axial nor 45°
MAX_SUGGEST = 12
MAX_PER_KIND = 8              # critique rows per kind (worst first; the last kept row says how many more)
MAX_FAILURE_IMAGES = 6
_EPS = 1e-9

KINDS = ("vias_over_budget", "necked_share", "detour", "bus_unbundled", "power_thinner_than_pad", "odd_angle_share")
OVERLAY_KINDS = ("congestion", "sketch", "failure")


# ── small accessors that tolerate a half-built context ────────────────────
def _pieces(ctx) -> list:
    st = ctx.state
    if st is None:
        return []
    try:
        return sorted(st.pieces(), key=lambda p: p.id)
    except Exception:  # noqa: BLE001
        return []


def _status(ctx, idx) -> str:
    try:
        return ctx.state.status(idx)
    except Exception:  # noqa: BLE001
        return "todo"


def _clearance(ctx, net) -> float:
    try:
        return float(ctx.oracle.clearance(net))
    except Exception:  # noqa: BLE001
        n = ctx.board.nets.get(net)
        return n.clearance if n else ctx.board.default_clr


def _wc(ctx, conn) -> tuple:
    """(track width, clearance) a connection needs at its narrowest — also when a stage left the ConnV2 fields 0."""
    nc = ctx.cfg.nets.get(conn.net) if ctx.cfg is not None and hasattr(ctx.cfg, "nets") else None
    bn = ctx.board.nets.get(conn.net)
    w = conn.neck or conn.width or 0.0
    if w <= 0 and nc is not None:
        w = min([nc.width] + list(nc.neck_widths or []))
    if w <= 0:
        w = bn.width if bn else 0.2
    cl = conn.clearance or (nc.clearance if nc is not None else 0.0) or _clearance(ctx, conn.net)
    return w, cl


def _untried(reason: str) -> bool:
    return "no search stage ran" in reason or "before this connection was tried" in reason


def _layer_name(ctx, layer) -> str:
    if isinstance(layer, str):
        return layer
    if isinstance(layer, int) and 0 <= layer < ctx.board.L:
        return ctx.board.layers[layer]
    return "all"


def _report(ctx, name):
    for r in reversed(ctx.reports or []):
        if r.name == name:
            return r
    return None


def _pad(ctx, name):
    cache = ctx.history.setdefault("_diag_pads", None)
    if cache is None:
        cache = {}
        for p in ctx.board.pads:
            cache.setdefault(p.name, p)
        ctx.history["_diag_pads"] = cache
    return cache.get(name)


def _r(v, n=3):
    return round(float(v), n)


def stages(ctx: Ctx) -> list[dict]:
    """One row per stage that ran, in order."""
    out = []
    for r in ctx.reports or []:
        s = r.score
        out.append({"name": r.name, "routed": int(r.routed), "failed": int(r.failed),
                    "score": {"new_errors": s.new_errors, "opens": s.opens, "necked_mm": _r(s.necked_mm, 2),
                              "vias": s.vias, "length_mm": _r(s.length_mm, 1), "bends": s.bends},
                    "work": int(r.work), "notes": [str(n) for n in (r.notes or [])][:6]})
    return out


def auto(ctx: Ctx) -> dict:
    """Everything the router decided on its own (for veto through the plan)."""
    a = getattr(ctx.x, "auto", None) or {}
    return {"roles": dict(sorted((a.get("roles") or {}).items())), "buses": list(a.get("buses") or []),
            "widths": dict(sorted((a.get("widths") or {}).items())),
            "layer_dirs": {_layer_name(ctx, k): v for k, v in sorted((a.get("layer_dirs") or {}).items(),
                                                                     key=lambda kv: str(kv[0]))}}


# ── the tile view: real Tiles when the global stage ran, an estimate otherwise ──
class _View:
    __slots__ = ("t", "ox", "oy", "nx", "ny", "nl", "real", "_tiles", "_est")

    def cap(self, layer, tx, ty, side) -> float:
        if self.real:
            try:
                return float(self._tiles.cap(layer, tx, ty, side))
            except Exception:  # noqa: BLE001
                return self._est
        return self._est

    def edge_mid(self, tx, ty, side):
        """Midpoint of the border between (tx, ty) and its E / N neighbour (N = ty + 1)."""
        if side == "E":
            return (self.ox + (tx + 1) * self.t, self.oy + (ty + 0.5) * self.t)
        return (self.ox + (tx + 0.5) * self.t, self.oy + (ty + 1) * self.t)

    def centre(self, tx, ty):
        return (self.ox + (tx + 0.5) * self.t, self.oy + (ty + 0.5) * self.t)

    def use(self, layer, tx, ty, side):
        """The global stage's own negotiated demand on a border, mm (tiles.useE / useN as tiles.py keeps them
        after a global route) — None when the tiles publish none."""
        if not self.real:
            return None
        arr = getattr(self._tiles, "useN" if side == "N" else "useE", None)
        if arr is None:
            return None
        try:
            return float(arr[(layer * self.ny + ty) * self.nx + tx])
        except Exception:  # noqa: BLE001
            return None


def _probe_origin(tiles, t, x0, y0):
    """Where tile (0, 0) starts, found through tile_of alone (the §9 interface has no origin)."""
    def edge(axis):
        base = tiles.tile_of(x0, y0)[axis]
        lo, hi = 0.0, t * 1.000001
        p = (x0 + hi, y0) if axis == 0 else (x0, y0 + hi)
        if tiles.tile_of(*p)[axis] == base:
            return None
        for _ in range(48):
            mid = (lo + hi) / 2
            p = (x0 + mid, y0) if axis == 0 else (x0, y0 + mid)
            if tiles.tile_of(*p)[axis] == base:
                lo = mid
            else:
                hi = mid
        return round((x0 if axis == 0 else y0) + hi - (base + 1) * t, 6)      # tiles start on a whole nanometre
    return edge(0), edge(1)


def _view(ctx) -> _View:
    key = ("_diag_view", id(ctx.tiles))
    v = ctx.history.get(key)
    if v is not None:
        return v
    b = ctx.board
    v = _View()
    tl = ctx.tiles
    df = b.rules.get("default") or {}
    w = max(df.get("width") or 0.2, b.rules["track_width"])
    cl = b.default_clr
    v._tiles = tl
    v.real = False
    if tl is not None:
        try:
            v.t, v.nx, v.ny, v.nl = float(tl.t), int(tl.nx), int(tl.ny), int(tl.nl)
            o = None
            for names in (("x0", "y0"), ("ox", "oy")):
                if all(isinstance(getattr(tl, n, None), (int, float)) for n in names):
                    o = (float(getattr(tl, names[0])), float(getattr(tl, names[1])))
                    break
            if o is None and isinstance(getattr(tl, "origin", None), (tuple, list)):
                o = (float(tl.origin[0]), float(tl.origin[1]))
            if o is None:
                px, py = _probe_origin(tl, v.t, b.bbox[0], b.bbox[1])
                o = (b.bbox[0] if px is None else px, b.bbox[1] if py is None else py)
            v.ox, v.oy = o
            v.real = v.t > 0 and v.nx > 0 and v.ny > 0
        except Exception:  # noqa: BLE001
            v.real = False
    if not v.real:
        t = getattr(ctx.x, "tile_mm", None) or 1.0
        bw, bh = b.bbox[2] - b.bbox[0], b.bbox[3] - b.bbox[1]
        while (math.ceil(bw / t) + 1) * (math.ceil(bh / t) + 1) * b.L > 60_000:
            t *= 2
        v.t, v.ox, v.oy = t, b.bbox[0], b.bbox[1]
        v.nx, v.ny, v.nl = max(1, math.ceil(bw / t - 1e-9)), max(1, math.ceil(bh / t - 1e-9)), b.L
    p = w + cl
    v._est = math.floor((v.t + cl) / p + 1e-9) * p          # capacity of an empty border (design §4.6)
    ctx.history[key] = v
    return v


def _crossings(v: _View, x1, y1, x2, y2):
    """Tile borders a segment crosses: [(tx, ty, side, position along the border in mm)]."""
    out = []
    t = v.t
    for side, a1, b1, a2, b2, o_a, o_b, n_a, n_b in (("E", x1, y1, x2, y2, v.ox, v.oy, v.nx, v.ny),
                                                     ("N", y1, x1, y2, x2, v.oy, v.ox, v.ny, v.nx)):
        ua, ub = (a1 - o_a) / t, (a2 - o_a) / t
        lo, hi = (ua, ub) if ua <= ub else (ub, ua)
        k = math.floor(lo) + 1
        while k <= hi and k < n_a:
            if k >= 1:
                f = (k - ua) / (ub - ua) if ub != ua else 0.0
                pos = b1 + f * (b2 - b1)
                j = math.floor((pos - o_b) / t)
                if 0 <= j < n_b:
                    out.append((k - 1, j, "E", pos) if side == "E" else (j, k - 1, "N", pos))
            k += 1
    return out


def _paths(ctx) -> dict:
    """{conn idx: [(tx, ty) | (tx, ty, layer), ...]} planned by the global stage (empty when it did not run
    or does not publish them)."""
    r = _report(ctx, "global")
    p = (r.data or {}).get("paths") if r is not None else None
    if isinstance(p, dict):
        return p
    g = ctx.history.get("global")
    if isinstance(g, dict) and isinstance(g.get("path"), dict):       # globalroute.py: {'path': .., 'layer': ..}
        lay = g.get("layer") if isinstance(g.get("layer"), dict) else {}
        out = {}
        for idx, path in g["path"].items():
            ls = lay.get(idx)
            if ls and len(ls) == len(path):
                out[idx] = [(t[0], t[1], l) for t, l in zip(path, ls)]
            else:
                out[idx] = [(t[0], t[1]) for t in path]
        return out
    return {}


def _demand(ctx) -> dict:
    """{(layer, tx, ty, side): [routed mm, planned mm, {net: mm}]} — copper in the state, and the planned
    paths of connections that have no copper yet (not on borders in the shadow of their own pads)."""
    ver = getattr(ctx.oracle, "version", None)
    key = (id(ctx.state), ver, len(ctx.reports or []), id(ctx.tiles))
    c = ctx.history.get("_diag_demand")
    if c is not None and c[0] == key:
        return c[1]
    v = _view(ctx)
    d = {}

    raw = {}                         # key -> ({net: routed mm}, {net: planned mm})

    def add(layer, tx, ty, side, net, mm, planned=0):
        e = raw.get((layer, tx, ty, side))
        if e is None:
            e = raw[(layer, tx, ty, side)] = ({}, {})
        if planned:
            # connections of one net share copper (they tee): one track per net and border, the widest
            if mm > e[1].get(net, 0.0):
                e[1][net] = mm
        else:
            e[0][net] = e[0].get(net, 0.0) + mm
    shadow_of = getattr(ctx.tiles, "pad_edges", None) if v.real else None

    def own(c_):
        s = set()
        if shadow_of is not None:
            for t_ in (c_.a, c_.b):
                try:
                    s |= set(shadow_of(t_.pad))
                except Exception:  # noqa: BLE001
                    pass
        return s
    clr = {}
    in_shadow = getattr(ctx.tiles, "own_shadow", None) if v.real else None
    booked = v.real and bool(getattr(ctx.tiles, "_reservations_booked", False))
    for p in _pieces(ctx):
        if p.kind != "seg":
            continue
        if booked and (p.protected or p.stage in ("power", "fanout")):
            continue                 # globalroute booked these reservations: their room is already off the capacity
        if p.net not in clr:
            n_ = ctx.board.nets.get(p.net)
            clr[p.net] = (_clearance(ctx, p.net), n_.id if n_ else 0)
        mm = p.w + clr[p.net][0]
        for tx, ty, side, pos in _crossings(v, p.x1, p.y1, p.x2, p.y2):
            if in_shadow is not None:
                # a track crossing a border under copper of its own net (leaving its pad) takes no free room
                try:
                    if in_shadow((((p.layer * v.ny + ty) * v.nx + tx) * 2) + (side == "N"), pos, clr[p.net][1], p.w / 2):
                        continue
                except Exception:  # noqa: BLE001
                    pass
            add(p.layer, tx, ty, side, p.net, mm)
    paths = _paths(ctx)
    for c_ in sorted(ctx.conns or [], key=lambda c: c.idx):
        path = paths.get(c_.idx)
        if not path or _status(ctx, c_.idx) == "routed":
            continue                                   # (an escape stub alone does not make a connection routed)
        cor = (ctx.corridors or {}).get(c_.idx)
        dl = (cor.layers[0] if cor is not None and cor.layers else (c_.a.layers[0] if c_.a.layers else 0))
        mm = (c_.width or _wc(ctx, c_)[0]) + (c_.clearance or _wc(ctx, c_)[1])
        sh = own(c_)
        for a, b in zip(path, path[1:]):
            la = a[2] if len(a) > 2 else dl
            if (a[0], a[1]) == (b[0], b[1]):
                continue
            if a[1] == b[1] and abs(a[0] - b[0]) == 1:
                tx, ty, side = min(a[0], b[0]), a[1], "E"
            elif a[0] == b[0] and abs(a[1] - b[1]) == 1:
                tx, ty, side = a[0], min(a[1], b[1]), "N"
            else:
                continue
            if sh and (((la * v.ny + ty) * v.nx + tx) * 2 + (side == "N")) in sh:
                continue                               # leaving its own pad: no demand there (tiles.pad_edges)
            add(la, tx, ty, side, c_.net, mm, 1)
    for k in raw:
        rt, pl = raw[k]
        nets = dict(rt)
        planned = 0.0
        for n in sorted(pl):
            if n not in rt:          # a net with copper on this border already: the open connection joins it
                planned += pl[n]
                nets[n] = pl[n]
        d[k] = [sum(rt[n] for n in sorted(rt)), planned, nets]
    ctx.history["_diag_demand"] = (key, d)
    return d


def _util(dem, cap) -> int:
    if cap <= _EPS:
        return 999 if dem > _EPS else -1
    return min(999, int(round(100.0 * dem / cap)))


def _load(v, key, e):
    """(demand mm, capacity mm) of one border: capacity is at least the routed copper that already crosses it.
    On a border without copper the global stage's own negotiated demand is used when the tiles publish it —
    it knows which crossings are free (a net inside the shadow of its own pads), a tile path does not."""
    routed, planned = e[0], e[1]
    if planned > 0 and routed <= _EPS:
        u = v.use(*key)
        if u is not None:
            planned = u
    return routed + planned, max(v.cap(*key), routed)


def congestion(ctx: Ctx) -> dict:
    """Utilisation of the tile borders, coarse (<= 40 x 40 cells per layer), in percent of capacity.

    A cell holds the worst border of its tiles; -1 = no capacity and nothing wants through, 999 = an unrouted
    connection planned through a border with no room. Demand = routed copper + planned paths of connections
    without copper; capacity is never below the routed copper already there (it fits, KiCad permitting).
    Layers nobody uses are listed in ``idle_layers`` instead of as all-zero grids.
    ``capacity`` says where the capacities come from: 'tiles' (exact geometry, global stage) or
    'estimate' (empty-border formula, no tile graph yet — utilisation is then a lower bound near parts)."""
    v = _view(ctx)
    d = _demand(ctx)
    k = max(1, math.ceil(max(v.nx, v.ny) / MAX_GRID))
    cols, rows = math.ceil(v.nx / k), math.ceil(v.ny / k)
    by_layer = {}
    for key, e in d.items():
        mm, cap = _load(v, key, e)
        by_layer.setdefault(key[0], []).append((key[1], key[2], key[3], mm, cap))
    out_layers, idle, hot = {}, [], []
    for layer in range(v.nl):
        items = by_layer.get(layer)
        name = _layer_name(ctx, layer)
        if not items:
            idle.append(name)
            continue
        grid = [[0] * cols for _ in range(rows)]
        for tx, ty, side, mm, cap in sorted(items):
            u = _util(mm, cap)
            cx, cy = min(cols - 1, tx // k), min(rows - 1, ty // k)
            if u > grid[cy][cx]:
                grid[cy][cx] = u
            if u >= 80:
                mx, my = v.edge_mid(tx, ty, side)
                hot.append((-u, name, round(mx, 2), round(my, 2)))
        if v.real:                               # blocked cells are only known with real capacities
            for cy in range(rows):
                for cx in range(cols):
                    if grid[cy][cx] == 0:
                        tx, ty = min(v.nx - 1, cx * k), min(v.ny - 1, cy * k)
                        if v.cap(layer, tx, ty, "E") <= _EPS and v.cap(layer, tx, ty, "N") <= _EPS:
                            grid[cy][cx] = -1
        out_layers[name] = grid
    hot.sort()
    return {"tile_mm": _r(v.t), "cell_mm": _r(v.t * k), "origin": [_r(v.ox), _r(v.oy)], "cols": cols, "rows": rows,
            "capacity": "tiles" if v.real else "estimate", "layers": out_layers, "idle_layers": idle,
            "max": {n: max(max(row) for row in g) for n, g in out_layers.items()},
            "hot": [{"layer": n, "at": [x, y], "util": -u} for u, n, x, y in hot[:5]],
            "legend": "util % of track capacity per cell (row = y, column = x from origin); -1 blocked; 999 demand, no room"}


def _cut_dict(ctx, c, at=None) -> dict:
    if isinstance(c, dict):
        d = dict(c)
    else:
        d = {"a": c.a, "b": c.b, "layer": c.layer, "capacity_mm": c.capacity_mm, "demand_mm": c.demand_mm,
             "nets": list(c.nets or [])}
    out = {"a": str(d.get("a")), "b": str(d.get("b")), "layer": _layer_name(ctx, d.get("layer")),
           "capacity_mm": _r(d.get("capacity_mm") or 0.0), "demand_mm": _r(d.get("demand_mm") or 0.0),
           "nets": sorted({str(n) for n in (d.get("nets") or [])})}
    out["overflow_mm"] = _r(out["demand_mm"] - out["capacity_mm"])
    at = at or d.get("at")
    if at is None:
        pa, pb = _pad(ctx, out["a"]), _pad(ctx, out["b"])
        if pa is not None and pb is not None:
            at = ((pa.x + pb.x) / 2, (pa.y + pb.y) / 2)
        elif pa is not None:
            at = (pa.x, pa.y)
    if at is not None:
        out["at"] = [_r(at[0], 2), _r(at[1], 2)]
    return out


def _edge_cut(ctx, v, layer, tx, ty, side, mm, nets, cap=None) -> dict:
    cap = v.cap(layer, tx, ty, side) if cap is None else cap
    c = None
    if v.real:
        try:
            c = ctx.tiles.cut_for_edge(layer, tx, ty, side, sorted(nets.items()))
        except Exception:  # noqa: BLE001
            c = None
    if c is None:
        nb = (tx + 1, ty) if side == "E" else (tx, ty + 1)
        c = Cut(a=f"tile({tx},{ty})|{side}", b=f"tile({nb[0]},{nb[1]})", layer=_layer_name(ctx, layer),
                capacity_mm=cap, demand_mm=mm, nets=sorted(nets))
    d = _cut_dict(ctx, c, at=v.edge_mid(tx, ty, side))
    if d["capacity_mm"] < cap - 1e-6:                  # routed copper proved more room than the tile model has
        d["capacity_mm"] = _r(cap)
        d["overflow_mm"] = _r(d["demand_mm"] - d["capacity_mm"])
    return d


def cuts(ctx: Ctx, top: int = 10) -> list[dict]:
    """The `top` most overflowed cuts: what the global stage reported, the cuts named by failure
    certificates, and every tile border an unrouted connection is planned through without room for it.
    Sorted by overflow."""
    found = {}

    def take(d):
        k = (d["a"], d["b"], d["layer"])
        if k not in found or d["overflow_mm"] > found[k]["overflow_mm"]:
            found[k] = d
    r = _report(ctx, "global")
    routed_all = bool(ctx.conns) and all(_status(ctx, c.idx) == "routed" for c in ctx.conns)
    for c in ((r.data or {}).get("cuts") or []) if (r is not None and not routed_all) else []:
        try:                                             # the plan's residual overflow — moot once all is routed
            take(_cut_dict(ctx, c))
        except Exception:  # noqa: BLE001
            continue
    for idx in sorted(ctx.history.get("diag_failed") or {}):
        c = ctx.history["diag_failed"][idx].get("cut")
        if c:
            take(dict(c))
    v = _view(ctx)
    if v.real:
        dem = _demand(ctx)
        for key in sorted(dem):
            e = dem[key]
            mm, cap = _load(v, key, e)
            if e[1] > 0 and mm > cap + 1e-6:             # an unrouted connection wants through and does not fit
                take(_edge_cut(ctx, v, *key, mm, e[2], cap))
    out = [d for d in found.values() if d["overflow_mm"] > 0]
    out.sort(key=lambda d: (-d["overflow_mm"], d["a"], d["b"], d["layer"]))
    return out[:max(0, int(top))]


# ── failure certificates ──────────────────────────────────────────────────
def _blocker_names(ctx, items, own) -> list[str]:
    """Rank whatever a stage recorded as blockers (net names, Blockers, piece ids, (x, count) pairs) as names."""
    score, first = {}, {}

    def name_of(it):
        if isinstance(it, Blocker):
            if it.net:
                return it.net
            return it.tag if isinstance(it.tag, str) else None
        if isinstance(it, bool):
            return None
        if isinstance(it, int):
            try:
                p = ctx.state.piece(it)
            except Exception:  # noqa: BLE001
                p = None
            return p.net if p is not None else None
        if isinstance(it, str):
            return it
        return None
    for i, it in enumerate(items or []):
        n_, w = it, 1.0
        if isinstance(it, (tuple, list)) and len(it) == 2 and isinstance(it[1], (int, float)) and not isinstance(it[1], bool):
            n_, w = it[0], float(it[1])
        n_ = name_of(n_)
        if not n_ or n_ == own:
            continue
        score[n_] = score.get(n_, 0.0) + w
        first.setdefault(n_, i)
    return sorted(score, key=lambda n: (-score[n], first[n], n))


def _static_share(ctx, conn, fail, where) -> float:
    s = fail.get("static_share")
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return min(1.0, max(0.0, float(s)))
    bl = [b for b in (fail.get("blocked_by") or []) if isinstance(b, Blocker)]
    if _untried(str(fail.get("reason") or "")):
        return 0.0                                     # nobody searched: nothing blocked it yet
    if not bl and ctx.oracle is not None and where is not None:
        # nothing recorded: a small exact probe — what stops a track of this width leaving the failure point
        # in each of the 8 directions (fixed copper / holes / edge vs. copper this run laid)
        lay = conn.a.layers[0] if conn.a.layers else 0
        w, cl_ = _wc(ctx, conn)
        reach = 3.0 * (w + cl_)
        uniq = {}
        for dx, dy in ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)):
            k = reach / math.hypot(dx, dy)
            try:
                hits = ctx.oracle.blockers(lay, where[0], where[1], where[0] + dx * k, where[1] + dy * k, w, conn.net, limit=4)
            except Exception:  # noqa: BLE001
                hits = []
            for b in hits:
                uniq[("s", str(b.tag), b.net, b.rule) if b.static else ("p", b.piece)] = b
        bl = [uniq[k] for k in sorted(uniq, key=repr)]
    if not bl:
        return 0.0
    return round(sum(1 for b in bl if b.static) / len(bl), 2)


def _probe(conn, fail, where) -> dict:
    p = fail.get("probe")
    enclosed = None
    out = {}
    if isinstance(p, str):
        enclosed = p
    elif isinstance(p, dict):
        e = p.get("enclosed")
        if isinstance(e, str):
            enclosed = e
        elif isinstance(p.get("a"), dict) or isinstance(p.get("b"), dict):
            ea = bool((p.get("a") or {}).get("enclosed"))
            eb = bool((p.get("b") or {}).get("enclosed"))
            enclosed = "a" if ea else ("b" if eb else "none")
            for end in ("a", "b"):
                if isinstance(p.get(end), dict) and "reached" in p[end]:
                    out[f"reached_{end}"] = int(p[end]["reached"])
        elif isinstance(e, bool):
            enclosed = (p.get("end") or "a") if e else "none"
        if "reached" in p:
            out["reached"] = int(p["reached"])
    if enclosed not in ("a", "b", "none"):
        enclosed = "none"
        if "boxed in" in (fail.get("reason") or "") and where is not None:
            da = (where[0] - conn.a.x) ** 2 + (where[1] - conn.a.y) ** 2
            db = (where[0] - conn.b.x) ** 2 + (where[1] - conn.b.y) ** 2
            enclosed = "a" if da <= db else "b"
    return {"enclosed": enclosed, **out}


def _pad_cut(ctx, conn, term) -> dict | None:
    """The tightest gap between the terminal's pad and a neighbouring pad of the same part (design §4.6 formula)."""
    pad = _pad(ctx, term.pad) if term.pad else None
    if pad is None:
        return None
    layer = term.layers[0] if term.layers else 0
    w, cl = _wc(ctx, conn)
    near = sorted((q for q in ctx.board.pads if q.ref == pad.ref and q is not pad and layer in q.layers),
                  key=lambda q: ((q.x - pad.x) ** 2 + (q.y - pad.y) ** 2, q.name))[:2]
    best = None
    v = _view(ctx)
    for q in near:
        c = None
        if v.real:
            try:
                c = ctx.tiles.pad_cut(pad.name, q.name, layer, w, cl)
            except Exception:  # noqa: BLE001
                c = None
        if c is None:
            try:
                gap = float(ctx.oracle.pad_gap(pad.name, q.name, layer))
            except Exception:  # noqa: BLE001
                continue
            if gap == float("inf"):
                continue
            tracks = max(0, math.floor((gap - cl) / (w + cl) + 1e-9))
            c = Cut(a=pad.name, b=q.name, layer=_layer_name(ctx, layer), capacity_mm=tracks * (w + cl),
                    demand_mm=w + cl, nets=[conn.net])
        d = _cut_dict(ctx, c, at=((pad.x + q.x) / 2, (pad.y + q.y) / 2))
        if conn.net not in d["nets"]:
            d["nets"] = sorted(d["nets"] + [conn.net])
        if best is None or (d["capacity_mm"], d["a"], d["b"]) < (best["capacity_mm"], best["a"], best["b"]):
            best = d
    return best


def _corridor_cut(ctx, conn) -> dict | None:
    cor = (ctx.corridors or {}).get(conn.idx)
    v = _view(ctx)
    if cor is None or not v.real:
        return None
    best = None
    dem = _demand(ctx)
    for key in sorted(dem):
        e = dem[key]
        if (key[1], key[2]) not in cor.tiles or conn.net not in e[2]:
            continue
        mm, cap = _load(v, key, e)
        over = mm - cap
        if over > 1e-6 and (best is None or over > best[0]):
            best = (over, key, mm, e[2], cap)
    if best is None:
        return None
    return _edge_cut(ctx, v, *best[1], best[2], best[3], best[4])


def failure(ctx: Ctx, conn: ConnV2, fail: dict) -> dict:
    """The failed[] entry of one connection: v1's keys (net, from, to, reason, where, blocked_by) plus the
    certificate (cut, static_share, mincut_nets, probe, tried). Also remembered in ctx.history['diag_failed']
    so cuts(), suggest() and the failure overlay can use it."""
    fail = dict(fail or {})
    a, b = conn.a, conn.b
    reason = str(fail.get("reason") or "not routed")
    w = fail.get("where")
    where = (float(w[0]), float(w[1])) if isinstance(w, (list, tuple)) and len(w) >= 2 else (a.x, a.y)
    e = {"net": conn.net, "conn": conn.idx,
         "from": a.pad or f"({_r(a.x, 2)}, {_r(a.y, 2)})",
         "to": b.pad or ("pour" if conn.role == "plane" else f"({_r(b.x, 2)}, {_r(b.y, 2)})"),
         "_a": (a.x, a.y), "_b": (b.x, b.y),
         "reason": reason, "where": [_r(where[0], 2), _r(where[1], 2)],
         "blocked_by": _blocker_names(ctx, fail.get("blocked_by"), conn.net)[:6]}
    e["static_share"] = _static_share(ctx, conn, fail, where)
    probe = _probe(conn, fail, where)
    e["probe"] = probe
    crossed = fail.get("crossed") or {}
    mc = fail.get("mincut_nets")
    if mc is None and isinstance(crossed, dict):
        mc = []
        for ci in sorted(crossed, key=lambda k: (-float(crossed[k] or 0), k)):
            try:
                mc.append(ctx.state.conn(ci).net)
            except Exception:  # noqa: BLE001
                continue
    seen = []
    for n in mc or []:
        if isinstance(n, str) and n != conn.net and n not in seen:
            seen.append(n)
    e["mincut_nets"] = seen[:8]
    e["tried"] = [str(t) for t in (fail.get("tried") or [])]
    cut = fail.get("cut")
    if cut is not None:
        try:
            cut = _cut_dict(ctx, cut)
        except Exception:  # noqa: BLE001
            cut = None
    if cut is None and probe["enclosed"] in ("a", "b"):
        cut = _pad_cut(ctx, conn, a if probe["enclosed"] == "a" else b)
    if cut is None:
        cut = _corridor_cut(ctx, conn)
    if cut is None and "boxed in" in reason:
        cut = _pad_cut(ctx, conn, a) or _pad_cut(ctx, conn, b)
    e["cut"] = cut
    fr = fail.get("frontier")
    if fr:
        e["_frontier"] = fr
    ctx.history.setdefault("diag_failed", {})[conn.idx] = e
    return e


# ── critique ──────────────────────────────────────────────────────────────
def _odd(p) -> bool:
    dx, dy = abs(p.x2 - p.x1), abs(p.y2 - p.y1)
    tol = 0.002 + 0.005 * max(dx, dy)
    return min(dx, dy, abs(dx - dy)) > tol


def _ref_nets(reference) -> dict:
    if not isinstance(reference, dict):
        return {}
    r = reference.get("nets") if isinstance(reference.get("nets"), dict) else reference
    out = {}
    for n, v in r.items():
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            out[n] = (float(v[0]), int(v[1]))
        elif isinstance(v, dict):
            out[n] = (float(v.get("length_mm") or 0.0), int(v.get("vias") or 0))
    return out


def critique(ctx: Ctx, reference: dict | None = None) -> list[dict]:
    """What is routed but not good: [{kind, net, value, limit, where}], worst first within a kind.

    kinds: vias_over_budget (per connection, against plan via_budget — or per net against ``reference``),
    necked_share, detour (> 2 x estimate), bus_unbundled, power_thinner_than_pad, odd_angle_share (board).
    reference: {net: [length_mm, vias]} (or {'nets': {...}}) of a routing to compare with, e.g. the human one.
    At most 8 rows per kind; the last kept row of a longer kind carries 'more': how many were left out."""
    return _cap_kinds(_critique(ctx, reference))


def _cap_kinds(rows: list[dict]) -> list[dict]:
    out, n = [], {}
    for r in rows:
        k = r["kind"]
        n[k] = n.get(k, 0) + 1
        if n[k] <= MAX_PER_KIND:
            out.append(r)
    last = {}
    for i, r in enumerate(out):
        last[r["kind"]] = i
    for k, i in last.items():
        if n[k] > MAX_PER_KIND:
            out[i] = {**out[i], "more": n[k] - MAX_PER_KIND}
    return out


def _critique(ctx: Ctx, reference: dict | None = None) -> list[dict]:
    pcs = _pieces(ctx)
    x = ctx.x
    conns = {c.idx: c for c in ctx.conns or []}
    by_conn, by_net = {}, {}
    for p in pcs:
        by_net.setdefault(p.net, []).append(p)
        if p.conn >= 0:
            by_conn.setdefault(p.conn, []).append(p)
    ref = _ref_nets(reference)
    out = []

    def mid(c):
        return [_r((c.a.x + c.b.x) / 2, 2), _r((c.a.y + c.b.y) / 2, 2)]

    # vias over budget
    rows = {}
    for idx in sorted(by_conn):
        c = conns.get(idx)
        if c is None:
            continue
        vb = x.net(c.net).via_budget if x is not None and hasattr(x, "net") else None
        if vb is None:
            continue
        nv = sum(1 for p in by_conn[idx] if p.kind == "via")
        if nv > vb and (c.net not in rows or nv > rows[c.net]["value"]):
            rows[c.net] = {"kind": "vias_over_budget", "net": c.net, "value": nv, "limit": vb, "where": mid(c)}
    for n in sorted(ref):
        if n in rows or n not in by_net:
            continue
        nv = sum(1 for p in by_net[n] if p.kind == "via")
        if nv > ref[n][1]:
            vs = [p for p in by_net[n] if p.kind == "via"]
            rows[n] = {"kind": "vias_over_budget", "net": n, "value": nv, "limit": ref[n][1],
                       "where": [_r(vs[0].x1, 2), _r(vs[0].y1, 2)]}
    out += sorted(rows.values(), key=lambda r: (r["limit"] - r["value"], r["net"]))

    # necked share
    rows = []
    for n in sorted(by_net):
        segs = [p for p in by_net[n] if p.kind == "seg" and p.length > 0]
        total = sum(p.length for p in segs)
        necked = [p for p in segs if p.w < p.target_w - 1e-6]
        nl = sum(p.length for p in necked)
        if total > 0 and nl / total > NECKED_LIMIT:
            worst = max(necked, key=lambda p: (p.length, -p.id))
            rows.append({"kind": "necked_share", "net": n, "value": _r(nl / total, 2), "limit": NECKED_LIMIT,
                         "where": [_r((worst.x1 + worst.x2) / 2, 2), _r((worst.y1 + worst.y2) / 2, 2)]})
    out += sorted(rows, key=lambda r: (-r["value"], r["net"]))

    # detours
    rows = []
    for idx in sorted(by_conn):
        c = conns.get(idx)
        if c is None or _status(ctx, idx) != "routed":
            continue
        length = sum(p.length for p in by_conn[idx] if p.kind == "seg")
        est = c.est or (max(abs(c.a.x - c.b.x), abs(c.a.y - c.b.y)) + 0.41421356 * min(abs(c.a.x - c.b.x), abs(c.a.y - c.b.y)))
        lim = DETOUR_LIMIT
        if c.net in ref and ref[c.net][0] > 0:
            continue                                   # judged per net below
        if est >= DETOUR_MIN_MM and length > lim * est:
            rows.append({"kind": "detour", "net": c.net, "value": _r(length / est, 2), "limit": lim, "where": mid(c),
                         "from": c.a.pad, "to": c.b.pad})
    for n in sorted(ref):
        if n in by_net and ref[n][0] >= DETOUR_MIN_MM:
            length = sum(p.length for p in by_net[n] if p.kind == "seg")
            if length > 1.5 * ref[n][0]:
                s = max((p for p in by_net[n] if p.kind == "seg"), key=lambda p: (p.length, -p.id))
                rows.append({"kind": "detour", "net": n, "value": _r(length / ref[n][0], 2), "limit": 1.5,
                             "where": [_r((s.x1 + s.x2) / 2, 2), _r((s.y1 + s.y2) / 2, 2)]})
    out += sorted(rows, key=lambda r: (-r["value"], r["net"], str(r.get("from"))))

    # buses that did not stay together
    buses = []
    def members_of(b):
        if not isinstance(b, dict):
            return [n for n in b if isinstance(n, str)] if isinstance(b, (list, tuple)) else []
        o = b.get("order")
        return list(o if isinstance(o, (list, tuple)) and o else b.get("nets") or [])
    for b in (getattr(x, "buses", None) or []):
        buses.append(members_of(b))
    for b in ((getattr(x, "auto", None) or {}).get("buses") or []):
        m = members_of(b)
        if m and not any(set(m) & set(k) for k in buses):
            buses.append(m)
    for members in buses:
        stats = {}
        for n in members:
            segs = [p for p in by_net.get(n, []) if p.kind == "seg"]
            if not segs:
                continue
            per = {}
            for p in segs:
                per[p.layer] = per.get(p.layer, 0.0) + p.length
            stats[n] = (max(sorted(per), key=lambda l: per[l]), sum(per.values()),
                        sum(1 for p in by_net[n] if p.kind == "via"))
        if len(stats) < 2:
            continue
        lays = [s[0] for s in stats.values()]
        major = max(sorted(set(lays)), key=lays.count)
        lens = sorted(s[1] for s in stats.values())
        med = lens[len(lens) // 2]
        off = [n for n in members if n in stats and (stats[n][0] != major or stats[n][1] > 1.5 * med + 2.0)]
        if off:
            s = max((p for p in by_net[off[0]] if p.kind == "seg"), key=lambda p: (p.length, -p.id))
            out.append({"kind": "bus_unbundled", "net": off[0], "value": len(off), "limit": 0,
                        "where": [_r((s.x1 + s.x2) / 2, 2), _r((s.y1 + s.y2) / 2, 2)], "nets": off,
                        "bus": members, "layer": _layer_name(ctx, major)})

    # power tracks thinner than their pads allow
    rows = []
    role = {}
    for c in conns.values():
        role.setdefault(c.net, c.role)
    from .planx import pad_minor
    pwf = getattr(x, "power_width_factor", 2.0) or 2.0
    for n in sorted(by_net):
        if role.get(n) not in ("power", "plane") or n not in ctx.board.nets:
            continue
        segs = [p for p in by_net[n] if p.kind == "seg" and p.length > 0]
        if not segs:
            continue
        net = ctx.board.nets[n]
        minor = [m for m in (pad_minor(p) for p in net.pads) if m > 0]
        lim = min(net.width * pwf, 0.8 * min(minor) if minor else 1.0, 1.0)
        lim = max(lim, net.width)
        thin = min(segs, key=lambda p: (p.w, p.id))
        if thin.w < lim - 1e-6:
            rows.append({"kind": "power_thinner_than_pad", "net": n, "value": _r(thin.w), "limit": _r(lim),
                         "where": [_r((thin.x1 + thin.x2) / 2, 2), _r((thin.y1 + thin.y2) / 2, 2)]})
    out += sorted(rows, key=lambda r: (r["value"] - r["limit"], r["net"]))

    # odd angles (board-wide)
    segs = [p for p in pcs if p.kind == "seg" and p.length > 1e-6]
    odd = [p for p in segs if _odd(p)]
    if segs and len(odd) / len(segs) > ODD_ANGLE_LIMIT:
        s = max(odd, key=lambda p: (p.length, -p.id))
        out.append({"kind": "odd_angle_share", "net": None, "value": _r(len(odd) / len(segs), 3), "limit": ODD_ANGLE_LIMIT,
                    "where": [_r((s.x1 + s.x2) / 2, 2), _r((s.y1 + s.y2) / 2, 2)]})
    return out


# ── suggestions: ready plan patches ───────────────────────────────────────
def _rect_around(ctx, at, half):
    b = ctx.board.bbox
    return [_r(max(b[0], at[0] - half), 2), _r(max(b[1], at[1] - half), 2),
            _r(min(b[2], at[0] + half), 2), _r(min(b[3], at[1] + half), 2)]


def _parts(ctx) -> dict:
    c = ctx.history.get("_diag_parts")
    if c is None:
        c = {}
        for p in ctx.board.pads:
            c[p.ref] = c.get(p.ref, 0) + 1
        ctx.history["_diag_parts"] = c
    return c


def _cut_patch(ctx, cut, net):
    """A patch that relieves a cut over capacity, with its explanation — or None."""
    pa, pb = _pad(ctx, cut["a"]), _pad(ctx, cut["b"])
    short = max(0.05, math.ceil((cut["demand_mm"] - cut["capacity_mm"] + 0.05) * 20 - 1e-9) / 20)
    gap = f"through {cut['a']}" if cut["a"] == cut["b"] else f"between {cut['a']} and {cut['b']}"
    line = (f"{cut['demand_mm']} mm of track + clearance must pass {gap} on {cut['layer']}, "
            f"{cut['capacity_mm']} mm fits")
    if pa is not None and pb is not None and pa.ref != pb.ref:
        parts = _parts(ctx)
        mv, other = (pa, pb) if (parts.get(pa.ref, 0), pa.ref) <= (parts.get(pb.ref, 0), pb.ref) else (pb, pa)
        dx, dy = mv.x - other.x, mv.y - other.y
        if abs(dx) >= abs(dy):
            by = [_r(math.copysign(short, dx) if dx else short, 2), 0.0]
        else:
            by = [0.0, _r(math.copysign(short, dy), 2)]
        return {"why": f"{line}: move {mv.ref} {short} mm away from {other.ref} (pcb_place(moves=[...])), then route again",
                "patch": {"move": {"ref": mv.ref, "by": by}}}
    if pa is not None and pb is not None:
        parts = _parts(ctx)
        if parts.get(pa.ref, 0) >= 8:
            return {"why": f"{line}: {pa.ref}'s pads leave no channel there — escape {net} through a via instead",
                    "patch": {"fanout": {pa.ref: {"style": "via"}}}}
        return None
    at = cut.get("at")
    if at is None:
        return None
    scale = max(0.3, min(0.9, round(cut["capacity_mm"] / cut["demand_mm"], 2))) if cut["demand_mm"] > 0 else 0.5
    t = _view(ctx).t
    return {"why": f"{line} (nets: {', '.join(cut['nets'][:5])}): plan fewer tracks through it so the global stage "
                   f"sends the rest around before detail work is spent",
            "patch": {"regions": [{"rect": _rect_around(ctx, at, t), "capacity_scale": scale,
                                   "layers": [cut["layer"]] if cut["layer"] in ctx.board.layers else None,
                                   "name": f"cut {cut['a']}"}]}}


def suggest(ctx: Ctx, failed: list[dict], crit: list[dict]) -> list[dict]:
    """[{why, patch}] — each patch is a ready plan fragment (merge it with planx.merge_patch, or by hand) or
    {'move': {'ref', 'by': [dx, dy]}} for pcb_place. Derived only from the certificates and the critique;
    most specific first, at most 12.

    Side effect (so the pictures of §6.3 need nothing from the caller): being the last diagnosis of a run, it
    leaves ``overlay_set(ctx, failed)`` on ``ctx.x.overlays``, where tools/route_tools.py finds it (cfg.x)."""
    cfg, x = ctx.cfg, ctx.x
    out, seen = [], set()

    def add(why, patch):
        for k in [k for k, v in patch.items() if isinstance(v, list)]:
            patch[k] = [{kk: vv for kk, vv in e.items() if vv is not None} if isinstance(e, dict) else e for e in patch[k]]
        key = repr(sorted(patch.items(), key=lambda kv: kv[0]))
        if key not in seen:
            seen.add(key)
            out.append({"why": why, "patch": patch})
    failed = [e for e in (failed or []) if isinstance(e, dict)]
    late = [e for e in failed if "time budget" in e.get("reason", "") or "effort" in e.get("reason", "")]
    if late and 2 * len(late) >= len(failed):
        tb = float(getattr(cfg, "time_budget", 90.0) or 90.0)
        if tb < 600:
            add(f"{len(late)} of {len(failed)} failed connections ran out of search budget",
                {"global": {"time_budget_s": int(min(600, max(tb * 2, tb + 60)))}})
        else:
            eff = float(getattr(cfg, "effort", 1.0) or 1.0)
            if eff < 5:
                add(f"{len(late)} failed connections ran out of search budget at the maximum time budget",
                    {"global": {"effort": min(5.0, round(eff * 2, 1))}})
    for e in failed:
        net, reason = e.get("net"), e.get("reason", "")
        if "outside the board outline" in reason or _untried(reason):
            continue                                   # a placement problem / nobody searched: no advice per connection
        cut = e.get("cut")
        if cut and cut.get("demand_mm", 0) > cut.get("capacity_mm", 0):
            s = _cut_patch(ctx, cut, net)
            if s:
                add(f"{net} {e.get('from')}→{e.get('to')}: " + s["why"], s["patch"])
        nc = cfg.nets.get(net) if cfg is not None else None
        if "max_" in reason and nc is not None:
            if nc.max_vias is not None:
                add(f"{net}: no route within max_vias={nc.max_vias}", {"nets": {net: {"max_vias": nc.max_vias + 1}}})
            elif nc.max_length is not None:
                add(f"{net}: no route within max_length={nc.max_length} mm",
                    {"nets": {net: {"max_length": _r(nc.max_length * 1.25, 1)}}})
        mc = [n for n in (e.get("mincut_nets") or []) if cfg is not None and n in cfg.nets]
        mov = mc or [n for n in (e.get("blocked_by") or []) if cfg is not None and n in cfg.nets]
        if mov and (mc or e.get("static_share", 0.0) < 0.5) and e not in late:
            order = [net] + [n for n in (cfg.used.get("order") or []) if n not in (net, "*")] + ["*"]
            add(f"{net} is blocked by routed copper of {', '.join(mov[:3])}: route it before them", {"order": order})
            w = e.get("where")
            if w:
                add(f"… or keep {mov[0]} out of the spot where {net} got stuck ({w[0]}, {w[1]})",
                    {"nets": {mov[0]: {"avoid": [_rect_around(ctx, w, 1.0)]}}})
        elif e.get("static_share", 0.0) >= 0.5 and nc is not None and e not in late:
            bn = ctx.board.nets.get(net)
            floor = max(ctx.board.rules["track_width"], (bn.width if bn else 0.0))
            if nc.width > floor + 1e-6 and not nc.neck_widths:
                add(f"{net} is boxed in by fixed copper at width {nc.width} mm: let it neck down where it does not fit",
                    {"nets": {net: {"neck": True}}})
            elif len(nc.layers_ok) < ctx.board.L and e.get("probe", {}).get("enclosed") == "none":
                add(f"{net} is blocked by fixed copper on its allowed layers: allow every copper layer",
                    {"nets": {net: {"layers": list(ctx.board.layers)}}})
    for c in crit or []:
        k, net = c.get("kind"), c.get("net")
        if k == "vias_over_budget":
            add(f"{net} used {c['value']} vias on one connection, budget {c['limit']}: keep it on one side "
                f"(or raise via_budget if the layer change is wanted)",
                {"nets": {net: {"max_vias": int(c["limit"])}}})
        elif k == "necked_share" and c.get("where"):
            add(f"{int(round(100 * c['value']))} % of {net} is below its width: plan fewer foreign tracks where it necks",
                {"regions": [{"rect": _rect_around(ctx, c["where"], 2.0), "capacity_scale": 0.6, "except": [net],
                              "name": f"room for {net}"}]})
        elif k == "detour":
            add(f"{net} is {c['value']}x its direct length: route it earlier", {"nets": {net: {"priority": 90}}})
        elif k == "bus_unbundled":
            b = {"nets": list(c.get("bus") or c.get("nets") or [])}
            if c.get("layer") in ctx.board.layers:
                b["layer"] = c["layer"]
            if len(b["nets"]) >= 2 and x is not None and not any(x.bus_of(n) for n in b["nets"]):
                add(f"{', '.join(c.get('nets') or [])} left the bundle: declare the bus so its members share a corridor",
                    {"buses": [b]})
            elif c.get("layer") in ctx.board.layers:
                for n in (c.get("nets") or [])[:3]:
                    if cfg is not None and n in cfg.nets and ctx.board.layers.index(c["layer"]) in cfg.nets[n].layers_ok:
                        add(f"{n} left its declared bus, which runs on {c['layer']}: prefer that layer for it",
                            {"nets": {n: {"prefer": [c["layer"]]}}})
        elif k == "power_thinner_than_pad":
            add(f"{net} runs at {c['value']} mm where its pads allow {c['limit']} mm", {"nets": {net: {"width": c["limit"]}}})
        elif k == "odd_angle_share" and x is not None and not getattr(x, "polish", True):
            add(f"{int(round(100 * c['value']))} % of the segments are at odd angles and polish is off",
                {"global": {"polish": True}})
    done = {repr(s["patch"]) for s in out}
    for cut in cuts(ctx, top=3):
        s = _cut_patch(ctx, cut, (cut["nets"] or ["?"])[0])
        if s and repr(s["patch"]) not in done:
            add(s["why"], s["patch"])
    if x is not None and hasattr(x, "overlays"):
        try:
            x.overlays = overlay_set(ctx, failed)
        except Exception:  # noqa: BLE001 — a picture must never cost the diagnosis
            x.overlays = None
    return out[:MAX_SUGGEST]


# ── overlays (primitives in board mm) ─────────────────────────────────────
def _heat(u):
    t = min(1.0, max(0.0, u / 150.0))
    return [int(60 + 195 * t), int(170 - 120 * t), int(60 - 30 * t), int(50 + 140 * t)]


def _clip(ctx, x, y):
    b = ctx.board.bbox
    return [min(b[2], max(b[0], float(x))), min(b[3], max(b[1], float(y)))]


def _clip_rect(ctx, x0, y0, x1, y1):
    a, b = _clip(ctx, x0, y0), _clip(ctx, x1, y1)
    return [a[0], a[1], b[0], b[1]]


_LAYER_RGB = ((235, 70, 60), (70, 120, 240), (60, 190, 90), (240, 160, 40), (170, 90, 220), (60, 200, 210))


def _lrgb(ctx, layer):
    L = ctx.board.L
    i = 0 if layer == 0 else (1 if layer == L - 1 else 2 + (layer - 1) % 4)
    return list(_LAYER_RGB[i])


def _cut_prims(ctx, cut, label=True):
    at = cut.get("at")
    if not at:
        return []
    pa, pb = _pad(ctx, cut["a"]), _pad(ctx, cut["b"])
    if pa is not None and pb is not None:
        pts = [_clip(ctx, pa.x, pa.y), _clip(ctx, pb.x, pb.y)]
    else:
        h = _view(ctx).t / 2
        vertical = cut["a"].endswith("|E")
        pts = [_clip(ctx, at[0], at[1] - h), _clip(ctx, at[0], at[1] + h)] if vertical else \
              [_clip(ctx, at[0] - h, at[1]), _clip(ctx, at[0] + h, at[1])]
    out = [{"type": "line", "pts": pts, "color": [255, 40, 40, 255], "width": 4}]
    if label:
        out.append({"type": "text", "at": _clip(ctx, at[0] + 0.3, at[1] + 0.3), "color": [255, 220, 220],
                    "text": f"{cut['a']} | {cut['b']}: needs {cut['demand_mm']}, fits {cut['capacity_mm']} mm"})
    return out


def overlays(ctx: Ctx, kind: str, conn: int | None = None) -> list[dict]:
    """Primitives in board mm, all inside the board's bounding box:

        {'type': 'rect', 'rect': [x0, y0, x1, y1], 'fill': [r, g, b, a] | None, 'outline': [r, g, b] | None}
        {'type': 'line', 'pts': [[x, y], ...], 'color': [r, g, b, a], 'width': px}  (or 'width_mm')
        {'type': 'text', 'at': [x, y], 'text': str, 'color': [r, g, b]}
        {'type': 'view', 'rect': [...]}     not drawn: the area a renderer should crop to

    kind 'congestion': heat of the worst layer per cell + the top cuts labelled.
    kind 'sketch': one polyline per connection through its planned tile centres (straight and dashed-looking
        thin when no global path exists) + overflowed borders in red.
    kind 'failure' (conn = its index; failure() must have been called for it): both terminals, the frontier
        reached from each end, the named cut and the blocking nets labelled."""
    if kind not in OVERLAY_KINDS:
        raise ValueError(f"overlay kind must be one of {OVERLAY_KINDS}, got {kind!r}")
    v = _view(ctx)
    prims = []
    if kind == "congestion":
        cg = congestion(ctx)
        cell = cg["cell_mm"]
        worst = {}
        for name in sorted(cg["layers"]):
            for j, row in enumerate(cg["layers"][name]):
                for i, u in enumerate(row):
                    if u > worst.get((i, j), 0):
                        worst[(i, j)] = u
        for (i, j) in sorted(worst):
            u = worst[(i, j)]
            if u >= 25:
                prims.append({"type": "rect", "rect": _clip_rect(ctx, v.ox + i * cell, v.oy + j * cell,
                                                                 v.ox + (i + 1) * cell, v.oy + (j + 1) * cell),
                              "fill": _heat(u), "outline": None})
        for c in cuts(ctx, top=5):
            prims += _cut_prims(ctx, c)
        return prims
    if kind == "sketch":
        paths = _paths(ctx)
        for c in sorted(ctx.conns or [], key=lambda c: c.idx):
            path = paths.get(c.idx)
            cor = (ctx.corridors or {}).get(c.idx)
            dl = cor.layers[0] if cor is not None and cor.layers else (c.a.layers[0] if c.a.layers else 0)
            if path:
                pts = [_clip(ctx, c.a.x, c.a.y)] + [_clip(ctx, *v.centre(t[0], t[1])) for t in path] + [_clip(ctx, c.b.x, c.b.y)]
                lay = path[0][2] if len(path[0]) > 2 else dl
                prims.append({"type": "line", "pts": pts, "color": _lrgb(ctx, lay) + [200], "width": 2, "net": c.net})
            else:
                prims.append({"type": "line", "pts": [_clip(ctx, c.a.x, c.a.y), _clip(ctx, c.b.x, c.b.y)],
                              "color": [230, 230, 230, 110], "width": 1, "net": c.net})
        if v.real:
            dem = _demand(ctx)
            for key in sorted(dem):
                (_layer, tx, ty, side), e = key, dem[key]
                mm, cap = _load(v, key, e)
                if e[1] > 0 and mm > cap + 1e-6:
                    mx, my = v.edge_mid(tx, ty, side)
                    h = v.t / 2
                    pts = [_clip(ctx, mx, my - h), _clip(ctx, mx, my + h)] if side == "E" else \
                          [_clip(ctx, mx - h, my), _clip(ctx, mx + h, my)]
                    prims.append({"type": "line", "pts": pts, "color": [255, 40, 40, 255], "width": 4})
        for c in cuts(ctx, top=5):
            prims += [p for p in _cut_prims(ctx, c) if p["type"] == "text"]
        return prims
    # failure
    e = (ctx.history.get("diag_failed") or {}).get(conn)
    if e is None:
        raise ValueError(f"no failure certificate for connection {conn!r}: call diag.failure(ctx, conn, fail) first")
    (ax, ay), (bx, by) = e["_a"], e["_b"]
    wx, wy = e["where"]
    m = 3.0
    x0, y0, x1, y1 = min(ax, bx, wx) - m, min(ay, by, wy) - m, max(ax, bx, wx) + m, max(ay, by, wy) + m
    if e.get("cut") and e["cut"].get("at"):
        cx, cy = e["cut"]["at"]
        x0, y0, x1, y1 = min(x0, cx - m), min(y0, cy - m), max(x1, cx + m), max(y1, cy + m)
    view = _clip_rect(ctx, x0, y0, x1, y1)
    prims.append({"type": "view", "rect": view})
    prims.append({"type": "line", "pts": [_clip(ctx, ax, ay), _clip(ctx, bx, by)], "color": [255, 235, 60, 200], "width": 2})
    fr = e.get("_frontier")
    if isinstance(fr, dict):
        ends = [("a", fr.get("a") or [], [90, 220, 255, 150]), ("b", fr.get("b") or [], [255, 150, 230, 150])]
    else:
        ends = [("a", fr or [], [90, 220, 255, 150])]
    for _end, pts, col in ends:
        for p in list(pts)[:400]:
            if isinstance(p, (list, tuple)) and len(p) >= 2 and view[0] <= p[0] <= view[2] and view[1] <= p[1] <= view[3]:
                prims.append({"type": "rect", "rect": _clip_rect(ctx, p[0] - 0.06, p[1] - 0.06, p[0] + 0.06, p[1] + 0.06),
                              "fill": col, "outline": None})
    blockers = set(e.get("blocked_by") or []) | set(e.get("mincut_nets") or [])
    labelled = set()
    for p in _pieces(ctx):
        if p.net not in blockers or p.kind != "seg":
            continue
        mx, my = (p.x1 + p.x2) / 2, (p.y1 + p.y2) / 2
        if not (view[0] <= mx <= view[2] and view[1] <= my <= view[3]):
            continue
        prims.append({"type": "line", "pts": [_clip(ctx, p.x1, p.y1), _clip(ctx, p.x2, p.y2)],
                      "color": [255, 140, 40, 230], "width_mm": p.w})
        if p.net not in labelled and p.length >= 0.5:
            labelled.add(p.net)
            prims.append({"type": "text", "at": _clip(ctx, mx, my), "text": p.net, "color": [255, 190, 120]})
    if e.get("cut"):
        prims += _cut_prims(ctx, e["cut"])
    for (px, py), name, enc in (((ax, ay), e["from"], "a"), ((bx, by), e["to"], "b")):
        boxed = e["probe"]["enclosed"] == enc
        prims.append({"type": "rect", "rect": _clip_rect(ctx, px - 0.35, py - 0.35, px + 0.35, py + 0.35), "fill": None,
                      "outline": [255, 60, 60] if boxed else [255, 235, 60]})
        prims.append({"type": "text", "at": _clip(ctx, px + 0.45, py - 0.6), "color": [255, 235, 60],
                      "text": f"{name}" + (" (boxed in)" if boxed else "")})
    s = 0.3
    prims.append({"type": "line", "pts": [_clip(ctx, wx - s, wy - s), _clip(ctx, wx + s, wy + s)], "color": [255, 235, 60, 255], "width": 3})
    prims.append({"type": "line", "pts": [_clip(ctx, wx - s, wy + s), _clip(ctx, wx + s, wy - s)], "color": [255, 235, 60, 255], "width": 3})
    rest = sorted(blockers - labelled)
    label = e["net"] + ": " + e["reason"][:60] + (f" · blocked by {', '.join(rest[:4])}" if rest else "")
    prims.append({"type": "text", "at": _clip(ctx, view[0] + 0.2, view[1] + 0.2), "text": label, "color": [255, 255, 255]})
    return prims


def overlay_set(ctx: Ctx, failed: list[dict] | None = None) -> dict:
    """{'congestion': prims, 'sketch': prims | None, 'failures': [{'net', 'conn', 'prims'}]} for the tool's
    renderer. 'sketch' only when no copper was routed (a stage-limited dry run); one crop per failure that has a
    certificate (failure() was called for it), at most 6."""
    ov = {"congestion": overlays(ctx, "congestion"), "sketch": None, "failures": []}
    if not any(p.kind == "seg" for p in _pieces(ctx)):
        ov["sketch"] = overlays(ctx, "sketch")
    known = ctx.history.get("diag_failed") or {}
    for e in failed or []:
        if len(ov["failures"]) >= MAX_FAILURE_IMAGES:
            break
        idx = e.get("conn") if isinstance(e, dict) else None
        if isinstance(e, dict) and _untried(str(e.get("reason") or "")):
            continue                                   # never tried: there is nothing to show yet
        if idx in known:
            ov["failures"].append({"net": e.get("net"), "conn": idx, "prims": overlays(ctx, "failure", idx)})
    return ov


def draw(img, to_px: Callable, prims: list[dict]) -> None:
    """Paint primitives on a Pillow image. ``to_px(x, y) -> (px, py)`` maps board mm to pixels."""
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(img, "RGBA")
    (ux, _uy), (vx, _vy) = to_px(0.0, 0.0), to_px(1.0, 0.0)
    scale = abs(vx - ux) or 1.0
    size = int(max(11, min(20, scale * 0.8)))
    try:
        font = ImageFont.load_default(size)
    except TypeError:       # Pillow < 10.1
        font = ImageFont.load_default()
    texts = []
    for p in prims:
        t = p.get("type")
        if t == "rect":
            x0, y0, x1, y1 = p["rect"]
            a, b = to_px(x0, y0), to_px(x1, y1)
            box = [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]
            fill = tuple(p["fill"]) if p.get("fill") else None
            outline = tuple(p["outline"]) if p.get("outline") else None
            d.rectangle(box, fill=fill, outline=outline, width=int(p.get("width", 2)) if outline else 0)
        elif t == "line":
            pts = [to_px(x, y) for x, y in p["pts"]]
            if len(pts) >= 2:
                w = int(round(p["width_mm"] * scale)) if p.get("width_mm") else int(p.get("width", 2))
                d.line(pts, fill=tuple(p.get("color") or (255, 255, 255, 255)), width=max(1, w))
        elif t == "text":
            texts.append(p)
    for p in texts:                                   # text last, on a dark plate so it stays readable
        x, y = to_px(*p["at"])
        s = str(p.get("text", ""))
        try:
            bb = d.textbbox((x, y), s, font=font)
        except Exception:  # noqa: BLE001
            bb = (x, y, x + 6 * len(s), y + size)
        dx = min(0, img.size[0] - 2 - bb[2])
        dy = min(0, img.size[1] - 2 - bb[3])
        d.rectangle([bb[0] + dx - 2, bb[1] + dy - 1, bb[2] + dx + 2, bb[3] + dy + 1], fill=(12, 12, 16, 190))
        d.text((x + dx, y + dy), s, fill=tuple(p.get("color") or (255, 255, 255)), font=font)


def render(board, prims: list[dict], path, title: str = "", width: int = 1400) -> str:
    """Board (outline, pads, the copper it holds) + primitives -> PNG at ``path``. Crops to the first 'view'
    primitive when there is one (a failure overlay)."""
    from PIL import Image as PImage, ImageDraw
    from ..guided import analysis as an
    view = next((p["rect"] for p in prims if p.get("type") == "view"), None)
    x0, y0, x1, y1 = view if view else board.bbox
    pad = 0.8 if view else 1.5
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    if view:
        width = min(width, 1100)
    scale = min(width / max(x1 - x0, 1e-6), 1500 / max(y1 - y0, 1e-6))
    im = PImage.new("RGB", (max(64, int((x1 - x0) * scale)), max(64, int((y1 - y0) * scale)) + 34), (18, 18, 22))
    d = ImageDraw.Draw(im, "RGBA")

    def P(x, y):
        return ((x - x0) * scale, (y - y0) * scale)
    an._base(board, d, P, scale, labels=True)
    L = board.L
    for lay in range(L - 1, -1, -1):
        col = _LAYER_RGB[0 if lay == 0 else (1 if lay == L - 1 else 2 + (lay - 1) % 4)]
        for _net, l, ax, ay, bx, by, w in board.kept_tracks:
            if l == lay:
                d.line([P(ax, ay), P(bx, by)], fill=col + (150,), width=max(1, int(round(w * scale))))
    for _net, x, y, size, _drill, _top, _bot in board.kept_vias:
        r = size / 2
        d.ellipse([P(x - r, y - r), P(x + r, y + r)], fill=(225, 225, 225, 170))
    draw(im, P, prims)
    an._legend(im, d, title or "router diagnostics")
    im.save(str(path))
    return str(path)


# ── everything at once (for the engine) ───────────────────────────────────
def build(ctx: Ctx, fails: dict | None = None, reference: dict | None = None, with_overlays: bool = True) -> dict:
    """Every §6.2 key in one call: {stages, auto, failed, congestion, cuts, critique, suggest, _overlays}.

    fails: {conn idx: fail dict}; default = every connection whose state is 'failed' (state.fail(idx)).
    '_overlays' = {'congestion': prims, 'sketch': prims | None, 'failures': [{'net', 'conn', 'prims'}]} is for
    the tool's renderer (tools/route_tools.py pops it); 'sketch' is only made when no copper was routed."""
    conns = {c.idx: c for c in ctx.conns or []}
    if fails is None:
        fails = {}
        for idx in sorted(conns):
            if _status(ctx, idx) == "failed":
                try:
                    fails[idx] = ctx.state.fail(idx) or {}
                except Exception:  # noqa: BLE001
                    fails[idx] = {}
    failed = [failure(ctx, conns[idx], fails[idx]) for idx in sorted(fails) if idx in conns]
    crit = critique(ctx, reference)
    out = {"stages": stages(ctx), "auto": auto(ctx), "failed": failed, "congestion": congestion(ctx),
           "cuts": cuts(ctx), "critique": crit, "suggest": suggest(ctx, failed, crit)}
    if with_overlays:
        out["_overlays"] = overlay_set(ctx, failed)
    return out
