"""Copper router v2: a staged pipeline on top of the guided router's model.

Design: ``docs/internal/ROUTER_V2_DESIGN.md`` (the single source of truth for the shared
types in ``types.py`` and the module interfaces).

    extract -> classify -> power -> fanout -> global -> detail -> repair -> polish -> judge

v2 imports v1's ``guided.{geom, grid, model, plan}`` and leaves those files
unchanged. The one legality test for everything v2 emits is ``oracle.Oracle``;
grids are search accelerators, never the judge.

This package imports nothing at import time: ``from copper_kicad.pcb.v2 import
types, oracle`` (or any stage module) pulls in only what it names, so stage
modules can land in any order (``engine.available_stages()`` decides what runs).
"""

# every module of the package, in pipeline order (engine.py is the only orchestrator)
MODULES = ("types", "oracle", "pieces", "kio", "legacy", "planx", "classify", "power", "tiles", "globalroute",
           "window", "search", "detail", "flow", "fanout", "repair", "polish", "judge", "diag", "engine")
