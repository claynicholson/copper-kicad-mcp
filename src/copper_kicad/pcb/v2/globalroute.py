"""S3 global: negotiated (PathFinder) routing on the tile graph, layer-assignment DP, corridors, cuts
(design §4.6).

    run(ctx, budget, only=None)      the stage: fills ctx.corridors (and ctx.tiles when the engine has none)
    pathfinder(...)                  negotiation alone -> {conn idx: [(tx, ty), ...]}
    assign_layers(...)               Viterbi layer assignment of one 2-D tile path -> (layers, layer_seq)
    layer_directions(...)            automatic preferred direction per layer

Graph: node ``(layer, tx, ty)`` = flat index ``(layer × ny + ty) × nx + tx``; in-layer edges are the tile
borders of tiles.py, a via edge joins any two layers of a column. Costs (mm):

    edge = T × layer_cost × (1 + dir_bias if against the layer's direction)
             × (1 + hist + WALL if the border has no capacity) × (1 + pres × over / p) × plan multipliers
             ÷ 2 on a border a bus sibling already uses
    via  = via_cost × (1 + hist + WALL_VIA if the column has no via site) × (1 + pres × over)

``over`` is what the border would be short by with this connection on it (mm; vias: count). A connection's
demand on a border is ``width + clearance``; borders inside the shadow of its own terminal pads are free for
it (it is leaving its own pad).

Crossings. Border capacities alone cannot see that two nets cross, and on a 2-layer board that is most of the
problem (without it a bus crossing "converges" with zero vias planned). Two things are done about it:

* exact test, at the end of every iteration (``crossings``): two paths of different nets that share a run of
  tiles on one layer must intersect iff the second leaves the first on its left at one end of the run and on
  its right at the other (a single shared tile passed straight by both is the simplest case). A run that
  holds a terminal of either path proves nothing and is left alone. Every forced crossing is overflow of
  ``min(demand)`` mm, re-routes both connections, and adds history to the nodes of the run.
* cheap price inside the search: the straight-through traffic of every node per axis (``hT`` / ``vT``); a step
  along one axis pays for the other axis's traffic in the two tiles it joins
  (× (1 + pres × min(d, cross / 2) / p)), and a turn inside a tile with straight-through traffic costs one via
  (``TURN_AT_TRAFFIC``), which makes the two-tile jog that dodges the first term dearer than the layer change.

The price is a proxy; the test is exact for the tile paths (it knows nothing of where inside a tile a pad
sits). What it cannot resolve in 30 iterations is reported as ``|cross`` cuts.

What other stages can read afterwards:

    ctx.corridors[idx]                    Corridor (tiles = path + 1 ring, layers, layer_seq, lateral, overflow)
    ctx.history['global']                 {'path': {idx: [(tx, ty)]}, 'layer': {idx: [layer per path tile]},
                                           'tee': {idx: conn idx whose corridor this one ends on},
                                           'crossings': {(layer, tx, ty, side): n}, 'cuts': [Cut], ...}
    ctx.history['global_state']           the negotiation state (re-used by run(..., only=[...]))
    ctx.tiles.utilisation(layer)          congestion picture of the kept iteration

``Corridor.layer_seq`` entries are ``(i, from_layer, to_layer)``: a via in tile ``path[i]``.
"""
from __future__ import annotations

import heapq
import math

from . import tiles as tiles_mod
from .tiles import Tiles
from .types import Budget, ConnV2, Corridor, Ctx, Cut, Score, StageReport

INF = float("inf")
WALL = 8.0                 # extra cost factor of a border without capacity (soft: the model may be wrong)
WALL_VIA = 2.0             # same for a column without a legal via site
MAX_ITERS = 30
AUTO_DIR_BIAS = 0.3        # automatic layer directions: soft penalty 1.3
VIA_BUDGET_FACTOR = 3.0    # layer DP: a via above the net's via_budget costs this many times more
MAX_DP_VIAS = 6
SHARE = 0.5                # 1 / (1 + share) with share = 1
TURN_AT_TRAFFIC = 1.0      # × via cost: a turn in a tile that carries straight-through traffic (jog-crossing proxy)
TEE_ONLY_EARLIER = True    # a connection may end only on paths of connections that precede it in rank order
PARTIAL_ITERS = 20         # a partial re-route (run(..., only=[...])) negotiates at most this long ...
PARTIAL_STALL = 4          # ... and stops after this many iterations without improvement
BOX_MARGIN = 8             # tiles: a search stays inside the box of its terminals grown by this (+2 per iteration);
#                            a search that finds nothing in the box is repeated on the whole board
_push, _pop = heapq.heappush, heapq.heappop


# ── tolerant access to the plan objects (planx.V2Cfg may not have landed) ──
def _xget(x, name, default=None):
    if x is None:
        return default
    v = getattr(x, name, None)
    if v is None:
        g = getattr(x, "glob", None)
        if isinstance(g, dict):
            v = g.get(name)
    return default if v is None else v


_NET_HINTS = ("via_budget", "waypoints", "between", "side_of")


def _xnet(x, net) -> dict:
    """v2 per-net hints {via_budget, waypoints, between, side_of}: ``x.nets[net]`` as a dict or an object."""
    nets = getattr(x, "nets", None) if x is not None else None
    if not nets:
        return {}
    try:
        h = nets.get(net)
    except AttributeError:
        return {}
    if h is None:
        return {}
    if isinstance(h, dict):
        return h
    return {k: getattr(h, k) for k in _NET_HINTS if getattr(h, k, None) is not None}


def _field(o, name, default=None):
    if isinstance(o, dict):
        v = o.get(name)
    else:
        v = getattr(o, name, None)
    return default if v is None else v


def _netcfg(cfg, net):
    try:
        return cfg.net(net)
    except (KeyError, AttributeError, TypeError):
        return None


class _Hint:
    """Everything the search needs to know about one net (cached per net)."""
    __slots__ = ("lays", "lok", "stepE", "stepN", "via_cost", "max_vias", "via_budget", "mul", "hscale", "waypoints",
                 "bus", "minstep", "novia")


class GlobalState:
    """Negotiation state over one ``Tiles``: usage, history, the 3-D tile path of every connection."""

    def __init__(self, tiles: Tiles, conns: list, cfg, x, dirs: dict | None = None):
        self.tiles, self.cfg, self.x = tiles, cfg, x
        self.nx, self.ny, self.nl, self.G, self.N = tiles.nx, tiles.ny, tiles.nl, tiles.G, tiles.N
        self.T, self.p = tiles.t, tiles.pitch
        self.dirs = dict(dirs or {})                 # automatic layer directions {layer: 'h' | 'v'}
        self.conns = {}
        self.order = []
        self.dem, self.free, self.by_net = {}, {}, {}
        N, G = self.N, self.G
        self.useE = [0.0] * N
        self.useN = [0.0] * N
        self.useV = [0] * G
        self.histE = [0.0] * N
        self.histN = [0.0] * N
        self.histV = [0.0] * G
        self.hT = [0.0] * N                           # straight-through traffic per node, east-west (mm of demand)
        self.vT = [0.0] * N                           # ... north-south
        self.histX = [0.0] * N                        # history of crossing conflicts per node
        self.anyX = False
        self.xnodes = []                              # [(over mm, node)] of the last overflow() call
        self.xruns = []                               # [(over mm, (nodes of the run), idx, idx)] likewise
        self.capV = [-1] * G
        self.novia = bytearray(G)
        self.paths = {}                              # idx -> [node, ...] from terminal a to its end
        self.pset = {}
        self.end_on = {}                             # idx -> conn idx whose path it ends on | -1 (its own terminal b)
        self.unrouted = {}                           # idx -> reason
        self.expansions = 0
        self.searches = 0
        self.iterations = 0
        self.overflow_first = None
        self.pres = 0.0
        self.margin = BOX_MARGIN                      # search box around a connection, tiles (grows per iteration)
        self.hweight = float(getattr(cfg, "hweight", 1.0) or 1.0)     # v1's A* weight (1.15): fewer expansions
        self._hints = {}
        self._mulbase = {}
        self._dist = [0.0] * N
        self._stamp = [0] * N
        self._done = [0] * N
        self._prev = [0] * N
        self._gen = 0
        self._tv = -1
        self.buses = {}
        for bi, bus in enumerate(_xget(x, "buses", []) or []):
            nets = list(_field(bus, "nets", []) or [])
            order = list(_field(bus, "order", []) or nets)
            lay = _field(bus, "layer")
            for n in nets:
                self.buses[n] = (bi, order.index(n) if n in order else len(order), lay, tuple(nets))
        self._novia_nets = []                         # [(region, bytearray)] of no-via regions that name nets
        for r in _xget(x, "regions", []) or []:
            if _field(r, "no_vias") and _field(r, "rect"):
                x0, y0, x1, y1 = _field(r, "rect")
                general = not _field(r, "nets") and not _field(r, "exc")
                mask = self.novia if general else bytearray(G)
                for ty in range(self.ny):
                    for tx in range(self.nx):
                        cx, cy = tiles.center(tx, ty)
                        if x0 <= cx <= x1 and y0 <= cy <= y1:
                            mask[ty * self.nx + tx] = 1
                if not general:
                    self._novia_nets.append((r, mask))
        self.add_conns(conns)
        self.refresh()

    # ── set-up ────────────────────────────────────────────────────────────
    def add_conns(self, conns):
        for c in conns:
            if c.idx in self.conns:
                self.conns[c.idx] = c
                continue
            self.conns[c.idx] = c
            self.by_net.setdefault(c.net, []).append(c.idx)
        for c in conns:
            w = c.width if c.width > 0 else self.tiles.ref_w
            cl = c.clearance if c.clearance > 0 else self.tiles.ref_cl
            self.dem[c.idx] = w + cl
            self.free[c.idx] = self.tiles.pad_edges(c.a.pad) | self.tiles.pad_edges(c.b.pad)
        self.order = sorted(self.conns.values(), key=lambda c: (c.rank, c.idx))

    def refresh(self):
        """(Re)read capacities from the tiles (after book / scale) and rebuild the per-border base factors."""
        t = self.tiles
        if self._tv == t.version:
            return
        self._tv = t.version
        self.capE = list(t.capE)
        self.capN = list(t.capN)
        hE, hN = t.hardE, t.hardN
        self.baseE = [INF if hE[s] else 1.0 + self.histE[s] + (WALL if self.capE[s] <= 1e-9 else 0.0)
                      for s in range(self.N)]
        self.baseN = [INF if hN[s] else 1.0 + self.histN[s] + (WALL if self.capN[s] <= 1e-9 else 0.0)
                      for s in range(self.N)]
        self.capV = [-1] * self.G

    def _via_cap(self, col):
        v = self.capV[col]
        if v < 0:
            v = self.capV[col] = self.tiles.via_cap(col % self.nx, col // self.nx)
        return v

    def _region_mul(self, net):
        """Per-node multiplier list from plan regions that apply to ``net`` (None when there is none)."""
        regs = [r for r in (getattr(self.cfg, "regions", None) or []) if r.applies(net)]
        if not regs:
            return None
        key = tuple(r.idx for r in regs)
        m = self._mulbase.get(key)
        if m is None:
            m = [1.0] * self.N
            t = self.tiles
            for r in regs:
                x0, y0, x1, y1 = r.rect
                lays = range(self.nl) if r.layers is None else r.layers
                for ty in range(self.ny):
                    for tx in range(self.nx):
                        a, b, c, d = t.rect(tx, ty)
                        if c <= x0 or a >= x1 or d <= y0 or b >= y1:
                            continue
                        whole = a >= x0 - 1e-9 and c <= x1 + 1e-9 and b >= y0 - 1e-9 and d <= y1 + 1e-9
                        cx, cy = (a + c) / 2, (b + d) / 2
                        inside = x0 <= cx <= x1 and y0 <= cy <= y1
                        if r.forbid:
                            f = INF if whole else (3.0 if inside else 1.0)
                        else:
                            f = r.cost if inside else 1.0
                        if f != 1.0:
                            for l in lays:
                                m[l * self.G + ty * self.nx + tx] *= f
            self._mulbase[key] = m
        return m

    def hint(self, c) -> _Hint:
        xn = _xnet(self.x, c.net)
        key = (c.net, bool(c.tree_target), c.idx if xn.get("side_of") else -1)
        h = self._hints.get(key)
        if h is not None:
            return h
        t, T, nl, G, nx = self.tiles, self.T, self.nl, self.G, self.nx
        nc = _netcfg(self.cfg, c.net)
        h = _Hint()
        lays = tuple(l for l in (nc.layers_ok if nc is not None and nc.layers_ok else range(nl)) if 0 <= l < nl)
        h.lays = lays or tuple(range(nl))
        h.lok = [l in h.lays for l in range(nl)]
        lcost = dict(nc.layer_cost) if nc is not None else {}
        ldir = dict(nc.layer_dir) if nc is not None else {}
        bias = float(getattr(self.cfg, "direction_bias", 0.5) or 0.0)
        h.bus = self.buses.get(c.net)
        if h.bus is not None and h.bus[2] is not None:
            want = h.bus[2]
            if not isinstance(want, int):
                want = t.board.layers.index(want) if want in t.board.layers else None
            if want is not None:
                for l in h.lays:
                    if l != want:
                        lcost[l] = max(lcost.get(l, 1.0), 2.5)
        h.stepE, h.stepN = [], []
        for l in range(nl):
            base = T * float(lcost.get(l, 1.0))
            d = ldir.get(l)
            b = bias
            if d is None and not ldir:
                d, b = self.dirs.get(l), AUTO_DIR_BIAS
            h.stepE.append(base * (1.0 + b) if d == "v" else base)
            h.stepN.append(base * (1.0 + b) if d == "h" else base)
        h.minstep = min(min(h.stepE[l], h.stepN[l]) for l in h.lays)
        h.via_cost = float(nc.via_cost if nc is not None else getattr(self.cfg, "via_cost", 5.0))
        h.max_vias = nc.max_vias if nc is not None else None
        h.via_budget = xn.get("via_budget")
        h.waypoints = list(xn.get("waypoints") or [])
        h.novia = self.novia
        for r, mask in self._novia_nets:
            ap = getattr(r, "applies", None)
            nets_, exc_ = _field(r, "nets"), _field(r, "exc")
            if ap(c.net) if ap is not None else ((not nets_ or c.net in nets_) and not (exc_ and c.net in exc_)):
                if h.novia is self.novia:
                    h.novia = bytearray(self.novia)
                for i, v in enumerate(mask):
                    if v:
                        h.novia[i] = 1
        # node multipliers: regions, avoid rectangles, guide corridor, 'between' cuts, 'side_of'
        mul = self._region_mul(c.net)
        own = False

        def writable():
            nonlocal mul, own
            if not own:
                mul = list(mul) if mul is not None else [1.0] * self.N
                own = True
            return mul

        def tiles_all():
            for ty in range(self.ny):
                for tx in range(nx):
                    yield tx, ty, ty * nx + tx

        if nc is not None and nc.avoid:
            m = writable()
            for x0, y0, x1, y1 in nc.avoid:
                for tx, ty, r in tiles_all():
                    a, b, cc, d = t.rect(tx, ty)
                    if cc <= x0 or a >= x1 or d <= y0 or b >= y1:
                        continue
                    whole = a >= x0 - 1e-9 and cc <= x1 + 1e-9 and b >= y0 - 1e-9 and d <= y1 + 1e-9
                    f = INF if whole else 3.0
                    for l in range(nl):
                        m[l * G + r] *= f
        if nc is not None and nc.corridor:
            m = writable()
            pts = nc.corridor["points"]
            half = nc.corridor["width"] / 2 + T * 0.71
            f = INF if nc.corridor.get("strict") else float(nc.corridor.get("outside_cost", 3.0))
            for tx, ty, r in tiles_all():
                cx, cy = t.center(tx, ty)
                if len(pts) == 1:
                    dmin = math.hypot(cx - pts[0][0], cy - pts[0][1])
                else:
                    dmin = min(_pt_seg(cx, cy, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                               for i in range(len(pts) - 1))
                if dmin > half:
                    for l in range(nl):
                        m[l * G + r] *= f
        for pair in xn.get("between") or []:
            mid = _pad_mid(t.board, pair)
            if mid is None:
                continue
            m = writable()
            tx0, ty0 = t.tile_of(*mid)
            for ty in range(max(0, ty0 - 1), min(self.ny, ty0 + 2)):
                for tx in range(max(0, tx0 - 1), min(nx, tx0 + 2)):
                    cx, cy = t.center(tx, ty)
                    if math.hypot(cx - mid[0], cy - mid[1]) <= T * 0.75 or (tx, ty) == (tx0, ty0):
                        for l in range(nl):
                            m[l * G + ty * nx + tx] *= 0.2
        for so in xn.get("side_of") or []:
            if isinstance(so, (tuple, list)):
                so = {"ref": so[0], "side": so[1] if len(so) > 1 else "cw"}
            box = _ref_box(t.board, _field(so, "ref"))
            if box is None:
                continue
            m = writable()
            px, py = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            ux, uy = c.b.x - c.a.x, c.b.y - c.a.y
            n = math.hypot(ux, uy) or 1.0
            ux, uy = ux / n, uy / n
            band = (abs(ux) * (box[2] - box[0]) + abs(uy) * (box[3] - box[1])) / 2 + T
            cw = str(_field(so, "side", "cw")).lower() == "cw"
            for tx, ty, r in tiles_all():
                cx, cy = t.center(tx, ty)
                dx, dy = cx - px, cy - py
                if abs(dx * ux + dy * uy) > band:
                    continue
                cross = ux * dy - uy * dx            # < 0: the path passes clockwise (on screen, y down)
                if (cross > 0) if cw else (cross < 0):
                    for l in range(nl):
                        m[l * G + r] *= 5.0
        h.mul = mul
        lo = 1.0
        if mul is not None:
            lo = min(1.0, min(v for v in mul))
        if h.bus is not None or not c.tree_target:
            lo *= SHARE
        h.hscale = lo
        self._hints[key] = h
        return h

    # ── usage ─────────────────────────────────────────────────────────────
    def _apply(self, idx, path, sign):
        d = self.dem[idx] * sign
        free = self.free[idx]
        G, nx = self.G, self.nx
        useE, useN, useV, hT, vT = self.useE, self.useN, self.useV, self.hT, self.vT
        a = path[0]
        pd = 0                                        # the previous in-layer step (0: none / a via)
        for b in path[1:]:
            st = b - a
            lo, df = (a, st) if st > 0 else (b, -st)
            if df >= G:
                useV[lo % G] += sign
                st = 0
            elif df == nx:
                if not free or (lo * 2 + 1) not in free:
                    useN[lo] += d
                if st == pd:
                    vT[a] += d                        # node a is passed straight, north-south
            else:
                if not free or (lo * 2) not in free:
                    useE[lo] += d
                if st == pd:
                    hT[a] += d
            pd = st
            a = b

    def rip(self, idx):
        p = self.paths.pop(idx, None)
        if p is not None:
            self._apply(idx, p, -1)
            self.pset.pop(idx, None)
            self.end_on.pop(idx, None)

    def _set(self, idx, path, end_on):
        self.paths[idx] = path
        self.pset[idx] = set(path)
        self.end_on[idx] = end_on
        self._apply(idx, path, 1)

    def recount(self):
        """Usage from scratch (after restoring a snapshot)."""
        self.useE = [0.0] * self.N
        self.useN = [0.0] * self.N
        self.useV = [0] * self.G
        self.hT = [0.0] * self.N
        self.vT = [0.0] * self.N
        for idx in sorted(self.paths):
            self._apply(idx, self.paths[idx], 1)

    def overflow(self):
        """-> (total mm, [(over mm, code)], [(over count, column)]) of the current usage. The total also holds
        the crossing conflicts, left in ``self.xnodes`` as [(over mm, node)]."""
        tot = 0.0
        edges, cols = [], []
        for side, use, cap in ((0, self.useE, self.capE), (1, self.useN, self.capN)):
            for s, u in enumerate(use):
                if u > 1e-9:
                    o = u - cap[s]
                    if o > 1e-6:
                        tot += o
                        edges.append((o, s * 2 + side))
        pv = self.tiles.via_size + self.tiles.ref_cl
        for col, u in enumerate(self.useV):
            if u > 0:
                o = u - self._via_cap(col)
                if o > 0:
                    tot += o * pv
                    cols.append((o, col))
        self.xruns = self.crossings()
        self.xnodes = [(o, run[0]) for o, run, _p, _q in self.xruns]
        for o, _run, _p, _q in self.xruns:
            tot += o
        return tot, edges, cols

    def crossings(self, only: int | None = None) -> list:
        """Forced same-layer crossings of the current paths: [(over mm, run nodes, idx_p, idx_q)], idx_p < idx_q
        (``only``: just the crossings connection ``only`` takes part in).

        Two paths of different nets share a maximal run of nodes t0..tm (consecutive in both). P comes into
        t0 from a and leaves tm to b; Q's neighbours just outside the run are c (at the t0 end) and d (at the
        tm end). With side(x) = sign of cross(in + out, x - t) taken at the respective end of the run along
        P's direction of travel, the paths must cross iff side(c) != side(d). A missing a, b, c or d (the
        path starts or ends in the run, or changes layer there) proves nothing."""
        G, nx = self.G, self.nx
        paths = self.paths
        occ = {}
        if only is None:
            for idx in sorted(paths):
                for n in paths[idx]:
                    occ.setdefault(n, []).append(idx)
        else:
            mine = set(paths[only])
            for idx in sorted(paths):
                for n in paths[idx]:
                    if n in mine:
                        occ.setdefault(n, []).append(idx)
        pairs = {}
        conns = self.conns
        for n, ids in occ.items():
            if len(ids) > 1:
                for i in range(len(ids)):
                    for j in range(i + 1, len(ids)):
                        if ids[i] != ids[j] and conns[ids[i]].net != conns[ids[j]].net \
                                and (only is None or ids[i] == only or ids[j] == only):
                            pairs.setdefault((ids[i], ids[j]), []).append(n)
        if not pairs:
            return []
        pos = {}
        out = []

        def xy(n):
            r = n % G
            return r % nx, r // nx

        for (p, q) in sorted(pairs):
            P, Q = paths[p], paths[q]
            pp = pos.get(p)
            if pp is None:
                pp = pos[p] = {n: i for i, n in enumerate(P)}
            pq = pos.get(q)
            if pq is None:
                pq = pos[q] = {n: i for i, n in enumerate(Q)}
            common = sorted(pp[n] for n in pairs[(p, q)])
            k = 0
            while k < len(common):
                i = j = common[k]
                while k + 1 < len(common) and common[k + 1] == j + 1 and abs(P[j + 1] - P[j]) < G \
                        and abs(pq[P[j + 1]] - pq[P[j]]) == 1:
                    j += 1
                    k += 1
                k += 1
                if i == 0 or j == len(P) - 1:
                    continue
                a, b = P[i - 1], P[j + 1]
                t0, tm = P[i], P[j]
                if abs(t0 - a) >= G or abs(b - tm) >= G:
                    continue
                qi, qj = pq[t0], pq[tm]
                if i == j:
                    ci, di = qi - 1, qi + 1
                else:
                    dq = 1 if qj > qi else -1
                    ci, di = qi - dq, qj + dq
                if ci < 0 or di < 0 or ci >= len(Q) or di >= len(Q):
                    continue
                c, d = Q[ci], Q[di]
                if abs(c - t0) >= G or abs(d - tm) >= G or c == a or d == b:
                    continue
                ax, ay = xy(a)
                bx, by = xy(b)
                x0, y0 = xy(t0)
                xm, ym = xy(tm)
                x1, y1 = xy(P[i + 1])
                xl, yl = xy(P[j - 1])
                cx, cy = xy(c)
                dx, dy = xy(d)
                ux, uy = (x0 - ax) + (x1 - x0), (y0 - ay) + (y1 - y0)
                s0 = ux * (cy - y0) - uy * (cx - x0)
                ux, uy = (xm - xl) + (bx - xm), (ym - yl) + (by - ym)
                sm = ux * (dy - ym) - uy * (dx - xm)
                if s0 * sm < 0:
                    dp, dq_ = self.dem[p], self.dem[q]
                    out.append((dp if dp < dq_ else dq_, tuple(P[i:j + 1]), p, q))
        return out

    def wirelength(self, ids=None) -> float:
        G, T = self.G, self.T
        tot = 0.0
        for idx in sorted(self.paths if ids is None else (i for i in ids if i in self.paths)):
            p = self.paths[idx]
            vc = self.hint(self.conns[idx]).via_cost
            a = p[0]
            for b in p[1:]:
                tot += vc if abs(b - a) >= G else T
                a = b
        return tot

    # ── terminals and targets ─────────────────────────────────────────────
    def _nodes(self, term, h):
        tx, ty = self.tiles.tile_of(term.x, term.y)
        r = ty * self.nx + tx
        lays = [l for l in term.layers if 0 <= l < self.nl] or list(h.lays)
        return [l * self.G + r for l in lays]

    def _side(self, c):
        """Connections of c's net on the far (b) side of c: {idx} whose paths c may end on."""
        if not c.tree_target or c.a.cluster == c.b.cluster:
            return ()
        sib = [i for i in self.by_net.get(c.net, ()) if i != c.idx and i in self.paths]
        if not sib:
            return ()
        parent = {}

        def find(v):
            while parent.setdefault(v, v) != v:
                parent[v] = parent[parent[v]]
                v = parent[v]
            return v

        for i in sib:
            o = self.conns[i]
            parent[find(o.a.cluster)] = find(o.b.cluster)
        fb = find(c.b.cluster)
        if find(c.a.cluster) == fb:
            return ()
        out = []
        me = (c.rank, c.idx)
        for i in sib:
            o = self.conns[i]
            if find(o.a.cluster) != fb:
                continue
            if TEE_ONLY_EARLIER and not (o.rank, o.idx) < me:      # only on copper detail will already have laid
                continue
            j, dep = i, False                        # never end on a connection that (transitively) ends on c
            seen = 0
            while j >= 0 and seen < 64:
                if j == c.idx:
                    dep = True
                    break
                j = self.end_on.get(j, -1)
                seen += 1
            if not dep:
                out.append(i)
        return out

    def _targets(self, c, h):
        tg = {}
        for i in self._side(c):
            for n in self.paths[i]:
                if n not in tg:
                    tg[n] = i
        for n in self._nodes(c.b, h):
            tg[n] = -1
        return tg

    def _shared(self, c, h):
        """Borders used by bus siblings (or same-net connections of a non-tree connection): cost ÷ 2 there."""
        ids = []
        if h.bus is not None:
            for n in h.bus[3]:
                if n != c.net:
                    ids += self.by_net.get(n, [])
        if not c.tree_target:
            ids += [i for i in self.by_net.get(c.net, ()) if i != c.idx]
        if not ids:
            return None
        G, nx = self.G, self.nx
        sh = set()
        for i in sorted(set(ids)):
            p = self.paths.get(i)
            if not p:
                continue
            a = p[0]
            for b in p[1:]:
                lo, df = (a, b - a) if a < b else (b, a - b)
                if df < G:
                    sh.add(lo * 2 + (1 if df == nx else 0))
                a = b
        return sh or None

    # ── the search ────────────────────────────────────────────────────────
    def _astar(self, srcs, tg, h, d, pres, shared, margin=None):
        """A* from ``srcs`` to any node in ``tg``. -> node path | None. ``margin``: search only inside the
        bounding box of sources and targets grown by that many tiles (None: the whole board)."""
        nx, ny, G = self.nx, self.ny, self.G
        self._gen += 1
        gen = self._gen
        dist, stamp, done, prev = self._dist, self._stamp, self._done, self._prev
        baseE, baseN, capE, capN, useE, useN = self.baseE, self.baseN, self.capE, self.capN, self.useE, self.useN
        capV, useV, histV, novia = self.capV, self.useV, self.histV, h.novia
        hT, vT = self.hT, self.vT
        hX = self.histX if self.anyX else None
        stepE, stepN, lok, lays, mul = h.stepE, h.stepN, h.lok, h.lays, h.mul
        pk = pres / self.p
        pkh, d2 = pk / 2.0, 2.0 * d
        viac = h.via_cost
        turnc = viac * TURN_AT_TRAFFIC
        # a pad on a layer the net may not run on is still reached / left with a via in its own tile
        vlays = tuple(sorted(set(lays) | {n // G for n in tg}))
        vok = h.max_vias != 0 and (len(vlays) > 1 or any(not lok[s // G] for s in srcs))
        via_cap = self.tiles.via_cap
        # heuristic: Manhattan distance to the target tile. A tree of many target tiles has no cheap exact
        # distance (its bounding box usually holds the source: h = 0 and the search floods), so the search
        # AIMS at the target tile nearest to the first source; it still ends on whichever target it meets.
        # (Measured on tinytapeout, whose +3V3 has 99 connections: 2/3 of all expansions were such floods.)
        txs = [(n % G) % nx for n in tg]
        tys = [(n % G) // nx for n in tg]
        gx0, gx1, gy0, gy1 = min(txs), max(txs), min(tys), max(tys)
        if gx0 != gx1 or gy0 != gy1:
            r0 = srcs[0] % G
            sx0, sy0 = r0 % nx, r0 // nx
            best = None
            for i in range(len(txs)):
                dd = abs(txs[i] - sx0) + abs(tys[i] - sy0)
                if best is None or dd < best[0] or (dd == best[0] and (txs[i], tys[i]) < best[1]):
                    best = (dd, (txs[i], tys[i]))
            bx0, bx1, by0, by1 = gx0, gx1, gy0, gy1          # the box still covers every target
            gx0 = gx1 = best[1][0]
            gy0 = gy1 = best[1][1]
        else:
            bx0, bx1, by0, by1 = gx0, gx1, gy0, gy1
        hs = h.minstep * h.hscale * self.hweight
        hx = [hs * (gx0 - i if i < gx0 else (i - gx1 if i > gx1 else 0)) for i in range(nx)]
        hy = [hs * (gy0 - j if j < gy0 else (j - gy1 if j > gy1 else 0)) for j in range(ny)]
        x0 = y0 = 0
        nx1, ny1 = nx - 1, ny - 1
        if margin is not None:
            sx = [(n % G) % nx for n in srcs]
            sy = [(n % G) // nx for n in srcs]
            x0, nx1 = max(0, min(bx0, min(sx)) - margin), min(nx - 1, max(bx1, max(sx)) + margin)
            y0, ny1 = max(0, min(by0, min(sy)) - margin), min(ny - 1, max(by1, max(sy)) + margin)
        heap = []
        for s in srcs:
            r = s % G
            stamp[s] = gen
            dist[s] = 0.0
            prev[s] = -1
            hh = hx[r % nx] + hy[r // nx]
            _push(heap, (hh, hh, s))
        exp = 0
        found = -1
        while heap:
            _f, _h, s = _pop(heap)
            if done[s] == gen:
                continue
            done[s] = gen
            if s in tg:
                found = s
                break
            exp += 1
            g = dist[s]
            l = s // G
            r = s - l * G
            ty = r // nx
            tx = r - ty * nx
            pin = prev[s]
            pin = s - pin if pin >= 0 else 0          # the step that led here (0: a source, or a via)
            tp = 0.0
            if pin and -G < pin < G:
                if hT[s] + vT[s] > 1e-6:
                    tp = turnc                        # turning in this tile means passing its through traffic
            else:
                pin = 0
            if lok[l]:
                if tx < nx1:
                    b = baseE[s]
                    n = s + 1
                    if b < INF and done[n] != gen:
                        c = stepE[l] * b
                        if pk:
                            o = useE[s] + d - capE[s]
                            if o > 0:
                                c *= 1.0 + pk * o
                            o = vT[s] + vT[n]
                            if o > 0:
                                c *= 1.0 + pkh * (o if o < d2 else d2)
                        if hX is not None:
                            c *= 1.0 + hX[s] + hX[n]
                        if mul is not None:
                            c *= mul[n]
                        if shared is not None and (s * 2) in shared:
                            c *= SHARE
                        if tp and pin != 1:
                            c += tp
                        c += g
                        if c < INF and (stamp[n] != gen or c < dist[n]):
                            stamp[n] = gen
                            dist[n] = c
                            prev[n] = s
                            hh = hx[tx + 1] + hy[ty]
                            _push(heap, (c + hh, hh, n))
                if tx > x0:
                    n = s - 1
                    b = baseE[n]
                    if b < INF and done[n] != gen:
                        c = stepE[l] * b
                        if pk:
                            o = useE[n] + d - capE[n]
                            if o > 0:
                                c *= 1.0 + pk * o
                            o = vT[s] + vT[n]
                            if o > 0:
                                c *= 1.0 + pkh * (o if o < d2 else d2)
                        if hX is not None:
                            c *= 1.0 + hX[s] + hX[n]
                        if mul is not None:
                            c *= mul[n]
                        if shared is not None and (n * 2) in shared:
                            c *= SHARE
                        if tp and pin != -1:
                            c += tp
                        c += g
                        if c < INF and (stamp[n] != gen or c < dist[n]):
                            stamp[n] = gen
                            dist[n] = c
                            prev[n] = s
                            hh = hx[tx - 1] + hy[ty]
                            _push(heap, (c + hh, hh, n))
                if ty < ny1:
                    b = baseN[s]
                    n = s + nx
                    if b < INF and done[n] != gen:
                        c = stepN[l] * b
                        if pk:
                            o = useN[s] + d - capN[s]
                            if o > 0:
                                c *= 1.0 + pk * o
                            o = hT[s] + hT[n]
                            if o > 0:
                                c *= 1.0 + pkh * (o if o < d2 else d2)
                        if hX is not None:
                            c *= 1.0 + hX[s] + hX[n]
                        if mul is not None:
                            c *= mul[n]
                        if shared is not None and (s * 2 + 1) in shared:
                            c *= SHARE
                        if tp and pin != nx:
                            c += tp
                        c += g
                        if c < INF and (stamp[n] != gen or c < dist[n]):
                            stamp[n] = gen
                            dist[n] = c
                            prev[n] = s
                            hh = hx[tx] + hy[ty + 1]
                            _push(heap, (c + hh, hh, n))
                if ty > y0:
                    n = s - nx
                    b = baseN[n]
                    if b < INF and done[n] != gen:
                        c = stepN[l] * b
                        if pk:
                            o = useN[n] + d - capN[n]
                            if o > 0:
                                c *= 1.0 + pk * o
                            o = hT[s] + hT[n]
                            if o > 0:
                                c *= 1.0 + pkh * (o if o < d2 else d2)
                        if hX is not None:
                            c *= 1.0 + hX[s] + hX[n]
                        if mul is not None:
                            c *= mul[n]
                        if shared is not None and (n * 2 + 1) in shared:
                            c *= SHARE
                        if tp and pin != -nx:
                            c += tp
                        c += g
                        if c < INF and (stamp[n] != gen or c < dist[n]):
                            stamp[n] = gen
                            dist[n] = c
                            prev[n] = s
                            hh = hx[tx] + hy[ty - 1]
                            _push(heap, (c + hh, hh, n))
            if vok and not novia[r]:
                cv = capV[r]
                if cv < 0:
                    cv = capV[r] = via_cap(tx, ty)
                c = viac * (1.0 + histV[r] + (WALL_VIA if cv <= 0 else 0.0))
                if pres:
                    o = useV[r] + 1 - cv
                    if o > 0:
                        c *= 1.0 + pres * o
                c += g
                hh = hx[tx] + hy[ty]
                for l2 in vlays:
                    if l2 != l:
                        n = l2 * G + r
                        if done[n] != gen and (stamp[n] != gen or c < dist[n]):
                            if not lok[l2] and n not in tg:
                                continue
                            if mul is not None and mul[n] == INF and n not in tg:
                                continue
                            stamp[n] = gen
                            dist[n] = c
                            prev[n] = s
                            _push(heap, (c + hh, hh, n))
        self.expansions += exp
        self.searches += 1
        if found < 0:
            return None
        path = [found]
        s = prev[found]
        while s >= 0:
            path.append(s)
            s = prev[s]
        path.reverse()
        return path

    def _patch(self, idx, d):
        """Make the borders in the shadow of the connection's own pads free for it; returns the undo list."""
        undo = []
        for code in self.free[idx]:
            s = code >> 1
            if code & 1:
                undo.append((code, self.capN[s], self.baseN[s]))
                if self.baseN[s] < INF:
                    self.capN[s] += d
                    self.baseN[s] = 1.0 + self.histN[s]
            else:
                undo.append((code, self.capE[s], self.baseE[s]))
                if self.baseE[s] < INF:
                    self.capE[s] += d
                    self.baseE[s] = 1.0 + self.histE[s]
        return undo

    def _unpatch(self, undo):
        for code, cap, base in undo:
            s = code >> 1
            if code & 1:
                self.capN[s], self.baseN[s] = cap, base
            else:
                self.capE[s], self.baseE[s] = cap, base

    def route(self, c, pres: float) -> bool:
        """Route one (currently unrouted) connection at present-congestion factor ``pres``."""
        h = self.hint(c)
        d = self.dem[c.idx]
        srcs = self._nodes(c.a, h)
        tg = self._targets(c, h)
        legs = []
        G, nx = self.G, self.nx
        for wp in h.waypoints:
            tx, ty = self.tiles.tile_of(wp[0], wp[1])
            lay = wp[2] if len(wp) > 2 else None
            if lay is not None and not isinstance(lay, int):
                lay = self.tiles.board.layers.index(lay) if lay in self.tiles.board.layers else None
            ls = [lay] if lay is not None else list(h.lays)
            legs.append({l * G + ty * nx + tx: -1 for l in ls})
        legs.append(tg)
        shared = self._shared(c, h)
        undo = self._patch(c.idx, d) if self.free[c.idx] else None
        try:
            path = []
            for leg in legs:
                part = self._astar(srcs, leg, h, d, pres, shared, self.margin)
                if part is None and self.margin is not None:
                    part = self._astar(srcs, leg, h, d, pres, shared)       # walled in inside the box: look wider
                if part is None:
                    path = None
                    break
                path = path + part[1:] if path else part
                srcs = [part[-1]]
        finally:
            if undo:
                self._unpatch(undo)
        if not path:
            self.unrouted[c.idx] = "no tile path (walled in by the outline, a forbid region or layer limits)"
            return False
        self.unrouted.pop(c.idx, None)
        self._set(c.idx, path, tg.get(path[-1], -1))
        return True

    # ── negotiation ───────────────────────────────────────────────────────
    def _stale(self, ids):
        """Connections whose path ends on a path that no longer passes there (or was ripped)."""
        out = []
        for c in self.order:
            if c.idx not in ids or c.idx not in self.paths:
                continue
            j = self.end_on.get(c.idx, -1)
            if j >= 0 and (j not in self.pset or self.paths[c.idx][-1] not in self.pset[j]):
                out.append(c)
        return out

    def _score(self):
        tot, edges, cols = self.overflow()
        return (round(tot, 6), round(self.wirelength(), 6)), edges, cols

    def _score_of(self, ids):
        """Like _score, but only what the connections ``ids`` take part in (a partial re-route cannot repair
        the rest of the board, and must not keep negotiating because of it)."""
        codes, cs = set(), set()
        for i in sorted(ids):
            p = self.paths.get(i)
            if p:
                e = self._elems(p)
                codes |= e[0]
                cs |= e[1]
        tot = 0.0
        edges, cols = [], []
        for code in sorted(codes):
            s = code >> 1
            o = (self.useN[s] - self.capN[s]) if code & 1 else (self.useE[s] - self.capE[s])
            if o > 1e-6:
                tot += o
                edges.append((o, code))
        pv = self.tiles.via_size + self.tiles.ref_cl
        for col in sorted(cs):
            o = self.useV[col] - self._via_cap(col)
            if o > 0:
                tot += o * pv
                cols.append((o, col))
        runs = {}
        for i in sorted(ids):
            if i in self.paths:
                for r in self.crossings(i):
                    runs[(r[2], r[3], r[1])] = r
        self.xruns = [runs[k] for k in sorted(runs)]
        self.xnodes = [(o, run[0]) for o, run, _p, _q in self.xruns]
        tot += sum(o for o, _run, _p, _q in self.xruns)
        return (round(tot, 6), round(self.wirelength(ids), 6)), edges, cols

    def negotiate(self, ids, budget: Budget, iters: int = MAX_ITERS) -> dict:
        """PathFinder over the connections ``ids`` (the others keep their paths and count as fixed usage).
        A partial run (fewer than all connections) is judged on its own connections only, stops after
        ``PARTIAL_STALL`` iterations without improvement and never runs more than ``PARTIAL_ITERS``."""
        self.refresh()
        self.margin = BOX_MARGIN
        ids = set(ids)
        todo = [c for c in self.order if c.idx in ids]
        partial = len(ids) < len(self.conns)
        if partial:
            iters = min(iters, PARTIAL_ITERS)
        score_of = (lambda: self._score_of(ids)) if partial else self._score
        stall = 0
        used0 = budget.used
        e0 = self.expansions

        def spend():
            nonlocal e0
            budget.charge(self.expansions - e0)
            e0 = self.expansions

        out_of_budget = False
        for c in todo:
            if c.idx in self.paths:
                self.rip(c.idx)
        for c in todo:
            if budget.left() <= 0:
                out_of_budget = True
                self.unrouted.setdefault(c.idx, "time budget")
                continue
            self.route(c, 0.0)
            spend()
        for _ in range(3):
            st = self._stale(ids)
            if not st:
                break
            for c in st:
                self.rip(c.idx)
                self.route(c, 0.0)
            spend()
        score, edges, cols = score_of()
        self.overflow_first = score[0]
        best = (score, dict(self.paths), dict(self.end_on))
        its = 1
        k = 0
        while score[0] > 1e-6 and its < iters and not out_of_budget:
            pres = 0.5 * 1.5 ** k
            k += 1
            self.pres = pres
            self.margin = BOX_MARGIN + 2 * k
            p = self.p
            hot = set()
            for o, code in edges:
                s = code >> 1
                hot.add(code)
                if code & 1:
                    self.histN[s] += o / p
                    if self.baseN[s] < INF:
                        self.baseN[s] += o / p
                else:
                    self.histE[s] += o / p
                    if self.baseE[s] < INF:
                        self.baseE[s] += o / p
            hotc = set()
            for o, col in cols:
                self.histV[col] += o
                hotc.add(col)
            hotx = set()
            for o, run, ip, iq in self.xruns:
                for node in run:
                    self.histX[node] += o / p
                hotx.add(ip)
                hotx.add(iq)
                self.anyX = True
            G, nx = self.G, self.nx
            redo = []
            for c in todo:
                pth = self.paths.get(c.idx)
                if not pth:
                    if c.idx in self.unrouted and self.unrouted[c.idx] != "time budget":
                        redo.append(c)
                    continue
                a = pth[0]
                hit = c.idx in hotx
                for b in pth[1:]:
                    if hit:
                        break
                    lo, df = (a, b - a) if a < b else (b, a - b)
                    if df >= G:
                        if lo % G in hotc:
                            hit = True
                    elif (lo * 2 + (1 if df == nx else 0)) in hot:
                        hit = True
                    a = b
                if hit:
                    redo.append(c)
            if not redo:
                break
            for c in redo:
                if budget.left() <= 0:
                    out_of_budget = True
                    break
                old, old_end = self.paths.get(c.idx), self.end_on.get(c.idx, -1)
                self.rip(c.idx)
                if not self.route(c, pres) and old is not None:
                    self._set(c.idx, old, old_end)
                    self.unrouted.pop(c.idx, None)
                spend()
            for _ in range(3):
                st = self._stale(ids)
                if not st:
                    break
                for c in st:
                    self.rip(c.idx)
                    self.route(c, pres)
                spend()
            its += 1
            score, edges, cols = score_of()
            if score < best[0]:
                best = (score, dict(self.paths), dict(self.end_on))
                stall = 0
            else:
                stall += 1
                if partial and stall >= PARTIAL_STALL:
                    break
        if best[0] != score or best[1] != self.paths:
            self.paths = dict(best[1])
            self.end_on = dict(best[2])
            self.pset = {i: set(p) for i, p in self.paths.items()}
            self.recount()
        self.iterations = its
        return {"iterations": its, "overflow_mm": best[0][0], "wirelength_mm": best[0][1],
                "overflow_first_mm": self.overflow_first, "out_of_budget": out_of_budget,
                "work": budget.used - used0}

    # ── layer assignment (Viterbi over path position × layer × vias used) ─
    def _ecost(self, l, ra, rb, d, pres, h, free):
        """Cost of the in-layer step between neighbouring columns ra -> rb on layer l (INF when closed)."""
        G, nx = self.G, self.nx
        lo = ra if ra < rb else rb
        s = l * G + lo
        if abs(rb - ra) == nx and self.ny > 1:
            code, base, cap, use, step = s * 2 + 1, self.baseN[s], self.capN[s], self.useN[s], h.stepN[l]
            hist = self.histN[s]
        else:
            code, base, cap, use, step = s * 2, self.baseE[s], self.capE[s], self.useE[s], h.stepE[l]
            hist = self.histE[s]
        if base == INF:
            return INF
        if free and code in free:
            cap, base = cap + d, 1.0 + hist
        c = step * base
        o = use + d - cap
        if pres and o > 0:
            c *= 1.0 + pres * o / self.p
        sa, sb = l * G + ra, l * G + rb
        if pres:
            other = self.hT if code & 1 else self.vT
            o = (other[sa] + other[sb]) / 2
            if o > 0:
                c *= 1.0 + pres * (o if o < d else d) / self.p
        if self.anyX:
            c *= 1.0 + self.histX[sa] + self.histX[sb]
        if h.mul is not None:
            c *= h.mul[sb]
        return c

    def assign(self, c, path2d, start_layers, end_layers, pres=None):
        """-> (layer per path tile, layer_seq, cost) or None when no assignment satisfies max_vias / layers."""
        h = self.hint(c)
        d = self.dem[c.idx] if c.idx in self.dem else (c.width + c.clearance)
        free = self.free.get(c.idx) or None
        pres = self.pres if pres is None else pres
        nx, G = self.nx, self.G
        cols = [ty * nx + tx for tx, ty in path2d]
        n = len(cols)
        vmax = MAX_DP_VIAS if h.max_vias is None else min(h.max_vias, MAX_DP_VIAS)
        vb = h.via_budget
        lays = h.lays
        last = tuple(sorted(set(lays) | {l for l in end_layers if 0 <= l < self.nl}))
        cur = {}
        for l in start_layers:
            if 0 <= l < self.nl:
                cur[(l, 0)] = (0.0, None)
        if not cur:
            return None
        back = []
        for i in range(n):
            r = cols[i]
            # at most one via in this tile
            if vmax > 0 and not h.novia[r]:
                cv = self._via_cap(r)
                vc = h.via_cost * (1.0 + self.histV[r] + (WALL_VIA if cv <= 0 else 0.0))
                o = self.useV[r] + 1 - cv
                if pres and o > 0:
                    vc *= 1.0 + pres * o
                add = {}
                for (l, v) in sorted(cur):
                    if v >= vmax:
                        continue
                    g = cur[(l, v)][0] + (vc * VIA_BUDGET_FACTOR if (vb is not None and v >= vb) else vc)
                    for l2 in (lays if i < n - 1 else last):
                        if l2 == l or (i < n - 1 and h.mul is not None and h.mul[l2 * G + r] == INF):
                            continue
                        k = (l2, v + 1)
                        e = add.get(k)
                        if e is None or g < e[0] - 1e-12:
                            add[k] = (g, (l, v))
            else:
                add = {}
            # arriving and leaving on the same layer: a turn in a tile with through traffic costs one via
            turn = 0 < i < n - 1 and (cols[i] - cols[i - 1]) != (cols[i + 1] - cols[i])
            here = {}
            for k, (g, _b) in cur.items():
                if turn and self.hT[k[0] * G + r] + self.vT[k[0] * G + r] > 1e-6:
                    g += h.via_cost * TURN_AT_TRAFFIC
                here[k] = (g, ("s", None))
            for k in sorted(add):
                g, frm = add[k]
                if k not in here or g < here[k][0] - 1e-12:
                    here[k] = (g, ("v", frm))
            back.append(here)
            if i == n - 1:
                break
            nxt = {}
            rb = cols[i + 1]
            for (l, v) in sorted(here):
                if not h.lok[l]:
                    continue
                ec = self._ecost(l, r, rb, d, pres, h, free)
                if ec == INF:
                    continue
                nxt[(l, v)] = (here[(l, v)][0] + ec, None)
            if not nxt:
                return None
            cur = nxt
        ends = [(g, v, l) for (l, v), (g, _b) in back[-1].items() if l in end_layers]
        if not ends:
            return None
        g, v, l = min(ends)
        layer = [0] * n
        seq = []
        for i in range(n - 1, -1, -1):
            kind, frm = back[i][(l, v)][1]
            layer[i] = l
            if kind == "v":
                seq.append((i, frm[0], l))
                l, v = frm
        seq.reverse()
        return layer, tuple(seq), g

    def layered(self, idx):
        """(path2d, layer per tile, layer_seq) of the stored 3-D path."""
        p = self.paths[idx]
        G, nx = self.G, self.nx
        path2d, layer, seq = [], [], []
        for n in p:
            l, r = divmod(n, G)
            t = (r % nx, r // nx)
            if path2d and path2d[-1] == t:
                seq.append((len(path2d) - 1, layer[-1], l))
                layer[-1] = l
            else:
                path2d.append(t)
                layer.append(l)
        # a via in the first tile changes the layer the path leaves on, not the one it arrives on: keep
        # 'layer' as the layer on which the path LEAVES each tile (the last tile: the layer it ends on)
        return path2d, layer, tuple(seq)

    def relayer(self, idx) -> bool:
        """Re-assign the layers of one routed connection with the DP; keeps the A* layers when the DP has no
        better (or no feasible) answer. Returns True when the path changed."""
        c = self.conns[idx]
        p = self.paths[idx]
        h = self.hint(c)
        G, nx = self.G, self.nx
        path2d, _layer, _seq = self.layered(idx)
        start = sorted({n // G for n in self._nodes(c.a, h)})
        end_col = p[-1] % G
        j = self.end_on.get(idx, -1)
        if j >= 0 and j in self.paths:
            end = sorted({n // G for n in self.paths[j] if n % G == end_col})
        else:
            end = sorted({n // G for n in self._nodes(c.b, h)})
        if h.waypoints:
            return False
        self._apply(idx, p, -1)
        res = self.assign(c, path2d, start, end)
        new = None
        if res is not None:
            layer, seq, _g = res
            via_at = {}
            for i, a, b in seq:
                via_at[i] = (a, b)
            new = []
            for i, (tx, ty) in enumerate(path2d):
                r = ty * nx + tx
                if i in via_at:
                    new.append(via_at[i][0] * G + r)
                    new.append(via_at[i][1] * G + r)
                else:
                    new.append(layer[i] * G + r)
        if new is None or new == p:
            self._apply(idx, p, 1)
            return False
        # the DP prices overflow, it does not forbid it: never trade a via for new overflow
        ea, eb = self._elems(p), self._elems(new)
        el = tuple(ea[i] | eb[i] for i in range(3))
        self._apply(idx, p, 1)
        before = self._over_at(*el) + self._cross_of(idx)
        self._apply(idx, p, -1)
        self._apply(idx, new, 1)
        self.paths[idx] = new
        if self._over_at(*el) + self._cross_of(idx) > before + 1e-9:
            self._apply(idx, new, -1)
            self._apply(idx, p, 1)
            self.paths[idx] = p
            return False
        self.pset[idx] = set(new)
        return True

    def _cross_of(self, idx) -> float:
        """Crossing overflow (mm) the connection takes part in, with the current paths."""
        return sum(o for o, _run, _p, _q in self.crossings(idx))

    def _elems(self, path):
        """(border codes, via columns, straight-through nodes) a path touches."""
        G, nx = self.G, self.nx
        codes, cols, nodes = set(), set(), set()
        a = path[0]
        pd = 0
        for b in path[1:]:
            st = b - a
            lo, df = (a, st) if st > 0 else (b, -st)
            if df >= G:
                cols.add(lo % G)
                st = 0
            else:
                codes.add(lo * 2 + (1 if df == nx else 0))
                if st == pd:
                    nodes.add(a)
            pd = st
            a = b
        return codes, cols, nodes

    def _over_at(self, codes, cols, _nodes=None) -> float:
        """Overflow (mm) on the given borders and via columns with the current usage."""
        tot = 0.0
        for code in codes:
            s = code >> 1
            o = (self.useN[s] - self.capN[s]) if code & 1 else (self.useE[s] - self.capE[s])
            if o > 1e-6:
                tot += o
        pv = self.tiles.via_size + self.tiles.ref_cl
        for col in cols:
            o = self.useV[col] - self._via_cap(col)
            if o > 0:
                tot += o * pv
        return tot


# ── small geometry helpers ────────────────────────────────────────────────
def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    if L <= 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _pad_mid(board, pair):
    try:
        a, b = pair
    except (TypeError, ValueError):
        return None
    pts = []
    for name in (a, b):
        hit = [p for p in board.pads if p.name == name]
        if not hit:
            return None
        pts.append((hit[0].x, hit[0].y))
    return (pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2


def _ref_box(board, ref):
    xs = [p.x for p in board.pads if p.ref == ref]
    ys = [p.y for p in board.pads if p.ref == ref]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _signal_layers(cfg, conns, nl) -> list[int]:
    lays = set()
    for c in conns:
        nc = _netcfg(cfg, c.net)
        lays.update(nc.layers_ok if nc is not None and nc.layers_ok else range(nl))
    return sorted(l for l in lays if 0 <= l < nl)


# ── public functions ──────────────────────────────────────────────────────
def layer_directions(board, conns: list[ConnV2], tiles: Tiles | None = None, layers=None) -> dict[int, str]:
    """Automatic preferred directions {layer index: 'h' | 'v'} (design §4.6). Empty unless the board has >= 2
    signal layers and >= 40 connections. The histogram of connection bearings (weighted by length) gives the
    dominant axis; it goes to the signal layer with the larger share of its free capacity along that axis
    (``tiles`` given; else the first layer), and the layers alternate from there."""
    lays = sorted(layers) if layers is not None else list(range(board.L))
    if len(lays) < 2 or len(conns) < 40:
        return {}
    hx = hy = 0.0
    for c in conns:
        hx += abs(c.b.x - c.a.x)
        hy += abs(c.b.y - c.a.y)
    dom, oth = ("h", "v") if hx >= hy else ("v", "h")
    first = 0
    if tiles is not None:
        best = None
        for i, l in enumerate(lays):
            base = l * tiles.G
            e = sum(tiles.capE[base:base + tiles.G])        # crossed by horizontal runs
            n = sum(tiles.capN[base:base + tiles.G])
            share = (e if dom == "h" else n) / (e + n) if e + n > 0 else 0.5
            key = (-round(share, 6), i)
            if best is None or key < best[0]:
                best = (key, i)
        first = best[1]
    return {l: (dom if (i - first) % 2 == 0 else oth) for i, l in enumerate(lays)}


def pathfinder(tiles: "Tiles", conns: list[ConnV2], cfg, x, budget: Budget, *, dirs: dict | None = None,
               state: "GlobalState | None" = None) -> dict[int, list[tuple[int, int]]]:
    """Negotiated global routing of ``conns`` -> {conn idx: 2-D tile path from terminal a}. Connections that
    could not be routed (budget, walled in) are missing from the result. Pass ``state`` to get at the usage,
    the 3-D paths and the overflow afterwards (``state.info`` holds the negotiation summary)."""
    st = state if state is not None else GlobalState(tiles, conns, cfg, x, dirs)
    iters = int(_xget(x, "negotiation_iters", MAX_ITERS) or MAX_ITERS)
    st.info = st.negotiate([c.idx for c in conns], budget, max(1, min(iters, 200)))
    out = {}
    for c in conns:
        if c.idx in st.paths:
            out[c.idx] = st.layered(c.idx)[0]
    return out


def assign_layers(tiles: "Tiles", conn: ConnV2, path2d: list, cfg, x, *, state: "GlobalState | None" = None,
                  end_layers=None) -> tuple:
    """Layer-assignment DP for one 2-D tile path -> ``(layers, layer_seq)``: the layers used (sorted) and the
    via hints ``((i, from_layer, to_layer), ...)`` (a via in tile ``path2d[i]``). Minimises edge cost on each
    layer + via cost × layer changes; the ends are tied to the terminal layers (``end_layers`` overrides the
    b side), ``layers_ok`` and ``max_vias`` are hard, ``via_budget`` is soft. ``((), ())`` when no assignment
    exists. ``state``: the negotiation state whose congestion should be priced (default: an empty board)."""
    st = state if state is not None else GlobalState(tiles, [conn], cfg, x)
    if conn.idx not in st.conns:
        st.add_conns([conn])
    h = st.hint(conn)
    start = sorted({n // st.G for n in st._nodes(conn.a, h)})
    end = sorted(end_layers) if end_layers is not None else sorted({n // st.G for n in st._nodes(conn.b, h)})
    res = st.assign(conn, [tuple(t) for t in path2d], start, end)
    if res is None:
        return (), ()
    layer, seq, _g = res
    return tuple(sorted(set(layer) | {a for _i, a, _b in seq})), seq


def _lateral(st: GlobalState, ids) -> tuple[dict, dict]:
    """Planarity hint: for every border the connections crossing it, ranked along the border.
    -> ({idx: {(layer, tx, ty, side): rank}}, {(layer, tx, ty, side): crossings})"""
    G, nx = st.G, st.nx
    cross = {}
    for idx in sorted(st.paths):
        c = st.conns[idx]
        p = st.paths[idx]
        a = p[0]
        for b in p[1:]:
            lo, df = (a, b - a) if a < b else (b, a - b)
            if df < G:
                side = 1 if df == nx else 0
                # coordinate along the border of the terminal the path is heading to, then of the other one
                fwd = b > a
                if side:                              # N border (horizontal line): rank along x
                    k = (c.b.x, c.a.x) if fwd else (c.a.x, c.b.x)
                else:
                    k = (c.b.y, c.a.y) if fwd else (c.a.y, c.b.y)
                bus = st.buses.get(c.net)
                cross.setdefault(lo * 2 + side, []).append((k, bus, c.rank, idx))
            a = b
    per = {}
    counts = {}
    for code in sorted(cross):
        items = cross[code]
        key = st.tiles.edge_of(code)
        counts[key] = len(items)
        if len(items) > 1:
            # declared buses keep their plan order: members get the bus's mean coordinate and are ordered by
            # their index in the bus, in the direction that agrees with their coordinates
            groups = {}
            for k, bus, _r, idx in items:
                if bus is not None:
                    groups.setdefault(bus[0], []).append((bus[1], k[0]))
            fix = {}
            for bi, mem in groups.items():
                if len(mem) < 2:
                    continue
                mean = sum(m[1] for m in mem) / len(mem)
                mi = sum(m[0] for m in mem) / len(mem)
                cov = sum((m[0] - mi) * (m[1] - mean) for m in mem)
                fix[bi] = (mean, -1.0 if cov < 0 else 1.0)
            ordered = []
            for k, bus, r, idx in items:
                if bus is not None and bus[0] in fix:
                    mean, sgn = fix[bus[0]]
                    ordered.append(((mean, 0, sgn * bus[1], k[1]), r, idx))
                else:
                    ordered.append(((k[0], 1, 0.0, k[1]), r, idx))
            ordered.sort()
        else:
            ordered = [(0, items[0][2], items[0][3])]
        for rank, (_k, _r, idx) in enumerate(ordered):
            if idx in ids:
                per.setdefault(idx, {})[key] = rank
    return per, counts


def _cuts(st: GlobalState, edges=None, cols=None) -> list[Cut]:
    """Named cuts of every overflowed border / via column / forced crossing of the current usage, worst first
    (``edges`` / ``cols``: the lists of an ``overflow()`` call just made, to save a second one)."""
    if edges is None or cols is None:
        _tot, edges, cols = st.overflow()
    xn = list(st.xruns)
    if not edges and not cols and not xn:
        return []
    G, nx = st.G, st.nx
    hot = {code for _o, code in edges}
    hotc = {col for _o, col in cols}
    dem, vdem = {}, {}
    for idx in sorted(st.paths):
        c = st.conns[idx]
        p = st.paths[idx]
        free = st.free[idx]
        a = p[0]
        for b in p[1:]:
            lo, df = (a, b - a) if a < b else (b, a - b)
            if df >= G:
                if lo % G in hotc:
                    vdem.setdefault(lo % G, []).append(c.net)
            else:
                code = lo * 2 + (1 if df == nx else 0)
                if code in hot and code not in free:
                    dem.setdefault(code, []).append((c.net, st.dem[idx]))
            a = b
    out = []
    for o, code in edges:
        l, tx, ty, side = st.tiles.edge_of(code)
        out.append((-o, code, st.tiles.cut_for_edge(l, tx, ty, side, dem.get(code, []))))
    pv = st.tiles.via_size + st.tiles.ref_cl
    for o, col in cols:
        tx, ty = col % nx, col // nx
        name = f"tile({tx},{ty})|via"
        out.append((-o * pv, st.N * 2 + col, Cut(a=name, b=name, layer="via", capacity_mm=round(st._via_cap(col) * pv, 6),
                                                demand_mm=round(st.useV[col] * pv, 6),
                                                nets=sorted(set(vdem.get(col, []))))))
    for k, (o, run, ip, iq) in enumerate(xn):
        l, r = divmod(run[0], G)
        tx, ty = r % nx, r // nx
        name = f"tile({tx},{ty})|cross"
        dd = (st.dem[ip], st.dem[iq])
        out.append((-o, st.N * 3 + k, Cut(a=name, b=name, layer=st.tiles.board.layers[l],
                                          capacity_mm=round(max(dd), 6), demand_mm=round(dd[0] + dd[1], 6),
                                          nets=sorted({st.conns[ip].net, st.conns[iq].net}))))
    out.sort(key=lambda t: (t[0], t[1]))
    return [c for _o, _k, c in out]


def _book_reservations(ctx: Ctx, tiles: Tiles) -> int:
    """Reservations take capacity: protected copper already placed (dog-bones, escape stubs: stage 'power' /
    'fanout') and soft shapes of the oracle (thermal-spoke corridors, reserved sites) are booked once per
    ``Tiles`` — unless the stage that made them already did (``tiles.reservations_booked``)."""
    if tiles.reservations_booked:
        return 0
    tiles.reservations_booked = True
    n = 0
    orc = tiles.oracle
    pbook = (ctx.history.get("power") or {}).get("book") if isinstance(ctx.history.get("power"), dict) else None
    by_power = isinstance(pbook, dict) and bool(pbook.get("booked"))     # power.book() already ran on these tiles
    if isinstance(pbook, dict):
        pbook["booked"] = True                        # ... and must not run after this
    pieces = []
    if ctx.state is not None:
        try:
            pieces = list(ctx.state.pieces())
        except Exception:  # noqa: BLE001  (pieces.py may still be a stub)
            pieces = []
    for p in sorted(pieces, key=lambda p: p.id):
        if not (p.protected or p.stage in ("power", "fanout")):
            continue
        if by_power and p.stage == "power":
            continue
        if p.kind == "via":
            tiles.book_via(*tiles.tile_of(p.x1, p.y1))
            n += 1
            continue
        d = p.w + orc.clearance(p.net)
        for tx, ty, side, _pos in tiles_mod._crossings(tiles, p.x1, p.y1, p.x2, p.y2):
            tiles.book(p.layer, tx, ty, "N" if side else "E", d)
            n += 1
    bb = (tiles.x0, tiles.y0, tiles.x0 + tiles.nx * tiles.t, tiles.y0 + tiles.ny * tiles.t)
    seen = set()
    for l in range(tiles.nl):
        try:
            soft = orc.near_soft(l, bb, 1.0)
        except Exception:  # noqa: BLE001
            soft = []
        for sh in ([] if by_power else soft):
            if sh.tag[0] == "reserve" and len(sh.layers) > 1:
                if sh.tag[1] not in seen:              # a reserved via site: one via of its column
                    seen.add(sh.tag[1])
                    tiles.book_via(*tiles.tile_of((sh.bbox[0] + sh.bbox[2]) / 2, (sh.bbox[1] + sh.bbox[3]) / 2))
                    n += 1
                continue
            n += tiles.book_shape(l, sh)
    return n


def _apply_regions(tiles: Tiles, x) -> int:
    if tiles.regions_scaled:
        return 0
    tiles.regions_scaled = True
    n = 0
    for r in _xget(x, "regions", []) or []:
        f = _field(r, "capacity_scale")
        rect = _field(r, "rect")
        if f is None or rect is None or f == 1.0 or _field(r, "nets"):
            continue                                  # (a border has one capacity: per-net scales are not expressible)
        lays = _field(r, "layers")
        if lays is not None:
            lays = [l if isinstance(l, int) else tiles.board.layers.index(l) for l in lays
                    if isinstance(l, int) or l in tiles.board.layers]
        n += tiles.scale_rect(rect, float(f), lays)
    return n


def run(ctx: Ctx, budget: Budget, only: list[int] | None = None) -> StageReport:
    """S3: global routing of ``ctx.conns`` (or re-routing of the connections ``only`` after capacity feedback).

    Builds ``ctx.tiles`` when the engine has none, negotiates to convergence within ``budget`` (work units, one
    per tile expansion), assigns layers and fills ``ctx.corridors``. Connections already routed in ``ctx.state``
    are left alone. Residual overflow is reported as named cuts in ``report.data['cuts']``."""
    used0 = budget.used
    x, cfg, board = ctx.x, ctx.cfg, ctx.board
    notes = []
    conns = list(ctx.conns)
    if ctx.state is not None:
        try:
            conns = [c for c in conns if ctx.state.status(c.idx) != "routed"]
        except Exception:  # noqa: BLE001
            pass
    # a pad-to-pour connection ("open" terminal b: no pad, no cluster, no layers, the convention of classify /
    # pieces.is_open) has no tile path: its dog-bone is a reservation (S1) and the pours pass closes it
    n_open = len(conns)
    conns = [c for c in conns if not (c.b.pad is None and not c.b.cluster and not c.b.layers)]
    n_open -= len(conns)
    if ctx.tiles is None:
        ctx.tiles = tiles_mod.make(board, ctx.oracle, conns, _xget(x, "tile_mm"))
    tiles = ctx.tiles
    booked = _book_reservations(ctx, tiles)
    scaled = _apply_regions(tiles, x)
    st = ctx.history.get("global_state")
    fresh = only is None or not isinstance(st, GlobalState) or st.tiles is not tiles
    if fresh:
        dirs = {}
        plan_dirs = any(getattr(nc, "layer_dir", None) for nc in (getattr(cfg, "nets", None) or {}).values())
        if not plan_dirs:
            dirs = layer_directions(board, conns, tiles, _signal_layers(cfg, conns, tiles.nl))
        st = GlobalState(tiles, conns, cfg, x, dirs)
        ctx.history["global_state"] = st
        ids = [c.idx for c in conns]
    else:
        st.add_conns([c for c in conns if c.idx not in st.conns])
        want = set(only or ())
        ids = [c.idx for c in conns if c.idx in want]
    auto = getattr(x, "auto", None)
    if isinstance(auto, dict) and st.dirs:
        auto["layer_dirs"] = {board.layers[l]: d for l, d in sorted(st.dirs.items())}
    iters = int(_xget(x, "negotiation_iters", MAX_ITERS) or MAX_ITERS)
    info = st.negotiate(ids, budget, max(1, min(iters, 200)))
    # layer assignment, in rank order, against the final congestion picture
    # (a connection that ends on another one's path is assigned after its host, so its end layer is the host's)
    relayered = 0
    idset = set(ids)

    def depth(i):
        n = 0
        while st.end_on.get(i, -1) >= 0 and n < 64:
            i = st.end_on[i]
            n += 1
        return n

    for _d, _r, idx in sorted((depth(c.idx), c.rank, c.idx) for c in st.order if c.idx in idset and c.idx in st.paths):
        if st.relayer(idx):
            relayered += 1
    tot, edges, cols = st.overflow()
    tiles.set_usage(st.useE, st.useN, st.useV)
    # per-connection overflow, corridors
    G, nx, ny = st.G, st.nx, st.ny
    over_e = {code: o for o, code in edges}
    over_c = {col: o for o, col in cols}
    over_x = {}
    for o, _run, ip, iq in st.xruns:
        over_x[ip] = over_x.get(ip, 0.0) + o
        over_x[iq] = over_x.get(iq, 0.0) + o
    pv = tiles.via_size + tiles.ref_cl
    lateral, crossings = _lateral(st, idset)
    hist = ctx.history.setdefault("global", {"path": {}, "layer": {}, "tee": {}})
    hist.setdefault("path", {})
    hist.setdefault("layer", {})
    hist.setdefault("tee", {})
    routed = failed = 0
    for c in st.order:
        if c.idx not in idset:
            continue
        p = st.paths.get(c.idx)
        if p is None:
            failed += 1
            ctx.corridors.pop(c.idx, None)
            for k in ("path", "layer", "tee"):
                hist[k].pop(c.idx, None)
            continue
        routed += 1
        path2d, layer, seq = st.layered(c.idx)
        ov = over_x.get(c.idx, 0.0)
        free = st.free[c.idx]
        a = p[0]
        for b in p[1:]:
            lo, df = (a, b - a) if a < b else (b, a - b)
            if df >= G:
                ov += over_c.get(lo % G, 0) * pv
            else:
                code = lo * 2 + (1 if df == nx else 0)
                if code not in free:
                    ov += over_e.get(code, 0.0)
            a = b
        ts = set()
        for tx, ty in path2d:
            for j in range(max(0, ty - 1), min(ny, ty + 2)):
                for i in range(max(0, tx - 1), min(nx, tx + 2)):
                    ts.add((i, j))
        cor = Corridor(conn=c.idx, tiles=frozenset(ts), layers=tuple(sorted({n // G for n in p})), layer_seq=seq,
                       lateral=lateral.get(c.idx, {}), overflow=round(ov, 6))
        cor.path = tuple(path2d)                      # extra attribute (not in types.Corridor): the ordered tile path
        ctx.corridors[c.idx] = cor
        hist["path"][c.idx] = list(path2d)
        hist["layer"][c.idx] = list(layer)
        j = st.end_on.get(c.idx, -1)
        if j >= 0:
            hist["tee"][c.idx] = j
        else:
            hist["tee"].pop(c.idx, None)
    cuts = _cuts(st, edges, cols)
    hist.update(cuts=cuts, crossings=crossings, overflow_mm=round(tot, 6), tile_mm=tiles.t,
                layer_dirs=dict(st.dirs), advisory=bool(tiles.advisory))
    if info["out_of_budget"]:
        notes.append(f"global routing ran out of budget after {info['iterations']} iteration(s): "
                     f"{failed} connection(s) have no corridor (detail falls back to a box)")
    for idx in sorted(st.unrouted):
        if idx in idset and st.unrouted[idx] != "time budget":
            c = st.conns[idx]
            notes.append(f"{c.net} ({c.a.pad or 'a'}→{c.b.pad or 'b'}): {st.unrouted[idx]}")
            if len(notes) > 12:
                break
    if tot > 1e-6:
        notes.append(f"residual overflow {tot:.2f} mm on {len(edges)} border(s), {len(cols)} via column(s) and "
                     f"{len(st.xruns)} same-layer crossing(s) after {info['iterations']} iteration(s): see cuts")
    if tiles.advisory:
        notes.append("tile capacities did not pass the calibration gate: corridors are advisory (cost, not mask)")
    if n_open:
        notes.append(f"{n_open} pad-to-pour connection(s) have no corridor (reserved in S1, closed by the pours pass)")
    score = Score()
    if ctx.state is not None:
        try:
            score = ctx.state.score()
        except Exception:  # noqa: BLE001
            pass
    rep = StageReport(name="global", routed=routed, failed=failed, score=score, work=budget.used - used0, notes=notes,
                      data={"iterations": info["iterations"], "overflow_mm": round(tot, 6),
                            "overflow_first_mm": info["overflow_first_mm"], "wirelength_mm": info["wirelength_mm"],
                            "expansions": st.expansions, "searches": st.searches, "relayered": relayered,
                            "crossings_left": len(st.xruns), "open_skipped": n_open,
                            "tile_mm": tiles.t, "nx": tiles.nx, "ny": tiles.ny, "layers": tiles.nl,
                            "cuts": [vars(c) for c in cuts], "vias": sum(len(ctx.corridors[i].layer_seq)
                                                                         for i in ctx.corridors if i in idset),
                            "layer_dirs": {board.layers[l]: d for l, d in sorted(st.dirs.items())},
                            "booked": booked, "regions_scaled": scaled, "advisory": bool(tiles.advisory),
                            "out_of_budget": info["out_of_budget"]})
    return rep
