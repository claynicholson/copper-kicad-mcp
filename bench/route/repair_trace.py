"""Trace what the v2 repair stage (pcb/v2/repair.py) does on a corpus board.

Routes a fresh copy of the unrouted board through the engine (dry: nothing is scored) with the stages up to
``repair`` and prints, for the repair stage: connections failed before, what each ladder step did, the work and
wall time split (repair's own code vs. detail.route_conn), and the certificates of what is left.

    python bench/route/repair_trace.py BOARD [--budget SECONDS] [--json OUT.json] [--stages a,b,c]

Scratch files go to bench/route/.work only (private KiCad config dir, private COPPER_HOME).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))
import common  # noqa: E402

common.sandbox()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--stages", default="classify,power,fanout,global,detail,repair")
    ap.add_argument("--set", action="append", default=[], help="repair constant, e.g. --set RESCUE_FIRST=1")
    ap.add_argument("--profile", action="store_true", help="cProfile the repair stage (top functions by own time)")
    a = ap.parse_args()
    from copper_kicad import project as projmod
    from copper_kicad.pcb import guided
    from copper_kicad.pcb.v2 import detail, repair

    for kv in a.set:
        k, v = kv.split("=", 1)
        setattr(repair, k, type(getattr(repair, k))(float(v)))
    src = HERE / "corpus" / a.board
    work = HERE / ".work" / ("repair_trace_" + a.board + "".join("_" + kv.replace("=", "") for kv in a.set))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for f in src.iterdir():
        if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
            shutil.copy2(f, work / f.name)
    pr = projmod.Project(work, a.board)

    trace = {"board": a.board}
    real_run = repair.run
    real_route = detail.route_conn
    spent = {"s": 0.0, "calls": 0, "soft": 0, "ok": 0}

    def timed_route(ctx, conn, budget, **kw):
        t = time.perf_counter()
        try:
            r = real_route(ctx, conn, budget, **kw)
        finally:
            spent["s"] += time.perf_counter() - t
        spent["calls"] += 1
        spent["soft"] += 1 if kw.get("soft_cost") is not None else 0
        spent["ok"] += 1 if r.get("ok") else 0
        return r

    def traced(ctx, budget, only=None):
        st = ctx.state
        before = [c.idx for c in ctx.conns if st.status(c.idx) != "routed"]
        reasons = {}
        for i in before:
            f = (st.fail(i) or {}) if hasattr(st, "fail") else {}
            k = str(f.get("code") or f.get("reason") or st.status(i))[:60]
            reasons[k] = reasons.get(k, 0) + 1
        try:                                              # which layers may the failed connections use, and how many vias
            from copper_kicad.pcb.v2 import window as wnd
            lays = {}
            for i in before:
                c = repair.memory(ctx).conn(ctx, i)
                nc = ctx.cfg.nets.get(c.net) if ctx.cfg is not None else None
                k = f"layers {tuple(wnd.allowed_layers(ctx, c))} max_vias {getattr(nc, 'max_vias', None)} " \
                    f"escape {c.a.escape}/{c.b.escape} role {c.role}"
                lays[k] = lays.get(k, 0) + 1
            print("failed connections:", json.dumps(lays, indent=1))
        except Exception as e:  # noqa: BLE001
            print("layers: n/a", type(e).__name__, e)
        detail.route_conn = timed_route
        c0 = ctx.oracle.checks
        t0 = time.perf_counter()
        prof = None
        if a.profile:
            import cProfile
            prof = cProfile.Profile()
            prof.enable()
        try:
            rep = real_run(ctx, budget, only=only)
        finally:
            detail.route_conn = real_route
            if prof is not None:
                import io
                import pstats
                prof.disable()
                buf = io.StringIO()
                pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(22)
                print(buf.getvalue()[:6000])
                buf = io.StringIO()
                pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats("repair.py", 25)
                print(buf.getvalue()[:6000])
        dt = time.perf_counter() - t0
        mem = repair.memory(ctx)
        left = {}
        for i, cert in sorted(rep.data.get("certificates", {}).items()):
            left[i] = {"net": mem.conn(ctx, i).net, "reason": cert["reason"][:140], "ladder": cert["ladder"],
                       "cut": cert["cut"], "static_share": cert["static_share"], "mincut_nets": cert["mincut_nets"][:6],
                       "enclosed": cert["probe"]["enclosed"], "suggest": [s["patch"] for s in cert["suggest"]]}
        steps = {}
        for i, t in rep.data.get("tried", {}).items():
            for s in t:
                steps[s] = steps.get(s, 0) + 1
        why = {}
        for e in rep.data.get("log", []):
            k = f"{e['step']} {e.get('rung', '')}: " + ("ok" if e.get("ok") else str(e.get("why", ""))[:70])
            k = "".join(ch if not ch.isdigit() else "#" for ch in k)
            why[k] = why.get(k, 0) + 1
        trace.setdefault("why", []).append(why)
        print("log:", json.dumps(why, indent=1))
        trace.setdefault("runs", []).append({
            "only": None if only is None else len(only), "failed_before": len(before), "fail_codes_before": reasons,
            "repaired": rep.routed, "failed_after": rep.failed, "stats": rep.data.get("stats"), "ladder_steps": steps,
            "work": rep.work, "budget": budget.total, "wall_s": round(dt, 3), "route_conn_s": round(spent["s"], 3),
            "repair_own_s": round(dt - spent["s"], 3), "route_conn_calls": spent["calls"], "soft_calls": spent["soft"],
            "route_ok": spent["ok"], "oracle_checks": ctx.oracle.checks - c0, "notes": rep.notes, "left": left,
            "score": rep.score.key()})
        return rep
    repair.run = traced
    facts = guided.analyze(pr, ripup="none")
    plan = dict(facts["suggested_plan"])
    g = dict(plan.get("global") or {})
    g.update({"engine": "v2", "stages": a.stages.split(",")})
    if a.budget:
        g["time_budget_s"] = a.budget
    plan["global"] = g
    t0 = time.time()
    out = guided.route(pr, plan, ripup="none", dry_run=True)
    trace["route_s"] = round(time.time() - t0, 2)
    trace["result"] = {k: out.get(k) for k in ("routed", "total", "percent")}
    trace["stages"] = [{k: s.get(k) for k in ("name", "routed", "failed", "work", "notes")} for s in out.get("stages") or []]
    for r in trace.get("runs", []):
        left = r.pop("left")
        print(json.dumps(r, default=str))
        for i, c in left.items():
            print("   ", i, json.dumps(c, default=str))
        r["left"] = left
    print("stages:", [(s["name"], s["routed"], s["failed"], s["work"]) for s in trace["stages"]])
    print("result:", trace["result"], "route_s", trace["route_s"])
    if a.json:
        Path(a.json).write_text(json.dumps(trace, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
