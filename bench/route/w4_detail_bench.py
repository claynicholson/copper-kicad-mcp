"""W4 measurement: run router v2's detail stage (v2/detail.py) alone on corpus boards.

No global stage is used (box corridors), no fan-out, no repair, no KiCad DRC: this measures window-build cost,
search throughput and how many signal connections pattern + window search close on their own, and checks every
emitted piece with the oracle. Connections come from v1's ``Router.plan_connections`` (signals pass only).

    python bench/route/w4_detail_bench.py [board ...] [--budget SECONDS] [--kicad-drc] [--global]

--global runs W3's global stage first (10 % of the budget), so detail works inside real corridors.

The extract of each board is cached under bench/route/.work/w4/. Nothing outside bench/route/.work is written.
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

common.sandbox()
sys.path.insert(0, str(common.REPO / "tests"))
from copper_kicad.pcb.guided import model, plan as planmod, router as v1router, session  # noqa: E402
from copper_kicad.pcb.v2 import detail, oracle as O, pieces as P, window as wnd  # noqa: E402
from copper_kicad.pcb.v2.types import WORK_PER_SECOND, Budget, ConnV2, Ctx, Terminal  # noqa: E402

WORK = common.WORK / "w4"


def scratch(board: str) -> Path:
    d = WORK / board
    d.mkdir(parents=True, exist_ok=True)
    for f in (common.CORPUS / board).iterdir():
        if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
            shutil.copy2(f, d / f.name)
    return d / f"{board}.kicad_pcb"


def load(board: str):
    """-> (model.Board, oracle overrides, pcb path); the extract is cached as JSON."""
    pcb = scratch(board)
    cache = WORK / f"{board}.extract.json"
    if cache.exists():
        ex = json.loads(cache.read_text(encoding="utf-8"))
    else:
        ex = session._run(pcb, [{"op": "extract"}], save=False)[0]
        cache.write_text(json.dumps(ex), encoding="utf-8")
    b = model.Board(ex, model.dru_minimums(pcb.with_suffix(".kicad_dru")))
    return b, O.local_overrides_from_file(pcb), pcb


def make_ctx(b, overrides, plan=None):
    cfg = planmod.resolve(b, plan if plan is not None else planmod.default_plan(b))
    r = v1router.Router.__new__(v1router.Router)       # only plan_connections is used: no board-wide grid
    r.b, r.cfg, r.mode, r.conns, r.skipped = b, cfg, "signals", [], []
    conns = []
    for c in r.plan_connections():
        nc = cfg.net(c.net)
        neck = min([w for w in nc.neck_widths if w < nc.width - 1e-6] or [nc.width])
        conns.append(ConnV2(idx=c.idx, net=c.net,
                            a=Terminal(c.pa.x, c.pa.y, tuple(c.pa.layers), pad=c.pa.name, cluster=c.a),
                            b=Terminal(c.pb.x, c.pb.y, tuple(c.pb.layers), pad=c.pb.name, cluster=c.b),
                            est=c.est, width=nc.width, neck=neck, clearance=nc.clearance, via=(nc.via, nc.drill),
                            rank=tuple(c.key), tree_target=True))
    orc = O.Oracle(b, overrides=overrides)
    ctx = Ctx(board=b, cfg=cfg, oracle=orc, conns=conns)
    if getattr(P, "STUB", False):
        import v2_w4_support
        ctx.state = v2_w4_support.FakeState(orc, conns)
    else:
        ctx.state = P.RouteState(orc, conns)
    return ctx


def run_board(board: str, seconds: float | None = None, kicad_drc: bool = False, with_global: bool = False) -> dict:
    b, ov, pcb = load(board)
    ctx = make_ctx(b, ov)
    secs = seconds if seconds is not None else ctx.cfg.time_budget
    budget = Budget(int(secs * WORK_PER_SECOND))
    wnd.clear_cache()
    glob = None
    if with_global:                                    # W3's S3 first: detail then runs inside real corridors
        from copper_kicad.pcb.v2 import globalroute
        ctx.x = getattr(ctx.cfg, "x", None)
        if ctx.x is None:
            from copper_kicad.pcb.v2 import planx
            ctx.x = planx.V2Cfg()
        t0 = time.perf_counter()
        grep = globalroute.run(ctx, budget.sub(0.10))
        glob = {"seconds": round(time.perf_counter() - t0, 2), "corridors": len(ctx.corridors),
                "tile_mm": getattr(ctx.tiles, "t", None), "notes": grep.notes[:3]}
    t0 = time.perf_counter()
    rep = detail.run(ctx, budget)
    wall = time.perf_counter() - t0
    bad = [p for p in ctx.state.pieces() if ctx.oracle.piece_ok(p) is not None]
    d = rep.data
    segs = [p for p in ctx.state.pieces() if p.kind == "seg"]
    odd = sum(1 for p in segs if not detail._aligned(p.x2 - p.x1, p.y2 - p.y1))
    short = sum(1 for p in segs if p.length < 0.2)
    out = {"board": board, "conns": len(ctx.conns), "routed": rep.routed, "failed": rep.failed,
           "pct": round(100.0 * rep.routed / max(1, len(ctx.conns)), 1), "wall_s": round(wall, 2),
           "work": rep.work, "budget": budget.total, "oracle_illegal": len(bad), "how": d["how"],
           "fail_codes": d["fail_codes"], "windows": d["windows"], "searches": d["searches"],
           "expansions": d["expansions"], "median_exp": d["median_expansions"],
           "build_s": d["build_s"], "search_s": d["search_s"], "emit_s": d["emit_s"], "pattern_s": d["pattern_s"],
           "build_share": d["build_share"], "exp_per_s": round(d["expansions"] / d["search_s"]) if d["search_s"] else 0,
           "ms_per_window": round(1000 * d["build_s"] / max(1, d["windows"]), 2),
           "cells_per_window": round(d["window_cells"] / max(1, d["windows"])),
           "emit_rejects": d["emit_rejects"], "necked": d["necked"], "segs": len(segs),
           "vias": sum(1 for p in ctx.state.pieces() if p.kind == "via"),
           "odd_angle_pct": round(100.0 * odd / max(1, len(segs)), 1),
           "short_seg_pct": round(100.0 * short / max(1, len(segs)), 1)}
    if glob is not None:
        out["global"] = glob
    if kicad_drc:
        tracks, vias, _ids = [], [], []
        for p in ctx.state.pieces():
            if p.kind == "seg":
                tracks.append([p.net, b.layers[p.layer], p.x1, p.y1, p.x2, p.y2, p.w])
            else:
                vias.append([p.net, p.x1, p.y1, p.w, p.drill])
        base = common.drc(pcb)
        session._run(pcb, [{"op": "commit", "tracks": tracks, "vias": vias}], save=True)
        after = common.drc(pcb)
        out["kicad"] = {"errors_before": base["errors"], "errors_after": after["errors"],
                        "by_type_after": {k: v for k, v in after["by_type"].items() if k.startswith("error")}}
    return out


def main(argv):
    seconds = None
    kd = "--kicad-drc" in argv
    wg = "--global" in argv
    if "--budget" in argv:
        seconds = float(argv[argv.index("--budget") + 1])
    boards = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--budget")]
    boards = boards or sorted(p.name for p in common.CORPUS.iterdir() if p.is_dir())
    rows = []
    for bname in boards:
        try:
            r = run_board(bname, seconds, kd, wg)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r = {"board": bname, "error": f"{type(e).__name__}: {e}"}
        rows.append(r)
        print(json.dumps(r), flush=True)
    (WORK / "last.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
