"""Min-cost flow by successive shortest paths, deterministic (design §4.5, §9). Imports nothing from v2.

    flow, cost, per_edge = min_cost_flow(n, edges, s, t, want)

``edges`` is a list of ``(u, v, cap, cost)`` over nodes ``0 .. n-1`` (parallel edges and both directions of a
pair are fine); ``want`` units are pushed from ``s`` to ``t`` at minimum total cost. When the network cannot
carry ``want`` the result is the maximum flow that exists, still at minimum cost for that amount (successive
shortest paths is optimal after every augmentation), so ``flow < want`` is the "infeasible" answer.

Exact and reproducible:

* costs are converted once to integers of ``1 / COST_SCALE`` (1e-9: a nanometre when costs are metres, a
  picometre for the millimetre costs v2 uses) and all path arithmetic is integer, so there is no float drift,
  no "almost negative" reduced cost, and ties are real ties;
* ties are broken by structure only: the heap orders by (distance, node number) and a node keeps the first
  edge (in input order of its tail node) that reached its final distance. Same input, same answer, always;
* negative costs are allowed (one Bellman-Ford pass seeds the potentials); a negative cycle reachable from
  ``s`` raises ``ValueError``.

Algorithm: Dijkstra on reduced costs with Johnson potentials, stopping when ``t`` is settled; each path is
saturated by its bottleneck. O(F x E log V) for F augmentations: the fan-out networks are unit-capacity
assignment problems of a few hundred edges (measured in tests/test_v2_flow.py).
"""
from __future__ import annotations

from heapq import heappop, heappush

COST_SCALE = 10 ** 9        # costs are rounded to 1e-9 of their unit before solving
_INF = float("inf")


def min_cost_flow(n: int, edges: list[tuple[int, int, int, float]], s: int, t: int,
                  want: int) -> tuple[int, float, list[int]]:
    """edges (u, v, cap, cost) -> (flow, cost, flow per edge).

    ``flow`` <= ``want`` units go from ``s`` to ``t``; ``cost`` is the sum of ``flow_e x cost_e`` with the
    caller's own (unrounded) costs; ``flow per edge`` is aligned with ``edges``. Capacities are non-negative
    integers. Raises ValueError for a node out of range, a negative or fractional capacity, a non-finite cost,
    or a negative-cost cycle reachable from ``s``."""
    m = len(edges)
    if not (0 <= s < n and 0 <= t < n):
        raise ValueError(f"source {s} / sink {t} out of range for {n} nodes")
    # residual graph: edge i forward = arc 2i, its reverse = arc 2i + 1
    to = [0] * (2 * m)
    cap = [0] * (2 * m)
    cost = [0] * (2 * m)
    adj = [[] for _ in range(n)]
    negative = False
    for i, e in enumerate(edges):
        u, v, c, w = e
        if not (0 <= u < n and 0 <= v < n):
            raise ValueError(f"edge {i}: node out of range ({u} -> {v}, {n} nodes)")
        if c != int(c) or c < 0:
            raise ValueError(f"edge {i}: capacity must be a non-negative integer, got {c!r}")
        if w != w or w in (_INF, -_INF):
            raise ValueError(f"edge {i}: cost must be finite, got {w!r}")
        wi = int(round(w * COST_SCALE))
        a = 2 * i
        to[a], cap[a], cost[a] = v, int(c), wi
        to[a + 1], cap[a + 1], cost[a + 1] = u, 0, -wi
        adj[u].append(a)
        adj[v].append(a + 1)
        if wi < 0 and c > 0:
            negative = True
    flow = 0
    want = int(want)
    if want <= 0 or s == t or m == 0:
        return 0, 0.0, [0] * m

    pot = [0] * n
    if negative:
        # Bellman-Ford from s over arcs with capacity, node order then arc order (deterministic)
        dist = [None] * n
        dist[s] = 0
        for _ in range(n):
            changed = False
            for u in range(n):
                du = dist[u]
                if du is None:
                    continue
                for a in adj[u]:
                    if cap[a] > 0:
                        nd = du + cost[a]
                        v = to[a]
                        if dist[v] is None or nd < dist[v]:
                            dist[v] = nd
                            changed = True
            if not changed:
                break
        else:
            raise ValueError("negative-cost cycle reachable from the source")
        big = max((d for d in dist if d is not None), default=0)
        pot = [big if d is None else d for d in dist]      # unreachable nodes: any value keeps reduced costs
        #                                                    irrelevant (no residual arc from the reached set)

    while flow < want:
        dist = [None] * n
        prev = [-1] * n
        done = [False] * n
        dist[s] = 0
        heap = [(0, s)]
        dt = None
        while heap:
            d, u = heappop(heap)
            if done[u]:
                continue
            done[u] = True
            if u == t:
                dt = d
                break
            pu = pot[u]
            for a in adj[u]:
                if cap[a] <= 0:
                    continue
                v = to[a]
                if done[v]:
                    continue
                nd = d + cost[a] + pu - pot[v]
                dv = dist[v]
                if dv is None or nd < dv:
                    dist[v] = nd
                    prev[v] = a
                    heappush(heap, (nd, v))
        if dt is None:
            break                                           # t is cut off: the flow is maximal
        # potentials: settled nodes move by their distance, everything else by the distance of t
        for v in range(n):
            dv = dist[v]
            pot[v] += dv if (done[v] and dv is not None and dv < dt) else dt
        # bottleneck of the path, then push
        push = want - flow
        v = t
        while v != s:
            a = prev[v]
            if cap[a] < push:
                push = cap[a]
            v = to[a ^ 1]
        v = t
        while v != s:
            a = prev[v]
            cap[a] -= push
            cap[a ^ 1] += push
            v = to[a ^ 1]
        flow += push

    per_edge = [cap[2 * i + 1] for i in range(m)]
    total = 0.0
    for i in range(m):
        if per_edge[i]:
            total += per_edge[i] * edges[i][3]
    return flow, total, per_edge


def assignment(costs: list[list[float | None]], capacity: list[int] | None = None) -> tuple[list[int], float]:
    """Convenience on top of min_cost_flow: rows -> columns, ``costs[r][c]`` = price or None (not allowed),
    each row at most once, column ``c`` at most ``capacity[c]`` times (default 1). Returns (column per row or
    -1, total cost): as many rows as possible are assigned, at minimum cost for that number."""
    nr = len(costs)
    nc = max((len(r) for r in costs), default=0)
    edges = []
    owner = []
    for r in range(nr):
        edges.append((0, 2 + r, 1, 0.0))
        owner.append(None)
    for r, row in enumerate(costs):
        for c, w in enumerate(row):
            if w is not None:
                edges.append((2 + r, 2 + nr + c, 1, w))
                owner.append((r, c))
    for c in range(nc):
        edges.append((2 + nr + c, 1, 1 if capacity is None else int(capacity[c]), 0.0))
        owner.append(None)
    _f, total, per = min_cost_flow(2 + nr + nc, edges, 0, 1, nr)
    out = [-1] * nr
    for i, f in enumerate(per):
        if f and owner[i] is not None:
            out[owner[i][0]] = owner[i][1]
    return out, total
