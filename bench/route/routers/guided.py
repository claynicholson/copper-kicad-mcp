"""The current guided router (copper_kicad.pcb.guided): pcb_route_plan's suggested plan, used as it is,
then pcb_route_guided. options: {"plan": {...}} is deep-merged over the suggested plan (for experiments;
the baseline passes nothing)."""
from __future__ import annotations

import time
from pathlib import Path

NAME = "guided (grid A* + negotiated rip-up), suggested plan as-is"


def available():
    return True, ""


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def route(project_dir: Path, pcb: Path, options: dict) -> dict:
    from copper_kicad import project as projmod
    from copper_kicad.pcb import guided
    pr = projmod.Project(Path(project_dir), Path(pcb).stem)
    t0 = time.time()
    facts = guided.analyze(pr, ripup="none")
    t1 = time.time()
    plan = facts["suggested_plan"]
    if options.get("plan"):
        plan = _merge(plan, options["plan"])
    out = guided.route(pr, plan, ripup="none")
    t2 = time.time()
    for k in ("_board", "_cfg", "_scratch"):
        out.pop(k, None)
    for e in out.get("failed") or []:
        e.pop("_a", None)
        e.pop("_b", None)
    hot = (facts.get("congestion") or {}).get("hot_spots") or []
    return {"routed": out.get("routed"), "total": out.get("total"), "percent": out.get("percent"),
            "failed": out.get("failed") or [], "skipped": out.get("skipped") or [],
            "removed_after_drc": out.get("removed_after_drc") or [], "notes": out.get("notes") or [],
            "warnings": out.get("warnings") or [], "passes": out.get("passes") or [],
            "repairs": out.get("repairs") or [], "added": out.get("added"),
            "drc_seen_by_router": {k: v for k, v in (out.get("drc") or {}).items()
                                   if k in ("errors", "new_errors", "warnings", "unconnected")},
            "plan": plan, "plan_used": out.get("plan_used"), "hot_spots": hot[:5],
            "phases": {"analyze": round(t1 - t0, 1), "route_total": round(t2 - t1, 1),
                       "search": round(sum(p.get("seconds", 0) for p in out.get("passes") or []), 1)}}
