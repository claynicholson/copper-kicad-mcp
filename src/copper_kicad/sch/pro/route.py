"""Bounded orthogonal grid router for wires inside one functional block, plus the geometric pre-check.

Grid: 1.27 mm cells. A* over (cell, heading). Every rule below is a way KiCad would otherwise silently connect:
C1 symbol bodies, field texts, power symbols and pin lengths are blocked;  C2 a pin tip is only ever a target;
C3 no collinear overlap with another wire;  C4 a foreign wire is crossed perpendicular only, never at one of its
end points / corners / junctions, and the route runs straight through;  C6 label anchors count as such nodes;
C7 label boxes reserved for other nets are blocked.
"""
from __future__ import annotations

import heapq
import itertools
import math
import re
import time

from .. import ops as O

U = 1.27
WINDOW = 20            # search window: bounding box of both wire groups + this many cells
BUDGET = 40000         # node expansions per route
MAX_SPAN = 26          # cells; named nets further apart than this stay on labels ("pro"; "pro2": the block diagonal)
BEND_COST = 8          # "pro2" costs (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md 5.2): 1 per cell, +8 per bend, +20 per crossing
CROSS_COST = 20


def bus_prefix(name: str) -> str:
    """'QSPI' for QSPI_SD0 / QSPI_SS / QSPI3 - the part of a net name that the members of a bus share ('' = none)."""
    pre = re.sub(r"(_[A-Za-z]*\d*|\d+)$", "", name or "")
    return pre if pre and pre != name else ""


def cell(p):
    return (int(round(p[0] / U)), int(round(p[1] / U)))


def seg_cells(a, b):
    ca, cb = cell(a), cell(b)
    if ca[1] == cb[1]:
        return [(i, ca[1]) for i in range(min(ca[0], cb[0]), max(ca[0], cb[0]) + 1)]
    return [(ca[0], j) for j in range(min(ca[1], cb[1]), max(ca[1], cb[1]) + 1)]


class Router:
    DIRS = [(1, 0), (0, 1), (-1, 0), (0, -1)]

    def __init__(self, hard_boxes, resv, pin_tips, wires, bounds):
        self.blocked = set()
        self.bounds = bounds
        for b in hard_boxes:
            self._fill(b, self.blocked)
        self.resv = {}
        for net, b in resv:
            s = set()
            self._fill(b, s, 0.0)
            for c in s:
                self.resv.setdefault(c, set()).add(net)
        self.pin = pin_tips                      # cell -> net or None
        self.h, self.v, self.node = {}, {}, {}   # cell -> net
        for n, a, b in wires:
            self.add(n, a, b)
        self.stats = {"routes": 0, "ok": 0, "expanded": 0, "crossings": 0, "time": 0.0, "rejected": 0}
        self.v2 = False                          # pro2 rules: bend / crossing costs, T-junctions only, crossings
        #                                          at least 2.54 mm away from every junction, wire end and pin tip
        self.xings = {c for c, n in self.h.items() if self.v.get(c) not in (None, n) and self.node.get(c) is None}

    def _arms(self, c, net) -> int:
        """How many of the four directions already carry a wire of `net` at cell c."""
        x, y = c
        n = 0
        if self.h.get(c) == net:
            n += (self.h.get((x - 1, y)) == net) + (self.h.get((x + 1, y)) == net)
        if self.v.get(c) == net:
            n += (self.v.get((x, y - 1)) == net) + (self.v.get((x, y + 1)) == net)
        return n

    @staticmethod
    def _near(c, cells) -> bool:
        x, y = c
        return any((x + i, y + j) in cells for i in (-1, 0, 1) for j in (-1, 0, 1))

    def _tee_ok(self, c, net) -> bool:
        """May a wire of `net` end on cell c of its own net? Not as the fourth arm of a junction, and not right
        next to a crossing (the new junction dot would sit beside it)."""
        return self._arms(c, net) < 3 and not self._near(c, self.xings) and c not in getattr(self, "no_tee", ())

    def _fill(self, b, s, pad=0.3):
        x0, x1 = math.ceil((b[0] - pad) / U), math.floor((b[2] + pad) / U)
        y0, y1 = math.ceil((b[1] - pad) / U), math.floor((b[3] + pad) / U)
        for i in range(x0, x1 + 1):
            for j in range(y0, y1 + 1):
                s.add((i, j))

    def add(self, n, a, b):
        ca, cb = cell(a), cell(b)
        self.node[ca] = n
        self.node[cb] = n
        if ca[1] == cb[1]:
            for i in range(min(ca[0], cb[0]), max(ca[0], cb[0]) + 1):
                self.h[(i, ca[1])] = n
        else:
            for j in range(min(ca[1], cb[1]), max(ca[1], cb[1]) + 1):
                self.v[(ca[0], j)] = n

    def route(self, net, src: set, dst: set, max_cost, max_bends=None, max_cross=None, accept=None):
        t0 = time.time()
        self.stats["routes"] += 1
        dx0 = min(c[0] for c in dst)
        dx1 = max(c[0] for c in dst)
        dy0 = min(c[1] for c in dst)
        dy1 = max(c[1] for c in dst)
        xs = [c[0] for c in src] + [dx0, dx1]
        ys = [c[1] for c in src] + [dy0, dy1]
        bx0, by0 = max(self.bounds[0], min(xs) - WINDOW), max(self.bounds[1], min(ys) - WINDOW)
        bx1, by1 = min(self.bounds[2], max(xs) + WINDOW), min(self.bounds[3], max(ys) + WINDOW)

        def hh(c):
            return max(dx0 - c[0], 0, c[0] - dx1) + max(dy0 - c[1], 0, c[1] - dy1)
        n_exp = 0
        pq, best, prev = [], {}, {}
        for c in sorted(src):
            if self.pin.get(c, 0) != 0 and len(src) > 1:
                continue
            if self.v2 and not self._tee_ok(c, net):
                continue
            for di in range(4):
                best[(c, di)] = 0
                heapq.heappush(pq, (hh(c), 0, c, di, False))
        goal = None
        while pq:
            _f, cost, c, di, crossing = heapq.heappop(pq)
            if best.get((c, di), 1e9) < cost:
                continue
            n_exp += 1
            if n_exp > BUDGET:
                break
            if c in dst and not crossing and c not in src:
                goal = (c, di)
                break
            if cost > max_cost:
                break
            for nd in range(4):
                if crossing and nd != di:
                    continue
                if (nd + 2) % 4 == di and cost > 0:
                    continue
                dx, dy = self.DIRS[nd]
                c2 = (c[0] + dx, c[1] + dy)
                if not (bx0 <= c2[0] <= bx1 and by0 <= c2[1] <= by1):
                    continue
                is_dst = c2 in dst
                if c2 in self.blocked and not is_dst:
                    continue
                if self.pin.get(c2, 0) != 0 and not is_dst:
                    continue
                rv = self.resv.get(c2)
                if rv and net not in rv and not is_dst:
                    continue
                hn, vn = self.h.get(c2), self.v.get(c2)
                cross = False
                step = 1
                if is_dst:
                    # arriving on the target wire: only its own net may be there, and never along a foreign wire
                    if (hn not in (None, net)) or (vn not in (None, net)):
                        continue
                    if self.v2 and not self._tee_ok(c2, net):
                        continue
                else:
                    par, perp = (hn, vn) if dx else (vn, hn)
                    if par is not None:
                        continue
                    if perp is not None:
                        if self.node.get(c2) is not None:
                            continue
                        if self.v2 and (self._near(c2, self.node) or self._near(c2, self.pin)):
                            continue
                        cross = True
                        step += CROSS_COST if self.v2 else 12
                if nd != di and cost > 0:
                    step += BEND_COST if self.v2 else 3
                side = [(c2[0] + dy, c2[1] + dx), (c2[0] - dy, c2[1] - dx)]
                if any(((self.h.get(s) if dx else self.v.get(s)) not in (None, net)) for s in side):
                    step += 2
                nc = cost + step
                if nc < best.get((c2, nd), 1e9):
                    best[(c2, nd)] = nc
                    prev[(c2, nd)] = (c, di)
                    heapq.heappush(pq, (nc + hh(c2), nc, c2, nd, cross))
        self.stats["expanded"] += n_exp
        self.stats["time"] += time.time() - t0
        if goal is None:
            return None
        path = [goal[0]]
        k = goal
        while k in prev:
            k = prev[k]
            path.append(k[0])
        path.reverse()
        segs = []
        s0 = path[0]
        for i in range(1, len(path)):
            if i == len(path) - 1 or (path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1]) != (
                    path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]):
                segs.append((s0, path[i]))
                s0 = path[i]
        crossed = []
        for c in path[1:-1]:
            hn, vn = self.h.get(c), self.v.get(c)
            if (hn not in (None, net)) or (vn not in (None, net)):
                crossed.append(c)
        if (max_bends is not None and len(segs) - 1 > max_bends) or (max_cross is not None and len(crossed) > max_cross):
            self.stats["rejected"] += 1
            return None
        if self.v2 and (self._near(path[0], self.xings) or self._near(path[-1], self.xings)
                        or any(self._near(c, [path[0], path[-1]]) for c in crossed)):
            self.stats["rejected"] += 1
            return None
        if accept is not None and not accept(segs, crossed):
            self.stats["rejected"] += 1
            return None
        self.stats["ok"] += 1
        self.stats["crossings"] += len(crossed)
        self.xings.update(crossed)
        out = []
        for a, b in segs:
            pa, pb = (round(a[0] * U, 4), round(a[1] * U, 4)), (round(b[0] * U, 4), round(b[1] * U, 4))
            self.add(net, pa, pb)
            out.append((net, pa, pb))
        return out


    def snapshot(self):
        return (dict(self.h), dict(self.v), dict(self.node), set(self.xings), dict(self.stats))

    def restore(self, snap):
        self.h, self.v, self.node, self.xings = dict(snap[0]), dict(snap[1]), dict(snap[2]), set(snap[3])
        self.stats.update({k: snap[4][k] for k in ("ok", "crossings")})


def components(wires, net, terms):
    """Wire-connected components of one net: [(cells, [terms])], deterministic order."""
    par: dict = {}

    def find(a):
        par.setdefault(a, a)
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    for n, a, b in wires:
        if n != net:
            continue
        seq = seg_cells(a, b)
        for c in seq:
            ra, rb = find(c), find(seq[0])
            if ra != rb:
                par[max(ra, rb)] = min(ra, rb)
    groups: dict = {}
    for c in list(par):
        groups.setdefault(find(c), set()).add(c)
    return [(cs, [t for t in terms if t[0] == net and cell(t[1]) in cs]) for _, cs in sorted(groups.items())]


def route_nets(R: Router, wires, terms, nets, blk_of_term, must: set, cross_block=False, skip_pair=None, span=None,
               limits=None, tight=None):
    """Join the wire groups of each net inside one block (closest pairs first). Nets in `must` (no name on the
    sheet: they cannot fall back to labels) are also joined across blocks. Returns #routed.
    span = how far apart (cells) two groups of a NAMED net may be and still get a wire: a number, or
    f(set of block indices) (pro2: the block diagonal); limits = (max bends, max added crossings) for named nets;
    tight(cells a, cells b) -> True when a wire between these two groups may not go AROUND anything (see WRAP)."""
    done_n = 0
    max_bends, max_cross = limits or (None, None)
    for net in nets:
        failed = set()
        while True:
            comps = components(wires, net, terms)
            if len(comps) < 2:
                break
            cands = []
            for i, j in itertools.combinations(range(len(comps)), 2):
                bi = {blk_of_term.get((t[0], t[1])) for t in comps[i][1]}
                bj = {blk_of_term.get((t[0], t[1])) for t in comps[j][1]}
                same = bool(bi & bj)
                if not same and net not in must and not cross_block:
                    continue
                if skip_pair is not None and net not in must and skip_pair(comps[i][0], comps[j][0]):
                    continue
                ci, cj = comps[i][0], comps[j][0]
                if len(ci) * len(cj) > 4000:
                    bi_ = (min(c[0] for c in ci), min(c[1] for c in ci), max(c[0] for c in ci), max(c[1] for c in ci))
                    bj_ = (min(c[0] for c in cj), min(c[1] for c in cj), max(c[0] for c in cj), max(c[1] for c in cj))
                    dmin = max(bi_[0] - bj_[2], bj_[0] - bi_[2], 0) + max(bi_[1] - bj_[3], bj_[1] - bi_[3], 0)
                else:
                    dmin = min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a in ci for b in cj)
                sp = MAX_SPAN if span is None else span(bi | bj) if callable(span) else span
                cands.append((dmin, not same, i, j, sp))
            cands.sort()
            ok = False
            for dmin, other, i, j, sp in cands:
                key = (frozenset(comps[i][0]), frozenset(comps[j][0]))
                if key in failed:
                    continue
                if net not in must and dmin > sp:
                    continue                      # far apart and named: two labels read better than a long wire
                if R.v2 and net not in must:
                    # 1 per cell, +8 per bend, +20 per crossing: a named net gets a few bends and a crossing, then
                    # a label reads better (a net without a name has to be wired whatever it takes)
                    # ... and so does a pair of names when the wire would have to go AROUND something: the length
                    # allowance is 1.4 x the straight distance (a wire that wraps a part's body is a maze to trace)
                    acc = None
                    if tight is not None and tight(comps[i][0], comps[j][0]):
                        lim = dmin + WRAP

                        def acc(segs_, _crossed, lim=lim):
                            if TIGHT_MAX_BENDS is not None and len(segs_) - 1 > TIGHT_MAX_BENDS:
                                # (package B) ... and a dogleg between an IC and a header (two bends, three of
                                # them side by side for a little bus) is two names as well: a wire between two
                                # multi-pin parts runs straight or turns once
                                return False
                            if TIGHT_NO_CROSS and _crossed:
                                # (package B) a programming header whose pin order is the other way round than the
                                # IC's: the wire that would have to cross its neighbour is two names instead
                                return False
                            return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segs_) <= lim
                    elif LOOSE is not None:
                        # (fix round 3) every other named wire: the bend / crossing allowance below may not be spent
                        # on LENGTH either - a wire that walks around the block's edge to a switch is a perimeter
                        # detour; beyond this both ends carry the name
                        lim = DETOUR * dmin + LOOSE

                        def acc(segs_, _crossed, lim=lim):
                            return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segs_) <= lim
                    segs = R.route(net, comps[i][0], comps[j][0], DETOUR * dmin + (max_bends or 3) * BEND_COST
                                   + (max_cross or 0) * CROSS_COST + 10, max_bends, max_cross, accept=acc)
                else:
                    cap = (3 * dmin + 40) if net in must else (2.0 * dmin + 22)
                    if R.v2:
                        cap += 6 * BEND_COST + 3 * CROSS_COST
                    segs = R.route(net, comps[i][0], comps[j][0], cap)
                if segs:
                    wires += segs
                    done_n += 1
                    ok = True
                    break
                failed.add(key)
            if not ok:
                break
    return done_n


# fix round 2: the cost allowance above also pays for bends and crossings, so a wire with two bends could spend all
# of it on LENGTH (a reset line that drops under the crystal and comes back up, MISO over two supply symbols). Between
# two multi-pin parts such a wire may be at most WRAP cells longer than the straight way (one row over and back);
# beyond that both ends carry the name.
WRAP = 4
TIGHT_MAX_BENDS = 1       # package B: ... and has at most one bend (None = no limit)
TIGHT_NO_CROSS = True     # package B: a named wire between two multi-pin parts is not drawn across another wire
LOOSE = 8                 # cells a named wire between any two groups may be longer than DETOUR x the straight way
DETOUR = 1.4              # pro2, named nets: longest wire as a multiple of the straight (Manhattan) distance


# cross-block wiring thresholds (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md 5.1)
X_MAX_LEN_MM = 60.0
X_MAX_BENDS = 4
X_MAX_CROSS = 2
X_MAX_TERMS = 4
X_MAX_COST = 150.0        # length in mm + 8 per bend + 20 per crossing
BUS_MIN = 8               # a bundle this wide is wired completely or stays a pair of label columns


def cross_cost(segs, crossed) -> tuple:
    """(length mm, bends, crossings, cost) of a route given as cell segments."""
    ln = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segs) * U
    bends = max(0, len(segs) - 1)
    return ln, bends, len(crossed), ln + 8 * bends + 20 * len(crossed)


def _cross_ok(segs, crossed) -> bool:
    ln, bends, nx, cost = cross_cost(segs, crossed)
    return ln <= X_MAX_LEN_MM + 0.01 and bends <= X_MAX_BENDS and nx <= X_MAX_CROSS and cost <= X_MAX_COST + 0.01


def _join_cross(R: Router, wires, terms, net, blk_of_term, adjacent) -> bool:
    """Join the wire groups of one named net that lie in DIFFERENT blocks, nearest pair first, as long as the wire
    stays within the thresholds. True when the net is in one piece afterwards."""
    failed = set()
    cap = int(X_MAX_LEN_MM / U) * 3 + X_MAX_BENDS * BEND_COST + X_MAX_CROSS * CROSS_COST
    while True:
        comps = [c for c in components(wires, net, terms)]
        real = [c for c in comps if c[1]]
        if len(real) < 2:
            return True
        cands = []
        for i, j in itertools.combinations(range(len(comps)), 2):
            if not comps[i][1] or not comps[j][1]:
                continue
            bi = {blk_of_term.get((t[0], t[1])) for t in comps[i][1]}
            bj = {blk_of_term.get((t[0], t[1])) for t in comps[j][1]}
            if bi & bj or not all(adjacent(a, b) for a in bi for b in bj):
                continue
            ci, cj = comps[i][0], comps[j][0]
            if len(ci) * len(cj) > 6000:
                continue
            dmin = min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a in ci for b in cj)
            if dmin * U > X_MAX_LEN_MM:
                continue
            cands.append((dmin, i, j))
        ok = False
        for dmin, i, j in sorted(cands):
            key = (frozenset(comps[i][0]), frozenset(comps[j][0]))
            if key in failed:
                continue
            segs = R.route(net, comps[i][0], comps[j][0], cap, X_MAX_BENDS, X_MAX_CROSS, accept=_cross_ok)
            if segs:
                wires += segs
                ok = True
                break
            failed.add(key)
        if not ok:
            return False


def route_cross(R: Router, wires, terms, nets, blk_of_term, adjacent, nterms) -> list:
    """Cross-block wiring by cost. `nets` = named signal nets; one that is still split over blocks which stand in
    the same or in neighbouring columns (`adjacent(block a, block b)`) and has at most X_MAX_TERMS pins
    (`nterms(net)`) is wired when the route stays within X_MAX_LEN_MM / X_MAX_BENDS / X_MAX_CROSS / X_MAX_COST.
    Nets between the same blocks form a bundle: they are routed one after the other in pin-row order (parallel
    tracks that do not cross), and a bundle of BUS_MIN or more is wired completely or not at all.
    Returns the nets that are in one piece because of it."""
    reqs: dict = {}
    for net in nets:
        if nterms(net) > X_MAX_TERMS:
            continue
        comps = [c for c in components(wires, net, terms) if c[1]]
        if len(comps) < 2:
            continue
        bl = frozenset(b for c in comps for b in {blk_of_term.get((t[0], t[1])) for t in c[1]} if b is not None)
        if len(bl) < 2:
            continue
        y = min(t[1][1] for c in comps for t in c[1])
        reqs.setdefault(bl, []).append((y, str(net), net))
    done = []
    for bl in sorted(reqs, key=lambda g: (min(reqs[g])[:2], sorted(g))):
        members = sorted(reqs[bl])
        snap, nw = R.snapshot(), len(wires)
        ok = [net for _y, _s, net in members if _join_cross(R, wires, terms, net, blk_of_term, adjacent)]
        if len(members) >= BUS_MIN and len(ok) < len(members):
            R.restore(snap)
            del wires[nw:]
            ok = []
        done += ok
    return done


def verify(sch, D) -> list[str]:
    """Geometric pre-check with KiCad's joining rules: every wire group must hold pins of ONE net, and a net
    without a name on this sheet must be in one piece. Names the offender (the kicad-cli gate only says 'differs')."""
    errs = []
    seen: dict = {}
    for g in O.geo_groups(sch):
        nets = sorted({D.view.pin_net.get(p) or f"?{p[0]}.{p[1]}" for p in g["pins"]})
        if len(nets) > 1:
            errs.append(f"short between {' and '.join(nets[:3])}")
        if len(nets) == 1 and not nets[0].startswith("?"):
            k = nets[0]
            named = bool(g["labels"] or g["power"])
            seen.setdefault(k, []).append(named)
    for k, parts in seen.items():
        if len(parts) > 1 and not all(parts):
            errs.append(f"net {k} is split into {len(parts)} pieces and has no name to join them")
    for k, ln in D.view.nets.items():
        if D.conn(k) and k not in seen and ln.pins:
            errs.append(f"net {k} lost its pins")
    return errs


# ───────────────────────── union-find pre-gate (pure Python, no kicad-cli) ─────────────────────────
def _pk(p):
    return (int(round(float(p[0]) * 100)), int(round(float(p[1]) * 100)))


def partition(sch, strict: bool = False) -> dict:
    """Connectivity of ONE sheet from its geometry alone, the way KiCad joins things.

    Items are joined by coincident points (wire ends, pin tips, label anchors, power-symbol pins), by junctions
    (which join every wire through their point), by labels sitting anywhere on a wire, and then by NAME (labels of
    one text and kind; power symbols of one value).  Where KiCad's behaviour depends on the version - a pin tip or a
    wire end that touches the INTERIOR of another wire without a junction - `strict=False` joins them and
    `strict=True` does not: a drawing is only unambiguous when both readings give the same answer.

    -> {'classes': {class id: {'pins': {(ref, pin)}, 'names': {(text, kind)}, 'power': {value}, 'flags': n}},
        'pin_class': {(ref, pin): class id}, 'nc': {(ref, pin)}}"""
    from ... import sexpr
    par: dict = {}

    def find(a):
        par.setdefault(a, a)
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            if repr(ra) < repr(rb):
                ra, rb = rb, ra
            par[ra] = rb
    horiz: dict = {}
    vert: dict = {}
    diag = []
    wires = sch.wires()
    for w in wires:
        ka, kb = _pk(w["a"]), _pk(w["b"])
        union(("p", ka), ("p", kb))
        if ka[1] == kb[1]:
            horiz.setdefault(ka[1], []).append((min(ka[0], kb[0]), max(ka[0], kb[0]), ka))
        elif ka[0] == kb[0]:
            vert.setdefault(ka[0], []).append((min(ka[1], kb[1]), max(ka[1], kb[1]), ka))
        else:
            diag.append((ka, kb))

    def through(k, ends: bool):
        """Wires whose interior (or, with `ends`, any point) holds k -> one end key of each."""
        out = []
        for lo, hi, kw in horiz.get(k[1], ()):
            if lo < k[0] < hi or (ends and lo <= k[0] <= hi):
                out.append(kw)
        for lo, hi, kw in vert.get(k[0], ()):
            if lo < k[1] < hi or (ends and lo <= k[1] <= hi):
                out.append(kw)
        for ka, kb in diag:
            cr = (kb[0] - ka[0]) * (k[1] - ka[1]) - (kb[1] - ka[1]) * (k[0] - ka[0])
            if cr == 0 and min(ka[0], kb[0]) <= k[0] <= max(ka[0], kb[0]) \
                    and min(ka[1], kb[1]) <= k[1] <= max(ka[1], kb[1]) and (ends or k not in (ka, kb)):
                out.append(ka)
        return out

    def touch(k, always: bool):
        """A point item at k is joined to the wires it lies on (their interiors only when `always` or not strict)."""
        if always or not strict:
            for kw in through(k, False):
                union(("p", k), ("p", kw))
    for j in sch.items("junction"):
        a = sexpr.find(j, "at")
        k = _pk((a[1], a[2]))
        for kw in through(k, True):
            union(("p", k), ("p", kw))
    for w in wires:
        touch(_pk(w["a"]), False)
        touch(_pk(w["b"]), False)
    labels = sch.labels()
    for l in labels:
        touch(_pk(l["at"]), True)
    pins, powers, flags = [], [], []
    from .gate import redundant_nc
    dead = set()                                       # hidden pins of type no_connect: a cross there is not kept
    for s in sch.symbols():
        pw = s.pins_world()
        if s.is_power:
            for q in pw[:1]:
                k = _pk(q["xy"])
                touch(k, False)
                (flags if O.is_flag(s) else powers).append((k, s.value))
        else:
            for q in pw:
                k = _pk(q["xy"])
                touch(k, False)
                pins.append((k, (s.ref, q["number"])))
                if redundant_nc(q):
                    dead.add((s.ref, q["number"]))
    for l in labels:                                   # names join: one text + kind is one net on the sheet
        union(("p", _pk(l["at"])), ("n", l["kind"], l["net"]))
    for k, v in powers:
        union(("p", k), ("n", "power", v))
    classes: dict = {}

    def cls(node):
        return classes.setdefault(find(node), {"pins": set(), "names": set(), "power": set(), "flags": 0})
    pin_class = {}
    for k, p in pins:
        cls(("p", k))["pins"].add(p)
        pin_class[p] = find(("p", k))
    for l in labels:
        cls(("p", _pk(l["at"])))["names"].add((l["net"], l["kind"]))
    for k, v in powers:
        cls(("p", k))["power"].add(v)
    for k, _v in flags:
        cls(("p", k))["flags"] += 1
    from .gate import nc_marked
    ncs = {_pk(q) for q in nc_marked(sch)}
    nc = {p for k, p in pins if k in ncs and p not in dead}
    return {"classes": classes, "pin_class": pin_class, "nc": nc}


def signature(sch, strict: bool = False) -> dict:
    """Layout-independent summary of partition(): what must be the same before and after a re-layout."""
    pt = partition(sch, strict)
    with_pins, loose = [], []
    for c in pt["classes"].values():
        row = (tuple(sorted(c["pins"])), tuple(sorted(c["names"])), tuple(sorted(c["power"])), c["flags"])
        if c["pins"]:
            with_pins.append(row)
        elif c["names"] or c["power"] or c["flags"]:
            loose.append(row[1:])
    return {"nets": sorted(with_pins), "loose": sorted(loose), "nc": sorted(pt["nc"])}


def verify_partition(sch, D, before: dict | None = None) -> list[str]:
    """Union-find pre-gate. The drawing must reproduce, under BOTH readings of KiCad's touching rules,
      (a) the pin -> net partition of `D`,  (b) the name inventory of every net,  (c) the PWR_FLAG count of every
      net,  (d) the set of no-connect pins.
    `before` = signature(original sheet): then the name-joined partition must also be exactly the one the sheet had
    (that makes hierarchical sheets checkable, where one netlist net may be several pieces on a sheet).
    Returns human-readable problems; [] = passes. It can only reject - the kicad-cli gate still decides."""
    errs: list[str] = []
    v = D.view

    def nm(k):
        return D.text(k) or str(k)
    for strict in (False, True):
        tag = "strict" if strict else "touching"
        pt = partition(sch, strict)
        by_net: dict = {}
        for c in pt["classes"].values():
            nets = sorted({v.pin_net.get(p) or f"?{p[0]}.{p[1]}" for p in c["pins"]})
            if len(nets) > 1:
                errs.append(f"[{tag}] short between {' and '.join(nm(n) for n in nets[:3])}")
                continue
            if nets:
                by_net.setdefault(nets[0], []).append(c)
        for k, ln in v.nets.items():
            cs = by_net.get(k, [])
            want_names = {(e[0], e[1]) for e in ln.names}
            got_names = set().union(*[c["names"] for c in cs]) if cs else set()
            if got_names != want_names:
                miss, extra = sorted(want_names - got_names), sorted(got_names - want_names)
                errs.append(f"[{tag}] net {nm(k)}: names differ" + (f", lost {[m[0] for m in miss]}" if miss else "")
                            + (f", gained {[m[0] for m in extra]}" if extra else ""))
            got_power = set().union(*[c["power"] for c in cs]) if cs else set()
            if got_power != set(ln.power):
                errs.append(f"[{tag}] net {nm(k)}: power symbols differ ({sorted(got_power)} vs {sorted(ln.power)})")
            nf = sum(c["flags"] for c in cs)
            if nf != ln.flags:
                errs.append(f"[{tag}] net {nm(k)}: {nf} PWR_FLAG instead of {ln.flags}")
            have = set().union(*[c["pins"] for c in cs]) if cs else set()
            if set(ln.pins) - have:
                errs.append(f"[{tag}] net {nm(k)} lost pins {sorted(set(ln.pins) - have)[:3]}")
            if before is None and len(cs) > 1 and len(ln.pins) > 1:
                errs.append(f"[{tag}] net {nm(k)} is split into {len(cs)} pieces")
        if pt["nc"] != set(v.nc):
            errs.append(f"[{tag}] no-connect pins differ: {sorted(pt['nc'] ^ set(v.nc))[:4]}")
        loose_f = sum(c["flags"] for c in pt["classes"].values() if not c["pins"])
        want_f = sum(o["flags"] for o in v.orphans)
        if loose_f != want_f:
            errs.append(f"[{tag}] {loose_f} PWR_FLAG on wires without pins instead of {want_f}")
        if before is not None:
            sg = signature(sch, strict)
            for key in ("nets", "loose", "nc"):
                if sg[key] != before[key]:
                    d = sorted(set(map(repr, before[key])) ^ set(map(repr, sg[key])))
                    errs.append(f"[{tag}] sheet {key} differ from the original: {d[0][:150] if d else ''}")
    out, seen = [], set()
    for e in errs:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out
