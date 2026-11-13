"""Router adapters for the routing benchmark.

A router is a module ``bench/route/routers/<name>.py`` with:

    NAME = "human readable name"
    def available() -> tuple[bool, str]            # (usable on this machine?, why not)
    def route(project_dir: Path, pcb: Path, options: dict) -> dict

``route`` gets a private copy of an UNROUTED board (``pcb``, with its .kicad_pro / .kicad_dru next to it in
``project_dir``) and must leave the routed board in the same file. Its return value is the router's OWN
report (free-form, stored under ``router_report``); the keys the harness understands are

    routed, total      connections the router believes it made / tried
    failed             [{net, from, to, reason, where:[x, y], blocked_by:[...]}]
    skipped            [{net, connections, reason}]
    plan               the plan / settings that were used
    phases             {name: seconds}

Nothing in that report is trusted for the score: completion, DRC, length, vias and determinism are measured
by the harness from the board file with pcbnew + kicad-cli. ``python bench/route/run.py --router v2`` loads
``routers/v2.py``.
"""
import importlib


def load(name: str):
    return importlib.import_module(f"routers.{name}")
