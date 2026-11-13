"""Focus — let the human point at things so any model can act on them.

Three channels, one normalised *focus* object::

    {'document': 'schematic'|'pcb', 'sheet': '/amp' (schematic), 'mode': 'box'|'point'|'selection',
     'box_mm': [x0,y0,x1,y1] | 'point_mm': [x,y], 'refs': ['C5','R3','U2'], 'nets': ['/EN','GND'],
     'pins': ['U2.EN'], 'age_s': 4.2, 'source': 'canvas'|'kicad'|'args'}

A. Copper Canvas (`canvas`)  — local viewer: hover, click = point, drag = box, shift = add, Esc = clear.
B. KiCad selection (`live`)  — the PCB editor's selection over KiCad's IPC API (optional `kicad-python`).
C. Explicit arguments        — `resolve(pr, document, box=… | near=…)`: pure, from the files alone.

Public API (stable — other tools build their `region` / `near` parameters on it)::

    resolve(pr, document, box=None, near=None, radius=None, sheet=None, side=None) -> dict
    select(pr, document, refs, pins=(), sheet=None) -> dict
    latest(pr=None) -> focus | None            # newest stored focus, cheap (file read)
    as_resource(pr=None) -> {'uri','mimeType','text'}   # for MCP resources
    current(pr, source='auto', wait=0) -> (focus | None, info)
    detail(pr, focus, render=True) -> ({'focus','chip','items',…}, [Image])
    point(pr, document, …) / clear(pr)         # what the canvas calls
    canvas.start(pr, launch=False) -> {'url', …};  canvas.serve(project=None, port=0, launch=True)  (CLI)
"""
from __future__ import annotations

from . import geometry, scene, store  # noqa: F401
from .api import as_resource, clear, current, detail, latest, point, public  # noqa: F401
from .geometry import ViewMap  # noqa: F401
from .resolver import chip, describe, resolve, select, to_focus  # noqa: F401
from .scene import sheets  # noqa: F401

__all__ = ["resolve", "select", "latest", "as_resource", "current", "detail", "point", "clear", "public",
           "chip", "describe", "to_focus", "sheets", "ViewMap", "canvas", "live", "svg"]


def __getattr__(name):          # canvas / live / svg load on first use (http.server, kipy, Pillow)
    if name in ("canvas", "live", "svg"):
        import importlib
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(name)
