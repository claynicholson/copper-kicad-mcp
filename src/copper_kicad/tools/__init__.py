"""Importing this package registers every Copper MCP tool.

Each area lives in its own module. A module that fails to import (e.g. a
missing optional dependency) is reported by `copper doctor` and through
`copper_status`. It never takes the whole server down.

Any `*_tools.py` module dropped into this package is picked up automatically;
MODULES only fixes the order the well-known ones are listed in.
"""
import importlib
import pkgutil
import traceback

MODULES = ["project_tools", "sch_tools", "library_tools", "parts_tools", "verify_tools",
           "pcb_tools", "route_tools", "focus_tools", "fab_tools"]
LOAD_ERRORS: dict[str, str] = {}

_present = {m.name for m in pkgutil.iter_modules(__path__) if m.name.endswith("_tools")}
_order = [m for m in MODULES if m in _present] + sorted(_present - set(MODULES))

for _m in _order:
    try:
        importlib.import_module(f"{__name__}.{_m}")
    except Exception as _e:  # noqa: BLE001
        LOAD_ERRORS[_m] = f"{type(_e).__name__}: {_e}\n" + traceback.format_exc(limit=3)
