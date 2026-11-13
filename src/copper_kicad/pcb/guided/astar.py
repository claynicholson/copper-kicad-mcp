"""Multi-layer, 8-direction A* on the routing grid.

State = ``layer * G + cell``. Costs are in grid steps (1.0 = one orthogonal
step on an unbiased layer). The incoming direction of the best arrival is
remembered per state, so bends cost extra without multiplying the state space.

Deterministic: the heap is ordered by ``(f, g, state)`` and nothing depends on
hashing or time.
"""
from __future__ import annotations

from heapq import heappop, heappush, heapify

SQ2 = 1.4142135623730951
# direction index -> (dx, dy); 8 = "no direction" (start / just changed layer)
DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))


def turn_table(bend: float):
    """turn[d_prev][d_new] -> extra cost. 45° = bend, 90° = 3×, 135° = 12×, U-turn = 40×."""
    t = []
    for a in range(9):
        row = []
        for b in range(8):
            if a == 8:
                row.append(0.0)
                continue
            k = abs(a - b)
            k = min(k, 8 - k)
            row.append((0.0, bend, 3.0 * bend, 12.0 * bend, 40.0 * bend)[k])
        t.append(tuple(row))
    return tuple(t)


def layer_moves(W, G, layer, ch, cv, cd):
    """Move table for one layer: (delta, dx, dy, dir, cost)."""
    out = []
    for d, (dx, dy) in enumerate(DIRS):
        if dx and dy:
            c = cd * SQ2
        elif dx:
            c = ch
        else:
            c = cv
        out.append((dy * W + dx, dx, dy, d, c))
    return tuple(out)


def search(grid, cm, net, sources, targets, tx, ty, *, moves, turn, via_cost, layers_ok, cmul=None, hist=None,
           soft=False, soft_cost=40.0, hweight=1.0, limit=400000, via_ok=True):
    """Find the cheapest path from any source state to any target state.

    sources   {state: initial_cost}
    targets   set/dict of states
    tx, ty    grid cell the heuristic aims at (octile distance; 0-weight = Dijkstra)
    moves     per-layer move tables (None for a layer the net may not use)
    cmul      optional array('f') per state: cost multiplier, < 0 = forbidden
    hist      optional array('f') per state: additive history (congestion) cost
    soft      True = other routed nets may be crossed at ``soft_cost`` (rip-up planning);
              static obstacles stay hard

    Returns (path_states | None, expansions, best_state) — best_state is the closed state
    nearest to the target (for "where did it get stuck" reports).
    """
    G, W = grid.G, grid.W
    gs, st, par, pd = grid.gs, grid.st, grid.par, grid.pd
    grid.sid += 1
    sid = grid.sid
    occ, vocc, S, SV = cm.occ, cm.vocc, cm.S, cm.SV
    heap = []
    for s, g0 in sources.items():
        st[s] = sid
        gs[s] = g0
        par[s] = -1
        pd[s] = 8
        i = s % G
        ax = i % W - tx
        ay = i // W - ty
        if ax < 0:
            ax = -ax
        if ay < 0:
            ay = -ay
        h = (ax + 0.41421356 * ay) if ax > ay else (ay + 0.41421356 * ax)
        heap.append((g0 + h * hweight, g0, s))
    heapify(heap)
    n_exp = 0
    best_s, best_h = -1, 1e18
    goal = -1
    L = grid.L
    while heap:
        f, g, s = heappop(heap)
        if g > gs[s]:
            continue
        if s in targets:
            goal = s
            break
        n_exp += 1
        if n_exp > limit:
            break
        l = s // G
        i = s - l * G
        y = i // W
        x = i - y * W
        hh = f - g
        if hh < best_h:
            best_h, best_s = hh, s
        tc = turn[pd[s]]
        for delta, dx, dy, d, c in moves[l]:
            n = s + delta
            o = occ[n]
            extra = 0.0
            if o and o != net:
                if not soft:
                    continue
                o = S[n]
                if o and o != net:
                    continue
                extra = soft_cost
            if cmul is not None:
                m = cmul[n]
                if m < 0:
                    continue
                c = c * m
            ng = g + c + tc[d] + extra
            if hist is not None:
                ng += hist[n]
            if st[n] == sid and gs[n] <= ng:
                continue
            st[n] = sid
            gs[n] = ng
            par[n] = s
            pd[n] = d
            ax = x + dx - tx
            ay = y + dy - ty
            if ax < 0:
                ax = -ax
            if ay < 0:
                ay = -ay
            h = (ax + 0.41421356 * ay) if ax > ay else (ay + 0.41421356 * ax)
            heappush(heap, (ng + h * hweight, ng, n))
        if via_ok and L > 1:
            o = vocc[i]
            extra = 0.0
            if o and o != net:
                if not soft:
                    continue
                o = SV[i]
                if o and o != net:
                    continue
                extra = soft_cost
            for l2 in layers_ok:
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
                    e2 = soft_cost
                if cmul is not None and cmul[n] < 0:
                    continue
                ng = g + via_cost + e2
                if st[n] == sid and gs[n] <= ng:
                    continue
                st[n] = sid
                gs[n] = ng
                par[n] = s
                pd[n] = 8
                heappush(heap, (ng + hh, ng, n))
    if goal < 0:
        return None, n_exp, best_s
    path = [goal]
    s = goal
    while par[s] >= 0:
        s = par[s]
        path.append(s)
    path.reverse()
    return path, n_exp, goal
