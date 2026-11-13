"""Router v2 (copper_kicad.pcb.v2) through the same entry point as the guided router: pcb_route_plan's
suggested plan with ``global.engine = "v2"``, then pcb_route_guided (design §11).

options:
    {"plan": {...}}        deep-merged over the suggested plan (after the engine switch, so it can override it)
    {"stages": [...]}      shorthand for plan.global.stages (subset / prefix of classify, power, fanout, global,
                           detail, repair, polish, judge)
    {"detail": "legacy"}   the detail slot runs v1's search (v2/legacy.py) instead of the window search: sets the
                           engine's switch COPPER_V2_DETAIL for the call ("window" = the default)
    {"classify": "full"}   force W5's classify (COPPER_V2_CLASSIFY) where the engine would use v1's plan
    {"fallback": false}    no legacy fallback after the window search (COPPER_V2_FALLBACK=0: the window search alone)
    {"alt": false}         no second opinion (COPPER_V2_ALT=0): the staged pipeline's own result, whatever it is
    {"order": "repair"}    repair ladder before the legacy fallback (COPPER_V2_ORDER; default: fallback first)
    {"stage1": true}       build stage 1 of the design (§12): stages classify, detail, judge + detail = legacy
    {"keep_skips": true}   keep v1's automatic "skip: true" of a ground net without a pour (default: dropped by
                           planx.v2_plan — v2 routes such a net as power, design §4.3)
    {"planes": false}      no plane rescue (COPPER_V2_PLANES=0)

    run.py --router v2 [--board X] --runs 2
    run.py --router v2 --name v2_stage1 --opt stage1=true
    run.py --router v2 --name v2_noglobal --opt stages='["classify","detail","repair","polish","judge"]'
    run.py --router v2 --name v2_b300 --opt plan='{"global":{"time_budget_s":300}}'

The report is the guided adapter's plus v2's own diagnostics: ``stages`` (per stage: routed, failed, score, work,
notes), ``cuts``, ``critique``, ``suggest``, ``auto`` and which stage modules were built (``stages_built``).
Nothing in it is trusted for the score (the harness measures the board file).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

NAME = "v2 (staged: classify, power, fanout, global, detail, repair, polish, judge), suggested plan as-is"

_CAPS = {"cuts": 10, "critique": 30, "suggest": 12}


def available():
    try:
        from copper_kicad.pcb.guided import plan as planmod
        from copper_kicad.pcb.v2 import engine, kio, legacy, pieces
    except Exception as e:  # noqa: BLE001
        return False, f"copper_kicad.pcb.v2 does not import: {type(e).__name__}: {e}"
    for m in (engine, pieces, kio, legacy):
        if getattr(m, "STUB", False):
            return False, f"{m.__name__} is still a stub (docs/ROUTER_V2_DESIGN.md §12: stage 1 needs pieces, kio, legacy, judge, engine)"
    if "engine" not in planmod.GLOBAL_FIELDS:
        return False, "plan.global.engine is not a plan field yet (guided/plan.py)"
    return True, ""


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _plain(v):
    """JSON-safe copy (the harness stores the report as JSON)."""
    return json.loads(json.dumps(v, default=str))


def route(project_dir: Path, pcb: Path, options: dict) -> dict:
    from copper_kicad import project as projmod
    from copper_kicad.pcb import guided
    pr = projmod.Project(Path(project_dir), Path(pcb).stem)
    t0 = time.time()
    facts = guided.analyze(pr, ripup="none")
    t1 = time.time()
    # the suggested plan as v2 takes it: engine switched, v1's automatic "skip the ground without a pour" dropped
    # (planx.v2_plan; {"keep_skips": true} sends the suggested plan exactly as v1 wrote it)
    from copper_kicad.pcb.v2 import planx
    if options.get("keep_skips"):
        plan = _merge(facts["suggested_plan"], {"global": {"engine": "v2"}})
    else:
        plan = planx.v2_plan(facts["suggested_plan"])
    env = {}
    if options.get("stage1"):
        plan = _merge(plan, {"global": {"stages": ["classify", "detail", "judge"]}})
        env["COPPER_V2_DETAIL"] = "legacy"
    if options.get("stages"):
        plan = _merge(plan, {"global": {"stages": list(options["stages"])}})
    if options.get("detail"):
        env["COPPER_V2_DETAIL"] = str(options["detail"])
    if options.get("fallback") is not None:
        env["COPPER_V2_FALLBACK"] = "1" if options["fallback"] else "0"
    if options.get("alt") is not None:
        env["COPPER_V2_ALT"] = "1" if options["alt"] else "0"
    if options.get("order"):
        env["COPPER_V2_ORDER"] = str(options["order"])
    if options.get("planes") is not None:
        env["COPPER_V2_PLANES"] = "1" if options["planes"] else "0"
    if options.get("classify"):
        env["COPPER_V2_CLASSIFY"] = str(options["classify"])
    if options.get("plan"):
        plan = _merge(plan, options["plan"])
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        out = guided.route(pr, plan, ripup="none")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    t2 = time.time()
    for k in [k for k in out if k.startswith("_")]:
        out.pop(k, None)
    for e in out.get("failed") or []:
        for k in [k for k in e if k.startswith("_")]:
            e.pop(k, None)
    hot = (facts.get("congestion") or {}).get("hot_spots") or []
    passes = out.get("passes") or []
    stages = out.get("stages") or []
    try:
        from copper_kicad.pcb.v2 import engine
        built = engine.available_stages()
    except Exception:  # noqa: BLE001
        built = {}
    rep = {"routed": out.get("routed"), "total": out.get("total"), "percent": out.get("percent"),
           "failed": out.get("failed") or [], "skipped": out.get("skipped") or [],
           "removed_after_drc": out.get("removed_after_drc") or [], "notes": out.get("notes") or [],
           "warnings": out.get("warnings") or [], "passes": passes,
           "repairs": out.get("repairs") or [], "added": out.get("added"),
           "drc_seen_by_router": {k: v for k, v in (out.get("drc") or {}).items()
                                  if k in ("errors", "new_errors", "constraint_errors", "new_top", "warnings", "unconnected")},
           "plan": plan, "plan_used": out.get("plan_used"), "hot_spots": hot[:5],
           "engine": (plan.get("global") or {}).get("engine"), "stages_built": built, "switches": env,
           "stages": stages, "auto": out.get("auto") or {}, "judge": out.get("judge") or {}, "portfolio": out.get("portfolio") or [],
           "constraints": out.get("constraints") or {},
           "phases": {"analyze": round(t1 - t0, 1), "route_total": round(t2 - t1, 1),
                      "search": round(sum(p.get("seconds", 0) or 0 for p in passes), 1)}}
    for key, cap in _CAPS.items():
        rep[key] = (out.get(key) or [])[:cap]
    return _plain(rep)
