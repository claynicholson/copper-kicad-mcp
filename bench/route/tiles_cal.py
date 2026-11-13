"""Router v2, W3: tile-capacity calibration against the human-routed corpus references and global-route
measurements (design 4.6 calibration gate, Appendix A3).

    PY bench/route/tiles_cal.py cal [board ...]        capacity vs human demand per board + the pooled gate
    PY bench/route/tiles_cal.py global [board ...]     tile build + negotiated global route: time, overflow, vias
    PY bench/route/tiles_cal.py extract [board ...]    (re)build the cached worker extracts only

The worker extract of every UNROUTED corpus board is cached in bench/route/.work/w3/ex_<board>.json (read
only: nothing is saved to the corpus). Results: bench/route/results/tiles_cal/*.json.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

CACHE = common.WORK / "w3"
OUT = common.RESULTS / "tiles_cal"


def boards() -> list[str]:
    return sorted(d.name for d in common.CORPUS.iterdir() if (d / f"{d.name}.kicad_pcb").exists())


def extract(board: str, force: bool = False) -> dict:
    """The worker extract of the unrouted corpus board (cached)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"ex_{board}.json"
    if f.exists() and not force:
        return json.loads(f.read_text(encoding="utf-8"))
    common.sandbox()
    from copper_kicad.pcb.guided import session
    d = CACHE / "scratch" / board
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    for src in (common.CORPUS / board).iterdir():
        if src.is_file() and src.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
            shutil.copy2(src, d / src.name)
    ex = session._run(d / f"{board}.kicad_pcb", [{"op": "extract"}], save=False)[0]
    f.write_text(json.dumps(ex), encoding="utf-8")
    return ex


def load(board: str):
    """-> (model.Board, Oracle) of the unrouted corpus board."""
    common.sandbox()
    from copper_kicad.pcb.guided import model
    from copper_kicad.pcb.v2 import oracle as O
    ex = extract(board)
    pcb = common.CORPUS / board / f"{board}.kicad_pcb"
    b = model.Board(ex, model.dru_minimums(pcb.with_suffix(".kicad_dru")))
    return b, O.Oracle(b, overrides=O.local_overrides_from_file(pcb))


def reference(board: str) -> list | None:
    f = common.CORPUS / board / "reference.json"
    if not f.exists():
        return None
    ref = json.loads(f.read_text(encoding="utf-8"))
    return [s[:7] for s in ref["segments"] if s[0]]


def conns_v1(b, plan=None):
    """(Config, [ConnV2]) from the v1 planner (classify.py may still be a stub): signal connections only."""
    from copper_kicad.pcb.guided import plan as P, router as R
    from copper_kicad.pcb.v2.types import ConnV2, Terminal
    cfg = P.resolve(b, plan)
    b.make_grid(cfg.step)
    r = R.Router(b, cfg)
    out = []
    for c in r.plan_connections():
        nc = cfg.net(c.net)
        ta = Terminal(c.pa.x, c.pa.y, tuple(c.pa.layers), c.pa.name, c.a)
        tb = Terminal(c.pb.x, c.pb.y, tuple(c.pb.layers), c.pb.name, c.b)
        out.append(ConnV2(idx=c.idx, net=c.net, a=ta, b=tb, est=c.est, role="signal", width=nc.width,
                          neck=(nc.neck_widths or [nc.width])[0], clearance=nc.clearance, via=(nc.via, nc.drill),
                          rank=c.key, tree_target=True))
    return cfg, out


def run_cal(names: list[str]) -> dict:
    from copper_kicad.pcb.v2 import tiles as TL
    OUT.mkdir(parents=True, exist_ok=True)
    cals = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3)
    table = {}
    pool = {c: [0, 0, 0] for c in cals}          # short, borders, used
    poolw = {c: 0 for c in cals}
    hist = {}
    for n in names:
        tracks = reference(n)
        if tracks is None:
            print(f"{n}: no reference routing (synthetic board) - skipped")
            continue
        b, o = load(n)
        t0 = time.perf_counter()
        t = TL.make(b, o, cal=1.0)
        dt = time.perf_counter() - t0
        res = TL.calibration(t, tracks, cals)
        res["by_width"] = {k: v for k, v in TL.calibration(t, tracks, cals, by="width").items()
                           if k in ("by_cal", "ratio_hist", "zero_cap_used")}
        raw = TL.reference_demand(t, tracks, own=False)
        res.update(tile_mm=t.t, nx=t.nx, ny=t.ny, nl=t.nl, ref_w=t.ref_w, ref_cl=t.ref_cl, build_s=round(dt, 3),
                   used_without_own_pad_exemption=len(raw))
        table[n] = res
        for c in cals:
            pool[c][0] += res["by_cal"][c]["short"]
            pool[c][1] += res["borders"]
            pool[c][2] += res["used"]
            poolw[c] += res["by_width"]["by_cal"][c]["short"]
        for k, v in res["ratio_hist"].items():
            hist[k] = hist.get(k, 0) + v
        print(f"{n}: T={t.t} {t.nx}x{t.ny}x{t.nl} build {dt:.2f}s  borders {res['borders']} used {res['used']} "
              f"zero-cap used {res['zero_cap_used']}  short@1.0 {res['by_cal'][1.0]['short']} "
              f"ok_all {res['by_cal'][1.0]['ok_all']:.4f} ok_used {res['by_cal'][1.0]['ok_used']:.4f}")
    pooled = {str(c): {"short": s, "ok_all": round(1 - s / bo, 5) if bo else 1.0,
                       "ok_used": round(1 - s / u, 5) if u else 1.0} for c, (s, bo, u) in pool.items()}
    pooled_w = {str(c): {"short": poolw[c], "ok_all": round(1 - poolw[c] / pool[c][1], 5) if pool[c][1] else 1.0,
                         "ok_used": round(1 - poolw[c] / pool[c][2], 5) if pool[c][2] else 1.0} for c in cals}
    print("pooled (count reading):", json.dumps(pooled))
    print("pooled (width reading):", json.dumps(pooled_w))
    print("ratio histogram (demand / capacity, used borders):", hist)
    out = {"boards": table, "pooled": pooled, "pooled_by_width": pooled_w, "ratio_hist": hist}
    (OUT / "calibration.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


def run_global(names: list[str], budget_s: float = 9.0) -> dict:
    from copper_kicad.pcb.v2 import globalroute as GR, tiles as TL
    from copper_kicad.pcb.v2.types import WORK_PER_SECOND, Budget, Ctx
    OUT.mkdir(parents=True, exist_ok=True)
    table = {}
    for n in names:
        b, o = load(n)
        cfg, conns = conns_v1(b)
        t0 = time.perf_counter()
        ctx = Ctx(board=b, cfg=cfg, oracle=o, conns=conns)
        ctx.tiles = TL.make(b, o, conns)
        t1 = time.perf_counter()
        bud = Budget(int(budget_s * WORK_PER_SECOND))
        rep = GR.run(ctx, bud)
        t2 = time.perf_counter()
        d = rep.data
        vias = sum(len(c.layer_seq) for c in ctx.corridors.values())
        row = {"conns": len(conns), "routed": rep.routed, "failed": rep.failed, "tile_mm": ctx.tiles.t,
               "nodes": ctx.tiles.N, "tiles_s": round(t1 - t0, 3), "global_s": round(t2 - t1, 3), "work": rep.work,
               "expansions": d.get("expansions"),
               "expansions_per_s": int((d.get("expansions") or 0) / max(1e-9, t2 - t1)),
               "iterations": d.get("iterations"), "overflow_mm": d.get("overflow_mm"),
               "overflow_first_mm": d.get("overflow_first_mm"), "cuts": len(d.get("cuts") or []), "vias": vias,
               "layer_dirs": d.get("layer_dirs"), "notes": rep.notes}
        table[n] = row
        print(n, json.dumps(row))
    (OUT / "global.json").write_text(json.dumps(table, indent=1, default=str), encoding="utf-8")
    return table


def main(argv):
    mode = argv[1] if len(argv) > 1 else "cal"
    names = [a for a in argv[2:] if not a.startswith("-")] or boards()
    common.sandbox()
    if mode == "extract":
        for n in names:
            t0 = time.time()
            ex = extract(n, force="--force" in argv)
            print(n, len(ex.get("pads") or []), "pads", round(time.time() - t0, 1), "s")
    elif mode == "cal":
        run_cal(names)
    elif mode == "global":
        run_global(names)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
