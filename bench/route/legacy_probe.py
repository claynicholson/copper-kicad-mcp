"""W1 probe: v2's stage-1 parts on their own — ``pieces.RouteState`` + ``legacy.route_all`` + ``kio`` — on corpus
boards, judged by KiCad. No engine, no judge loop, no repair: signals pass, commit to a scratch copy, up to two
pours passes on the refilled board, one final DRC. It answers "what do connection-level verify / rip and the
oracle as the exact judge give before anything else is added" and measures the legacy stage's throughput.

    python bench/route/legacy_probe.py                       every corpus board (table + results/legacy_probe.json)
    python bench/route/legacy_probe.py one_air_max coldfire  chosen boards
    python bench/route/legacy_probe.py --twice ecc83         route twice, compare the copper (determinism)
    python bench/route/legacy_probe.py --rip net             v1's whole-net negotiation rip (A/B against 'conn')
    python bench/route/legacy_probe.py --no-emit-check       oracle only in the final self-check (A/B)
    python bench/route/legacy_probe.py --budget 600          plan.global.time_budget_s

The benchmark of record is ``run.py --router v2`` (the whole engine, scored by the harness); this script is a
diagnostic for the legacy stage. Scratch files stay under bench/route/.work.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


def run(name: str, budget_s: float | None = None, pours: bool = True, rip_level: str | None = None,
        emit_check: bool | None = None, tag: str = "probe") -> dict:
    """Route corpus board ``name`` with the W1 parts only; -> measured facts (see module doc)."""
    from copper_kicad import project as projmod
    from copper_kicad.pcb.guided import model, plan as planmod
    from copper_kicad.pcb.v2 import kio, legacy, oracle as O, pieces as PS
    from copper_kicad.pcb.v2.types import WORK_PER_SECOND, Budget, Ctx
    saved = (legacy.RIP_LEVEL, legacy.EMIT_CHECK)
    if rip_level:
        legacy.RIP_LEVEL = rip_level
    if emit_check is not None:
        legacy.EMIT_CHECK = emit_check
    src = common.CORPUS / name
    work = common.WORK / "legacy_probe" / tag / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for f in src.iterdir():
        if f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            shutil.copy2(f, work / f.name)
    out = {"board": name, "rip_level": legacy.RIP_LEVEL, "emit_check": legacy.EMIT_CHECK}
    sc = None
    try:
        pr = projmod.Project(work, name)
        dru = model.dru_minimums(pr.dir / f"{pr.name}.kicad_dru")
        t0 = time.time()
        ex = kio.extract(pr, "none", None)
        total = ex.get("unconnected")
        b = model.Board(ex, dru)
        plan = planmod.default_plan(b, planmod.classify(b))
        if budget_s:
            plan.setdefault("global", {})["time_budget_s"] = budget_s
        cfg = planmod.resolve(b, plan)
        o = O.Oracle(b, overrides=O.local_overrides_from_file(pr.pcb))
        conns = legacy.plan_connections(b, cfg, "signals")
        st = PS.RouteState(o, conns)
        ctx = Ctx(board=b, cfg=cfg, oracle=o, state=st, conns=conns)
        budget = Budget(int(cfg.time_budget * WORK_PER_SECOND))
        c0 = o.checks
        t1 = time.time()
        rep = legacy.route_all(ctx, budget)
        dt = time.time() - t1
        stats = rep.data["stats"]
        out["grid_mm"] = cfg.step
        out["signals"] = {"conns": len(conns), "routed": rep.routed, "failed": rep.failed, "seconds": round(dt, 1),
                          "work": rep.work, "stats": stats, "oracle_checks": o.checks - c0,
                          "expansions_per_s": int(stats["expansions"] / dt) if dt > 0.05 else None,
                          "pieces": len(st), "not_connected": len(rep.data["not_connected"]),
                          "self_check": sum(1 for f in rep.data["failed"] if f["reason"] == legacy.SELF_CHECK),
                          "budget_out": sum(1 for f in rep.data["failed"] if "time budget" in f["reason"])}
        out["illegal_pieces"] = sum(1 for p in st.pieces() if o.piece_ok(p) is not None)
        tracks, vias, ids = st.export()
        sig = [tracks, vias]
        base_raw = kio.baseline_drc(pr)
        sc = kio.scratch(pr)
        res = kio.commit_and_check(pr, sc, tracks, vias, base_raw, drc=not pours)
        all_ids, all_uuids = list(ids), list(res["uuids"])
        if pours:
            for _rnd in range(2):
                b2 = model.Board(res["ex"], dru)
                cfg2 = planmod.resolve(b2, plan)
                o2 = O.Oracle(b2, overrides=O.local_overrides_from_file(sc.pcb))
                conns2 = legacy.plan_connections(b2, cfg2, "pours")
                if not conns2:
                    break
                st2 = PS.RouteState(o2, conns2)
                ctx2 = Ctx(board=b2, cfg=cfg2, oracle=o2, state=st2, conns=conns2)
                t2 = time.time()
                rep2 = legacy.route_all(ctx2, Budget(max(budget.left(), int(0.2 * budget.total))))
                out.setdefault("pours", []).append(
                    {"conns": len(conns2), "routed": rep2.routed, "failed": rep2.failed,
                     "seconds": round(time.time() - t2, 1), "stats": rep2.data["stats"],
                     "self_check": sum(1 for f in rep2.data["failed"] if f["reason"] == legacy.SELF_CHECK),
                     "illegal_pieces": sum(1 for p in st2.pieces() if o2.piece_ok(p) is not None)})
                tr2, vi2, _ids2 = st2.export()
                sig += [tr2, vi2]
                if not tr2 and not vi2:
                    break
                res = kio.commit_and_check(None, sc, tr2, vi2, base_raw, fresh=False, drc=False)
            res["raw"] = kio._s._raw_drc(sc)
            res["new"] = kio.new_markers(res["raw"], base_raw)
            res["stats"] = kio._s._run(sc.pcb, [{"op": "stats"}], save=False)[0]
        left = res["stats"]["unconnected"]
        out["connections"] = total
        out["unconnected_after"] = left
        out["done_pct"] = round(100.0 * (total - left) / total, 1) if total else 100.0
        out["new_errors"] = [[m["type"], m.get("at")] for m in res["new"]]
        out["new_detail"] = [{"type": m["type"], "message": m["message"], "items": m["items"], "at": m.get("at")}
                             for m in res["new"]]
        # blame against the signal pass's pieces only (the pours passes work on fresh states of their own)
        out["blamed"] = [kio.marker_pieces(m, all_uuids, all_ids, st) for m in res["new"]]
        unc = res["stats"]["unconnected_by_net"]
        # nets whose every signal connection the router calls routed, but KiCad still lists as unconnected
        out["routed_but_open_nets"] = sorted(
            n for n in {c.net for c in conns} if unc.get(n)
            and all(st.status(c.idx) == "routed" for c in conns if c.net == n))
        out["signature"] = hashlib.sha1(json.dumps(sig).encode()).hexdigest()[:16]
        out["seconds"] = round(time.time() - t0, 1)
    finally:
        legacy.RIP_LEVEL, legacy.EMIT_CHECK = saved
        if sc is not None:
            shutil.rmtree(sc.dir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    return out


def baseline() -> dict:
    """done % of the guided (v1) baseline per board, from results/guided.json when it is there."""
    try:
        d = json.loads((common.RESULTS / "guided.json").read_text(encoding="utf-8"))
        rows = d.get("boards") if isinstance(d, dict) else d
        out = {}
        for r in rows or []:
            run0 = (r.get("runs") or [{}])[0]
            out[r.get("board")] = (run0.get("completion_pct"), run0.get("drc_new_errors"))
        return out
    except Exception:  # noqa: BLE001
        return {}


def main(argv) -> int:
    common.sandbox()
    flags = [a for a in argv if a.startswith("--")]
    args = [a for a in argv if not a.startswith("--")]
    vals = {}
    for k in ("--rip", "--budget", "--name"):
        if k in argv:
            i = argv.index(k)
            vals[k] = argv[i + 1]
            args.remove(argv[i + 1])
    boards = args or sorted(p.name for p in common.CORPUS.iterdir() if (p / "meta.json").exists())
    base = baseline()
    rows = []
    for name in boards:
        try:
            r = run(name, budget_s=float(vals["--budget"]) if "--budget" in vals else None, rip_level=vals.get("--rip"),
                    emit_check=False if "--no-emit-check" in flags else None)
            if "--twice" in flags:
                r2 = run(name, budget_s=float(vals["--budget"]) if "--budget" in vals else None,
                         rip_level=vals.get("--rip"), emit_check=False if "--no-emit-check" in flags else None, tag="probe2")
                r["same_twice"] = r2["signature"] == r["signature"]
        except Exception as e:  # noqa: BLE001
            import traceback
            r = {"board": name, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}
        rows.append(r)
        s = r.get("signals") or {}
        b0 = base.get(name) or (None, None)
        print(f"{name:20s} done {r.get('done_pct')} % (v1 baseline {b0[0]}), new DRC {len(r.get('new_errors') or [])} "
              f"(v1 {b0[1]}), signals {s.get('routed')}/{s.get('conns')} in {s.get('seconds')} s, "
              f"{s.get('expansions_per_s')} exp/s, emit rejects {(s.get('stats') or {}).get('emit_rejects')}, "
              f"self-check {s.get('self_check')}, illegal {r.get('illegal_pieces')}, not-connected {s.get('not_connected')}, "
              f"routed-but-open nets {len(r.get('routed_but_open_nets') or [])}"
              + (f", same twice {r.get('same_twice')}" if "same_twice" in r else "")
              + (f"  ERROR {r['error']}" if r.get("error") else ""), flush=True)
    name = vals.get("--name") or "legacy_probe"
    (common.RESULTS / f"{name}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    ok = [r for r in rows if "done_pct" in r]
    tot = sum(r["connections"] for r in ok)
    left = sum(r["unconnected_after"] for r in ok)
    if tot:
        print(f"TOTAL {tot - left}/{tot} = {100.0 * (tot - left) / tot:.1f} %, new DRC errors "
              f"{sum(len(r['new_errors']) for r in ok)}, boards complete and clean "
              f"{sum(1 for r in ok if not r['unconnected_after'] and not r['new_errors'])}/{len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
