"""Polish (router v2, S6) on real routed boards: what it changes, how long it takes, and what KiCad says.

Takes a board some router already routed (default: the guided router's benchmark output in
``.work/runs/guided/<board>/run1``), turns its copper into v2 pieces (one connection per pad-to-pad /
pad-to-junction trail), runs ``v2.polish.run`` on the real ``RouteState`` and reports

    segments, odd-angle share, share of segments < 0.2 mm, vias, length  (before -> after)
    seconds, work units, candidates, oracle checks, edits per pass
    self-checks: every new piece oracle-legal; the pad connectivity of every net unchanged
    --drc: kicad-cli DRC of the polished board against the DRC of the board as routed (new errors, unconnected)

    PY="C:/Program Files/KiCad/10.0/bin/python.exe"
    $PY bench/route/polish_bench.py                      # every board that has a routed run
    $PY bench/route/polish_bench.py ecc83 coldfire --drc
    $PY bench/route/polish_bench.py --source guided_600 --passes entry,stairs

Scratch only: copies go to bench/route/.work/polish_bench; results to results/polish_bench/<source>.json.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import time
from pathlib import Path

import common

common.sandbox()
from copper_kicad.pcb.guided import model, session  # noqa: E402
from copper_kicad.pcb.v2 import oracle as O  # noqa: E402
from copper_kicad.pcb.v2 import pieces as PC  # noqa: E402
from copper_kicad.pcb.v2 import polish as PL  # noqa: E402
from copper_kicad.pcb.v2.types import Budget, ConnV2, Ctx, Terminal  # noqa: E402


def _key(x, y):
    return (int(round(x * 1e6)), int(round(y * 1e6)))


class Cfg:
    regions = ()
    nets = {}


def scratch(source: str, board: str) -> Path:
    src = common.WORK / "runs" / source / board / "run1"
    d = common.WORK / "polish_bench" / source / board
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
            shutil.copy2(f, d / f.name)
    dru = common.CORPUS / board / f"{board}.kicad_dru"
    if dru.exists() and not (d / dru.name).exists():
        shutil.copy2(dru, d / dru.name)
    return d / f"{board}.kicad_pcb"


def trails(board, orc, tracks, vias):
    """Routed copper -> (connections, piece specs). One connection per trail between pads / junctions / dead
    ends; a via inside a trail belongs to it, a via at a junction is a reservation (conn -1)."""
    by_net = {}
    for t in tracks:
        if t[0] and math.hypot(t[4] - t[2], t[5] - t[3]) > 1e-6:
            by_net.setdefault(t[0], ([], []))[0].append(t)
    for v in vias:
        if v[0]:
            by_net.setdefault(v[0], ([], []))[1].append(v)
    conns, specs = [], []
    for net in sorted(by_net):
        n = board.nets.get(net)
        if n is None:
            continue
        segs, vs = by_net[net]
        nid = n.id
        via_at = {_key(v[1], v[2]): v for v in vs}

        def node(layer, k):
            return ("v", k) if k in via_at else (layer, k)
        adj = {}
        for i, t in enumerate(segs):
            for k, other in ((_key(t[2], t[3]), _key(t[4], t[5])), (_key(t[4], t[5]), _key(t[2], t[3]))):
                adj.setdefault(node(t[1], k), []).append((i, node(t[1], other), t[1], k))

        def pad_at(layer, k):
            x, y = k[0] / 1e6, k[1] / 1e6
            for s in orc.near(layer, (x, y, x, y), 0.0):
                if s.cls == "cu" and s.net == nid and getattr(s.tag, "name", None) and s.dist_pt(x, y) <= 1e-6:
                    return s.tag
            return None

        def terminal(nd):
            lst = adj[nd]
            if len(lst) != 2:
                return True
            for _i, _o, l, k in lst:                  # a pad centre ends a trail; a point merely inside a pad does not
                p = pad_at(l, k)
                if p is not None and _key(p.x, p.y) == k:
                    return True
            return False
        used = set()
        via_owner = {}
        for nd in sorted(adj, key=str):
            if not terminal(nd):
                continue
            for i, other, layer, k in sorted(adj[nd]):
                if i in used:
                    continue
                idx = len(conns)
                mine = []
                a_layer, a_k = layer, k
                cur, here = i, other
                while True:
                    used.add(cur)
                    mine.append(cur)
                    t = segs[cur]
                    if terminal(here):
                        b_layer, b_k = t[1], here[1]
                        break
                    if here[0] == "v":
                        via_owner.setdefault(here[1], idx)
                    nxt = [e for e in adj[here] if e[0] != cur]
                    if not nxt or nxt[0][0] in used:
                        b_layer, b_k = t[1], here[1]
                        break
                    cur, here = nxt[0][0], nxt[0][1]

                def term(layer, k):
                    p = pad_at(layer, k)
                    return Terminal(k[0] / 1e6, k[1] / 1e6, (layer,), pad=p.name if p else None,
                                    cluster=p.cluster if p else 0)
                w = min(segs[j][6] for j in mine)
                conns.append(ConnV2(idx=idx, net=net, a=term(a_layer, a_k), b=term(b_layer, b_k), est=0.0,
                                    role="signal", width=w, neck=w, clearance=orc.clearance(net), via=(n.via, n.drill),
                                    rank=(idx,)))
                for j in mine:
                    t = segs[j]
                    specs.append(dict(net=net, conn=idx, kind="seg", layer=t[1], x1=t[2], y1=t[3], x2=t[4], y2=t[5],
                                      w=t[6], stage="legacy"))
        for j, t in enumerate(segs):              # closed loops without a terminal: left alone
            if j not in used:
                specs.append(dict(net=net, conn=-1, kind="seg", layer=t[1], x1=t[2], y1=t[3], x2=t[4], y2=t[5],
                                  w=t[6], stage="legacy"))
        for k, v in sorted(via_at.items()):
            specs.append(dict(net=net, conn=via_owner.get(k, -1), kind="via", x1=v[1], y1=v[2], w=v[3], drill=v[4],
                              stage="legacy"))
    return conns, specs


def shape(st):
    segs = [p for p in st.pieces() if p.kind == "seg"]
    n = len(segs) or 1
    odd = short = 0
    for p in segs:
        dx, dy = abs(p.x2 - p.x1), abs(p.y2 - p.y1)
        if not (dx < 2e-6 or dy < 2e-6 or abs(dx - dy) < 2e-6):
            odd += 1
        if math.hypot(dx, dy) < 0.2:
            short += 1
    sc = st.score()
    return {"segments": len(segs), "odd_pct": round(100.0 * odd / n, 1), "short_pct": round(100.0 * short / n, 1),
            "vias": sc.vias, "length_mm": round(sc.length_mm, 2), "bends": sc.bends}


def groups(st, nets):
    return {net: sorted(tuple(cs) for cs, _ps in st.net_groups(net, True)) for net in nets}


def run_board(source: str, board: str, passes, drc: bool, seconds: float) -> dict:
    pcb = scratch(source, board)
    ex = session._run(pcb, [{"op": "extract"}], save=False)[0]
    tracks = [t for t in ex.get("tracks") or []]
    vias = [v for v in ex.get("vias") or []]
    bare = dict(ex)
    bare["tracks"], bare["vias"] = [], []
    b = model.Board(bare, model.dru_minimums(pcb.with_suffix(".kicad_dru")))
    orc = O.Oracle(b, overrides=O.local_overrides_from_file(pcb))
    conns, specs = trails(b, orc, tracks, [v for v in vias if v[5] == 0 and v[6] == b.L - 1])
    st = PC.RouteState(orc, conns)
    for s in specs:
        st.add(**s)
    for c in conns:
        st.set_status(c.idx, "routed")
    nets = sorted({c.net for c in conns})
    strict = sum(1 for c in conns if st.connected(c.idx))
    loose = sum(1 for c in conns if st.connected(c.idx, True))
    before = shape(st)
    g0 = groups(st, nets)
    ctx = Ctx(board=b, cfg=Cfg(), oracle=orc, state=st, conns=conns)
    budget = Budget(int(seconds * 200_000))
    t0 = time.perf_counter()
    rep = PL.run(ctx, budget, passes)
    dt = time.perf_counter() - t0
    after = shape(st)
    bad = [p.id for p in st.pieces() if p.stage == "polish" and orc.piece_ok(p) is not None]
    same = groups(st, nets) == g0
    out = {"board": board, "connections": len(conns), "connected_strict": strict, "connected_loose": loose,
           "before": before, "after": after, "seconds": round(dt, 2), "work": rep.work,
           "units_per_s": int(rep.work / dt) if dt > 0 else 0, "data": rep.data, "notes": rep.notes,
           "illegal_new_pieces": len(bad), "pad_connectivity_unchanged": same}
    if drc:
        d0 = common.drc(pcb)
        tr, vi, _ids = st.export()
        session._run(pcb, [{"op": "ripup", "mode": "all", "nets": []}, {"op": "commit", "tracks": tr, "vias": vi},
                           {"op": "fill"}], save=True)
        d1 = common.drc(pcb)
        new = common.new_errors(d1["error_list"], d0["error_list"])
        out["drc"] = {"errors_before": d0["errors"], "errors_after": d1["errors"], "new_errors": len(new),
                      "unconnected_before": d0["unconnected"], "unconnected_after": d1["unconnected"],
                      "new": [{"type": e["type"], "at": e["at"], "items": e["items"]} for e in new[:8]],
                      "blind_vias_dropped": len(vias) - len([v for v in vias if v[5] == 0 and v[6] == b.L - 1])}
    return out


def main(argv):
    source = "guided"
    passes = PL.PASSES
    seconds = 60.0
    drc = "--drc" in argv
    boards = []
    it = iter(argv)
    for a in it:
        if a == "--source":
            source = next(it)
        elif a == "--passes":
            passes = tuple(next(it).split(","))
        elif a == "--seconds":
            seconds = float(next(it))
        elif a == "--slack":
            PL.CLEANUP_SLACK_MM = float(next(it))
        elif not a.startswith("--"):
            boards.append(a)
    root = common.WORK / "runs" / source
    if not boards:
        boards = sorted(d.name for d in root.iterdir() if (d / "run1" / f"{d.name}.kicad_pcb").exists())
    res = []
    for bd in boards:
        try:
            r = run_board(source, bd, passes, drc, seconds)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r = {"board": bd, "error": f"{type(e).__name__}: {e}"}
        res.append(r)
        if "error" in r:
            print(f"{bd:22s} ERROR {r['error']}")
            continue
        b, a = r["before"], r["after"]
        line = (f"{bd:22s} conns {r['connections']:4d} (strict {r['connected_strict']}, loose {r['connected_loose']})  "
                f"segs {b['segments']}->{a['segments']}  odd {b['odd_pct']}->{a['odd_pct']}%  "
                f"<0.2mm {b['short_pct']}->{a['short_pct']}%  vias {b['vias']}->{a['vias']}  "
                f"len {b['length_mm']}->{a['length_mm']}  {r['seconds']} s  {r['work']} units "
                f"({r['units_per_s'] // 1000} k/s)  edits {r['data']['edits']}  "
                f"illegal {r['illegal_new_pieces']}  pads-same {r['pad_connectivity_unchanged']}  "
                f"rejects {r['data']['rejects']} deferred {r['data']['deferred']}")
        if "drc" in r:
            d = r["drc"]
            line += (f"  DRC err {d['errors_before']}->{d['errors_after']} (new {d['new_errors']})  "
                     f"unconnected {d['unconnected_before']}->{d['unconnected_after']}")
        print(line, flush=True)
        for n in r["notes"]:
            print(f"{'':22s} note: {n}")
    outdir = common.HERE / "results" / "polish_bench"
    outdir.mkdir(parents=True, exist_ok=True)
    name = source + ("" if passes == PL.PASSES else "_" + "-".join(passes))
    name += f"_slack{PL.CLEANUP_SLACK_MM:g}" if PL.CLEANUP_SLACK_MM else ""
    (outdir / f"{name}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
