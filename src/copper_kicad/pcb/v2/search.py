"""v2-owned A* on a window grid (design §4.7); forked from ``guided/astar.py``, which stays untouched.

State = ``layer_index * G + cell`` (layer_index = position in ``win.layers``). Costs are in grid steps (1.0 = one
orthogonal step on an unbiased layer): callers divide millimetre prices (via cost, rip prices, history) by
``win.pitch``. Eight directions, through vias, any number of sources and targets.

Deterministic: heap entries are ``(f, g, seq, state)`` with a per-search sequence number; nothing depends on
hashing, time or randomness. Work is charged to the ``Budget``: ``CHARGE_SEARCH`` per search started plus one
unit per expansion; a search never runs past ``min(limit, budget.left())``.
"""
from __future__ import annotations

from collections import deque
from heapq import heapify, heappop, heappush

from ..guided.astar import DIRS, SQ2, layer_moves
from .types import CHARGE_EXPANSION, CHARGE_SEARCH, Budget
from .window import MASKED, Window

OCT = SQ2 - 1.0
H_SAMPLES = 8


class Heur(list):
    """Per-cell heuristic (grid steps, already times hweight), filled lazily: an entry < 0 is not computed yet
    and ``fill(cell)`` computes, stores and returns it. A plain list of floats >= 0 works in its place."""
    __slots__ = ("fill", "samples", "weight")


def heuristic(win: Window, targets, tile_dist: dict | None = None, hweight: float = 1.0) -> list[float]:
    """Heuristic for ``search``: ``hweight x max(octile distance to the nearest of <= 8 sampled target cells,
    tile-level distance inside the corridor - T*sqrt(2))`` per 2-D cell, in grid steps.

    targets    search states (any layer); sampled evenly in sorted order.
    tile_dist  {(tx, ty): mm to the target tiles inside the corridor} (reverse Dijkstra over the corridor's
               tiles) or None. Needs a window built with a tiles mask.
    """
    g = win.grid
    G, W = g.G, g.W
    cells = sorted({t % G for t in targets})
    if len(cells) > H_SAMPLES:
        n = len(cells)
        cells = [cells[(k * (n - 1)) // (H_SAMPLES - 1)] for k in range(H_SAMPLES)]
    samples = tuple((c % W, c // W) for c in cells)
    h = Heur([-1.0])
    h *= G
    h.samples, h.weight = samples, hweight
    col, row, T, p = win.col_tile, win.row_tile, win.tile_t, win.pitch
    use_tiles = bool(tile_dist) and col is not None
    slack = T * SQ2
    one = samples[0] if len(samples) == 1 else None

    def fill(i):
        y = i // W
        x = i - y * W
        if one is not None:
            ax = x - one[0]
            ay = y - one[1]
            if ax < 0:
                ax = -ax
            if ay < 0:
                ay = -ay
            best = (ax + OCT * ay) if ax > ay else (ay + OCT * ax)
        else:
            best = 1e18
            for tx, ty in samples:
                ax = x - tx
                ay = y - ty
                if ax < 0:
                    ax = -ax
                if ay < 0:
                    ay = -ay
                d = (ax + OCT * ay) if ax > ay else (ay + OCT * ax)
                if d < best:
                    best = d
            if best > 1e17:
                best = 0.0
        if use_tiles:
            td = tile_dist.get((col[x], row[y]))
            if td is not None:
                td = (td - slack) / p
                if td > best:
                    best = td
        best *= hweight
        h[i] = best
        return best
    h.fill = fill
    return h


def default_moves(win: Window, layers_ok) -> tuple:
    """Uniform move tables, one per window layer (None for a layer not in ``layers_ok``)."""
    g = win.grid
    ok = {win.lidx[l] for l in layers_ok if l in win.lidx}
    return tuple(layer_moves(g.W, g.G, li, 1.0, 1.0, 1.0) if li in ok else None for li in range(len(win.layers)))


def _soft_get(soft_cost):
    if soft_cost is None:
        return None
    if isinstance(soft_cost, dict):
        default = soft_cost.get(None)
        get = soft_cost.get
        if default is None:
            return get
        return lambda pid: get(pid, default)
    c = float(soft_cost)
    return lambda pid: c


def search(win: Window, net_id: int, sources: dict, targets, *, h: list | None, turn, via_cost: float | None,
           layers_ok, cmul=None, hist=None, soft_cost=None, bend_state: bool = False, limit: int, budget: Budget,
           moves=None) -> tuple[list | None, dict]:
    """Cheapest path from any source state to any target state.

    sources    {state: initial cost in steps}
    targets    set / dict of states
    h          ``heuristic(...)`` result, or None for Dijkstra
    turn       ``guided.astar.turn_table(bend)``
    via_cost   steps per through via; None = no layer changes
    layers_ok  board layers the path may use
    cmul       per-state cost multiplier (array, < 0 = forbidden) or None
    hist       per-state additive cost or None
    soft_cost  None = routed copper of other nets is hard. A number, or {piece id: cost} (key None = default,
               a piece without a price stays hard): cost in steps per cell entered inside that piece's halo;
               the pieces crossed by the returned path are reported in ``info['crossed']``.
    bend_state True = the incoming direction is part of the state (exact bend costs; ~3x slower, dict based).
    moves      per-window-layer move tables (``guided.astar.layer_moves``) for direction-biased layers.

    -> (path states | None, info) with info = {expanded, reason: 'ok'|'limit'|'budget'|'exhausted',
       crossed: {piece id: cells}, frontier: [states nearest to the target], best: state | -1,
       open: True when the search touched a MASKED cell (a wider corridor / window could go on)}.
    """
    info = {"expanded": 0, "reason": "exhausted", "crossed": {}, "frontier": [], "best": -1, "open": False}
    avail = budget.left() - CHARGE_SEARCH
    if avail <= 0:
        info["reason"] = "budget"
        return None, info
    cap = int(limit)
    by_budget = False
    if avail < cap:
        cap, by_budget = avail, True
    g = win.grid
    cm = win.cm
    G, W = g.G, g.W
    if moves is None:
        moves = default_moves(win, layers_ok)
    lis = tuple(li for li in range(len(win.layers)) if moves[li] is not None)
    vias = via_cost is not None and len(lis) > 1
    scget = _soft_get(soft_cost)
    if h is None:
        h = [0.0] * G
    fill = getattr(h, "fill", None)
    args = (win, net_id, sources, targets, h, fill, turn, via_cost if vias else None, lis, cmul, hist, scget, cap, moves)
    goal, n_exp, best_s, touched, par, heap = (_run_bend if bend_state else _run)(*args)
    budget.charge(CHARGE_SEARCH + CHARGE_EXPANSION * min(n_exp, cap))
    info["expanded"] = min(n_exp, cap)
    info["best"] = best_s
    info["open"] = touched
    if goal is None:
        if n_exp > cap:
            info["reason"] = "budget" if by_budget else "limit"
        fr = [best_s] if best_s >= 0 else []
        if heap:
            rest = sorted((e[0] - e[1], e[2], e[3]) for e in heap)[:H_SAMPLES - 1]
            fr += [(s // 9 if bend_state else s) for _h, _q, s in rest]
        info["frontier"] = fr
        return None, info
    path = [goal]
    s = goal
    if bend_state:
        k = par[1]
        par = par[0]
        while True:
            k = par.get(k, -1)
            if k < 0:
                break
            path.append(k // 9)
    else:
        while par[s] >= 0:
            s = par[s]
            path.append(s)
    path.reverse()
    info["reason"] = "ok"
    if scget is not None:
        info["crossed"] = crossed(win, net_id, path)
    return path, info


def crossed(win: Window, net_id: int, path) -> dict:
    """{piece id: cells} of the routed foreign pieces whose halo a path runs through (tracks and via sites)."""
    out = {}
    occ, owner, multi = win.cm.occ, win.owner, win.multi
    vowner, vmulti = win.vowner, win.vmulti
    G = win.grid.G
    prev = None
    for s in path:
        o = occ[s]
        if o and o != net_id:
            pid = owner[s]
            if pid:
                for q in multi.get(s) or (pid,):
                    out[q] = out.get(q, 0) + 1
        if prev is not None and prev // G != s // G:
            i = s % G
            pid = vowner[i]
            if pid:
                for q in vmulti.get(i) or (pid,):
                    out[q] = out.get(q, 0) + 1
        prev = s
    return dict(sorted(out.items()))


def _run(win, net, sources, targets, hc, fill, turn, via_cost, lis, cmul, hist, scget, cap, moves):
    g = win.grid
    cm = win.cm
    G = g.G
    gs, st, par, pd = g.gs, g.st, g.par, g.pd
    g.sid += 1
    sid = g.sid
    occ, vocc, S, SV = cm.occ, cm.vocc, cm.S, cm.SV
    owner, vowner = win.owner, win.vowner
    soft = scget is not None
    heap = []
    seq = 0
    for s in sorted(sources):
        g0 = sources[s]
        st[s] = sid
        gs[s] = g0
        par[s] = -1
        pd[s] = 8
        i = s % G
        hv = hc[i]
        if hv < 0.0:
            hv = fill(i)
        seq += 1
        heap.append((g0 + hv, g0, seq, s))
    heapify(heap)
    n_exp = 0
    best_s, best_h = -1, 1e18
    goal = None
    touched = False
    while heap:
        f, gc, _q, s = heappop(heap)
        if gc > gs[s]:
            continue
        if s in targets:
            goal = s
            break
        n_exp += 1
        if n_exp > cap:
            break
        l = s // G
        i = s - l * G
        hh = f - gc
        if hh < best_h:
            best_h, best_s = hh, s
        tc = turn[pd[s]]
        for delta, _dx, _dy, d, c in moves[l]:
            n = s + delta
            o = occ[n]
            extra = 0.0
            if o and o != net:
                if o == MASKED:
                    touched = True
                    continue
                if not soft:
                    continue
                o = S[n]
                if o and o != net:
                    if o == MASKED:
                        touched = True
                    continue
                extra = scget(owner[n])
                if extra is None:
                    continue
            if cmul is not None:
                m = cmul[n]
                if m < 0:
                    continue
                c = c * m
            ng = gc + c + tc[d] + extra
            if hist is not None:
                ng += hist[n]
            if st[n] == sid and gs[n] <= ng:
                continue
            st[n] = sid
            gs[n] = ng
            par[n] = s
            pd[n] = d
            ni = i + delta
            hv = hc[ni]
            if hv < 0.0:
                hv = fill(ni)
            seq += 1
            heappush(heap, (ng + hv, ng, seq, n))
        if via_cost is not None:
            o = vocc[i]
            extra = 0.0
            if o and o != net:
                if not soft:
                    continue
                o = SV[i]
                if o and o != net:
                    continue
                extra = scget(vowner[i])
                if extra is None:
                    continue
            for l2 in lis:
                if l2 == l:
                    continue
                n = l2 * G + i
                o = occ[n]
                e2 = extra
                if o and o != net:
                    if not soft:
                        continue
                    o = S[n]
                    if o and o != net:
                        continue
                    x = scget(owner[n])
                    if x is None:
                        continue
                    if x > e2:
                        e2 = x
                if cmul is not None and cmul[n] < 0:
                    continue
                ng = gc + via_cost + e2
                if st[n] == sid and gs[n] <= ng:
                    continue
                st[n] = sid
                gs[n] = ng
                par[n] = s
                pd[n] = 8
                seq += 1
                heappush(heap, (ng + hh, ng, seq, n))
    return goal, n_exp, best_s, touched, par, heap


def _run_bend(win, net, sources, targets, hc, fill, turn, via_cost, lis, cmul, hist, scget, cap, moves):
    """Same search with the incoming direction in the state: key = state * 9 + direction (8 = none)."""
    g = win.grid
    cm = win.cm
    G = g.G
    occ, vocc, S, SV = cm.occ, cm.vocc, cm.S, cm.SV
    owner, vowner = win.owner, win.vowner
    soft = scget is not None
    gsd, par = {}, {}
    heap = []
    seq = 0
    for s in sorted(sources):
        g0 = sources[s]
        k = s * 9 + 8
        gsd[k] = g0
        i = s % G
        hv = hc[i]
        if hv < 0.0:
            hv = fill(i)
        seq += 1
        heap.append((g0 + hv, g0, seq, k))
    heapify(heap)
    n_exp = 0
    best_s, best_h = -1, 1e18
    goal = None
    goal_k = -1
    touched = False
    get = gsd.get
    while heap:
        f, gc, _q, k = heappop(heap)
        if gc > gsd[k]:
            continue
        s = k // 9
        if s in targets:
            goal, goal_k = s, k
            break
        n_exp += 1
        if n_exp > cap:
            break
        l = s // G
        i = s - l * G
        hh = f - gc
        if hh < best_h:
            best_h, best_s = hh, s
        tc = turn[k - s * 9]
        for delta, _dx, _dy, d, c in moves[l]:
            n = s + delta
            o = occ[n]
            extra = 0.0
            if o and o != net:
                if o == MASKED:
                    touched = True
                    continue
                if not soft:
                    continue
                o = S[n]
                if o and o != net:
                    if o == MASKED:
                        touched = True
                    continue
                extra = scget(owner[n])
                if extra is None:
                    continue
            if cmul is not None:
                m = cmul[n]
                if m < 0:
                    continue
                c = c * m
            ng = gc + c + tc[d] + extra
            if hist is not None:
                ng += hist[n]
            nk = n * 9 + d
            old = get(nk)
            if old is not None and old <= ng:
                continue
            gsd[nk] = ng
            par[nk] = k
            ni = i + delta
            hv = hc[ni]
            if hv < 0.0:
                hv = fill(ni)
            seq += 1
            heappush(heap, (ng + hv, ng, seq, nk))
        if via_cost is not None:
            o = vocc[i]
            extra = 0.0
            if o and o != net:
                if not soft:
                    continue
                o = SV[i]
                if o and o != net:
                    continue
                extra = scget(vowner[i])
                if extra is None:
                    continue
            for l2 in lis:
                if l2 == l:
                    continue
                n = l2 * G + i
                o = occ[n]
                e2 = extra
                if o and o != net:
                    if not soft:
                        continue
                    o = S[n]
                    if o and o != net:
                        continue
                    x = scget(owner[n])
                    if x is None:
                        continue
                    if x > e2:
                        e2 = x
                if cmul is not None and cmul[n] < 0:
                    continue
                ng = gc + via_cost + e2
                nk = n * 9 + 8
                old = get(nk)
                if old is not None and old <= ng:
                    continue
                gsd[nk] = ng
                par[nk] = k
                seq += 1
                heappush(heap, (ng + hh, ng, seq, nk))
    return goal, n_exp, best_s, touched, (par, goal_k), heap


def probe(win: Window, net_id: int, sources: dict, limit: int, budget: Budget, *, layers_ok=None,
          vias: bool = True) -> dict:
    """Flood from ``sources`` over free cells (all allowed layers, through vias), at most ``limit`` expansions.

    -> {enclosed, reached, frontier_blockers: [(piece id | 'static', cells)], static_share, open, frontier}
       enclosed   the flood ran dry before ``limit`` without touching a MASKED cell: these sources are walled
                  in by copper / keep-outs / the board edge, whatever the corridor
       reached    cells flooded
       frontier_blockers  distinct blocked cells next to the flood, per routed piece (dynamic copper, by piece
                  id) and for everything static together, most cells first
       static_share  static blocked cells / all blocked cells on the frontier (1.0 = no rip-up can help)
       open       the flood touched a MASKED cell (corridor mask or window border)
       frontier   [(free state, blocked neighbour state)] samples, at most 16, in flood order
    Charges one unit per expansion.
    """
    g = win.grid
    cm = win.cm
    G = g.G
    occ, vocc, S = cm.occ, cm.vocc, cm.S
    owner = win.owner
    st = g.st
    g.sid += 1
    sid = g.sid
    lis = tuple(win.lidx[l] for l in sorted(layers_ok if layers_ok is not None else win.layers) if l in win.lidx)
    deltas = tuple(dy * g.W + dx for dx, dy in DIRS)
    cap = max(0, min(int(limit), budget.left()))
    dq = deque()
    for s in sorted(sources):
        if st[s] != sid:
            st[s] = sid
            dq.append(s)
    n_exp = 0
    touched = False
    static_n = 0
    tally = {}
    frontier = []
    vias = vias and len(lis) > 1
    while dq and n_exp < cap:
        s = dq.popleft()
        n_exp += 1
        l = s // G
        if l not in lis:
            continue
        for delta in deltas:
            n = s + delta
            if st[n] == sid:
                continue
            o = occ[n]
            if o and o != net_id:
                st[n] = sid                         # count each blocked cell once
                if o == MASKED or S[n] == MASKED:
                    touched = True
                    continue
                o = S[n]
                if o and o != net_id:
                    static_n += 1
                else:
                    pid = owner[n]
                    tally[pid] = tally.get(pid, 0) + 1
                if len(frontier) < 16:
                    frontier.append((s, n))
                continue
            st[n] = sid
            dq.append(n)
        if vias:
            i = s - l * G
            o = vocc[i]
            if o and o != net_id:
                continue
            for l2 in lis:
                n = l2 * G + i
                if l2 != l and st[n] != sid:
                    o = occ[n]
                    if not (o and o != net_id):
                        st[n] = sid
                        dq.append(n)
    budget.charge(CHARGE_EXPANSION * n_exp)
    blocked = static_n + sum(tally.values())
    fb = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    if static_n:
        fb.append(("static", static_n))
        fb.sort(key=lambda kv: (-kv[1], str(kv[0])))
    return {"enclosed": (not dq) and not touched and cap > 0, "reached": n_exp,
            "frontier_blockers": fb, "static_share": round(static_n / blocked, 3) if blocked else 1.0,
            "open": touched, "frontier": frontier}
