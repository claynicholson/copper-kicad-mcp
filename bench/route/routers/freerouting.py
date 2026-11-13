"""Freerouting (Java) through Copper's own pcb_route(mode='auto') pipeline, WITHOUT the guided finisher
(finish=False), so the comparison is Freerouting alone. Needs a Java 25 runtime + the Freerouting jar that
Copper already downloaded (read-only from ~/.copper/tools); the benchmark never downloads anything."""
from __future__ import annotations

import os
import time
from pathlib import Path

NAME = "Freerouting 2.2.3 via pcb_route(mode='auto'), no guided finish"


def _tools():
    shared = Path.home() / ".copper" / "tools"
    java = next(iter(sorted(shared.glob("jre-*/**/bin/java.exe")) or sorted(shared.glob("jre-*/**/bin/java"))), None)
    jar = next(iter(sorted(shared.glob("freerouting-*.jar"))), None)
    return java, jar


def available():
    java, jar = _tools()
    if os.environ.get("COPPER_JAVA") and os.environ.get("COPPER_FREEROUTING_JAR"):
        return True, ""
    if java is None or jar is None:
        return False, "no Java runtime / Freerouting jar under ~/.copper/tools (the benchmark does not download)"
    return True, ""


def route(project_dir: Path, pcb: Path, options: dict) -> dict:
    java, jar = _tools()
    if java is not None:
        os.environ.setdefault("COPPER_JAVA", str(java))
    if jar is not None:
        os.environ.setdefault("COPPER_FREEROUTING_JAR", str(jar))
    from copper_kicad import project as projmod
    from copper_kicad.pcb import route as routemod
    pr = projmod.Project(Path(project_dir), Path(pcb).stem)
    t0 = time.time()
    out = routemod.autoroute(pr, passes=int(options.get("passes", 30)), timeout=int(options.get("timeout", 600)),
                             finish=False, keep_worse=True)
    by = out.get("unrouted_by_net") or {}
    return {"routed": (out.get("unrouted_before") or 0) - (out.get("unrouted_after") or 0),
            "total": out.get("unrouted_before"),
            "failed": [{"net": n, "connections": k, "reason": "left unrouted by Freerouting"} for n, k in sorted(by.items())],
            "skipped": [], "rounds": out.get("rounds"), "timed_out": out.get("timed_out"),
            "reverted": out.get("reverted"), "downloaded": out.get("downloaded"),
            "plane_layers": out.get("plane_layers"), "signal_layers": out.get("signal_layers"),
            "plane_fanout": out.get("plane_fanout"), "necked_nets": out.get("necked_nets"),
            "plan": {"passes": int(options.get("passes", 30)), "timeout": int(options.get("timeout", 600)),
                     "finish": False},
            "phases": {"route_total": round(time.time() - t0, 1), "search": out.get("router_seconds")}}
