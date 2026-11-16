"""Routability preflight: cheap proofs that an autoroute cannot finish, found before Freerouting runs.

One read-only board extract (the guided router's model) and one occupancy map at the fab's MINIMUM track
width and clearance — the most permissive class there is, so nothing this check calls blocked could be routed
by a necked-down track either. For every pad of a net that still has unrouted connections it floods the free
grid cells reachable from the pad (same layer moves, plus a layer change wherever a minimum via fits). A pad
whose flood dies out before it reaches open board (``min_free`` free cells, or ``max_room`` cells in all) or
another unconnected piece of its own net is *walled in*: no router can connect it in this placement.

Over-capacity regions (more ratsnest lines through a cell than tracks fit through it on the signal layers)
are reported as warnings only: ratsnest lines are straight, real tracks detour, so that is a hint, not a proof.
"""
from __future__ import annotations

import time
from collections import deque


def check(pr, ignore=(), nets=None, cap_s: float = 15.0, min_free: int = 200, max_room: int = 3000,
          max_pads: int = 4000) -> dict:
    """-> {ok, seconds, checked_pads, walled: [...], over_capacity: [...], hot_spots: [...],
    suggested_moves: [...], complete: bool}. ok=False only for walled-in pads (a proof)."""
    from .guided import session
    t0 = time.time()
    b, _ex, _before, _ripped = session.load_board(pr)
    return check_board(b, ignore=ignore, nets=nets, cap_s=cap_s - (time.time() - t0), min_free=min_free,
                       max_room=max_room, max_pads=max_pads, t0=t0)


def check_board(b, ignore=(), nets=None, cap_s: float = 15.0, min_free: int = 200, max_room: int = 3000,
                max_pads: int = 4000, t0: float | None = None) -> dict:
    """The preflight on a loaded ``guided.model.Board`` (see ``check``)."""
    from .guided import analysis as an
    from .guided import plan as planmod
    t0 = time.time() if t0 is None else t0
    cap_s = max(1.0, cap_s) + (time.time() - t0)
    cfg = planmod.resolve(b, None)
    b.make_grid(cfg.step)
    R = b.rules
    # Grid soundness: a legal track centre line may run anywhere, the grid only tests cell centres, which can sit
    # up to step/sqrt(2) from it. Shrinking every halo by that much (plus the grid's own EPS) means no cell a
    # legal path passes through is ever marked blocked; with 8-neighbour moves the flood over-approximates
    # every real path, so "no way out" here is a proof.
    from .guided.grid import EPS
    slack = 0.7072 * cfg.step + EPS
    real = b.class_key(R["track_width"], R["clearance"], R["via"], R["drill"])
    key = (round(real[0] - slack, 4), real[1], round(max(0.01, real[2] - slack), 4), real[3])
    cm = b.class_map(key)
    g = b.grid
    S, SV, G, W, L = cm.S, cm.SV, g.G, g.W, g.L
    facts = planmod.classify(b)
    ignore = set(ignore or ())
    want = set(nets) if nets else None
    todo = [n for n, f in facts.items() if f.get("unrouted") and n not in ignore and (want is None or n in want)]
    walled, checked, complete = [], 0, True
    for name in sorted(todo):
        net = b.nets[name]
        nid = net.id
        # centre cells of this net's pads, by connected piece: reaching another piece = a path exists
        centres = {}
        for p in net.pads:
            if p.layers:
                ix, iy = g.cell(p.x, p.y)
                centres.setdefault(iy * W + ix, set()).add(p.cluster or id(p))
        for p in net.pads:
            if not p.layers:
                continue
            if time.time() - t0 > cap_s or checked >= max_pads:
                complete = False
                break
            checked += 1
            mine = p.cluster or id(p)
            ix, iy = g.cell(p.x, p.y)
            seeds = []
            for dy in range(-3, 4):
                for dx in range(-3, 4):
                    jx, jy = ix + dx, iy + dy
                    if not (0 < jx < W - 1 and 0 < jy < g.H - 1):
                        continue
                    k = jy * W + jx
                    for lay in p.layers:
                        v = S[lay * G + k]
                        if v == 0 or v == nid:
                            seeds.append(lay * G + k)
            if not seeds:
                continue           # cannot even seed on the grid: no proof either way, never fail on it
            seen = set(seeds)
            dq = deque(seeds)
            free = 0
            escaped = False
            while dq:
                c = dq.popleft()
                if S[c] == 0:
                    free += 1
                k = c % G
                other = centres.get(k)
                if free >= min_free or len(seen) >= max_room or (other and any(o != mine for o in other)):
                    escaped = True
                    break
                lay = c // G
                for nk in (k - 1, k + 1, k - W, k + W, k - W - 1, k - W + 1, k + W - 1, k + W + 1):
                    nc = lay * G + nk
                    if nc not in seen:
                        v = S[nc]
                        if v == 0 or v == nid:
                            seen.add(nc)
                            dq.append(nc)
                vv = SV[k]
                if vv == 0 or vv == nid:
                    for l2 in range(L):
                        if l2 != lay:
                            nc = l2 * G + k
                            if nc not in seen:
                                v = S[nc]
                                if v == 0 or v == nid:
                                    seen.add(nc)
                                    dq.append(nc)
            if not escaped:
                near = sorted({q.ref for q in b.pads if q.ref != p.ref and abs(q.x - p.x) < 2.5 and abs(q.y - p.y) < 2.5})
                walled.append({"pad": p.name, "net": name, "at": [round(p.x, 2), round(p.y, 2)],
                               "layers": [b.layers[x] for x in p.layers],
                               "reachable_cells": len(seen), "parts_around": near[:6]})
        if not complete:
            break
    cong = an.congestion(b, cfg)
    pitch = 2 * real[0] + real[1]
    nsig = max(1, L)
    over = []
    for c in cong.get("_cells") or []:
        cap = c["free"] * nsig * cong["cell_mm"] / pitch
        if c["lines"] and c["lines"] > 1.5 * cap:
            x0, y0 = cong["origin"]
            cs = cong["cell_mm"]
            over.append({"rect": [round(x0 + c["i"] * cs, 1), round(y0 + c["j"] * cs, 1),
                                  round(x0 + (c["i"] + 1) * cs, 1), round(y0 + (c["j"] + 1) * cs, 1)],
                         "demand": c["lines"], "capacity": round(cap, 1), "parts": c["refs"][:6]})
    over.sort(key=lambda o: -(o["demand"] - o["capacity"]))
    moves = []
    for w in walled[:8]:
        ref = w["pad"].split(".")[0]
        if w["parts_around"]:
            moves.append({"why": f"{w['pad']} ({w['net']}) is walled in by {', '.join(w['parts_around'][:3])}",
                          "try": f"pcb_place(moves=[{{ref:'{w['parts_around'][0]}', x:..., y:...}}]) to open a gap of "
                                 f"{round(pitch + 0.2, 2)} mm at {w['at']}, or rotate {ref}"})
        else:
            moves.append({"why": f"{w['pad']} ({w['net']}) is walled in by copper / keep-outs / the board edge",
                          "try": f"move {ref} or remove the copper / rule area around {w['at']}"})
    return {"ok": not walled, "seconds": round(time.time() - t0, 2), "checked_pads": checked, "complete": complete,
            "nets_checked": len(todo), "grid_mm": cfg.step, "walled": walled[:20], "walled_total": len(walled),
            "over_capacity": over[:6], "hot_spots": cong.get("hot_spots") or [], "suggested_moves": moves}
