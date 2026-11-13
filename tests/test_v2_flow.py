"""Router v2 min-cost flow (design §4.5, §9, §10: known small networks; infeasible -> partial flow;
deterministic ties). Pure Python, no KiCad.

Run:  python tests/test_v2_flow.py [test_name ...] [--perf]
"""
from __future__ import annotations

import itertools
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os as _os, tempfile as _tempfile  # noqa: E401,E402
_os.environ.setdefault("COPPER_HOME", _tempfile.mkdtemp(prefix="copper-test-home-"))  # isolate project state
from copper_kicad.pcb.v2 import flow  # noqa: E402
from copper_kicad.pcb.v2.flow import min_cost_flow  # noqa: E402


# ── an independent reference: Bellman-Ford successive shortest paths on floats ──
def reference(n, edges, s, t, want):
    to, cap, cost, adj = [], [], [], [[] for _ in range(n)]
    for u, v, c, w in edges:
        adj[u].append(len(to)); to.append(v); cap.append(c); cost.append(w)
        adj[v].append(len(to)); to.append(u); cap.append(0); cost.append(-w)
    fl, total = 0, 0.0
    while fl < want:
        dist = [float("inf")] * n
        prev = [-1] * n
        dist[s] = 0.0
        for _ in range(n):
            ch = False
            for u in range(n):
                if dist[u] == float("inf"):
                    continue
                for a in adj[u]:
                    if cap[a] > 0 and dist[u] + cost[a] < dist[to[a]] - 1e-12:
                        dist[to[a]] = dist[u] + cost[a]
                        prev[to[a]] = a
                        ch = True
            if not ch:
                break
        if dist[t] == float("inf"):
            break
        push = want - fl
        v = t
        while v != s:
            push = min(push, cap[prev[v]])
            v = to[prev[v] ^ 1]
        v = t
        while v != s:
            cap[prev[v]] -= push
            cap[prev[v] ^ 1] += push
            v = to[prev[v] ^ 1]
        fl += push
        total += push * dist[t]
    return fl, total


def check_flow(n, edges, s, t, want, res):
    """Capacity, conservation and cost bookkeeping of a result."""
    fl, cost, per = res
    assert len(per) == len(edges)
    bal = [0] * n
    c = 0.0
    for (u, v, cap, w), f in zip(edges, per):
        assert 0 <= f <= cap and f == int(f), (u, v, cap, f)
        bal[u] -= f
        bal[v] += f
        c += f * w
    assert abs(c - cost) < 1e-9 * max(1.0, abs(c)), (c, cost)
    assert fl <= want
    for v in range(n):
        if s == t:
            assert bal[v] == 0
        elif v == s:
            assert bal[v] == -fl, (bal[v], fl)
        elif v == t:
            assert bal[v] == fl
        else:
            assert bal[v] == 0, (v, bal[v])


# ── known small networks ──────────────────────────────────────────────────
def test_single_path_and_bottleneck():
    e = [(0, 1, 5, 1.0), (1, 2, 3, 2.0)]
    assert min_cost_flow(3, e, 0, 2, 2) == (2, 6.0, [2, 2])
    assert min_cost_flow(3, e, 0, 2, 9) == (3, 9.0, [3, 3])          # the 3-unit edge is the bottleneck
    assert min_cost_flow(3, e, 0, 2, 0) == (0, 0.0, [0, 0])
    assert min_cost_flow(3, e, 0, 0, 4) == (0, 0.0, [0, 0])          # source == sink


def test_textbook_network():
    # two routes 0-1-3 (cost 2) and 0-2-3 (cost 3), a cross edge 1-2 (0.5)
    e = [(0, 1, 2, 1.0), (0, 2, 1, 2.0), (1, 3, 1, 1.0), (2, 3, 2, 1.0), (1, 2, 1, 0.5)]
    assert min_cost_flow(4, e, 0, 3, 1) == (1, 2.0, [1, 0, 1, 0, 0])
    assert min_cost_flow(4, e, 0, 3, 2) == (2, 4.5, [2, 0, 1, 1, 1])
    r = min_cost_flow(4, e, 0, 3, 3)
    assert r == (3, 7.5, [2, 1, 1, 2, 1])
    check_flow(4, e, 0, 3, 3, r)


def test_needs_the_residual_edge():
    # the greedy first path 0-1-2-3 (cost 3) must be partly undone to carry 2 units: optimum 0-1-3 + 0-2-3
    e = [(0, 1, 1, 1.0), (1, 2, 1, 1.0), (2, 3, 1, 1.0), (0, 2, 1, 5.0), (1, 3, 1, 5.0)]
    assert min_cost_flow(4, e, 0, 3, 1) == (1, 3.0, [1, 1, 1, 0, 0])
    r = min_cost_flow(4, e, 0, 3, 2)
    assert r == (2, 12.0, [1, 0, 1, 1, 1]), r


def test_infeasible_gives_the_partial_flow():
    e = [(0, 1, 1, 1.0), (0, 2, 1, 4.0), (1, 3, 1, 1.0), (2, 3, 1, 1.0), (0, 4, 5, 0.1)]     # node 4 is a dead end
    r = min_cost_flow(5, e, 0, 3, 10)
    assert r == (2, 7.0, [1, 1, 1, 1, 0]), r
    check_flow(5, e, 0, 3, 10, r)
    assert min_cost_flow(3, [(0, 1, 3, 1.0)], 0, 2, 2) == (0, 0.0, [0])            # sink unreachable
    assert min_cost_flow(2, [], 0, 1, 2) == (0, 0.0, [])
    assert min_cost_flow(2, [(0, 1, 0, 1.0)], 0, 1, 2) == (0, 0.0, [0])            # zero capacity


def test_parallel_edges_and_capacity_split():
    e = [(0, 1, 2, 3.0), (0, 1, 1, 1.0), (0, 1, 4, 2.0)]
    assert min_cost_flow(2, e, 0, 1, 5) == (5, 1.0 + 4 * 2.0, [0, 1, 4])
    assert min_cost_flow(2, e, 0, 1, 7) == (7, 1.0 + 8.0 + 6.0, [2, 1, 4])


def test_negative_costs_and_cycle_detection():
    e = [(0, 1, 1, 2.0), (1, 2, 1, -1.5), (0, 2, 1, 1.0), (2, 3, 2, 0.0)]
    r = min_cost_flow(4, e, 0, 3, 2)
    assert r == (2, 1.5, [1, 1, 1, 2]), r
    assert min_cost_flow(4, e, 0, 3, 1) == (1, 0.5, [1, 1, 0, 1])
    try:
        min_cost_flow(3, [(0, 1, 1, 1.0), (1, 2, 1, -2.0), (2, 1, 1, 1.0)], 0, 2, 1)
        raise AssertionError("a negative cycle must raise")
    except ValueError as ex:
        assert "negative" in str(ex)


def test_bad_input_raises():
    for n, e, s, t in ((2, [(0, 2, 1, 1.0)], 0, 1), (2, [(0, 1, -1, 1.0)], 0, 1), (2, [(0, 1, 1.5, 1.0)], 0, 1),
                       (2, [(0, 1, 1, float("nan"))], 0, 1), (2, [(0, 1, 1, float("inf"))], 0, 1),
                       (2, [(0, 1, 1, 1.0)], 0, 5)):
        try:
            min_cost_flow(n, e, s, t, 1)
            raise AssertionError(f"must raise: {e} {s} {t}")
        except ValueError:
            pass


# ── deterministic ties ────────────────────────────────────────────────────
def test_ties_are_broken_by_structure():
    # three equal routes of equal length: the one through the lowest-numbered node wins, whatever the edge order says
    e1 = [(0, 1, 1, 1.0), (0, 2, 1, 1.0), (0, 3, 1, 1.0), (1, 4, 1, 1.0), (2, 4, 1, 1.0), (3, 4, 1, 1.0)]
    assert min_cost_flow(5, e1, 0, 4, 1)[2] == [1, 0, 0, 1, 0, 0]
    assert min_cost_flow(5, e1, 0, 4, 2)[2] == [1, 1, 0, 1, 1, 0]
    e2 = [e1[2], e1[1], e1[0], e1[5], e1[4], e1[3]]
    assert min_cost_flow(5, e2, 0, 4, 1)[2] == [0, 0, 1, 0, 0, 1]
    # equal parallel edges: the first in input order
    assert min_cost_flow(2, [(0, 1, 1, 1.0), (0, 1, 1, 1.0)], 0, 1, 1)[2] == [1, 0]
    # float noise does not create or hide a tie: 0.1 + 0.2 and 0.3 are the same cost here (in floats
    # 0.1 + 0.2 > 0.3 and the 0.3 route would win). A real tie keeps the predecessor that was settled first:
    # the node reached at 0.1, whatever its number
    e3 = [(0, 1, 1, 0.1), (1, 3, 1, 0.2), (0, 2, 1, 0.3), (2, 3, 1, 0.0)]
    assert min_cost_flow(4, e3, 0, 3, 1)[2] == [1, 1, 0, 0]
    e4 = [(0, 2, 1, 0.1), (2, 3, 1, 0.2), (0, 1, 1, 0.3), (1, 3, 1, 0.0)]
    assert min_cost_flow(4, e4, 0, 3, 1)[2] == [1, 1, 0, 0]
    assert 0.1 + 0.2 > 0.3


def test_same_twice_and_no_input_mutation():
    rng = random.Random(7)
    for _ in range(40):
        n = rng.randint(4, 10)
        e = [(rng.randrange(n), rng.randrange(n), rng.randint(0, 3), rng.choice((0.0, 0.5, 1.0, 1.0, 2.0)))
             for _ in range(rng.randint(5, 30))]
        keep = list(e)
        a = min_cost_flow(n, e, 0, n - 1, 5)
        b = min_cost_flow(n, e, 0, n - 1, 5)
        assert a == b and e == keep


# ── against brute force / an independent solver ───────────────────────────
def test_random_networks_match_the_reference():
    rng = random.Random(20261110)
    for trial in range(300):
        n = rng.randint(3, 9)
        e = []
        for _ in range(rng.randint(3, 24)):
            u, v = rng.randrange(n), rng.randrange(n)
            if u == v:
                continue
            e.append((u, v, rng.randint(0, 4), round(rng.uniform(0.0, 5.0), 3)))
        want = rng.randint(1, 8)
        r = min_cost_flow(n, e, 0, n - 1, want)
        check_flow(n, e, 0, n - 1, want, r)
        fl, cost = reference(n, e, 0, n - 1, want)
        assert r[0] == fl, (trial, r[0], fl)
        assert abs(r[1] - cost) < 1e-6, (trial, r[1], cost)


def test_assignment_matches_brute_force():
    rng = random.Random(42)
    for trial in range(120):
        nr, nc = rng.randint(1, 5), rng.randint(1, 5)
        costs = [[None if rng.random() < 0.3 else round(rng.uniform(0.1, 9.0), 2) for _ in range(nc)] for _ in range(nr)]
        got, total = flow.assignment(costs)
        # brute force: the largest matching, then the cheapest
        best = (0, 0.0)
        cols = list(range(nc)) + [-1] * nr
        seen = set()
        for perm in itertools.permutations(cols, nr):
            if perm in seen:
                continue
            seen.add(perm)
            if any(c >= 0 and costs[r][c] is None for r, c in enumerate(perm)):
                continue
            k = sum(1 for c in perm if c >= 0)
            w = sum(costs[r][c] for r, c in enumerate(perm) if c >= 0)
            if k > best[0] or (k == best[0] and w < best[1] - 1e-9):
                best = (k, w)
        assert sum(1 for c in got if c >= 0) == best[0], (trial, costs, got, best)
        assert abs(total - best[1]) < 1e-6, (trial, costs, got, total, best)
        used = [c for c in got if c >= 0]
        assert len(used) == len(set(used))
    # column capacities
    got, total = flow.assignment([[1.0, 5.0], [1.0, 5.0], [1.0, 5.0]], capacity=[2, 1])
    assert sorted(got) == [0, 0, 1] and total == 7.0 and got == [0, 0, 1]


def test_node_capacity_by_splitting():
    """The construction fan-out uses for via columns: a column is a node pair joined by a capacity-1 edge."""
    # pads 2,3 both prefer column A (4->5), column B (6->7) is dearer; each column takes one via
    e = [(0, 2, 1, 0.0), (0, 3, 1, 0.0), (2, 4, 1, 1.0), (3, 4, 1, 1.2), (2, 6, 1, 3.0), (3, 6, 1, 2.0),
         (4, 5, 1, 0.0), (6, 7, 1, 0.0), (5, 1, 1, 0.0), (7, 1, 1, 0.0)]
    r = min_cost_flow(8, e, 0, 1, 2)
    assert r[0] == 2 and r[1] == 3.0 and r[2][2:6] == [1, 0, 0, 1]


def test_throughput():
    """Measured, not asserted tightly: a 60 x 60 assignment (3600 edges) and a 16-pad fan-out sized problem."""
    rng = random.Random(1)
    n = 60
    costs = [[round(rng.uniform(0.5, 6.0), 3) for _ in range(n)] for _ in range(n)]
    t = time.perf_counter()
    got, total = flow.assignment(costs)
    dt = time.perf_counter() - t
    assert sorted(got) == list(range(n))
    small = [[round(rng.uniform(0.5, 6.0), 3) if rng.random() < 0.4 else None for _ in range(48)] for _ in range(16)]
    t = time.perf_counter()
    for _ in range(50):
        flow.assignment(small)
    ds = (time.perf_counter() - t) / 50
    print(f"    60x60 assignment (3720 edges): {dt * 1000:.0f} ms; 16 pads x 48 sites (~300 edges): {ds * 1000:.2f} ms")
    assert dt < 20.0 and ds < 1.0


if __name__ == "__main__":
    import traceback
    fails = 0
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t0 = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t0:.1f}s)")
            except Exception as ex:  # noqa: BLE001
                fails += 1
                traceback.print_exc()
                print(f"FAIL {name}: {ex}")
    sys.exit(1 if fails else 0)
