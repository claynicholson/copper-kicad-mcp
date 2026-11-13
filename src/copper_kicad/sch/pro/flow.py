"""Signal flow of a sheet at block level ("pro2", docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 4).

net roles -> block quotient graph (who drives whom; supply input -> regulator -> loads) -> feedback edges taken
out -> longest-path ranks with the connectors pinned to the first / last rank -> barycenter order inside a rank.
Read-only over the Design: nothing here can change a net, only where a frame is drawn.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .blocks import Design, _refkey

_BUS = re.compile(r"^(.*?)(\d+)$")
_KIND = {"input": 0, "power": 1, "main": 2, "ic": 3, "io": 4, "misc": 5}


@dataclass
class Flow:
    roles: dict = field(default_factory=dict)      # net key -> {'role', 'driver': (ref, pin) | None, 'sinks': [...]}
    edges: dict = field(default_factory=dict)      # (block a, block b) -> {'w': weight, 'kind': 'signal' | 'power',
    #                                                 'feedback': bool}   a drives b (feedback edges: b drives a)
    rank: dict = field(default_factory=dict)       # block id -> 0 .. last
    pin: dict = field(default_factory=dict)        # block id -> 'first' | 'last' | None (connector blocks)
    order: list = field(default_factory=list)      # block ids: by rank, inside a rank top to bottom
    edge_blocks: set = field(default_factory=set)  # blocks that hold a connector (they belong in an outer column)
    shared: dict = field(default_factory=dict)     # frozenset({a, b}) -> signal nets the two blocks share


def net_roles(D: Design) -> dict:
    """{net: {'role': 'gnd' | 'rail' | 'bus_member' | 'signal', 'driver': (ref, pin) | None, 'sinks': [(ref, pin)]}}.
    The driver is the pin whose electrical type drives (output, power output, open collector ...); a net between
    passives and inputs only has none."""
    v = D.view
    sup = getattr(D, "supplyish", set())
    stems: dict = {}
    for k in v.nets:
        m = _BUS.match(D.text(k) or "")
        if m and k not in sup:
            stems.setdefault(m.group(1), []).append(k)
    bus = {k for ks in stems.values() if len(ks) >= 4 for k in ks}
    out = {}
    for k, ln in v.nets.items():
        drv = None
        for p in sorted(ln.pins, key=lambda p: (_refkey(p[0]), str(p[1]))):
            t = D.ptypes.get(p, "")
            if "output" in t or "power_out" in t or "open_" in t or "tri_state" in t:
                drv = p
                break
        role = "gnd" if D.is_gnd(k) else "rail" if k in sup else "bus_member" if k in bus else "signal"
        out[k] = {"role": role, "driver": drv, "sinks": [p for p in ln.pins if p != drv]}
    return out


def conn_direction(D: Design, c: str):
    """Which way a connector faces in the signal flow, read through the passives behind it:
    'supply' = it brings a rail in that nothing on the sheet makes (through a fuse / diode / ferrite at most),
    'in' = its signals end at inputs (a sensor or line input), 'out' = an output drives it, None = cannot tell."""
    v, pins_of = D.view, D.pins_of
    sup = getattr(D, "supplyish", set())
    refs_on = lambda k: sorted({p[0] for p in v.nets[k].pins}, key=_refkey)   # noqa: E731
    mine = [k for k in dict.fromkeys(pins_of[c].values()) if k in v.nets and not D.is_gnd(k)]
    if not mine:
        return None

    def made(k):
        return k in getattr(D, "sourced", set()) or any("power_out" in D.ptypes.get(p, "") for p in v.nets[k].pins)
    if len(mine) <= 2:
        seen, front = set(mine), list(mine)
        for _ in range(3):
            nxt = []
            for k in front:
                if k in D.rails and not made(k):
                    return "supply"
                for r in refs_on(k):
                    if r != c and len(pins_of[r]) == 2 and re.match(r"(F|D|FB|L)\d", r) and not D.is_conn(r):
                        for k2 in pins_of[r].values():
                            if k2 not in seen and k2 in v.nets and not D.is_gnd(k2) and not (k2 in sup and made(k2)):
                                seen.add(k2)
                                nxt.append(k2)
            front = nxt
    seen = {k for k in mine if k not in sup}
    front = list(seen)
    ins = outs = 0
    for _ in range(4):
        nxt = []
        for k in front:
            for p in v.nets[k].pins:
                r = p[0]
                if r == c or D.is_conn(r):
                    continue
                if len(pins_of[r]) <= 3 and r not in D.root_of:      # a passive: look at what is behind it
                    for k2 in pins_of[r].values():
                        if k2 not in seen and k2 in v.nets and k2 not in sup:
                            seen.add(k2)
                            nxt.append(k2)
                    continue
                t = D.ptypes.get(p, "")
                if "output" in t or "open_" in t or "tri_state" in t:
                    outs += 1
                elif t == "input":
                    ins += 1
        front = nxt
    return "out" if outs else "in" if ins else None


def _conn_blocks(D: Design) -> set:
    return {D.block_of[r] for r in D.units if D.is_conn(r) and D.npins(r) >= 2 and r in D.block_of}


def analyse(D: Design) -> Flow:
    F = Flow()
    F.roles = net_roles(D)
    blocks = {b.id: b for b in D.blocks}
    ids = sorted(blocks, key=lambda i: (blocks[i].rank, _refkey(i)))
    v = D.view
    # ── edges: signals (pin types, D.flow) and the supply path (input connector -> regulator -> loads) ──
    raw: dict = {}
    for (a, b), w in getattr(D, "flow", {}).items():
        back = D.flow.get((b, a), 0)
        if a in blocks and b in blocks and w > back:
            raw[(a, b)] = {"w": float(w - back), "kind": "signal", "feedback": False}
    reg_in, reg_out = getattr(D, "reg_in", {}), getattr(D, "reg_out", {})
    feed = getattr(D, "feed_of", {})
    for r, ins in sorted(reg_in.items()):
        rb = D.block_of.get(r)
        if rb is None:
            continue
        if not (set(ins) & getattr(D, "sourced", set())):  # fed from outside: after every supply input
            for c in sorted(feed, key=_refkey):
                cb = D.block_of.get(c)
                if cb is not None and cb != rb and blocks[cb].kind == "input" and (rb, cb) not in raw:
                    raw.setdefault((cb, rb), {"w": 0.5, "kind": "power", "feedback": False})
        for k in sorted(reg_out.get(r, ())):
            ln = v.nets.get(k)
            if ln is None:
                continue
            for ref in sorted({p[0] for p in ln.pins}, key=_refkey):
                ob = D.block_of.get(ref)
                if ob is None or ob == rb or blocks[ob].kind == "input":
                    continue
                if any(k in reg_out.get(a, ()) for a in blocks[ob].anchors):
                    continue                              # two sources of one rail: neither feeds the other
                if (rb, ob) not in raw and (ob, rb) not in raw:
                    raw[(rb, ob)] = {"w": 0.5, "kind": "power", "feedback": False}
    # ── feedback: the strongest edges first; an edge that would close a cycle is a feedback edge ──
    succ: dict = {i: set() for i in ids}

    def reaches(a, b):
        seen, st = set(), [a]
        while st:
            x = st.pop()
            if x == b:
                return True
            if x in seen:
                continue
            seen.add(x)
            st.extend(succ[x])
        return False
    for (a, b), e in sorted(raw.items(), key=lambda t: (-t[1]["w"], t[1]["kind"] != "signal", _refkey(t[0][0]), _refkey(t[0][1]))):
        if reaches(b, a):
            e["feedback"] = True
        else:
            succ[a].add(b)
        F.edges[(a, b)] = e
    # ── pins: supply inputs and signal sources on the left, the connectors a design talks through on the right ──
    for i in ids:
        b = blocks[i]
        if b.kind == "input":
            F.pin[i] = "first"
        elif b.kind == "io":
            fwd = sum(e["w"] for (a, c), e in F.edges.items() if a == i and e["kind"] == "signal")
            back = sum(e["w"] for (a, c), e in F.edges.items() if c == i and e["kind"] == "signal")
            way = conn_direction(D, i) if (fwd == 0 and back == 0 and i in D.units) else None
            F.pin[i] = "first" if ((fwd > 0 and back == 0) or way in ("supply", "in")) else "last"
        elif b.kind == "misc":
            F.pin[i] = "last"
        else:
            F.pin[i] = None
    # ── longest path ──
    rank = {i: 0 for i in ids}
    free = [i for i in ids if F.pin[i] is None]
    for i in free:
        if any(F.pin[a] == "first" for a in ids):
            rank[i] = 1                                   # nothing shares the rank of the input connectors
    for _ in range(len(ids) + 1):
        ch = False
        for a in ids:
            for b in sorted(succ[a], key=_refkey):
                if F.pin[b] is None and F.pin[a] != "last" and rank[b] < rank[a] + 1:
                    rank[b] = rank[a] + 1
                    ch = True
        if not ch:
            break
    last = max([rank[i] for i in free] or [0]) + 1
    for i in ids:
        if F.pin[i] == "first":
            rank[i] = 0
        elif F.pin[i] == "last":
            rank[i] = last
    F.rank = rank
    # ── order inside a rank: barycenter of the neighbours (2 sweeps down, 2 up), supply above signal ──
    und: dict = {i: {} for i in ids}
    for (a, b), e in F.edges.items():
        und[a][b] = und[a].get(b, 0) + e["w"]
        und[b][a] = und[b].get(a, 0) + e["w"]
    shared: dict = {}
    for k, ln in v.nets.items():
        if F.roles[k]["role"] in ("gnd", "rail") or not D.conn(k):
            continue
        bs = sorted({D.block_of[p[0]] for p in ln.pins if D.block_of.get(p[0]) in blocks}, key=_refkey)
        for x in range(len(bs)):
            for y in range(x + 1, len(bs)):
                shared.setdefault(frozenset((bs[x], bs[y])), []).append(k)
                und[bs[x]][bs[y]] = und[bs[x]].get(bs[y], 0) + 0.25
                und[bs[y]][bs[x]] = und[bs[y]].get(bs[x], 0) + 0.25
    F.shared = shared
    base = {i: (_KIND.get(blocks[i].kind, 9), getattr(blocks[i], "stage", 0.0), _refkey(i)) for i in ids}
    layers: dict = {}
    for i in ids:
        layers.setdefault(rank[i], []).append(i)
    for r in layers:
        layers[r].sort(key=lambda i: base[i])
    rs = sorted(layers)

    def sweep(seq):
        for r in seq:
            where = {i: n for q in rs if q != r for n, i in enumerate(layers[q])}
            cur = {i: n for n, i in enumerate(layers[r])}

            def bary(i):
                ws = [(where[j], w) for j, w in und[i].items() if j in where]
                if not ws:
                    return float(cur[i])
                return sum(p * w for p, w in ws) / sum(w for _p, w in ws)
            layers[r].sort(key=lambda i: (round(bary(i), 3), base[i]))
    for _ in range(2):
        sweep(rs[1:])
        sweep(list(reversed(rs[:-1])))
    F.order = [i for r in rs for i in layers[r]]
    F.edge_blocks = _conn_blocks(D)
    return F


def violations(F: Flow, col_of: dict) -> int:
    """Non-feedback signal edges drawn right to left (driver's column right of the sink's)."""
    return sum(1 for (a, b), e in F.edges.items()
               if e["kind"] == "signal" and not e["feedback"] and a in col_of and b in col_of and col_of[a] > col_of[b])
