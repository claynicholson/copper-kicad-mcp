"""verify · design_review · render — KiCad's own checks, engineering review and eyes."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import project as P
from .. import render as R
from ..mcp.registry import Result, ToolError, tool
from ..review import design_review as DR
from ..review import verify as V
from ..review.common import out_dir

PROJECT = {"type": "string", "description": "Project folder or .kicad_pro/.kicad_sch/.kicad_pcb path. Omit to use the active project."}


def _one(f: dict) -> dict:
    d = {k: f.get(k) for k in ("severity", "type", "message", "at", "fix") if f.get(k) is not None}
    for k in ("net", "sheet", "section", "suggestion"):
        if f.get(k):
            d[k] = f[k]
    pins = [i.get("pin_ref") or i.get("ref") for i in f.get("items", []) if isinstance(i, dict)]
    pins = [p for p in pins if p]
    if pins:
        d["refs"] = list(dict.fromkeys(pins))
    elif f.get("items") and all(isinstance(i, str) for i in f["items"]):
        d["refs"] = f["items"][:12]
    return d


GROUP_MIN = 4   # this many findings of one kind collapse into a single entry
# noisy types whose messages differ only by the item named → one entry per type
GROUP_BY_TYPE = {"lib_footprint_mismatch", "lib_symbol_mismatch", "footprint_symbol_mismatch", "missing_footprint",
                 "extra_footprint", "net_conflict", "silk_overlap", "silk_over_copper", "silk_edge_clearance",
                 "text_height", "text_thickness", "endpoint_off_grid", "unconnected_items", "footprint_filter",
                 "simulation_model_issue", "courtyards_overlap", "solder_mask_bridge", "lib_footprint_issues"}


# copper conflicts are only "the same problem" between the same nets: merging GPIO10-vs-XIN with GND-vs-BOOTSEL
# under one fix sent the agent to re-route the wrong nets
SPLIT_BY_NETS = {"clearance", "shorting_items", "tracks_crossing", "hole_clearance", "copper_edge_clearance"}


def _nets_key(f: dict) -> tuple:
    return tuple(sorted({i.get("net") for i in f.get("items", []) if isinstance(i, dict) and i.get("net")}))


def _compact(fs: list[dict], limit: int) -> tuple[list[dict], int]:
    """Findings → compact entries. Repeats of one problem (same severity, type and
    message, e.g. 30 footprints from one missing library or 12 unconnected pins)
    become ONE entry with `count` and all `refs`, so the model sees every kind of
    problem instead of the first 25 copies of one. Returns (entries, omitted)."""
    groups: dict = {}
    for f in fs:
        base = (f.get("message") or "").split(" — ")[0]
        if f.get("type") in GROUP_BY_TYPE:
            base = ""          # one entry per type: the messages differ only by the footprint/part named
        nets = _nets_key(f) if f.get("type") in SPLIT_BY_NETS else ()
        groups.setdefault((f.get("severity"), f.get("type"), base, f.get("sheet"), nets), []).append(f)
    entries = []
    for (sev, typ, base, sheet, _nets), g in groups.items():
        if len(g) < GROUP_MIN:
            entries += [_one(f) for f in g]
            continue
        d = _one(g[0])
        refs = list(dict.fromkeys(r for f in g for r in (_one(f).get("refs") or [])))
        first = (g[0].get("message") or "").split(" — ")[0]
        d["message"] = f"{base} ×{len(g)}" if base else f"{len(g)}× {typ}, e.g. {first}"
        if typ == "unconnected_items":
            d["fix"] = "pcb_route() to autoroute every unrouted connection (pcb_pour(net='GND') first if GND is a plane)"
        d["count"] = len(g)
        d["refs"] = refs[:60] + ([f"… +{len(refs) - 60} more"] if len(refs) > 60 else [])
        pins = [r for r in refs if "." in r]
        if typ == "pin_not_connected" and pins:
            d["fix"] = (f"wire the pins that are used (sch_apply connect); mark the rest unused in one call: "
                        f"sch_apply(ops=[{{\"op\":\"no_connect\",\"pins\":{json.dumps(pins[:40])}}}])")
        elif d.get("fix"):
            fixes = list(dict.fromkeys(f.get("fix") for f in g if f.get("fix")))
            if len(fixes) == 1:
                d["fix"] = d["fix"] + f"  [same fix for each of the {len(g)} refs]"
            else:              # different targets per item: never claim one fix covers them all
                d["fix"] = fixes[0]
                d["fixes"] = fixes[1:5] + ([f"… +{len(fixes) - 5} more (raise limit or open the report)"] if len(fixes) > 5 else [])
        if typ in SPLIT_BY_NETS and _nets:
            d["nets"] = list(_nets)
        entries.append(d)
    return entries[:limit], max(0, len(entries) - limit)


def _trim(fs: list[dict], limit: int) -> list[dict]:
    return _compact(fs, limit)[0]


@tool("verify", """Run KiCad's own checks and get actionable findings. Use after EVERY batch of edits (sch_apply, pcb_*), and before fab_export.

what: list of 'erc' (schematic electrical rules), 'drc' (board rules + unrouted nets + schematic↔board parity; zones are refilled in memory,
the file is not modified) and 'review' (engineering heuristics, see design_review). Default: ['erc'] plus 'drc' when a board exists.
Each finding has severity, type, message, refs (pins like 'U1.3' / footprints), at [x,y] mm (on `sheet` for sub-sheets) and `fix` = the exact
next tool call, already pointed at the right sub-sheet. Repeats of one problem are merged into one entry with `count` + all `refs`.
`next` is the single most useful call to make now; `new` / `fixed` compare with the previous run. Checks run on the SAVED files.
Example: verify(what=['erc','drc'])""",
      {"what": {"type": "array", "items": {"type": "string", "enum": ["erc", "drc", "review"]}},
       "project": PROJECT,
       "refill": {"type": "boolean", "description": "refill zones before DRC (default true)"},
       "parity": {"type": "boolean", "description": "check schematic↔board parity in DRC (default true)"},
       "limit": {"type": "integer", "description": "max finding entries returned per check (default 25)"}},
      title="Verify (ERC/DRC/review)", read_only=True, idempotent=True, core=True, group="verify",
      help="""## Examples
        verify()                                             # ERC, plus DRC when a board exists
        verify(what=['erc'])
        verify(what=['erc','drc','review'], limit=40)
        verify(what=['drc'], parity=false)                   # board rules only, while the schematic is still moving

        ## Reading findings
        - `fix` is a ready call. On a sub-sheet it already names the sheet:
          sch_apply(sheet="power", ops=[{"op":"no_connect","pin":"U1.3"}])
        - 12 unconnected pins come back as ONE entry with count=12 and a single
          sch_apply(ops=[{"op":"no_connect","pins":["U1.3","U1.4", ...]}]) call. Wire the pins that are used first.
        - When merged items need different fixes, `fix` is the first and `fixes` lists the others.
        - A pin on a power symbol or PWR_FLAG (#PWR.., #FLG..) that is not connected: delete that symbol,
          sch_apply(ops=[{"op":"delete","ref":"#FLG03"}]); never no_connect it.
        - Dangling wires: sch_apply(ops=[{"op":"delete","dangling":true}]) removes every stray wire run;
          {"op":"delete","wire_at":[x,y]} removes just the segment at a finding's `at`.
        - DRC clearance / short / crossing: `fix` deletes the exact track and via uuids of that violation
          (pcb_edit(ops=[{"op":"delete","uuids":[...]}]) then pcb_route()). It never rips up a whole net.
          When the clash is pad-to-pad or zone-to-pad there is no track to delete: move a part or refill the pour.
        - ERC `multiple_net_names` and `same_local_global_label` are escalated to errors: they usually mean two signals are shorted.
        - A pin conflict where one side is a PWR_FLAG is called benign in its `fix`.

        ## Notes
        - `note` appears when the schematic or board is open in KiCad: results are for the last SAVED file.
        - `erc.new` / `erc.fixed` (same for drc) compare with the previous verify run; `fixed_findings` names what went away.
        - `more` means entries were cut by `limit`; the full JSON report path is in `report`.""")
def verify(what=None, project=None, refill=True, parity=True, limit=25, _progress=None):
    pr = P.resolve(project)
    if not what:
        what = ["erc"] + (["drc"] if pr.pcb.exists() else [])
    prog = _progress or (lambda f, m="": None)
    data: dict = {"project": pr.name}
    parts = []
    if "erc" in what:
        prog(0.1, "ERC")
        e = V.run_erc(pr)
        data["erc"] = {"errors": e["errors"], "warnings": e["warnings"], "new": e["new"], "fixed": e["fixed"],
                       "by_type": e["by_type"], **_listing(e["violations"], limit), "fixed_findings": e["fixed_findings"],
                       "report": e["report"]}
        parts.append(f"ERC {e['errors']} errors/{e['warnings']} warnings" + _d(e))
    if "drc" in what:
        if not pr.pcb.exists():
            data["drc"] = {"skipped": "no board yet — pcb_sync() creates it"}
        else:
            prog(0.45, "DRC")
            d = V.run_drc(pr, refill=refill, parity=parity)
            data["drc"] = {"errors": d["errors"], "warnings": d["warnings"], "unconnected": d["unconnected"],
                           "parity": d["parity"], "new": d["new"], "fixed": d["fixed"], "by_type": d["by_type"],
                           "unrouted_nets": d["unrouted_nets"][:30], **_listing(d["violations"], limit), "fixed_findings": d["fixed_findings"],
                           "report": d["report"]}
            parts.append(f"DRC {d['errors']} errors/{d['warnings']} warnings, {d['unconnected']} unrouted, {d['parity']} parity" + _d(d))
            try:    # layout quality, gated by the very numbers above (review/elegance.py)
                from ..review import elegance as EL
                el = EL.brief(EL.elegance(pr, {"errors": d["errors"], "unconnected": d["unconnected"]}), 4)
                data["elegance"] = el
                parts.append(f"elegance {el['score']}" + (f" (ungated {el['ungated']})" if el.get("gates") else ""))
            except Exception:  # noqa: BLE001 — a score must never fail verify
                pass
    if "review" in what:
        prog(0.8, "design review")
        r = DR.review(pr, include_erc="erc" not in what)
        if "erc" in what:
            # reuse the ERC we just ran for the power-flag check
            r["findings"] = [f for f in r["findings"] if f["type"] != "rail_not_driven"]
        data["review"] = {"counts": r["counts"], **_listing(r["findings"], limit), "stats": _review_stats(r["stats"])}
        parts.append(f"review {r['counts']['error']}E/{r['counts']['warning']}W/{r['counts']['info']}I")
    try:
        if pr.sch.exists() and not any(re.search(r"^\s*\(symbol\s+\(lib_id ", f.read_text(encoding="utf-8", errors="replace"), re.M)
                                       for f in pr.sheets()):
            data["_empty_sch"] = True
    except OSError:
        pass
    if "drc" in data and not data["drc"].get("skipped"):
        try:
            txt = pr.pcb.read_text(encoding="utf-8", errors="replace")
            if not re.search(r"^\s*\(footprint ", txt, re.M):
                data["_empty_board"] = True
        except OSError:
            pass
    data["next"] = _next(data)
    data.pop("_empty_board", None)
    data.pop("_empty_sch", None)
    open_now = [f.name for f in (pr.sch, pr.pcb) if f.exists() and P.is_open_in_kicad(f)]
    if open_now:
        data["note"] = (f"{', '.join(open_now)} is open in KiCad — these results are for the last SAVED file; "
                        f"save in KiCad (Ctrl+S) first if you edited it there")
    for sec in ("erc", "drc"):     # keep the payload compact: drop empty bookkeeping
        for k in ("fixed_findings", "unrouted_nets", "by_type"):
            if sec in data and not data[sec].get(k, True):
                data[sec].pop(k, None)
    return Result(data, summary="; ".join(parts) + (f" → {data['next']}" if data["next"] else ""))


def _listing(fs: list[dict], limit: int) -> dict:
    entries, more = _compact(fs, limit)
    d = {"findings": entries}
    if more:
        d["more"] = f"{more} more finding group(s) not shown — raise `limit` or open the report"
    return d


def _review_stats(st: dict) -> dict:
    """Keep the review stats compact: the per-kind histogram is only noise for the model."""
    return {k: v for k, v in st.items() if k not in ("by_kind",)}


def _d(r: dict) -> str:
    if r.get("first_run"):
        return " (first verify run — no earlier result to compare)"
    if r.get("new") or r.get("fixed"):
        return f" (+{r['new']} new, -{r['fixed']} fixed)"
    return " (no change)"


def _drc_next(dr: dict) -> str:
    """`next` for a board that is connected but has DRC errors: a call, like every other `next` — the first error
    finding's own fix from its tool call on, or a look at the board where the findings point."""
    from ..mcp import registry
    for f in dr.get("findings") or []:
        if f.get("severity") != "error":
            continue
        fix = str(f.get("fix") or "")
        for m in re.finditer(r"\b([a-z][a-z0-9_]+)\(", fix):
            if m.group(1) in registry.TOOLS:
                return f"fix DRC errors, first {f.get('type')}: {fix[m.start():]}"
        break
    return "render(target='pcb') to look at findings[].at, then fix the DRC errors (see findings[].fix)"


def _next(d: dict) -> str:
    if d.get("_empty_sch"):
        return ("the schematic has no parts yet — parts_search(query=...) then sch_apply(ops=[{\"op\":\"place\",...},"
                "{\"op\":\"connect\",...}])")
    e = d.get("erc") or {}
    if e.get("errors"):
        return f"fix ERC errors first: {e['findings'][0]['fix'] if e.get('findings') else 'see findings'}"
    dr = d.get("drc") or {}
    if dr.get("parity"):
        return "pcb_sync() — board is out of date with the schematic"
    if dr.get("unconnected"):
        return f"pcb_route() — {dr['unconnected']} unrouted connection(s) on {', '.join(dr.get('unrouted_nets', [])[:6])}"
    if dr.get("errors"):
        return _drc_next(dr)
    rv = d.get("review") or {}
    if (rv.get("counts") or {}).get("error"):
        return "address design_review errors"
    lib = set((e.get("by_type") or {})) | set((dr.get("by_type") or {}))
    if lib & {"lib_symbol_issues", "footprint_link_issues", "lib_footprint_issues"}:
        return "library(action='repair', dry_run=false) — KiCad can't resolve some symbol/footprint libraries (see warnings)"
    if (rv.get("counts") or {}).get("warning"):
        return "review the design_review warnings (each has a fix)"
    soft = {"endpoint_off_grid", "lib_symbol_mismatch", "simulation_model_issue", "footprint_filter"}
    ew = [f for f in (e.get("findings") or []) if f.get("severity") == "warning" and f.get("type") not in soft]
    if ew:
        return f"ERC warning {ew[0].get('type')}: {ew[0].get('fix')}"
    if "drc" in d and not dr.get("skipped"):
        if d.get("_empty_board"):
            return "the board has no footprints yet — pcb_sync() to bring the schematic's parts onto it"
        return "clean — fab_export() when you're ready to order"
    return ""


@tool("design_review", """Engineering review of the schematic netlist (and the board when there is one): catches what passes ERC but kills boards.
Decoupling, I2C pull-ups, crystal load caps, USB-C CC resistors, USB ESD, regulator caps and feedback-divider voltage, floating EN/RESET/BOOT pins,
LED resistors, undriven rails, part sourcing (+ the fab's assembly-class fee), missing footprints, label typos, thin power-path tracks, thermal pads without vias, sheet style.
Read-only. Each finding carries a `fix` with the exact ops to resolve it. checks: subset of the enum (default all).
Example: design_review(checks=['decoupling','usb_c_cc'])""",
      {"checks": {"type": "array", "items": {"type": "string", "enum": DR.CHECKS}},
       "project": PROJECT,
       "limit": {"type": "integer", "description": "max findings returned (default 40)"}},
      title="Design review", read_only=True, idempotent=True, group="verify",
      help="""## What each group of checks looks for
        - ICs without a decoupling cap on each power pin (measured against the IC's own ground net, so a protection IC
          whose ground is BATN gets its cap to BATN).
        - I2C nets without pull-ups; crystals without load caps.
        - USB-C CC1/CC2 without 5.1k pull-downs (sink) or tied together; USB D+/D- without ESD when an IC is on the data lines.
        - Regulators without input/output caps; adjustable regulators whose feedback divider does not give the rail's named voltage.
        - Floating EN / RESET / BOOT pins; LEDs without a series resistor; rails nothing drives (needs a PWR_FLAG).
        - Parts without the project's part number (LCSC for the default lcsc provider + jlcpcb profile, with the JLC
          Extended-part setup fee estimate; MPN or your own field with other providers); symbols without footprints.
        - Named nets with a single pin (label typos, with 'did you mean'); nets whose names differ only by case or separators.
        - Board-aware: power-path nets (inductor, battery, power FET, switch node) narrower than 0.4 mm or left in a thin
          net class; exposed pads of power ICs without thermal vias.

        - schematic_style (rules: docs/SCHEMATIC_STYLE.md), read from the saved sheets: power symbols pointing the wrong
          way (supplies up, grounds and negative rails down); four-way junctions (max three wires); nets hopping by label
          between neighbours instead of a wire; bare VCC/VDD rail names; rails without a min/typ/max voltage or current
          note; passives without tolerance / rating / package and sheets without a standard-parts note; global labels
          with inter-sheet references off; discrete transistors without the IEEE 315 envelope circle; package-shaped
          or unfilled IC symbols; test points without an expected reading; no-connect inputs without a note; a root
          sheet that is not a block diagram; a crammed sheet. Style findings are warnings or notes, never errors.

        Net names inside a `fix` are label names (`VBAT`), not KiCad's path form (`/VBAT`, `/power/VBAT`), so the ops can be
        pasted into sch_apply as they are. The finding's `net` field keeps KiCad's full name.

        ## Examples
        design_review()
        design_review(checks=['decoupling','usb_c_cc'])
        design_review(checks=['sourcing','footprints'], limit=80)""")
def design_review(checks=None, project=None, limit=40):
    pr = P.resolve(project)
    r = DR.review(pr, checks=checks)
    c = r["counts"]
    return Result({"counts": c, **_listing(r["findings"], limit),
                   "stats": _review_stats(r["stats"]), "checks_run": r["checks_run"]},
                  summary=f"design review: {c['error']} error(s), {c['warning']} warning(s), {c['info']} note(s)")


@tool("render", """Look at the design: returns PNG image(s) the model can see. Use after edits to check layout, and with `crop` to zoom on a finding's `at`.

target: 'schematic' (every sheet, or `sheet`='name' for one), 'pcb' (2D layers), 'pcb3d' (raytraced 3D).
layers (pcb): default all copper + silkscreen + front mask + outline (no F.Fab: its value texts sit on top of the reference
designators); pass e.g. ['F.Cu','Edge.Cuts'] to choose. view: pcb → top|bottom; pcb3d → top|bottom|iso|iso_bottom|front.
crop: [x0,y0,x1,y1] in mm document coordinates (schematic / pcb top view). width: pixels, ≤ 2400 (default 1400).
Example: render(target='schematic', crop=[90,40,180,110])""",
      {"target": {"type": "string", "enum": ["schematic", "pcb", "pcb3d"]},
       "sheet": {"type": "string", "description": "schematic sheet name or file stem (default: all sheets, max 8)"},
       "layers": {"type": "array", "items": {"type": "string"}},
       "view": {"type": "string", "enum": ["top", "bottom", "iso", "iso_bottom", "front"]},
       "crop": {"type": "array", "items": {"type": "number"}, "description": "[x0,y0,x1,y1] mm"},
       "width": {"type": "integer"},
       "project": PROJECT},
      ["target"], title="Render", read_only=True, idempotent=True, group="verify",
      help="""## Examples
        render(target='schematic')                                   # every sheet (max 8)
        render(target='schematic', sheet='power')                    # one sub-sheet by name or file stem
        render(target='schematic', crop=[90,40,180,110])             # zoom: a finding at [120,80] -> crop=[100,60,140,100]
        render(target='pcb')                                         # top view, default layers
        render(target='pcb', layers=['F.Cu','B.Cu','Edge.Cuts'])     # copper only (inner layers are added on 4-layer boards)
        render(target='pcb', layers=['F.Cu','F.SilkS','F.Fab','Edge.Cuts'])   # add F.Fab to see part values and outlines
        render(target='pcb', view='bottom')                          # mirrored, as seen from the back
        render(target='pcb', crop=[100,100,130,120])                 # board coordinates, as in pcb_read
        render(target='pcb3d', view='iso')

        ## Notes
        - PCB layers are painted back to front, so front copper is never hidden under a back pour.
        - Default PCB layers: F.Cu, B.Cu (+ In*.Cu), F.SilkS, B.SilkS, F.Mask, Edge.Cuts. The bottom view defaults to
          B.Cu, B.SilkS, B.Mask, Edge.Cuts.
        - crop is ignored for the mirrored bottom view; use view='top' with layers=['B.Cu', ...] to zoom on the back unmirrored.
        - An unknown `sheet` is an error (NOT_FOUND), not a render of the root sheet.
        - A crop is rasterised at higher resolution first, so zooming gives real detail, then scaled to `width`.""")
def render(target, sheet=None, layers=None, view=None, crop=None, width=1400, project=None):
    pr = P.resolve(project)
    width = max(300, min(int(width or 1400), 2400))
    if crop is not None and (len(crop) != 4 or crop[2] <= crop[0] or crop[3] <= crop[1]):
        raise ToolError("crop must be [x0, y0, x1, y1] with x1>x0 and y1>y0 (mm)", code="BAD_ARGUMENT")
    out = out_dir(pr) / "render"
    final_w = width
    if crop is not None and target in ("schematic", "pcb"):
        page_w = _page_width_mm(pr, target)
        if page_w:
            width = int(max(width, min(8000, width * page_w / max(1.0, crop[2] - crop[0] + 4))))
    if target == "schematic":
        if not pr.sch.exists():
            raise ToolError("no schematic to render", code="NO_SCHEMATIC", fix="project_create(...) / project_open(...)")
        key = sheet.strip().strip("/").split("/")[-1].replace(".kicad_sch", "") if sheet else None
        imgs = R.schematic(pr.sch, out, width=width, crop_mm=crop, max_sheets=32 if sheet else 8, sheet=key or None)
        for i in imgs:
            _fit(i.path, final_w)
        return Result({"target": "schematic", "images": [i.path for i in imgs]}, images=imgs,
                      summary=f"rendered {len(imgs)} schematic sheet(s)")
    if not pr.pcb.exists():
        raise ToolError("no board to render", code="NO_PCB", fix="pcb_sync() to create the board from the schematic")
    if target == "pcb":
        v = view or "top"
        if v not in ("top", "bottom"):
            raise ToolError("pcb view must be top or bottom (use target='pcb3d' for iso)", code="BAD_ARGUMENT")
        note = None
        if v == "bottom":
            lay = layers or ["B.Cu", "B.SilkS", "B.Mask", "Edge.Cuts"]
            if crop is not None:
                # the mirrored plot flips X, so board-coordinate crops would land on the wrong spot
                note = "crop ignored for the mirrored bottom view — use view='top' with layers=['B.Cu',…] to zoom unmirrored"
                width = final_w
            im = R.pcb_layers(pr.pcb, out, lay, width=width, crop_mm=None, mirror=True, name="pcb-bottom", auto_inner=not layers)
        else:
            im = R.pcb_layers(pr.pcb, out, layers, width=width, crop_mm=crop, name="pcb-top", auto_inner=not layers)
        _fit(im.path, final_w)
        d = {"target": "pcb", "view": v, "image": im.path}
        if note:
            d["note"] = note
        return Result(d, images=[im], summary=f"rendered PCB {v}")
    v = view or "iso"
    if v not in R.VIEWS:
        raise ToolError(f"3D view must be one of {list(R.VIEWS)}", code="BAD_ARGUMENT")
    im = R.pcb_3d(pr.pcb, out, view=v, width=width, height=int(width * 0.64))
    return Result({"target": "pcb3d", "view": v, "image": im.path}, images=[im], summary=f"rendered 3D {v}")


_PAPER = {"A5": 210, "A4": 297, "A3": 420, "A2": 594, "A1": 841, "A0": 1189, "A": 279.4, "B": 431.8, "C": 558.8,
          "D": 863.6, "E": 1117.6, "USLetter": 279.4, "USLegal": 355.6, "USLedger": 431.8}


def _page_width_mm(pr, target: str) -> float | None:
    """Width of the rendered document so a crop can be rasterised at full detail."""
    import re
    if target == "pcb":
        from ..review.project_info import board_outline_mm
        bb = board_outline_mm(pr.pcb)
        return bb[0] if bb else None
    try:
        head = pr.sch.read_text(encoding="utf-8")[:3000]
    except OSError:
        return None
    m = re.search(r'\(paper "([^"]+)"(?:\s+([\d.]+)\s+([\d.]+))?(\s+portrait)?', head)
    if not m:
        return 297.0
    if m.group(1) == "User" and m.group(2):
        return float(m.group(2))
    w = _PAPER.get(m.group(1), 297.0)
    if m.group(4):  # portrait: width is the short side
        short = {"A5": 148, "A4": 210, "A3": 297, "A2": 420, "A1": 594, "A0": 841, "USLetter": 215.9, "A": 215.9}
        return short.get(m.group(1), w)
    return w


def _fit(path: str, width: int):
    """Downscale (never upscale) a PNG to at most `width` px wide."""
    try:
        from PIL import Image as PImage
        im = PImage.open(path)
        if im.width > width:
            h = int(im.height * width / im.width)
            im.resize((width, max(1, h)), PImage.LANCZOS).save(path)
    except Exception:  # noqa: BLE001
        pass
