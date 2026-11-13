"""focus — what the human is pointing at (Copper Canvas, KiCad selection, or explicit mm arguments).

Never changes the design. `open` starts a loopback-only viewer; `clear` forgets the stored focus.
"""
from __future__ import annotations

from .. import focus as F
from .. import project as projmod
from ..mcp.registry import Result, ToolError, tool

PROJECT = {"type": "string", "description": "Project folder or .kicad_pro/.kicad_pcb path. Omit to use the active project."}

HOW_TO_POINT = ("in the Copper Canvas: click a part or pin = point at it, drag = box, shift = add, Esc = clear "
                "(with KiCad's API enabled, selecting in the PCB editor works too)")


def _canvas_hint(pr) -> dict:
    live = F.canvas.running(pr)
    if live:
        return {"running": True, "url": live["url"]}
    return {"running": False, "fix": "focus(action='open') starts it and gives the user the URL"}


@tool("focus",
      """What the user is pointing at. Call this whenever they say "this part", "these", "here", "that corner", "the cap next to U2" — instead of guessing.

action:
 get (default) — the latest focus: what the user clicked / box-dragged in the Copper Canvas, or selected in KiCad's PCB editor. wait=N waits up to N s for their NEXT click or box (say "point at it", then focus(wait=60)).
 resolve — no human needed: what is inside box=[x0,y0,x1,y1] or nearest near=[x,y] (mm, document coordinates) on document='schematic' (sheet=…) or 'pcb'.
 open — start the Copper Canvas (local viewer of the schematic sheets + board, reloads live as you edit) and return its URL; opens the user's browser unless launch=false.
 clear — forget the focus.

Returns {focus:{document, sheet?, mode:'box'|'point'|'selection', box_mm|point_mm, refs:[…], pins?:['U2.EN'], nets:[…], age_s, source}, chip:'R3, C5, U2.EN', items:[{ref, value, footprint, at:[x,y], rot?, side|unit?, nets, pins?}], stale?, missing?} plus a cropped PNG with the focus outlined in orange.
 refs = parts inside or touching the box / the part under the cursor. pins = the pin under the cursor, or the pins a box cut out of a larger part. Empty refs with box_mm/point_mm = the user means that AREA (e.g. "put it here"); nearest / parts_within_mm then say where the parts are.
 also:['C37 (bottom)'] = other parts under the same spot (two-sided board) — re-resolve with side= if that is the one meant. Big boxes: refs is complete, items details the first 40 (items_total), pins are capped (pins_total).
 age_s = seconds since they pointed; stale=true = the design changed since (items are always re-read from the current files); missing = refs that no longer exist.
 focus:null = nothing pointed yet → `canvas` says whether the viewer is running.
Use the result directly: refs/pins feed sch_apply / pcb_place / pcb_route, box_mm feeds render(crop=…).

Examples: focus() · focus(wait=60) · focus(action='open') · focus(action='resolve', document='pcb', box=[100,100,120,115]) · focus(action='resolve', document='schematic', sheet='/amp', near=[120.65,80.01])""",
      {"action": {"type": "string", "enum": ["get", "resolve", "open", "clear"]},
       "wait": {"type": "number", "description": "get: seconds to wait for the user's next click/box (0 = don't wait, max 300)"},
       "source": {"type": "string", "enum": ["auto", "canvas", "kicad"],
                  "description": "get: auto (default) = whichever the user touched last; kicad = the PCB editor selection (needs KiCad's IPC API; KiCad has none for schematics)"},
       "document": {"type": "string", "enum": ["schematic", "pcb"], "description": "resolve: which document the mm coordinates are in"},
       "sheet": {"type": "string", "description": "resolve (schematic): sheet path or name, e.g. '/' or '/amp/left' (default root)"},
       "box": {"type": "array", "items": {"type": "number"}, "description": "resolve: [x0,y0,x1,y1] mm — everything inside or touching"},
       "near": {"type": "array", "items": {"type": "number"}, "description": "resolve: [x,y] mm — nearest part and pin"},
       "radius": {"type": "number", "description": "resolve near: search radius mm (default 5.08 schematic, 3 pcb)"},
       "side": {"type": "string", "enum": ["top", "bottom"], "description": "resolve (pcb): only footprints on this side (default both; a pad under the point beats a bare courtyard)"},
       "render": {"type": "boolean", "description": "attach the cropped PNG of the focus (default true)"},
       "launch": {"type": "boolean", "description": "open: also open the user's browser (default true)"},
       "project": PROJECT},
      title="Focus (what the user points at)", idempotent=True, core=True, group="focus")
def focus(action=None, wait=0, source="auto", document=None, sheet=None, box=None, near=None, radius=None,
          side=None, render=True, launch=True, project=None, _progress=None):
    pr = projmod.resolve(project)
    if action is None:
        action = "resolve" if (box is not None or near is not None) else "get"

    if action == "open":
        info = F.canvas.start(pr, launch=bool(launch))
        out = {"url": info["url"], "launched": info["launched"], "reused": info["reused"],
               "shows": [d for d, p in (("schematic", pr.sch), ("pcb", pr.pcb)) if p.exists()],
               "how": HOW_TO_POINT,
               "next": "ask the user to point, then focus(wait=60)"}
        if not info["launched"]:
            out["tell_user"] = f"Open this link in your browser: {info['url']}"
        if not info["reused"]:
            out["lifetime"] = "runs inside this MCP server (loopback only) until the server exits; `copper canvas` keeps one open independently"
        return Result(out, summary=f"Copper Canvas for {pr.name}: {info['url']}"
                                   + (" (opened in the browser)" if info["launched"] else " — give this link to the user"))

    if action == "clear":
        F.clear(pr)
        return Result({"cleared": True, "focus": None}, summary="focus cleared")

    if action == "resolve":
        if document is None:
            raise ToolError("resolve needs document='schematic' or 'pcb' (which drawing the mm coordinates are in)",
                            code="BAD_ARGUMENT",
                            fix="focus(action='resolve', document='pcb', box=[x0,y0,x1,y1]) — or near=[x,y]")
        if document == "pcb" and sheet is not None:
            raise ToolError("`sheet` only applies to document='schematic'", code="BAD_ARGUMENT",
                            fix="drop `sheet`, or use document='schematic'")
        res = F.resolve(pr, document, box=box, near=near, radius=radius, sheet=sheet, side=side)
        f = F.to_focus(res, "args")
        out, images = F.detail(pr, f, render=bool(render), live_items=res["items"])
        out["focus"]["age_s"] = 0
        if res.get("warnings"):
            out["warnings"] = res["warnings"]
        n = len(res["refs"])
        return Result(out, images=images,
                      summary=(f"{n} part{'s' if n != 1 else ''}: {out['chip']}" if n else
                               f"nothing there ({out['chip'] or 'empty'})"
                               + (f"; nearest is {res['nearest']['ref']} {res['nearest']['dist_mm']} mm away"
                                  if res.get("nearest") else "")))

    # get
    f, info = F.current(pr, source=source or "auto", wait=wait or 0, progress=_progress)
    if not f:
        out = {"focus": None, "canvas": _canvas_hint(pr), "how": HOW_TO_POINT}
        out.update({k: info[k] for k in ("waited_s", "timed_out") if k in info})
        if source == "auto" and info.get("kicad") and not out["canvas"]["running"]:
            out["kicad"] = "selection not readable (KiCad API off or kicad-python missing) — the Canvas needs neither"
        out["next"] = ("focus(action='open'), then ask the user to point and call focus(wait=60)"
                       if not out["canvas"]["running"] else "ask the user to point in the Canvas, then focus(wait=60)")
        return Result(out, summary="the user has not pointed at anything yet"
                                   + (f" (waited {info['waited_s']} s)" if info.get("timed_out") else ""))
    out, images = F.detail(pr, f, render=bool(render))
    out.update({k: info[k] for k in ("waited_s", "timed_out") if k in info})
    if info.get("timed_out"):
        out["note"] = "no NEW pointing within the wait — this is the previous focus"
    summary = f"focus ({f['source']}, {out['focus']['age_s']} s ago): {out['chip'] or 'empty'}"
    if out.get("stale"):
        summary += " — design changed since; items re-read from the current files"
    return Result(out, images=images, summary=summary)
