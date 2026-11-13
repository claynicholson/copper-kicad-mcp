"""board_build — the board in one call: sync, outline, placement, routing, pour, DRC, one report.

Every agent runs these steps in this order with default arguments; doing them server-side replaces five
or six round trips (each of which re-reads the whole conversation) with one, and five step reports with
one that is bounded in size. Nothing here is new engine code: each step calls the same function as its
single-step tool (pcb_sync, pcb_setup, pcb_place, pcb_route_guided / pcb_route, pcb_pour, verify).

Data safety. The board is read once before anything is written:
  NEW       no tracks, vias or net zones: the full flow.
  EXISTING  anything else (hand-made or already routed): sync never removes footprints, the outline is
            kept, only footprints this sync added are auto-placed, only missing connections are routed
            (existing copper is never ripped up), zones are refilled and none is added unless `pour` says so.
An existing outline is never redrawn unless outline='replace' (the plain rectangle project_create drew
on a NEW board is the one exception: width/height resize it).

Time box. A client may give up on a call after 60 s, so the flow stops at a step boundary before it
would overrun `time_budget` and returns what is done plus `pending`; calling board_build() again
resumes (the progress is kept in .copper/build.json and is only trusted while the board file is unchanged).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

from .. import project as projmod
from ..mcp.registry import Image, Result, ToolError, tool
from ..pcb import board
from ..pcb import place as placer

STEPS = ["sync", "setup", "place", "route", "pour", "verify"]
ESTIMATE_S = {"sync": 4, "setup": 4, "place": 9, "route": 12, "pour": 8, "verify": 4}   # measured on a 17-part board
_BUSY: dict[str, tuple[threading.Lock, dict]] = {}
_BUSY_GUARD = threading.Lock()


def _budget(time_budget, progress) -> float:
    if time_budget and time_budget > 0:
        return float(time_budget)
    env = os.environ.get("COPPER_CALL_BUDGET_S", "").strip()
    if env:
        try:
            return max(1.0, float(env))
        except ValueError:
            pass
    return 240.0 if getattr(progress, "has_token", False) else 45.0


def _netlist_hash(nl: dict) -> str:
    comps = sorted((c.get("ref", ""), c.get("footprint", ""), c.get("value", "")) for c in nl.get("components", []))
    nets = sorted((n.get("name", ""), sorted(str(x) for x in (n.get("nodes") or n.get("pins") or []))) for n in nl.get("nets", [])) \
        if isinstance(nl.get("nets"), list) else sorted((k, json.dumps(v, sort_keys=True, default=str)) for k, v in (nl.get("nets") or {}).items())
    return hashlib.sha1(json.dumps([comps, nets], default=str).encode("utf-8")).hexdigest()[:16]


def _state_file(pr):
    return pr.copper_dir / "build.json"


def _load_state(pr) -> dict:
    try:
        return json.loads(_state_file(pr).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(pr, st: dict):
    try:
        _state_file(pr).write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


def _net_zones(snap: dict) -> list[dict]:
    return [z for z in snap.get("zones") or [] if z.get("net") and not z.get("rule_area")]


def _is_plain_rect(ol: dict | None) -> bool:
    """The outline project_create(board_size=…) draws: four corners, axis-aligned."""
    if not ol:
        return False
    poly = ol.get("polygon") or []
    if len(poly) not in (4, 5):
        return False
    x0, y0, x1, y1 = ol["bbox"]
    return all(min(abs(p[0] - x0), abs(p[0] - x1)) < 0.01 and min(abs(p[1] - y0), abs(p[1] - y1)) < 0.01 for p in poly)


def _outside(snap: dict) -> list[str]:
    ol = snap.get("outline")
    if not ol:
        return []
    x0, y0, x1, y1 = ol["bbox"]
    return [f["ref"] for f in snap.get("footprints", []) if not (x0 <= f["x"] <= x1 and y0 <= f["y"] <= y1)]


def _auto_size(snap: dict) -> tuple[float, float]:
    """Board size for parts that have no outline yet: courtyard area / 0.35 fill, 3:2, rounded up to 5 mm."""
    area = 0.0
    for f in snap.get("footprints", []):
        b = f.get("bbox")
        if b:
            area += max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    area = max(area / 0.35, 400.0)
    h = (area / 1.5) ** 0.5
    up = lambda v: float(int(-(-v // 5)) * 5)   # noqa: E731
    return up(h * 1.5), up(h)


def registry_props(tool_name: str) -> dict:
    from ..mcp import registry
    t = registry.TOOLS.get(tool_name)
    return t.schema.get("properties", {}) if t else {}


def _fab_tool() -> str:
    return "fab_export"


_CALL = re.compile(r"\b([a-z][a-z0-9_]+)\((?:[^()]|\([^()]*\))*\)")


def _call_in(text: str | None) -> str | None:
    """The first call to a registered tool named in a fix hint (`next` must be a call, not prose)."""
    from ..mcp import registry
    for m in _CALL.finditer(text or ""):
        if m.group(1) in registry.TOOLS:
            return m.group(0)
    return None


FIX_MAX = 220


def _short_fix(fix: str, limit: int = FIX_MAX) -> str:
    """A fix hint bounded in size WITHOUT cutting a call in half: a fix that ends in the middle of
    `sch_apply(ops=[{"op":"p` cannot be run and costs the agent a verify() round trip to read it again.
    Short hints go out whole; a long one keeps its text up to the end of the first complete tool call
    (or only that call); prose without a call is cut at a word and marked."""
    fix = re.sub(r"\s*\[same fix for each[^\]]*\]?\s*$", "", fix or "").strip()
    if len(fix) <= limit:
        return fix
    from ..mcp import registry
    for m in _CALL.finditer(fix):
        if m.group(1) in registry.TOOLS:
            return fix[:m.end()] if m.end() <= limit * 2 else m.group(0)
    return fix[:limit].rsplit(" ", 1)[0] + " …"


def _groups(findings: list, limit: int = 6) -> tuple[list, dict]:
    """verify's findings -> (error groups for the report, warning counts by type)."""
    errs, warn = [], {}
    for f in findings or []:
        n = f.get("count") or 1
        if f.get("severity") == "error" and f.get("type") != "unconnected_items":
            if len(errs) < limit:
                g = {"type": f.get("type"), "n": n, "msg": (f.get("message") or "")[:140]}
                if f.get("at") is not None:
                    g["at"] = f["at"]
                if f.get("refs"):
                    g["refs"] = f["refs"][:6]
                if f.get("fix"):
                    g["fix"] = _short_fix(f["fix"])
                errs.append(g)
        elif f.get("severity") != "error":
            warn[f.get("type") or "other"] = warn.get(f.get("type") or "other", 0) + n
    return errs, warn


@tool("board_build",
      """The whole board in ONE call: update the PCB from the schematic, outline, placement, routing, ground pour and
KiCad's DRC + ERC, with one compact report (a line per stage, error groups with fixes, `fab_ready`, `next`). Use it
instead of pcb_sync, pcb_setup, pcb_place, pcb_route, pcb_pour and verify one after another.
width/height (mm) apply when the board has no outline (omit both to size it from the parts) or with
outline='replace'. An existing outline, existing tracks and placed parts are kept: on a board that already has
copper only new footprints are placed and only missing connections are routed.
If the result has `pending`, the time box was reached: call board_build() again to continue.
Example: board_build(width=50, height=32)""",
      {"width": {"type": "number", "description": "mm"},
       "height": {"type": "number", "description": "mm"},
       "radius": {"type": "number", "description": "corner radius mm"},
       "holes": {"type": "string", "enum": ["none", "M2", "M2.5", "M3", "M4"], "description": "corner mounting holes"},
       "layers": {"type": "integer", "enum": [2, 4], "description": "copper layers (default: as is)"},
       "outline": {"type": "string", "enum": ["keep", "replace"]},
       "steps": {"type": "array", "items": {"type": "string", "enum": STEPS}, "description": "only these (default all)"},
       "router": {"type": "string", "enum": ["auto", "guided", "freerouting"],
                  "description": "auto: built-in router, then Freerouting if installed"},
       "pour": {"type": "array", "items": {"type": "object"}, "description": "[{net, layers}] (default: ground, outer layers)"},
       "keep": {"type": "array", "items": {"type": "string"}, "description": "refs not to move"},
       "moves": {"type": "array", "items": {"type": "object"}, "description": "[{ref,x,y,rot,side,lock}]"},
       "time_budget": {"type": "integer", "description": "seconds before returning `pending` (default 45)"},
       "image": {"type": "string", "enum": ["final", "on_fail", "none"], "description": "one small picture (default final)"},
       "project": {"type": "string", "description": "project path (default: active)"},
       "while_open": {"type": "string", "enum": ["refuse", "revert"], "description": "refuse (default) | revert"}},
      title="Build the board (sync to DRC)", destructive=True, open_world=False, group="pcb", max_bytes=6000,
      help="""## What it does, step by step
        sync    pcb_sync(): footprints and nets from the schematic. Never removes footprints. On a board that has
                copper and was not built by board_build, footprints that differ from the schematic's are reported
                and kept as drawn (pcb_sync() swaps them).
        setup   pcb_setup(width, height, radius, holes, layers): only when there is no outline, with outline='replace',
                or to resize the plain rectangle project_create drew on a board without copper.
        place   pcb_place(auto=true): the whole board when it has no copper yet, otherwise only the footprints this
                sync added. `keep` and `moves` are passed through.
        route   2 layers: route the signals, pour, then connect the pads the pour did not reach.
                4 layers: pour the planes first, then route.
                router='auto' uses the built-in deterministic router; when connections are left AND Freerouting is
                already installed it hands them over. It never starts the Freerouting download by itself.
        pour    the ground net on F.Cu + B.Cu with stitching vias (2 layers). On a board that already has zones they
                are refilled and none is added unless `pour` is given.
        verify  one DRC + ERC at the end.

        ## The report
        {"ok": true, "fab_ready": false, "state": "new",
         "stages": {"sync": "+17 parts, 24 nets", "setup": "50x32 mm r1.5", "place": "17 placed, ratsnest 934 -> 535 mm",
                    "route": "guided 35/35, 18 vias, 437 mm", "pour": "GND F.Cu+B.Cu, 1 stitch via"},
         "erc": [0, 0], "drc": {"errors": 1, "warnings": 1, "unrouted": 0, "parity": 0},
         "findings": [{"type": "starved_thermal", "n": 1, "msg": "...", "at": [148.6, 100.0], "refs": ["J1.SH"], "fix": "..."}],
         "other_warnings": {"silk_over_copper": 1}, "blockers": ["drc"], "checkpoint": "...-before-board-build",
         "next": "..."}
        - ok: every step ran. fab_ready: no ERC/DRC errors, nothing unrouted, board and schematic in step.
        - findings: DRC/ERC ERRORS grouped by kind (at most 6 groups); warnings are counts in other_warnings.
          verify() lists everything.
        - pending: steps not run yet because the time box was reached. board_build() continues.
        - failed: the step that stopped the flow, with its error and fix; the stages before it are done.
        - checkpoint: checkpoint(action='restore', id=...) undoes the whole call.

        ## Examples
        board_build(width=50, height=32)
        board_build(width=60, height=40, radius=2, holes='M3', layers=4)
        board_build()                                    # after a schematic change: sync, place the new parts, route them
        board_build(steps=['route','pour','verify'])     # after moving parts by hand
        board_build(width=80, height=50, outline='replace')""")
def board_build(width=None, height=None, radius=0.0, holes=None, layers=None, outline="keep", steps=None, router="auto",
                pour=None, keep=None, moves=None, time_budget=0, image=None, project=None, while_open="refuse",
                _progress=None):
    if (width is None) != (height is None):
        raise ToolError("pass both width and height (mm), or neither", code="BAD_ARGUMENT",
                        fix="board_build(width=50, height=32)")
    for label, v in (("width", width), ("height", height)):
        if v is not None and not (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 10000):
            raise ToolError(f"{label} must be a board size in mm greater than 0 (got {v!r})", code="BAD_ARGUMENT",
                            fix="board_build(width=50, height=32), or omit both to size the board from its parts")
    pr = projmod.resolve(project)
    key = str(pr.dir).lower()
    with _BUSY_GUARD:
        lock, info = _BUSY.setdefault(key, (threading.Lock(), {}))
    if not lock.acquire(blocking=False):
        raise ToolError(f"board_build has been running on this project for {int(time.time() - info.get('t0', time.time()))} s "
                        f"({info.get('step', 'starting')})", code="BUSY", fix="wait for it to finish, then copper_status()",
                        data={"retry_after_s": 15})
    try:
        info.update(t0=time.time(), step="starting")
        return _build(pr, width, height, radius, holes, layers, outline, steps, router, pour, keep, moves,
                      _budget(time_budget, _progress), image, while_open, _progress, info)
    except ToolError as e:
        if e.code == "LOAD_FAILED" and not e.fix:        # a damaged board file: say how to get a good one back
            e.fix = ("the board file is damaged or not a KiCad board: checkpoint(action='list') then "
                     "checkpoint(action='restore', id=...) brings back an earlier revision; open it in KiCad to see the load error")
        raise
    finally:
        lock.release()


def _build(pr, width, height, radius, holes, layers, outline, steps, router, pour, keep, moves, budget, image,
           while_open, progress, info):
    from . import pcb_tools as T
    t0 = time.time()
    proj = str(pr.dir / f"{pr.name}.kicad_pro") if pr.pro.exists() else str(pr.dir)
    prog = progress or (lambda f, m="": None)
    cancelled = getattr(progress, "cancelled", None) or (lambda: False)
    if pr.pcb.exists():
        projmod.guard_writable(pr.pcb, while_open)        # an open board costs one error, not a half-run flow
    if not pr.sch.exists():
        raise ToolError("the project has no schematic to build a board from", code="NO_SCHEMATIC",
                        fix="sch_apply(ops=[...]) first")

    # ── state: what may this call touch? ──────────────────────────────────
    st = _load_state(pr)
    rev = projmod.revision(pr.pcb) if pr.pcb.exists() else None
    resume = st.get("in_progress") if (st.get("in_progress") and st["in_progress"].get("rev") == rev and rev) else None
    # One board read up front and one after each step that changes what later steps look at (every read starts
    # KiCad's Python once); the unrouted count is carried from each step's own DRC instead of being read again.
    snap0 = board.read(pr, what=["summary", "geom", "nets"]) if pr.pcb.exists() else {}
    cache = {"snap": snap0 or None, "unconnected": snap0.get("unconnected"), "zoned": bool(_net_zones(snap0))}

    def snapshot(fresh=False):
        if fresh or not cache["snap"]:
            cache["snap"] = board.read(pr, what=["summary", "geom", "nets"])
            cache["unconnected"] = cache["snap"].get("unconnected")
            cache["zoned"] = bool(_net_zones(cache["snap"]))
        return cache["snap"]
    has_copper = bool(snap0.get("tracks") or snap0.get("vias") or cache["zoned"]
                      or any(f.get("locked") for f in snap0.get("footprints", [])))
    state = resume["state"] if resume else ("existing" if has_copper else "new")
    done_before = list(resume["done"]) if resume else []
    want = [s for s in STEPS if (not steps or s in steps) and s not in done_before]
    stages: dict = dict(resume.get("stages") or {}) if resume else {}
    notes: list[str] = []
    refs_before = {f["ref"] for f in snap0.get("footprints", [])}
    new_refs: list[str] = list(resume.get("new_refs") or []) if resume else []
    cp = (resume or {}).get("checkpoint") or (projmod.checkpoint(pr, "before-board-build") if pr.pcb.exists() else None)
    done: list[str] = []
    failed = None
    place_problem = None
    pour_nets: list[str] = list(resume.get("pour_nets") or []) if resume else []
    open_pads: list[str] = []        # pads the last router pass could not connect

    def remaining():
        return budget - (time.time() - t0)

    def pace():
        """How much slower than the estimates this machine is running right now (1 = as measured). The
        estimates are from an idle machine; on a loaded one a step took 3 to 5 times as long and a call that
        trusted them ran 67 s into a 45 s box, past the point where a client gives up."""
        est = sum(ESTIMATE_S[s] for s in done)
        return max(1.0, (time.time() - t0) / est) if est else 1.0

    def run(step, fn):
        """Run one step unless the time box says stop. Returns False when the flow must stop here."""
        nonlocal failed
        if failed or step not in want:
            return not failed
        if cancelled():
            notes.append("cancelled by the client")
            return False
        if done and remaining() < ESTIMATE_S[step] * pace():
            return False                                 # leave it pending; at least one step always runs
        info["step"] = step
        prog((STEPS.index(step) + 0.1) / len(STEPS), step)
        try:
            fn()
        except ToolError as e:
            if not done and not done_before:
                raise                                    # nothing happened yet: a plain error is the clearest answer
            failed = {"step": step, "error": e.code, "message": e.message[:300]}
            if e.fix:
                failed["fix"] = e.fix[:300]
            return False
        done.append(step)
        return True

    # ── sync ──────────────────────────────────────────────────────────────
    def do_sync():
        nonlocal new_refs
        nl = board.netlist(pr)
        h = _netlist_hash(nl)
        if pr.pcb.exists() and st.get("netlist") == h and st.get("parity") == 0:
            stages["sync"] = "in step with the schematic (skipped)"
            return
        # Footprints are swapped to the schematic's only on a board this tool built. On somebody's hand-made
        # board the schematic often names the same footprint through another library (project-local copy vs the
        # stock library): swapping would redraw every part of a finished layout from today's library and can add
        # DRC errors that were not there (measured: KiCad's complex_hierarchy demo, 68 swaps, 0 -> 2 errors).
        swap = state == "new" or bool(st.get("built_here"))
        differ = []
        if not swap:
            want_fp = {c.get("ref"): c.get("footprint") for c in nl.get("components", [])}
            differ = sorted(f["ref"] for f in snap0.get("footprints", [])
                            if want_fp.get(f["ref"]) and f.get("footprint") and want_fp[f["ref"]] != f["footprint"])
        r = T.pcb_sync(project=proj, remove_extra=False, update_footprints=swap, while_open=while_open, render=False).data
        after = snapshot(fresh=True)
        new_refs = sorted({f["ref"] for f in after.get("footprints", [])} - refs_before)
        line = f"+{r['added']} parts" if r.get("added") else "no new parts"
        if r.get("updated"):
            line += f", {r['updated']} updated"
        if r.get("replaced"):
            line += f", {r['replaced']} footprints swapped"
        line += f", {r.get('nets')} nets"
        if r.get("extra"):
            line += f"; {len(r['extra'])} on the board but not in the schematic ({', '.join(r['extra'][:6])}): kept"
        errs = r.get("errors") or []
        if errs:
            line += f"; {len(errs)} footprints NOT found: " + ", ".join(e.get("ref", "?") for e in errs[:6])
            notes.append(f"missing footprints: {errs[0].get('ref')}: {str(errs[0].get('error'))[:120]}"
                         + (f" (fix: {str(errs[0].get('fix'))[:140]})" if errs[0].get("fix") else ""))
        if differ:
            line += (f"; {len(differ)} footprints differ from the schematic's ({', '.join(differ[:6])}"
                     f"{' …' if len(differ) > 6 else ''}): kept as drawn")
            notes.append("this board was not built by board_build, so its footprints were not replaced; "
                         "pcb_sync() swaps them to the schematic's (it redraws those parts: check DRC after)")
        stages["sync"] = line
        st["netlist"] = h
        if state == "new":
            st["built_here"] = True

    # ── setup (outline, holes, layers) ────────────────────────────────────
    def do_setup():
        nonlocal width, height
        snap = snapshot()
        ol = snap.get("outline")
        kw = {}
        same_size = bool(ol) and width is not None and abs(ol["width"] - width) < 0.01 and abs(ol["height"] - height) < 0.01
        # The one outline this flow may redraw unasked: the plain rectangle project_create drew, on a board without
        # copper, at the SAME size (to round its corners). Any other existing outline (drawn by hand, imported from
        # a CAD file, or a different size) is kept until the caller says outline='replace'.
        if ol and outline != "replace" and not (state == "new" and _is_plain_rect(ol) and same_size and radius):
            stages["setup"] = (f"kept the existing {ol['width']:g}x{ol['height']:g} mm outline"
                               + (" (outline='replace' redraws it)" if width is not None and not same_size else ""))
        else:
            if width is None:
                if state != "new":
                    raise ToolError("the board has no outline and already has copper: pass width and height",
                                    code="NO_OUTLINE", fix="board_build(width=..., height=...)")
                if "auto_size" in registry_props("pcb_setup"):
                    kw["auto_size"] = True                # the outline module's own sizing
                else:
                    width, height = _auto_size(snap)
                notes.append("no size given: the board was sized from the parts (pass width and height to choose)")
            if width is not None:
                kw.update(width=width, height=height, radius=radius or 0.0)
        if holes and holes != "none" and "setup" not in done_before:
            kw["holes"] = holes
        if layers and layers != snap.get("copper_layers"):
            kw["layers"] = layers
        if kw:
            r = T.pcb_setup(project=proj, while_open=while_open, render=False, **kw).data
            cache["snap"] = None                         # outline / layers changed
            o = r.get("outline") or {}
            line = f"{o.get('width', 0):g}x{o.get('height', 0):g} mm" + (f" r{radius:g}" if kw.get("radius") else "")
            line += f", {r.get('copper_layers')} layers"
            if kw.get("holes"):
                line += f", {kw['holes']} holes"
            if r.get("rules"):
                line += ", fab rules set"
            stages["setup"] = line
        elif "setup" not in stages:
            stages["setup"] = f"{ol['width']:g}x{ol['height']:g} mm outline already there" if ol else "nothing to set up"

    # ── place ─────────────────────────────────────────────────────────────
    def do_place():
        nonlocal place_problem
        kept_placed = 0
        snap = snapshot()
        if not snap.get("outline"):
            raise ToolError("the board has no outline to place into", code="NO_OUTLINE", fix="board_build(width=..., height=...)")
        kw = dict(project=proj, while_open=while_open, render=False)
        if state == "new":
            out, ov = _outside(snap), placer.overlaps(snap)
            if not out and not ov and not new_refs and not moves:
                stages["place"] = "every part is on the board, no overlaps (skipped)"
                return
            kw.update(auto=True)
            # Parts that were ALREADY inside this same outline before the call, without overlaps, are somebody's
            # placement (by hand in KiCad, or an earlier call): only the others are placed. (Judged on the board
            # as it was read at the start, so the grid pcb_sync parks new parts in never counts as placed.)
            ol0 = snap0.get("outline") or {}
            inside0 = (refs_before - set(_outside(snap0))) if ol0 and ol0.get("bbox") == (snap.get("outline") or {}).get("bbox") else set()
            todo = sorted(f["ref"] for f in snap.get("footprints", []) if f["ref"] not in inside0)
            if inside0 and todo and not ov and not resume:
                kw["refs"] = todo
                kept_placed = len(inside0)
            if keep:
                kw["keep"] = keep
        else:
            if new_refs:
                kw.update(auto=True, refs=new_refs)
            elif not moves:
                stages["place"] = "existing placement kept (no new parts)"
                return
        if moves:
            kw["moves"] = moves
        r = T.pcb_place(**kw).data
        cache["unconnected"] = ((r.get("verify") or {}).get("drc") or {}).get("unconnected", cache["unconnected"])
        sc = r.get("score") or {}
        n = len(r.get("changed") or [])
        line = (f"{n} placed" + (f", {kept_placed} already on the board kept" if kept_placed else "")
                if state == "new" else f"{n} new parts placed, the rest kept") + \
            f", ratsnest {sc.get('ratsnest_mm_before', 0):.0f} -> {sc.get('ratsnest_mm_after', 0):.0f} mm"
        ov = (r.get("verify") or {}).get("courtyard_overlaps")
        if ov:
            line += f", {ov} courtyard overlaps"
        for f in r.get("findings") or []:
            if f.get("code") == "DID_NOT_FIT":
                place_problem = f
                line += "; " + f.get("message", "some parts did not fit")
        stages["place"] = line

    # ── route + pour ──────────────────────────────────────────────────────
    def pour_plan(snap_sum, nets):
        if pour:
            return [{"net": p["net"], "layers": p.get("layers"), "stitch": p.get("stitch", True)} for p in pour if p.get("net")]
        if state != "new":
            return []                                   # a board with copper: refill what is there, add nothing
        gnd = sorted((n for n in nets if placer.is_ground(n["name"])), key=lambda n: -n.get("pads", 0))
        if not gnd:
            return []
        if (snap_sum.get("copper_layers") or 2) >= 4:
            plan = [{"net": gnd[0]["name"], "layers": ["In1.Cu"], "stitch": False}]
            pw = sorted((n for n in nets if placer.is_power(n["name"]) and not placer.is_ground(n["name"])),
                        key=lambda n: -n.get("pads", 0))
            if pw:
                plan.append({"net": pw[0]["name"], "layers": ["In2.Cu"], "stitch": False})
            return plan
        return [{"net": gnd[0]["name"], "layers": ["F.Cu", "B.Cu"], "stitch": True}]

    def run_pour(plan, snap_sum):
        lines = []
        if not plan:
            if cache["zoned"]:
                r = T.pcb_pour(project=proj, fill_only=True, while_open=while_open, render=False).data
                cache["unconnected"] = r.get("unrouted", cache["unconnected"])
                lines.append("refilled the existing zones")
            else:
                return "no ground net to pour (skipped)"
        for p in plan:
            kw = dict(project=proj, net=p["net"], stitch=bool(p.get("stitch")), while_open=while_open, render=False)
            if p.get("layers"):
                kw["layers"] = p["layers"]
            r = T.pcb_pour(**kw).data
            cache["zoned"] = True
            cache["unconnected"] = r.get("unrouted", cache["unconnected"])
            line = f"{p['net']} on {'+'.join(p.get('layers') or ['F.Cu', 'B.Cu'])}"
            for c in r.get("changed") or []:
                m = re.match(r"(\d+) stitching vias", c)
                if m and int(m.group(1)):
                    line += f", {m.group(1)} stitch vias"
                m = re.match(r"(\d+) vias joining isolated", c)
                if m:
                    line += f", {m.group(1)} island vias"
            for f in r.get("findings") or []:
                if f.get("code") == "POUR_ISLAND":
                    line += "; " + f["message"][:150]
            lines.append(line)
            if p["net"] not in pour_nets:
                pour_nets.append(p["net"])
        return "; ".join(lines)

    def run_route(label):
        """Guided router first; Freerouting only when asked for, or for what is left when it is already installed."""
        from . import route_tools as RT
        left = cache["unconnected"]
        if left is None:
            left = snapshot(fresh=True).get("unconnected")
        if not left:
            return "nothing to route"
        lines = []
        if router in ("auto", "guided"):
            r = RT.pcb_route_guided(project=proj, ripup="none", render=False, while_open=while_open).data
            if r.get("nothing_to_route") and not (r.get("added") or {}).get("tracks"):
                lines.append("guided: nothing routable left")
            elif r.get("not_written"):
                lines.append(f"guided 0/{r.get('total')}: {str(r['not_written'])[:120]}")
            else:
                a = r.get("added") or {}
                lines.append(f"guided {r.get('routed')}/{r.get('total')}, {a.get('vias', 0)} vias, {a.get('length_mm', 0):.0f} mm")
            left = (r.get("drc") or {}).get("unconnected", left)
            fails = [e for e in r.get("failed") or []]
            open_pads[:] = [e["from"] for e in fails if e.get("from")]
            if fails:
                notes.append(f"router: {fails[0].get('net')} {fails[0].get('from')}->{fails[0].get('to')}: "
                             f"{str(fails[0].get('reason'))[:140]}" + (f" (+{len(fails) - 1} more)" if len(fails) > 1 else ""))
            open_signal = [e for e in fails if e.get("net") not in pour_nets and not placer.is_ground(e.get("net") or "")]
        else:
            open_signal = [True]
        if open_signal and router in ("auto", "freerouting"):
            from ..pcb import freerouting
            ready = False
            try:
                ready = bool(freerouting.status().get("ready"))
            except Exception:  # noqa: BLE001
                pass
            if router == "freerouting" or ready:
                tmo = int(max(20, remaining() - 8))
                skip = pour_nets or [n["name"] for n in snapshot().get("nets", []) if placer.is_ground(n["name"])]
                r = T.pcb_route(project=proj, ignore_nets=skip or None, timeout=tmo, while_open=while_open,
                                render=False, _progress=progress).data
                if r.get("nothing_to_route"):
                    lines.append("freerouting: nothing to route")
                else:
                    lines.append(f"freerouting {r.get('unrouted_before')} -> {r.get('unrouted_after')} unrouted in {r.get('seconds')} s"
                                 + (" (reverted: worse)" if r.get("reverted") else ""))
                left = (r.get("verify") or {}).get("drc", {}).get("unconnected", left)
            elif router == "auto":
                lines.append("pcb_route() would download Freerouting to try the rest (not started automatically)")
        cache["unconnected"] = left
        return f"{label}: " + "; ".join(lines) if label else "; ".join(lines)

    def do_route():
        snap_sum = snapshot()
        if (snap_sum.get("copper_layers") or 2) >= 4 and state == "new" and "pour" in want and not cache["zoned"]:
            # planes first: the router then reserves those layers and fans the plane nets out with vias
            stages["pour"] = run_pour(pour_plan(snap_sum, snap_sum.get("nets") or []), snap_sum)
            stages["_planes_first"] = True
        stages["route"] = run_route("")

    def do_pour():
        if stages.pop("_planes_first", None):
            T.pcb_pour(project=proj, fill_only=True, while_open=while_open, render=False)
            return
        snap_sum = snapshot()
        plan = pour_plan(snap_sum, snap_sum.get("nets") or [])
        stages["pour"] = run_pour(plan, snap_sum)
        if plan and cache["unconnected"] and "route" in (done + done_before) and remaining() > ESTIMATE_S["route"] / 2:
            # the pour never reaches every pad: connect the ones it missed (short stubs / vias), then refill
            extra = run_route("")
            if extra and "nothing" not in extra:
                stages["route"] = (stages.get("route", "") + "; after pour: " + extra).strip("; ")

    final: dict = {}

    def do_verify():
        from . import verify_tools as V
        final.update(V.verify(what=["erc", "drc"], project=proj, limit=12).data)

    flow = [("sync", do_sync), ("setup", do_setup), ("place", do_place), ("route", do_route), ("pour", do_pour),
            ("verify", do_verify)]
    for step, fn in flow:
        if not run(step, fn):
            break
    stages.pop("_planes_first", None)
    pending = [s for s in want if s not in done] if not failed else []
    if failed:
        pending = []

    # ── report ────────────────────────────────────────────────────────────
    # a `pending` report lists only the steps THIS call ran: the earlier ones were reported by the call that ran
    # them, and the finished report lists them all again (the full dict stays in .copper/build.json for the resume)
    shown = {k: v for k, v in stages.items() if k in done} if (pending and done_before) else stages
    data: dict = {"ok": not failed and not pending, "fab_ready": False, "state": state, "stages": shown}
    if final:
        e, d = final.get("erc") or {}, final.get("drc") or {}
        data["erc"] = [e.get("errors", 0), e.get("warnings", 0)]
        data["drc"] = {"errors": d.get("errors", 0), "warnings": d.get("warnings", 0),
                       "unrouted": d.get("unconnected", 0), "parity": d.get("parity", 0)}
        errs, warn = _groups((e.get("findings") or []) + (d.get("findings") or []))
        if d.get("unconnected"):
            nets = d.get("unrouted_nets") or []
            on_pour = bool(nets) and all(n in pour_nets or placer.is_ground(n) for n in nets)
            pad = open_pads[0] if open_pads else "REF.PAD"
            g = {"type": "unrouted", "n": d["unconnected"],
                 "msg": f"{d['unconnected']} connection(s) open on " + ", ".join(nets[:8])
                        + (" (pads the pour cannot reach)" if on_pour else ""),
                 "fix": (f"pcb_route(tracks=[{{\"from\":\"{pad}\",\"to\":\"<another pad of that net>\"}}]) or move the track "
                         "that walls the pad in, then pcb_pour(fill_only=true)" if on_pour else
                         "pcb_route_guided(nets=[...]) with a plan (pcb_route_plan() shows what is in the way), or more "
                         "room: board_build(width=..., height=..., outline='replace')")}
            if open_pads:
                g["refs"] = open_pads[:8]
            errs.insert(0, g)
        if place_problem:
            errs.insert(0, {"type": "did_not_fit", "n": 1, "msg": place_problem.get("message", "")[:140],
                            "fix": "a bigger board: board_build(width=..., height=..., outline='replace') then "
                                   "pcb_place(auto=true, clear_tracks=true) and board_build(steps=['route','pour','verify'])"})
        if errs:
            data["findings"] = errs
        if warn:
            data["other_warnings"] = warn
        blockers = [c for c, bad in (("erc", e.get("errors")), ("drc", d.get("errors")), ("unrouted", d.get("unconnected")),
                                     ("parity", d.get("parity"))) if bad]
        if blockers:
            data["blockers"] = blockers
        data["fab_ready"] = not blockers and not place_problem and not failed
        st["parity"] = d.get("parity", 0)
    if notes:
        data["notes"] = notes[:5]
    if cp:
        data["checkpoint"] = cp
    if failed:
        data["failed"] = failed
    if pending:
        data["done"] = done_before + done
        data["pending"] = pending
    data["seconds"] = round(time.time() - t0, 1)
    fab = _fab_tool()
    if pending:
        data["next"] = "board_build()" if not steps else f"board_build(steps={json.dumps(pending)})"
    elif failed:
        # a refused argument is fixed by the same call with a better argument; anything else: the step's own fix
        data["next"] = _call_in(failed.get("fix")) or ("board_build(...) with the argument corrected"
                                                       if failed.get("error") == "BAD_ARGUMENT" else "copper_status()")
    elif not final:
        data["next"] = "verify()"
    elif data["fab_ready"]:
        data["next"] = f"{fab}()"
    else:       # the first error group's fix has the detail; `next` is just the call
        blk = data.get("blockers") or []
        data["next"] = _call_in((data.get("findings") or [{}])[0].get("fix")) or (
            "verify(what=['drc']) lists what differs between board and schematic" if blk == ["parity"] else "verify()")

    # progress for a resume: only valid while nobody else touches the board
    if pending:
        st["in_progress"] = {"state": state, "done": done_before + done, "stages": stages, "new_refs": new_refs,
                             "pour_nets": pour_nets, "checkpoint": cp,
                             "rev": projmod.revision(pr.pcb) if pr.pcb.exists() else None}
    else:
        st.pop("in_progress", None)
    _save_state(pr, st)

    # one small picture of the result
    mode = image or ("none" if os.environ.get("COPPER_IMAGES", "final").strip().lower() == "never" else "final")
    images = []
    untouched = bool(failed) and not any(x in done for x in ("setup", "place", "route", "pour")) and image is None
    if pr.pcb.exists() and not pending and not untouched and (mode == "final" or (mode == "on_fail" and not data["fab_ready"])):
        try:
            from .. import render
            ol = (snapshot().get("outline") or {})
            w = int(max(480, min(1000, (ol.get("width") or 60) * 14)))
            im = render.pcb_layers(pr.pcb, pr.out_dir, layers=["B.Cu", "F.Cu", "F.SilkS", "Edge.Cuts"], width=w, name="board-build")
            images.append(Image(path=im.path, caption="the board after board_build (top view, both copper layers)"))
            if (ol.get("width") or 0) > 100:
                data["zoom"] = "render(target='pcb', crop=[x0,y0,x1,y1]) for detail"
        except ToolError:
            pass

    drc = data.get("drc")
    head = f"board_build ({state} board): " + ", ".join(done_before + done or ["nothing run"])
    if drc:
        head += f"; DRC {drc['errors']} errors, {drc['unrouted']} unrouted; ERC {data['erc'][0]} errors"
        if drc.get("parity"):
            head += f"; {drc['parity']} board/schematic differences"
    if pending:
        head += f"; PENDING {', '.join(pending)} (time box)"
    if failed:
        head += f"; STOPPED at {failed['step']}: {failed['error']}"
    head += f" -> {data['next'][:110]}"
    return Result(data, images, summary=head)
