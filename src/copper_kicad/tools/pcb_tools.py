"""PCB tools: pcb_read, pcb_sync, pcb_setup, pcb_place, pcb_route, pcb_pour, pcb_edit.

Board edits run in short-lived pcbnew worker processes (KiCad's own Python);
every write is refused while the board is open in KiCad (unless
while_open='revert'), checkpointed first, and re-verified with kicad-cli DRC.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import project as projmod
from .. import render
from ..mcp.registry import Image, Result, ToolError, tool
from ..pcb import board, place as placer, rules as rules_mod

PROJECT = {"type": "string", "description": "Project folder or .kicad_pro/.kicad_pcb path. Omit to use the active project."}
WHILE_OPEN = {"type": "string", "enum": ["refuse", "revert"],
              "description": "If the board is open in KiCad: 'refuse' (default) or 'revert' = write anyway, then use File ▸ Revert in KiCad."}
RENDER = {"type": "boolean", "description": "Attach a PNG of the board (default true)."}


def _render(pr, name="pcb", layers=None, width=1100) -> list[Image]:
    try:
        return [render.pcb_layers(pr.pcb, pr.out_dir, layers=layers or ["B.Cu", "F.Cu", "F.SilkS", "Edge.Cuts"],
                                  width=width, name=name)]
    except ToolError:
        return []


def _findings(d: dict, snapshot: dict | None = None) -> list[dict]:
    f = []
    if d.get("parity"):
        f.append({"severity": "warning", "code": "SCHEMATIC_PARITY",
                  "message": f"{d['parity']} board/schematic mismatches", "fix": "pcb_sync()",
                  "examples": [p["message"] + " " + ", ".join(p.get("items") or []) for p in d.get("parity_top", [])[:4]]})
    if d.get("unconnected"):
        f.append({"severity": "info", "code": "UNROUTED", "message": f"{d['unconnected']} unrouted connections",
                  "fix": "pcb_route(mode='auto') — or pcb_route(tracks=[...]) for specific connections"})
    real = {k: v for k, v in (d.get("by_type") or {}).items() if not k.endswith("lib_footprint_issues")
            and not k.endswith("lib_footprint_mismatch") and not k.endswith("invalid_outline")}
    for t in d.get("top", []):
        if t["type"] == "invalid_outline" and "no edges" in (t.get("message") or ""):
            f.append({"severity": "info", "code": "NO_OUTLINE", "message": "the board has no Edge.Cuts outline yet",
                      "fix": "pcb_setup(width=..., height=...)"})
            continue
        if t["type"] == "hole_clearance" and _same_footprint(t.get("items") or []):
            f.append({"severity": "info", "code": "FOOTPRINT_INTERNAL_HOLE_CLEARANCE", "message": t["message"],
                      "items": t.get("items"),
                      "fix": "both items belong to the same footprint, so this spacing comes from the library footprint "
                             "itself, not from placement or routing. Check it against your fab's hole-to-copper limit; "
                             "if acceptable, exclude it in KiCad's DRC dialog, otherwise choose another footprint."})
            continue
        if t["severity"] == "error" and t["type"] != "unconnected_items":
            f.append({"severity": "error", "code": t["type"], "message": t["message"], "items": t.get("items"),
                      "at": t.get("at"), "fix": _fix_for(t["type"], t)})
            if len([x for x in f if x["severity"] == "error"]) >= 8:
                break
    if real:
        f.append({"severity": "info", "code": "DRC_SUMMARY", "message": ", ".join(f"{k}×{v}" for k, v in sorted(real.items()))})
    if snapshot and snapshot.get("outline"):
        x0, y0, x1, y1 = snapshot["outline"]["bbox"]
        out = [fp["ref"] for fp in snapshot.get("footprints", []) if not (x0 <= fp["x"] <= x1 and y0 <= fp["y"] <= y1)]
        if out:
            f.append({"severity": "warning", "code": "OUTSIDE_OUTLINE",
                      "message": f"{len(out)} footprints outside the board outline: {_listed(out, 40)}",
                      "refs": out, "fix": "pcb_place(auto=true)"})
    return f


def _listed(names: list, limit: int = 40) -> str:
    """Every name, or the first ``limit`` plus an explicit '… and N more' — a count next to a silently
    shortened list reads as if the missing ones were not counted."""
    names = list(names)
    return ", ".join(names[:limit]) + (f", … and {len(names) - limit} more" if len(names) > limit else "")


def _resolve_net(pr, name: str, names: set | None = None) -> str:
    """Accept 'VBUS' for the board net '/VBUS' (root-sheet local labels) when unambiguous."""
    if names is None:
        names = {n["name"] for n in board.read(pr, what=["nets"]).get("nets", [])}
    if name in names:
        return name
    cands = [n for n in names if n.rsplit("/", 1)[-1] == name.rsplit("/", 1)[-1]]
    if len(cands) == 1:
        return cands[0]
    raise ToolError(f"net '{name}' is not on the board" + (f" (ambiguous: {', '.join(sorted(cands))})" if cands else ""),
                    code="NOT_FOUND",
                    fix=f"pass the exact name — board nets: {', '.join(sorted(names)[:30])}" if names else
                        "the board has no nets yet: pcb_sync() first")


def _same_footprint(items: list[str]) -> bool:
    import re
    refs = {m.group(1) for it in items for m in [re.search(r" of ([A-Za-z#]+[\w-]*)", it or "")] if m}
    return len(items) >= 2 and len(refs) == 1


_COPPER_ITEM = re.compile(r"^(Track|Via|Arc)\b")


def _fix_for(t: str, v: dict | None = None) -> str:
    """Fix for one DRC violation. Copper-clash fixes name the exact track/via uuids of the violation
    (never a whole-net rip-up, which would destroy hand routing on boards Copper did not route)."""
    v = v or {}
    items, uuids = v.get("items") or [], v.get("uuids") or []
    tracks = [u for d, u in zip(items, uuids) if _COPPER_ITEM.match(d or "")]
    zone = any((d or "").startswith("Zone") for d in items)
    if t in ("clearance", "shorting_items", "tracks_crossing", "hole_clearance", "via_dangling", "track_dangling"):
        if tracks:
            return (f"pcb_edit(ops=[{{op:'delete', uuids:{tracks}}}]) removes the offending track/via, then "
                    "pcb_route() re-routes just that connection")
        if zone:
            return "zone fill is stale or too close: pcb_pour(fill_only=true), or pcb_pour(net=..., clearance=...)"
        if t == "hole_clearance":
            return "move the part or via away from the hole: pcb_place(moves=[{ref, x, y}])"
        return "two footprints' pads clash: pcb_place(moves=[{ref, x, y}]) to separate them"
    return {
        "courtyards_overlap": "pcb_place(auto=true) or pcb_place(moves=[{ref, x, y}])",
        "copper_edge_clearance": "move the part inward: pcb_place(moves=[...]) or pcb_place(auto=true)",
        "invalid_outline": "pcb_setup(width=..., height=...)",
        "malformed_courtyard": "check the footprint in the library",
        "annular_width": "via/pad ring below the fab minimum: pcb_edit delete the via (uuids) and re-add with size 0.6/drill 0.3",
        "silk_over_copper": "cosmetic — usually safe for JLC (silk is clipped)",
        "text_height": "pcb_setup(silk_text=1.0)",
        "starved_thermal": "a track or via beside the pad cut one of its thermal spokes: move that copper away "
                           "(pcb_edit delete its uuids → pcb_route), or pcb_pour(net=..., pad_connection='solid') "
                           "to join the pour's pads without spokes",
    }.get(t, "inspect with pcb_read() and render()")


def _drc_brief(d: dict) -> dict:
    return {k: d[k] for k in ("errors", "warnings", "unconnected", "parity") if k in d}


# ── pcb_read ──────────────────────────────────────────────────────────────
@tool("pcb_read",
      """Read the PCB: footprints (ref, value, position mm, rotation, side, LCSC/MPN), nets, track/via counts,
unrouted count, board outline/size and layer count. Read-only and cheap — call it before and after PCB edits.

what: any of summary | footprints | nets | pads | tracks | widths | zones | drawings | elegance (default summary+footprints+nets).
elegance = the 0-100 layout score with every measured number behind it (verify shows the short form).
zones lists copper pours and rule areas; teardrops are one `teardrop_zones` count, not an entry each.
widths = compact per-net copper summary {net: {width_mm: length_mm, layers, vias}} (check power widths cheaply);
tracks = every segment/via with its uuid (large — combine with net=...).
refs: only these footprints (e.g. ["U1","C3"]). net: only that net's pads/tracks.
drc=true also runs KiCad DRC (errors, unconnected, schematic parity).
source='live' reads the board open in the running KiCad PCB editor (needs the KiCad API enabled + kicad-python).
Example: pcb_read(what=["footprints"], refs=["U1"]) or pcb_read(what=["nets"], net="GND").""",
      {"project": PROJECT,
       "what": {"type": "array", "items": {"type": "string", "enum": ["summary", "footprints", "nets", "pads", "tracks",
                                                                      "widths", "zones", "drawings", "elegance"]}},
       "refs": {"type": "array", "items": {"type": "string"}},
       "net": {"type": "string", "description": "Filter nets/pads/tracks to this net name"},
       "drc": {"type": "boolean", "description": "Also run kicad-cli DRC (slower; default false)"},
       "source": {"type": "string", "enum": ["file", "live"], "description": "file (default) or live IPC board"},
       "limit": {"type": "integer", "description": "Max footprints/tracks listed (default 200)"}},
      read_only=True, idempotent=True, core=True, group="pcb", title="Read PCB")
def pcb_read(project=None, what=None, refs=None, net=None, drc=False, source="file", limit=200):
    if source == "live":
        from ..pcb import live
        try:
            return Result(live.read_live(limit), summary="live board from the running KiCad PCB editor")
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"live board not available: {e}", code="NO_LIVE_BOARD", fix="pcb_read() (file mode)",
                            data={"capability": live.capability()})
    pr = projmod.resolve(project)
    what = list(what or ["summary", "footprints", "nets"])
    w = set(what)
    want_elegance = "elegance" in w
    w.discard("elegance")
    if want_elegance and not w:
        w.add("summary")
    if "pads" in w:
        w |= {"footprints"}
    want_widths = "widths" in w
    if want_widths:
        w.discard("widths")
        w.add("tracks")
    snap = board.read(pr, what=sorted(w), refs=refs)
    if want_widths:
        snap["widths"] = _width_summary(snap.get("track_list") or [], net)
        if "tracks" not in what:
            snap.pop("track_list", None)
    fps = snap.get("footprints")
    if fps is not None:
        if net:
            fps = [f for f in fps if any(p.get("net") == net for p in f.get("pad_list", []))] if "pads" in w else fps
        for f in fps:
            if "pad_list" in f and net:
                f["pad_list"] = [p for p in f["pad_list"] if p.get("net") == net]
        snap["footprints"] = [_compact_fp(f) for f in fps[:limit]]
        if len(fps) > limit:
            snap["truncated"] = f"{len(fps) - limit} more footprints — pass refs=[...] or limit"
    if net and snap.get("nets"):
        snap["nets"] = [n for n in snap["nets"] if n["name"] == net]
    if net and snap.get("track_list"):
        snap["track_list"] = [t for t in snap["track_list"] if t["net"] == net]
    if snap.get("track_list") and len(snap["track_list"]) > limit:
        snap["track_list"] = snap["track_list"][:limit]
    if drc:
        d = board.drc(pr, parity=True)
        snap["drc"] = d
        snap["findings"] = _findings(d, snap if fps is not None else None)
    if want_elegance:
        from ..review import elegance as _el
        snap["elegance"] = _el.elegance(pr, snap.get("drc"), metrics=True)
    try:
        from ..pcb import live
        cap = live.capability()
        if cap["available"]:
            snap["live_available"] = True   # pcb_read(source='live') reads the board open in KiCad
    except Exception:  # noqa: BLE001
        pass
    ol = snap.get("outline")
    if ol:
        snap["outline"] = _compact_outline(ol)
    size = f"{ol['width']}×{ol['height']} mm" if ol else "no outline"
    summ = (f"{pr.pcb.name}: {snap.get('footprint_count')} footprints, {size}, {snap.get('copper_layers')} layers, "
            f"{snap.get('tracks')} tracks, {snap.get('vias')} vias, {snap.get('unconnected')} unrouted")
    return Result(snap, summary=summ)


def _width_summary(tl: list[dict], net: str | None = None) -> dict:
    import math
    out = {}
    for t in tl:
        if net and t["net"] != net:
            continue
        e = out.setdefault(t["net"] or "(no net)", {"widths": {}, "layers": set(), "vias": 0})
        if t["type"] == "via":
            e["vias"] += 1
            continue
        ln = math.hypot(t["end"][0] - t["start"][0], t["end"][1] - t["start"][1])
        k = f"{t['width']:g}"
        e["widths"][k] = round(e["widths"].get(k, 0) + ln, 2)
        e["layers"].add(t["layer"])
    return {n: {"widths": e["widths"], "layers": sorted(e["layers"]), "vias": e["vias"]} for n, e in sorted(out.items())}


NOISE_FIELDS = {"KiLib_Generator", "LCSC", "MPN", "Sheetname", "Sheetfile", "ki_fp_filters", "ki_description",
                "ki_keywords"}


def _compact_fp(f: dict) -> dict:
    """One footprint as an agent needs it: identity, placement, sourcing; no UUIDs / generator noise."""
    out = {k: f[k] for k in ("ref", "value", "footprint", "x", "y", "rot", "side") if k in f}
    if f.get("bbox"):
        out["bbox"] = [round(v, 2) for v in f["bbox"]]
    for k in ("lcsc", "mpn", "flags", "pad_list"):
        if f.get(k):
            out[k] = f[k]
    if f.get("locked"):
        out["locked"] = True
    extra = {k: v for k, v in (f.get("fields") or {}).items() if k not in NOISE_FIELDS}
    if extra:
        out["fields"] = extra
    return out


def _compact_outline(ol: dict) -> dict:
    out = {k: ol[k] for k in ("bbox", "width", "height", "closed") if k in ol}
    poly = ol.get("polygon") or []
    if poly and not (len(poly) == 4 and {p[0] for p in poly} <= {ol["bbox"][0], ol["bbox"][2]}):
        if len(poly) <= 24:
            out["polygon"] = poly
        else:
            out["polygon_points"] = len(poly)   # rounded corners / curves: bbox is what placement needs
    return out


# ── pcb_sync ──────────────────────────────────────────────────────────────
@tool("pcb_sync",
      """Update the PCB from the schematic (KiCad's "Update PCB from Schematic", F8). Creates the .kicad_pcb if missing
(KiCad 10 format, 2 layers). Adds missing footprints (resolved through the global + project fp-lib-tables), updates
value / LCSC / MPN / other fields on existing ones, swaps changed footprints in place, assigns every pad's net,
keeps existing positions, and puts newly added footprints in a tidy grid beside the board outline.
Run it after every schematic change that touches parts or nets, then pcb_place(auto=true) for new parts.
Refuses (writes nothing) if the schematic has unannotated (R?) or duplicate references.
Typical new-board flow: pcb_sync → pcb_setup(width, height) → pcb_place(auto=true) → pcb_route() → pcb_pour() → verify.

Copper follows the nets: tracks/vias/zones of a renamed net move to the new name; teardrops of a vanished net
are removed; tracks/vias left from deleted parts are reported (ORPHAN_COPPER with their uuids).
remove_extra=true deletes footprints that are no longer in the schematic (mounting holes / board-only items are kept)
and that orphaned copper.
Example: pcb_sync() → then pcb_setup(width=50, height=40) on a new board.""",
      {"project": PROJECT,
       "remove_extra": {"type": "boolean", "description": "Delete board footprints whose symbol was deleted (default false: report only)"},
       "update_footprints": {"type": "boolean", "description": "Swap footprints whose schematic Footprint field changed (default true)"},
       "while_open": WHILE_OPEN, "render": RENDER},
      destructive=False, idempotent=True, group="pcb", title="Update PCB from schematic")
def pcb_sync(project=None, remove_extra=False, update_footprints=True, while_open="refuse", render=True):
    pr = projmod.resolve(project)
    nl = board.netlist(pr)
    _check_annotation(nl)
    libs = board.fp_libs(pr)
    create = not pr.pcb.exists()
    res = board.run_ops(pr, [{"op": "sync", "components": nl["components"], "nets": nl["nets"], "libs": libs,
                              "remove_extra": remove_extra, "update_footprints": update_footprints}],
                        create=create, while_open=while_open, label="before-pcb-sync")
    rep = res["results"][0]
    try:   # older projects / templates: 'VBAT*' never matched the local-label net '/VBAT'
        pats_added = rules_mod.normalize_project(pr, while_open)
    except ToolError:
        pats_added = []
    d = board.drc(pr, parity=True)
    findings = _findings(d)
    if rep.get("orphan_copper"):
        uu = [u for v in rep["orphan_copper"].values() for u in v]
        findings.append({"severity": "warning", "code": "ORPHAN_COPPER",
                         "message": f"{len(uu)} tracks/vias on nets that no longer exist "
                                    f"({', '.join(sorted(rep['orphan_copper'])[:6])}) — left from deleted parts",
                         "fix": (f"pcb_edit(ops=[{{op:'delete', uuids:{uu}}}])" if len(uu) <= 12 else
                                 "pcb_sync(remove_extra=true) deletes them")})
    if rep.get("stale_pours"):
        findings.append({"severity": "warning", "code": "STALE_POUR",
                         "message": f"copper pours on vanished nets: {', '.join(sorted(set(rep['stale_pours'])))}",
                         "fix": f"pcb_pour(net='{sorted(set(rep['stale_pours']))[0]}', remove=true), then pour the right net"})
    for e in rep.get("errors", []):
        findings.insert(0, {"severity": "error", "code": "FOOTPRINT_MISSING", "message": f"{e['ref']}: {e['error']}",
                            "fix": e.get("fix")})
    if rep.get("extra"):
        findings.append({"severity": "warning", "code": "EXTRA_FOOTPRINTS",
                         "message": f"on the board but not in the schematic: {', '.join(rep['extra'])}",
                         "fix": "pcb_sync(remove_extra=true)"})
    if rep.get("added"):
        findings.append({"severity": "info", "code": "NEW_PARTS_UNPLACED",
                         "message": f"{len(rep['added'])} new footprints were put beside the board",
                         "fix": "pcb_setup(width=..., height=...) if there is no outline yet, then pcb_place(auto=true)"})
    changed = ([f"created {pr.pcb.name}"] if create else []) + \
        [f"added {a['ref']} ({a['footprint']})" for a in rep["added"]] + \
        [f"updated {u['ref']}: {', '.join(u['changes'])}" for u in rep["updated"]] + \
        [f"replaced {r['ref']}: {r['from']} → {r['to']}" for r in rep["replaced"]] + \
        [f"removed {r}" for r in rep["removed"]] + \
        ([f"{rep['net_changes']} pad net assignments"] if rep["net_changes"] else []) + \
        [f"copper re-netted {k} ({v} items)" for k, v in (rep.get("renamed_copper") or {}).items()] + \
        [f"removed {v} stale copper items of vanished net {k}" for k, v in (rep.get("removed_copper") or {}).items()] + \
        ([f"Power net-class patterns + {', '.join(pats_added)} (match local-label nets)"] if pats_added else [])
    if len(changed) > 30:
        changed = changed[:30] + [f"… and {len(changed) - 30} more (pcb_read(what=['footprints']) for the full list)"]
    data = {"changed": changed, "added": len(rep["added"]), "updated": len(rep["updated"]),
            "replaced": len(rep["replaced"]), "removed": rep["removed"], "extra": rep["extra"],
            "nets": rep["nets"], "unassigned_pads": rep["unassigned_pads"][:40],
            "pins_without_pad": rep.get("pins_without_pad", []), "errors": rep["errors"],
            "revision": res["revision"], "verify": {"drc": _drc_brief(d)}, "findings": findings}
    imgs = _render(pr, "pcb-sync") if render else []
    summ = (f"pcb_sync: +{len(rep['added'])} added, {len(rep['updated'])} updated, {len(rep['replaced'])} replaced, "
            f"{len(rep['removed'])} removed; parity {d['parity']}, unrouted {d['unconnected']}")
    return Result(data, images=imgs, summary=summ)


def _check_annotation(nl: dict):
    """KiCad's F8 refuses unannotated / duplicate references — so do we (they would silently merge)."""
    seen, dup, unann = {}, set(), []
    for c in nl["components"]:
        r = c["ref"]
        if not r or r.endswith("?"):
            unann.append(r or "(empty)")
        elif r in seen:
            dup.add(r)
        seen[r] = 1
    if unann or dup:
        msg = []
        if unann:
            msg.append(f"unannotated references ({', '.join(sorted(set(unann))[:8])})")
        if dup:
            msg.append(f"duplicate references {', '.join(sorted(dup)[:12])}")
        raise ToolError("schematic is not fully annotated: " + "; ".join(msg) + ". Nothing was written.",
                        code="NOT_ANNOTATED",
                        fix="give every symbol a unique reference: sch_apply(ops=[{op:'set_field', ref:..., field:'Reference', "
                            "value:'R7'}]) or Tools > Annotate Schematic in KiCad, then pcb_sync() again")


# ── pcb_setup ─────────────────────────────────────────────────────────────
@tool("pcb_setup",
      """Board setup in one call: outline (rectangle width×height mm, optional corner radius, top-left origin), mounting
holes at the corners, copper layer count, fab design rules + net classes, silkscreen text size.
Defaults follow the project's fab profile (JLCPCB out of the box): 2 layers, rules preset 'jlc' (0.127 mm track/space
minimum, 0.6/0.3 mm vias, 0.3 mm copper-to-edge, 0.5 mm hole-to-hole) written to the .kicad_pro and a managed block in the
.kicad_dru. rules: 'fab' (the active profile), a profile name ('pcbway', 'oshpark', 'generic', your own; add '_advanced' for
its tighter tier), 'jlc' / 'jlc_advanced', 'relaxed' or 'none'.
Net classes: values you do not pass keep what the project already has (project_create templates set them);
a project without classes gets Default 0.2 mm and Power 0.4 mm. Power-class patterns (GND, +*, *VCC*, *3V3*, …)
are matched against KiCad's full net names, so power_nets=['VBAT','SW'] also catches the local-label nets '/VBAT'
and '/SW' (each bare name gets a '*/NAME' twin). The result lists how many nets each power_nets pattern matched
and warns about patterns that match nothing and about power-path nets (inductor / regulator / FET / battery
nets) still in the Default class.
Only the parts you pass are changed: pcb_setup(holes='M3') just adds holes, pcb_setup(power_nets=['VBAT'],
power_track=0.8) only edits the Power class. Re-running replaces the outline / holes rather than duplicating them.
No size given by the user? auto_size=true picks the smallest comfortable rectangle for the parts (unrouted boards only).
Outline from a CAD file: outline_file='enclosure.step' (STEP/STPZ, DXF, SVG, STL; or 'auto' = the one file in the
project's mechanical/ folder) takes the board body, or the cavity of an enclosure, with its arcs, cutouts and
mounting holes. The result says what was picked; when the pick is uncertain it lists candidates and shows a
picture. outline_dry_run=true only analyses (works while the board is open in KiCad).
Examples: pcb_setup(width=40, height=30) · pcb_setup(width=50, height=50, radius=2, holes='M3', layers=4) ·
pcb_setup(auto_size=true, holes='M3') · pcb_setup(outline_file='mechanical/case.step', outline_dry_run=true) ·
pcb_setup(power_nets=['BATN','SW'], power_track=0.8)""",
      {"project": PROJECT,
       "width": {"type": "number", "description": "Board width mm"},
       "height": {"type": "number", "description": "Board height mm"},
       "radius": {"type": "number", "description": "Corner radius mm (0 = square)"},
       "origin": {"type": "array", "items": {"type": "number"}, "description": "Top-left corner [x,y] mm (default [100,100])"},
       "polygon": {"type": "array", "items": {"type": "array"}, "description": "Custom outline [[x,y],...] instead of width/height"},
       "holes": {"type": "string", "enum": ["none", "M2", "M2.5", "M3", "M4"], "description": "Corner mounting holes"},
       "hole_inset": {"type": "number", "description": "Hole centre distance from the board edges mm (default 3.5)"},
       "hole_net": {"type": "string", "description": "Use plated holes tied to this net (e.g. GND)"},
       "layers": {"type": "integer", "enum": [2, 4, 6], "description": "Copper layers"},
       "rules": {"type": "string",
                 "description": "Design-rule preset: fab | <fab profile>[_advanced] | jlc | jlc_advanced | relaxed | none "
                                "(default on first setup: the project's fab profile)"},
       "default_track": {"type": "number", "description": "Default net class track width mm (keeps the project's value if omitted)"},
       "power_track": {"type": "number", "description": "Power net class track width mm (keeps the project's value if omitted)"},
       "via": {"type": "array", "items": {"type": "number"}, "description": "Default via [diameter, drill] mm"},
       "power_nets": {"type": "array", "items": {"type": "string"},
                      "description": "Extra Power-class nets / wildcard patterns, e.g. ['VBAT','BATN','SW','*_PWR']"},
       "silk_text": {"type": "number", "description": "Reference designator height mm (most fabs print ≥ 0.8; default 1.0)"},
       "auto_size": {"type": "boolean", "description": "Pick the board size from the parts (no tracks/zones/locked parts)"},
       "outline_file": {"type": "string", "description": "CAD file for the outline (.step .stp .stpz .dxf .svg .stl) or 'auto'"},
       "outline_mode": {"type": "string", "enum": ["auto", "board", "enclosure", "face"],
                        "description": "board = file is the PCB; enclosure = fit into its cavity; face = use outline_pick"},
       "outline_pick": {"type": "integer", "description": "Candidate id from an earlier result's candidates[]"},
       "outline_axis": {"type": "string", "enum": ["+x", "-x", "+y", "-y", "+z", "-z"],
                        "description": "Which way is up (component side / enclosure opening)"},
       "outline_layer": {"type": "string", "description": "DXF layer / SVG group id or label holding the outline"},
       "outline_units": {"type": "string", "enum": ["mm", "inch", "cm", "m"], "description": "Units for STL / unit-less DXF, SVG"},
       "outline_clearance": {"type": "number", "description": "Inward offset mm (default 0; 0.5 for an enclosure)"},
       "outline_flip": {"type": "boolean", "description": "Mirror left-right (drawn from the solder side)"},
       "outline_holes": {"type": "string", "enum": ["mounting", "all", "none"],
                         "description": "mounting (default): 1.8-6.6 mm circles become mounting holes; all: every inner contour is cut"},
       "outline_keep_position": {"type": "boolean", "description": "Keep the file's coordinates instead of moving to origin"},
       "outline_dry_run": {"type": "boolean", "description": "Analyse and show the pick, write nothing"},
       "while_open": WHILE_OPEN, "render": RENDER},
      destructive=False, idempotent=True, group="pcb", title="Board outline, holes, rules")
def pcb_setup(project=None, width=None, height=None, radius=0.0, origin=None, polygon=None, holes=None, hole_inset=3.5,
              hole_net=None, layers=None, rules=None, default_track=None, power_track=None, via=None, power_nets=None,
              silk_text=None, while_open="refuse", render=True, auto_size=None, outline_file=None, outline_mode="auto",
              outline_pick=None, outline_axis=None, outline_layer=None, outline_units=None, outline_clearance=None,
              outline_flip=False, outline_holes="mounting", outline_keep_position=False, outline_dry_run=False):
    pr = projmod.resolve(project)
    if not pr.pcb.exists():
        raise ToolError("no board yet", code="NO_BOARD", fix="pcb_sync() first — it creates the board from the schematic")
    imp = size_fail = None
    if outline_file or auto_size or outline_dry_run:
        from ..pcb import setup_ext
        if outline_file and (width is not None or height is not None or polygon or auto_size):
            raise ToolError("outline_file cannot be combined with width/height, polygon or auto_size", code="BAD_ARGUMENT",
                            fix="pass only outline_file (plus outline_* options)")
        if auto_size and (polygon or (width is not None and height is not None)):
            raise ToolError("auto_size cannot be combined with a full width+height or polygon", code="BAD_ARGUMENT",
                            fix="pass auto_size=true alone, or with only width or only height to fix one side")
        if outline_dry_run and not outline_file:
            raise ToolError("outline_dry_run needs outline_file", code="BAD_ARGUMENT")
    if outline_file:
        # parsed before anything is written; a dry run never needs the board to be writable
        imp = setup_ext.prepare_outline(pr, outline_file, mode=outline_mode, pick=outline_pick, axis=outline_axis,
                                        layer=outline_layer, units=outline_units, clearance=outline_clearance,
                                        flip=outline_flip, holes_policy=outline_holes, keep_position=outline_keep_position,
                                        origin=origin, holes=holes,
                                        hole_net=_resolve_net(pr, hole_net) if hole_net and not outline_dry_run else None,
                                        snap0=None if origin else board.read(pr, what=["summary"]))
        if outline_dry_run:
            d_ = imp.describe(candidates=True)
            return Result({"dry_run": True, "outline_import": d_, "findings": imp.findings},
                          images=imp.preview(pr, show_candidates=True),
                          summary=f"outline dry run: {d_['file']} [{d_['mode']}] {d_['size'][0]}×{d_['size'][1]} mm, "
                                  f"{len(imp.ol['holes'])} mounting holes, {d_['cutouts']} cutouts — nothing written")
    # rules go to the .kicad_pro, which pcbnew rewrites on save — so an open board blocks the whole call
    projmod.guard_writable(pr.pcb, while_open)
    changed, ops = [], []
    snap0 = board.read(pr, what=["summary", "footprints"] if (imp or auto_size) else ["summary"])
    size_info = None
    if imp:
        ops += imp.ops
    elif auto_size:
        why = setup_ext.auto_size_refusal(snap0)
        if why:
            raise ToolError(f"auto_size would move every part, and {why}. Nothing was written.", code="BAD_ARGUMENT",
                            fix="pcb_setup(width=..., height=...) sets a size explicitly; to start the layout over: "
                                "pcb_place(auto=true, clear_tracks=true) after removing zones (pcb_pour(remove=true))")
        geo = board.read(pr, what=["geom"])
        _attach_sch_positions(pr, geo)
        size_ops, size_info = setup_ext.size_ops(pr, geo, holes=holes, hole_inset=hole_inset, origin=origin, width=width,
                                                 height=height, radius=radius,
                                                 hole_net=_resolve_net(pr, hole_net) if hole_net else None)
        if size_ops is None:      # nothing is guessed: the outline stays as it was, the rest of the call still applies
            size_fail = {"severity": "warning", "code": "AUTO_SIZE_FAILED",
                         "message": f"no board size was found: {size_info['failed']}; the outline is unchanged",
                         "fix": "pcb_setup(width=..., height=...) with a size of your choice"}
            if not (layers or rules or silk_text or power_nets or default_track or power_track or via):
                return Result({"changed": [], "auto_size": {k: size_info[k] for k in ("failed", "seconds") if k in size_info},
                               "findings": [size_fail]},
                              summary="pcb_setup: auto_size found no size that places every part; board unchanged")
            size_info = None
        else:
            ops += size_ops
    elif (width is None) != (height is None):
        raise ToolError("pass both width and height", code="BAD_ARGUMENT")
    if imp or auto_size:
        pass
    elif polygon:
        ops.append({"op": "outline", "polygon": polygon})
        ops.append({"op": "outline_prop", "set": {"src": "user"}, "unset": ["top", "bottom", "file", "mode"]})
    elif width is not None:
        if width < 5 or height < 5:
            raise ToolError("board must be at least 5×5 mm", code="BAD_ARGUMENT")
        org = origin or (snap0["outline"]["bbox"][:2] if snap0.get("outline") else [100.0, 100.0])
        ops.append({"op": "outline", "width": width, "height": height, "radius": radius or 0, "origin": org})
        ops.append({"op": "outline_prop", "set": {"src": "user"}, "unset": ["top", "bottom", "file", "mode"]})
    if layers:
        ops.append({"op": "layers", "count": layers})
    sch_holes = (size_info or {}).get("schematic_holes") or []
    if holes and holes != "none" and not imp and not auto_size:
        from ..pcb import holes as holesmod
        sch_holes = [f["ref"] for f in holesmod.schematic_holes(board.read(pr, what=["footprints"]).get("footprints") or [])]
    if sch_holes and holes and holes != "none":
        # the schematic's own mounting holes are the board's holes: nothing is added on top of them
        changed.append(f"no {holes} holes added: the schematic already has {len(sch_holes)} mounting hole(s) "
                       f"({', '.join(sch_holes[:8])}); pcb_place(auto=true) seats them at the corners")
    elif holes and holes != "none" and not imp and not auto_size:
        lib = board.fp_libs(pr).get("MountingHole")
        try:   # never reuse a schematic reference (e.g. MountingHole symbols H1..H4) for a board-only hole
            reserved = [c["ref"] for c in board.netlist(pr)["components"]] if pr.sch.exists() else []
        except ToolError:
            reserved = []
        ops.append({"op": "holes", "size": holes, "inset": hole_inset, "lib_path": lib,
                    "net": _resolve_net(pr, hole_net) if hole_net else None, "plated": bool(hole_net),
                    "reserved_refs": reserved})
    if silk_text:
        ops.append({"op": "text_size", "size": silk_text, "thickness": max(0.15, round(silk_text * 0.15, 3))})
    if rules is None and not snap0.get("outline") and (width is not None or imp or auto_size):
        rules = rules_mod.default_preset(pr)   # first-time setup gets the fab profile's rules ('jlc' by default)
    rules_res = None
    netclass_args = any(v is not None for v in (default_track, power_track, via)) or bool(power_nets)
    if rules and rules != "none":
        projmod.checkpoint(pr, "before-pcb-setup-rules")
        rules_res = rules_mod_apply(pr, rules, default_track, power_track, via, power_nets, silk_text or 1.0, while_open)
        changed.append(f"design rules '{rules}' + net classes Default {rules_res['netclasses']['Default']['track']} mm / "
                       f"Power {rules_res['netclasses']['Power']['track']} mm → {pr.pro.name}, {Path(rules_res['dru']).name}")
    elif netclass_args:
        projmod.checkpoint(pr, "before-pcb-setup-netclasses")
        nc = rules_mod.set_netclasses(pr, default_track=default_track, power_track=power_track,
                                      via=tuple(via) if via else None, extra_power=power_nets, while_open=while_open)
        rules_res = {"netclasses": {"Default": nc["Default"], "Power": dict(nc["Power"], patterns=len(nc["Power"]["patterns"]))}}
        changed.append(f"net classes Default {nc['Default']['track']} mm / Power {nc['Power']['track']} mm"
                       + (f", Power patterns + {', '.join(power_nets)}" if power_nets else "") + f" → {pr.pro.name}")
    res = None
    cp_id = projmod.checkpoint(pr, "before-pcb-setup") if imp else None
    if ops:
        res = board.run_ops(pr, ops, while_open=while_open, label="before-pcb-setup")
        for o, r in zip(ops, res["results"]):
            if o["op"] == "outline":
                ol = r["outline"] or {}
                changed.append(f"outline {ol.get('width')}×{ol.get('height')} mm at {ol.get('bbox', [None, None])[:2]}"
                               f" (replaced {r['edge_shapes_removed']} old edge shapes)")
            elif o["op"] == "holes":
                changed.append(f"{len(r['added'])} {o['size']} mounting holes" + (f" (replaced {', '.join(r['removed'])})" if r["removed"] else ""))
            elif o["op"] == "outline_shapes":
                ol = r["outline"] or {}
                changed.append(f"outline from {imp.rep['file']} [{imp.rep['mode']}]: {ol.get('width')}×{ol.get('height')} mm, "
                               f"{r['shapes']['lines']} lines / {r['shapes']['arcs']} arcs / {r['shapes']['circles']} circles, "
                               f"{len(imp.ol['cutouts'])} cutouts (replaced {r['edge_shapes_removed']} old edge shapes)")
                imp.written = {"size": [ol.get("width"), ol.get("height")], "area": r["area"], "shapes": r["shapes"]}
            elif o["op"] == "holes_multi" and (r["added"] or r["removed"]):
                changed.append(f"{len(r['added'])} mounting holes ({', '.join(sorted({a_['size'] for a_ in r['added']})) or 'none'})"
                               + (f" (replaced {', '.join(r['removed'])})" if r["removed"] else ""))
            elif o["op"] == "layers":
                changed.append(f"copper layers {r['from']} → {r['to']}")
            elif o["op"] == "text_size":
                changed.append(f"{r['texts_resized']} silkscreen references → {r['size']} mm")
    if not changed:
        raise ToolError("nothing to do", code="BAD_ARGUMENT",
                        fix="pass width+height (outline), holes, layers, rules, silk_text, or net-class settings "
                            "(power_nets, power_track, default_track, via)")
    if size_info:
        changed.insert(0, f"board size chosen for the parts: {size_info['width']:g}×{size_info['height']:g} mm "
                          f"(utilisation {size_info['utilisation']}, {len(size_info['tried'])} trial placements, {size_info['seconds']} s)"
                       + (f"; was {snap0['outline']['width']}×{snap0['outline']['height']} mm" if snap0.get("outline") else ""))
    d = board.drc(pr, parity=True)
    snap = board.read(pr, what=["footprints", "nets"])
    findings = _findings(d, snap)
    findings += _netclass_findings(pr, snap, power_nets)
    if imp:
        findings = imp.findings + setup_ext.after_outline(pr, snap0, cp_id) + findings
    if size_fail:
        findings.insert(0, size_fail)
    if rules_res is not None and power_nets:
        names = [n["name"] for n in snap.get("nets", [])]
        rules_res["power_nets_matched"] = {p: sum(rules_mod.pattern_matches(rules_mod.power_variants(p), names).values())
                                           for p in power_nets}
    data = {"changed": changed, "outline": _compact_outline(snap["outline"]) if snap.get("outline") else None,
            "copper_layers": snap.get("copper_layers"),
            "rules": rules_res, "revision": projmod.revision(pr.pcb), "verify": {"drc": _drc_brief(d)},
            "findings": findings}
    imgs = _render(pr, "pcb-setup") if render else []
    if imp:
        data["outline_import"] = imp.describe()
        if imp.rep.get("ambiguous"):        # an uncertain pick always comes with the picture of what was picked
            imgs = imp.preview(pr) + imgs
    if size_info:
        data["auto_size"] = {k: size_info[k] for k in ("width", "height", "utilisation", "seconds")}
    return Result(data, images=imgs, summary="pcb_setup: " + "; ".join(changed))


def rules_mod_apply(pr, preset, default_track, power_track, via, power_nets, silk, while_open):
    return rules_mod.apply(pr, preset=preset, default_track=default_track, power_track=power_track,
                           via=tuple(via) if via else None, extra_power=power_nets, silk_text=silk,
                           while_open=while_open)


_PATH_FN = re.compile(r"^(V?IN\d*|VCC|VBAT|BAT\+?|VOUT\d*|OUT\d*|SW\d*|LX\d*|PH|BST|VSYS|CHRG?_?IN|D\d?|S\d?|"
                      r"DRAIN\d?|SOURCE\d?)$", re.I)
_PWR_IC = re.compile(r"(TP40\d\d|MCP738\d\d|BQ2\d|CN30\d\d|IP5\d\d\d|DW01|FS8205|8205|MT36\d\d|SX13\d\d|"
                     r"MP\d{4}|LM\d{4}|TPS\d|AO\d{4}|SI\d{4}|DMG\d|IRF|MOSFET|MOS)", re.I)


def _netclass_findings(pr, snap: dict, power_nets) -> list[dict]:
    """Power patterns that match no net, and power-path nets that are still in the Default class."""
    out = []
    nets = {n["name"]: n for n in snap.get("nets", [])}
    if power_nets:
        dead = [p for p in power_nets
                if not any(rules_mod.pattern_matches(rules_mod.power_variants(p), nets).values())]
        if dead:
            out.append({"severity": "warning", "code": "POWER_PATTERN_UNMATCHED",
                        "message": f"power_nets {dead} match no board net (patterns are matched against full names "
                                   f"such as '/VBAT')",
                        "fix": f"use names from pcb_read(what=['nets']) — e.g. {', '.join(sorted(nets)[:8])}"})
    try:
        geo = board.read(pr, what=["geom"])
    except ToolError:
        return out
    suspects = {}
    for fp in geo.get("footprints", []):
        pre = re.sub(r"\d.*$", "", fp["ref"]).upper()
        text = f"{fp.get('value', '')} {fp.get('footprint', '')}"
        pads = [p for p in fp.get("pad_list", []) if p.get("net")]
        if pre == "L" and len(pads) == 2 and "ferrite" not in text.lower():
            why = f"inductor {fp['ref']}"
            for p in pads:
                suspects.setdefault(p["net"], why)
        elif pre in ("U", "IC", "Q") and (placer.REG_RE.search(text) or _PWR_IC.search(text)):
            for p in pads:
                if _PATH_FN.match((p.get("fn") or "").strip()):
                    suspects.setdefault(p["net"], f"{fp['ref']}.{p.get('fn')}")
        elif pre == "BT":
            for p in pads:
                suspects.setdefault(p["net"], f"battery {fp['ref']}")
    bad = {n: why for n, why in suspects.items()
           if n in nets and nets[n].get("class") in ("Default", "") and not placer.is_ground(n)
           and not n.startswith("unconnected-") and nets[n].get("pads", 0) >= 2}
    if bad:
        names = sorted(bad)[:10]
        out.append({"severity": "warning", "code": "POWER_PATH_DEFAULT_CLASS",
                    "message": "power-path nets still in the Default (signal-width) class: "
                               + ", ".join(f"{n} ({bad[n]})" for n in names),
                    "fix": f"pcb_setup(power_nets={[n.rsplit('/', 1)[-1] for n in names]}) "
                           "— skip any that are low-current sense/feedback nets"})
    return out


# ── pcb_place ─────────────────────────────────────────────────────────────
@tool("pcb_place",
      """Place footprints. Two modes, combinable (explicit moves are applied after auto placement):

auto=true — connectivity-driven auto placement inside the outline: connectors on the edges facing out (power input
first), ESD arrays at the connector they protect, RF modules with the antenna on a board edge (plus copper keepouts
under the antenna), the main MCU/IC central, decoupling caps hugging the IC power pin they serve (the IC drawn
nearest in the schematic; every IC gets one before any gets a second), crystal + load caps at the MCU's oscillator
pins, pull-ups/series parts at their pin, regulators near their input, everything else by wirelength;
courtyard-legal (no overlaps, inside the edge clearance, around holes), on a 0.5 mm grid. Locked footprints and
keep=[...] stay put; refs=[...] auto-places only those parts around the existing parts and tracks. Every part
the placement left alone is listed in `skipped` with the reason (locked, keep, bottom side, no legal spot) and a
fix; moves=[{"ref":"U2","unlock":true}] in the same call unlocks a part first so this placement places it.
Several structured candidates are placed and scored for elegance (even use of the board, connectors on the edge,
decoupling caps within 4 mm of their pin, tidy rows); the best is written and `elegance` reports its score.
Side-entry connectors (USB, JST-SH ...) sit with the body flush with the board edge; overhang={"J1": 1.2} lets one
stick out (clamped so pads and holes keep the copper-to-edge clearance). A board with no outline at all is sized
for its parts first (as pcb_setup(auto_size=true)).
On a board that already has tracks, auto placement of everything is refused (moved parts would leave tracks
shorting other nets): pass refs=[new parts], or clear_tracks=true to rip up all tracks/vias first.
Returns the ratsnest length before/after (lower is better) and the role assigned to each part.

moves=[{ref, x, y, rot, side, lock, unlock}] — exact positions in mm (board coordinates, same as pcb_read), rot in
degrees, side 'top'|'bottom', lock=true to pin a part so later auto placement keeps it (lock=false unpins it). A move
that puts a pad on another net's pad or track is refused (force=true overrides); tracks a move leaves dangling are
reported with their uuids. Moving a LOCKED part is refused (LOCKED, nothing written) unless that move has
unlock=true (add lock=true to lock it again at the new spot). {"ref":"U2","lock":true} / {"ref":"U2","unlock":true}
alone lock / unlock a part without moving it (pcb_edit has lock / unlock ops for many parts at once).
Examples: pcb_place(auto=true) · pcb_place(moves=[{"ref":"J1","x":101,"y":115,"rot":90}]) ·
pcb_place(auto=true, keep=["J1","U1"], spacing=1.0) · pcb_place(auto=true, moves=[{"ref":"U2","unlock":true}]) ·
pcb_place(moves=[{"ref":"J1","x":101,"y":115,"unlock":true,"lock":true}])""",
      {"project": PROJECT,
       "auto": {"type": "boolean", "description": "Run automatic placement"},
       "moves": {"type": "array", "items": {"type": "object"}, "description": "[{ref,x,y,rot,side,lock,unlock,force}]"},
       "refs": {"type": "array", "items": {"type": "string"}, "description": "Auto-place only these refs"},
       "keep": {"type": "array", "items": {"type": "string"}, "description": "Refs auto placement must not move"},
       "spacing": {"type": "number", "description": "Courtyard gap mm between parts (default: adaptive 0.3–1.5 by board fill)"},
       "edge_clearance": {"type": "number", "description": "Courtyard distance from the board edge mm (default 0.5)"},
       "clear_tracks": {"type": "boolean", "description": "auto=true on a routed board: delete all tracks/vias first"},
       "dry_run": {"type": "boolean", "description": "Compute and report the auto placement without writing"},
       "tidy_silk": {"type": "boolean", "description": "Re-seat moved parts' reference designators clear of pads/parts (default true)"},
       "candidates": {"type": "integer", "description": "Max placement candidates to try and score (default 6; 1 = one pass)"},
       "overhang": {"type": "object", "description": "{ref: mm} a side-entry connector may stick out past the edge (default 0 = flush)"},
       "elegance": {"type": "boolean", "description": "Report the elegance score (default true; false saves about 1 s)"},
       "while_open": WHILE_OPEN, "render": RENDER},
      destructive=False, group="pcb", title="Place footprints")
def pcb_place(project=None, auto=False, moves=None, refs=None, keep=None, spacing=None, edge_clearance=0.5,
              clear_tracks=False, dry_run=False, tidy_silk=True, while_open="refuse", render=True, candidates=6,
              overhang=None, elegance=True):
    pr = projmod.resolve(project)
    if not auto and not moves:
        raise ToolError("pass auto=true and/or moves=[...]", code="BAD_ARGUMENT")
    if not isinstance(moves or [], list) or not all(isinstance(m, dict) for m in moves or []):
        raise ToolError("moves must be a list of objects", code="BAD_ARGUMENT", fix='moves=[{"ref":"R1","x":110,"y":105}]')
    snap = board.read(pr, what=["geom"])
    # auto placement is computed from this snapshot before anything is written, so a part that this call
    # unlocks (a move item with unlock=true and no position of its own) must already be free in it
    unlock = []
    if auto:
        unlock = list(dict.fromkeys(m["ref"] for m in moves or [] if m.get("ref") and m.get("unlock")
                                    and not any(m.get(k) is not None for k in ("x", "y", "rot", "side"))))
        have = {f["ref"] for f in snap["footprints"]}
        gone = [r for r in unlock if r not in have]
        if gone:
            raise ToolError(f"no footprint {', '.join(gone)} on the board", code="NOT_FOUND",
                            fix="pcb_read(what=['footprints']) lists the footprints")
    _unlock_in_snapshot(snap, unlock)
    _attach_sch_positions(pr, snap)
    before_score = placer.score(snap)
    items, auto_res = [], None
    pre_ops = []
    sized = None
    if auto:
        from ..pcb import elegant, setup_ext
        if not snap.get("outline"):
            # no Edge.Cuts at all: size the board for its parts (an open outline is an error, never replaced)
            why = None if not dry_run else "dry run"
            why = why or setup_ext.auto_size_refusal(snap)
            sops, sized = (None, {"failed": why}) if why else setup_ext.size_ops(pr, snap)
            if sops is None:
                raise ToolError("the board has no outline to place into"
                                + (f" and none could be chosen ({sized['failed']})" if sized.get("failed") else ""),
                                code="NO_OUTLINE", fix="pcb_setup(width=..., height=...) or pcb_setup(auto_size=true) first")
            board.run_ops(pr, sops, while_open=while_open, label="before-auto-size")
            snap = board.read(pr, what=["geom"])
            _unlock_in_snapshot(snap, unlock)
            _attach_sch_positions(pr, snap)
            before_score = placer.score(snap)
        if (snap.get("tracks") or snap.get("vias")) and not refs and not dry_run:
            if not clear_tracks:
                raise ToolError(f"the board already has {snap.get('tracks')} tracks and {snap.get('vias')} vias: "
                                "auto-placing every part would leave them shorting other nets. Nothing was moved.",
                                code="TRACKS_EXIST",
                                fix="pcb_place(auto=true, refs=[<new parts>]) to place only new parts around the routing, "
                                    "or pcb_place(auto=true, clear_tracks=true) to rip up all tracks/vias, then pcb_route()")
            pre_ops.append({"op": "delete", "all_tracks": True})
            snap["track_segs"] = []
        try:
            auto_res = elegant.auto_place(snap, refs=refs, keep=keep, spacing=spacing, edge_clearance=edge_clearance,
                                          candidates=max(1, int(candidates or 1)), overhang=overhang,
                                          rules=setup_ext.edge_rules(pr))
        except ValueError as e:
            raise ToolError(str(e), code="PLACE_FAILED")
        items += auto_res["moves"]
    for m in moves or []:
        if "ref" not in m:
            raise ToolError("each move needs a ref", code="BAD_ARGUMENT")
        if (m.get("x") is None) != (m.get("y") is None):
            raise ToolError(f"move for {m['ref']} needs both x and y (mm)", code="BAD_ARGUMENT",
                            fix='e.g. {"ref":"R1","x":110,"y":105} — pcb_read(refs=[...]) shows current positions')
        if not any(m.get(k) is not None for k in ("x", "rot", "side", "lock")) and not m.get("unlock"):
            raise ToolError(f"move for {m['ref']} changes nothing", code="BAD_ARGUMENT",
                            fix="pass x+y, rot, side, lock and/or unlock")
        items.append(dict({k: m[k] for k in MOVE_KEYS if k in m}, check=True))
    skipped = []
    if auto_res:
        skipped = placer.auto_skipped(snap, [m["ref"] for m in auto_res["moves"]], refs=refs, keep=keep,
                                      unplaced=auto_res["unplaced"])
    if dry_run:
        return Result({"dry_run": True, "moves": items, "auto": auto_res and {k: v for k, v in auto_res.items() if k != "moves"},
                       "skipped": skipped, "score_before": before_score},
                      summary=f"dry run: {len(items)} moves, ratsnest {before_score} → {auto_res and auto_res['score_after']} mm")
    # placed=auto in the board's COPPER_OUTLINE property marks a layout that is entirely Copper's own
    all_auto = bool(auto_res) and not refs and not keep and not moves and not auto_res["unplaced"]
    if unlock:
        pre_ops.insert(0, {"op": "lock", "locked": False, "refs": unlock})
    ops = pre_ops + [{"op": "place", "items": items}]
    if auto_res and (auto_res.get("notes") or {}).get("flush"):
        # shell tabs right at the edge would get starved thermal reliefs from a later pour
        ops.append({"op": "edge_pads_solid", "refs": auto_res["notes"]["flush"]})
    if all_auto:
        ops.append({"op": "outline_prop", "set": {"placed": "auto"}})
    elif moves:
        ops.append({"op": "outline_prop", "unset": ["placed"]})
    if tidy_silk:
        ops.append({"op": "silk_tidy2", "refs": None if all_auto else [i["ref"] for i in items], "hide": "if_auto"})
    res = board.run_ops(pr, ops, while_open=while_open, label="before-pcb-place")
    placed = res["results"][len(pre_ops)]["placed"]
    silk = res["results"][-1] if tidy_silk else {}
    after = board.read(pr, what=["geom"])
    kos = placer.antenna_keepouts(after)
    if kos or any(z.get("rule_area") for z in after.get("zones") or []):
        board.run_ops(pr, [{"op": "keepout", "areas": kos}], while_open=while_open, label="antenna-keepout")
    after_score = placer.score(after)
    ov = placer.overlaps(after)
    d = board.drc(pr, parity=True)
    findings = _findings(d, after)
    if ov:
        findings.insert(0, {"severity": "warning", "code": "COURTYARD_OVERLAP",
                            "message": f"overlapping courtyards: {', '.join('/'.join(p) for p in ov[:10])}",
                            "fix": "pcb_place(auto=true) or move one of each pair"})
    dang = {p["ref"]: p["dangling_tracks"] for p in placed if p.get("dangling_tracks")}
    if dang:
        uu = [u for v in dang.values() for u in v]
        findings.insert(0, {"severity": "warning", "code": "TRACKS_DANGLING",
                            "message": f"{len(uu)} tracks/vias no longer reach the pads of {', '.join(dang)} after the move",
                            "fix": f"pcb_edit(ops=[{{op:'delete', uuids:{uu[:20]}}}]) then pcb_route()"})
    if any(o["op"] == "delete" for o in pre_ops):
        findings.insert(0, {"severity": "info", "code": "TRACKS_CLEARED",
                            "message": "all tracks and vias were deleted before placement (clear_tracks=true)",
                            "fix": "pcb_route() to route the new placement"})
    if silk.get("hidden"):
        findings.append({"severity": "info", "code": "SILK_HIDDEN",
                         "message": f"no readable spot for the reference of {', '.join(silk['hidden'][:12])}: hidden on the "
                                    "silkscreen (still on the fab layer)",
                         "fix": "pcb_place(auto=true, spacing=1.0) for more room"})
    if silk.get("crowded"):
        findings.append({"severity": "info", "code": "SILK_CROWDED",
                         "message": f"no clean spot for the reference of {', '.join(silk['crowded'][:12])}"
                                    + (f" (shrunk to {silk.get('min_size')} mm)" if silk.get("shrunk") else ""),
                         "fix": "pcb_place(auto=true, spacing=1.0) for more room, or pcb_edit to hide/move those references"})
    if auto_res and auto_res["unplaced"]:
        findings.insert(0, {"severity": "error", "code": "DID_NOT_FIT",
                            "message": f"no legal spot for {', '.join(auto_res['unplaced'])} (left where they were)",
                            "fix": "pcb_setup(width=..., height=...) for a bigger board, pcb_place(auto=true, spacing=0.3), or layers=4"})
    if auto_res and after_score > before_score * 1.02:
        findings.insert(0, {"severity": "warning", "code": "PLACEMENT_WORSE",
                            "message": f"auto placement increased the ratsnest ({before_score} → {after_score} mm) — "
                                       "the previous layout was better connected",
                            "fix": "checkpoint(action='restore') to undo, or pcb_place(auto=true, keep=[...]) to pin the good parts"})
    moved = [p for p in placed if "after" in p and (abs(p["after"]["x"] - p["before"]["x"]) > 1e-3 or
                                                  abs(p["after"]["y"] - p["before"]["y"]) > 1e-3 or
                                                  p["after"]["rot"] != p["before"]["rot"] or p["after"]["side"] != p["before"]["side"])]
    mv = [f"{p['ref']} → ({p['after']['x']}, {p['after']['y']}) rot {p['after']['rot'] % 360:g} {p['after']['side']}"
          for p in moved]
    if len(mv) > 40:
        mv = mv[:40] + [f"… and {len(mv) - 40} more"]
    # every part that was asked for and not placed: the placer's own list plus anything the worker refused
    skipped += [p for p in placed if "skipped" in p and p["ref"] not in {s_["ref"] for s_ in skipped}]
    lock_changes = {}
    for o, r in zip(ops, res["results"]):
        if o["op"] == "lock" and r["changed"]:
            lock_changes["locked" if r["locked"] else "unlocked"] = list(r["changed"])
    for p in placed:
        if "locked" in p:
            lock_changes.setdefault("locked" if p["locked"] else "unlocked", []).append(p["ref"])
    for k, v in lock_changes.items():
        mv.append(f"{k} {', '.join(v[:40])}")
    loud = [s_ for s_ in skipped if s_["skipped"] in ("locked", "not on the board") or s_["skipped"].startswith("on the bottom")]
    if loud:
        lk = [s_["ref"] for s_ in loud if s_["skipped"] == "locked"]
        un = ", ".join(f'{{"ref":"{r}","unlock":true}}' for r in lk[:12])
        findings.insert(0, {"severity": "warning" if any(s_["ref"] in set(refs or []) for s_ in loud) else "info",
                            "code": "PARTS_SKIPPED",
                            "message": "auto placement left these parts where they were: "
                                       + ", ".join(f"{s_['ref']} ({s_['skipped'].split(' (')[0]})" for s_ in loud[:20])
                                       + (f" … and {len(loud) - 20} more" if len(loud) > 20 else ""),
                            "fix": (f"pcb_place(auto=true, {'refs=' + str(lk[:12]) + ', ' if refs else ''}moves=[{un}]) "
                                    "to unlock and place the locked ones" if lk else loud[0].get("fix", "see `skipped`"))})
    data = {"changed": mv,
            "skipped": skipped,
            "score": {"ratsnest_mm_before": before_score, "ratsnest_mm_after": after_score},
            "revision": res["revision"], "verify": {"drc": _drc_brief(d), "courtyard_overlaps": len(ov)},
            "findings": findings}
    if auto_res:
        data["auto"] = {"spacing": auto_res["spacing"], "fill_ratio": auto_res["fill_ratio"], "roles": auto_res["roles"]}
        notes = auto_res.get("notes") or {}
        if len(auto_res.get("tried") or []) > 1:
            data["auto"]["variant"] = auto_res["variant"]
            data["auto"]["tried"] = auto_res["tried"]
        if notes.get("flush"):
            data["auto"]["flush_with_edge"] = notes["flush"]
            n_silk = (d.get("by_type") or {}).get("warning:silk_edge_clearance", 0)
            if n_silk:   # the connector's own silk now ends at / past the edge: not printed, not a defect
                data["expected_warnings"] = {"silk_edge_clearance": n_silk, "because": f"{', '.join(notes['flush'])} flush with the edge"}
        for ref, mm_ in (notes.get("clamped") or {}).items():
            findings.append({"severity": "info", "code": "OVERHANG_CLAMPED",
                             "message": f"{ref} was kept {mm_} mm further in than asked so its pads / holes keep the "
                                        "copper-to-edge clearance"})
        if notes.get("decap_far"):
            findings.append({"severity": "warning", "code": "DECAP_FAR",
                             "message": "decoupling caps further than 4 mm from the pin they serve: "
                                        + ", ".join(f"{c} {dist} mm from {pin}" for c, pin, dist in notes["decap_far"][:8]),
                             "fix": "pcb_place(auto=true, spacing=0.3) or a larger board; or move them by hand "
                                    "(pcb_place(moves=[...]))"})
    if sized:
        data["changed"].insert(0, f"the board had no outline: sized {sized['width']:g}×{sized['height']:g} mm for its parts")
        data["auto_size"] = {k: sized[k] for k in ("width", "height", "utilisation")}
    if elegance:
        from ..review import elegance as _el
        e_ = _el.safe_brief(pr, d, 0)
        if e_:
            data["elegance"] = e_
    if lock_changes:
        data["lock"] = lock_changes
    summ = (f"pcb_place: moved {len(moved)} footprints; "
            + "".join(f"{k} {len(v)}; " for k, v in lock_changes.items())
            + (f"skipped {len(loud)} ({', '.join(s_['ref'] for s_ in loud[:8])}); " if loud else "")
            + f"ratsnest {before_score} → {after_score} mm; "
            f"{len(ov)} courtyard overlaps; DRC errors {d['errors']}")
    return Result(data, images=_render(pr, "pcb-place") if render else [], summary=summ)


MOVE_KEYS = ("ref", "x", "y", "rot", "side", "lock", "unlock", "force")


def _ref_list(v, name) -> list:
    """refs of a lock / unlock op → list of refs ('*' = all). A bare string is one ref."""
    if v is None or v is False:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)) or not all(isinstance(r, str) and r for r in v):
        raise ToolError(f"{name} must be a list of references", code="BAD_ARGUMENT",
                        fix=f'e.g. {name}=["U1","J2"], or ["*"] for every part')
    return list(dict.fromkeys(v))


def _unlock_in_snapshot(snap: dict, unlock) -> list:
    """Mark the footprints this call unlocks as free in a board snapshot (auto placement is computed from
    the snapshot before the unlock is written). Returns the refs that were locked."""
    out = []
    for f in snap["footprints"] if unlock else []:
        if f.get("locked") and f["ref"] in unlock:
            f["locked"] = False
            out.append(f["ref"])
    return out


def _attach_sch_positions(pr, snap: dict):
    """Give every footprint its schematic symbol position as fp['sch'] = [sheet key, x, y], so the
    placer can tell which IC a decoupling cap was drawn next to. Best effort (read-only)."""
    try:
        from .. import sexpr
        pos = {}
        for f in sorted(Path(pr.dir).glob("*.kicad_sch")):
            tree = sexpr.loads(f.read_text(encoding="utf-8"))
            for sym in sexpr.find_all(tree, "symbol"):
                at = sexpr.find(sym, "at")
                if not at or len(at) < 3:
                    continue
                inst = sexpr.find(sym, "instances")
                refs = []
                for proj in sexpr.find_all(inst, "project") if inst else []:
                    for pth in sexpr.find_all(proj, "path"):
                        r = sexpr.value(pth, "reference")
                        if r:
                            refs.append((str(r), str(pth[1])))
                if not refs:
                    for prop in sexpr.find_all(sym, "property"):
                        if len(prop) > 2 and prop[1] == "Reference":
                            refs.append((str(prop[2]), ""))
                for r, path in refs:
                    pos.setdefault(r, [f"{f.name}|{path}", float(at[1]), float(at[2])])
        for fp in snap.get("footprints", []):
            if fp["ref"] in pos:
                fp["sch"] = pos[fp["ref"]]
    except Exception:  # noqa: BLE001
        pass


# ── pcb_route ─────────────────────────────────────────────────────────────
@tool("pcb_route",
      """Route the board.

mode='auto' (default when no tracks given): autoroute every unrouted connection with Freerouting (pinned version,
headless). Java + the router are downloaded on first use into $COPPER_HOME/tools (default ~/.copper/tools, ~120 MB,
one time; the result's `downloaded` says so). Existing tracks and vias are kept exactly as they are (exported as fixed),
so on a hand-routed board only the missing connections are added. The DSN is made DRC-consistent: items of
different net classes keep the largest class clearance (as KiCad DRC does), and power nets whose class width cannot
leave a fine-pitch pad (0.4 mm QFN) are routed at the widest legal width (`necked_nets`). Connections Freerouting
cannot finish are handed to Copper's guided router (`guided_finish`: how many it routed and why the rest failed;
finish=false skips this); its result is only kept when it routes more without adding DRC errors. If the result has
more DRC errors and no fewer unrouted connections than before, the board is restored (`reverted`).
Typical 10–30 part board: 10–90 s; a dense 4-layer board with a 0.4 mm-pitch QFN: 4–7 min and usually some
connections left at the QFN (what is left is a pad-escape problem, not a lack of layers — opening an inner layer
does not finish them; move the decoupling caps off the pad rows or finish by hand / pcb_route_guided).

2-layer flow: pcb_route(ignore_nets=["GND"]) → pcb_pour(net="GND", stitch=true).
4-layer flow: pcb_pour(net="GND", layers=["In1.Cu"]) and pcb_pour(net="+3V3", layers=["In2.Cu"]) FIRST, then
pcb_route() — plane layers are reserved (no traces) and plane nets reach their plane through vias; then optionally
pcb_pour(net="GND", stitch=true) on the outer layers. On a 4-layer board both inner layers are reserved by default;
signal_layers=["F.Cu","In2.Cu","B.Cu"] (any subset) overrides that.
clear_existing=true rips up all tracks/vias first (also when nothing is unrouted, e.g. after changing net classes).
Returns unrouted before/after and `unrouted_nets` {net: count}.
After routing, the tracks this call created are polished (collinear segments merged, 90 degree corners chamfered to
two 45s, pad entries centred) and unreadable reference designators are re-seated; tracks that existed before the
call and locked items are never touched, and the polish is undone if it adds a DRC error or an unrouted connection
(polish=false skips it). `elegance` = the 0-100 layout score with its top hints.
mode='polish' runs the same clean-up on every unlocked track (nets=[...] limits it) without routing anything.

tracks=[...] — explicit routing, one entry per connection:
  {"from":"U1.3","to":"C3.1","width":0.3,"layer":"F.Cu","style":"45"|"L"|"L2"|"direct","waypoints":[[x,y]],"via_at":[x,y]}
  Pads on different layers get a via automatically. Refuses to join pads on different nets, and checks the path against
  other nets' copper: if every style collides nothing is drawn (ROUTE_BLOCKED names the obstacles; add waypoints).
Examples: pcb_route() · pcb_route(ignore_nets=["GND"]) · pcb_route(tracks=[{"from":"J1.1","to":"U1.3","width":0.5}])""",
      {"project": PROJECT,
       "mode": {"type": "string", "enum": ["auto", "tracks", "polish"]},
       "tracks": {"type": "array", "items": {"type": "object"}},
       "passes": {"type": "integer", "description": "Freerouting max passes (default 30)"},
       "timeout": {"type": "integer", "description": "Seconds before the router is stopped (default 600, whole call)"},
       "ignore_nets": {"type": "array", "items": {"type": "string"}, "description": "Nets the autorouter must skip"},
       "clear_existing": {"type": "boolean", "description": "Delete existing tracks/vias before autorouting"},
       "signal_layers": {"type": "array", "items": {"type": "string"},
                         "description": "Copper layers the autorouter may put traces on (default: all but plane layers; "
                                        "outer layers only on a 4-layer board)"},
       "keep_worse": {"type": "boolean", "description": "Keep the routed result even if it adds DRC errors without "
                                                         "reducing unrouted connections (default false: restore)"},
       "finish": {"type": "boolean", "description": "Hand what Freerouting leaves to the guided router (default true)"},
       "polish": {"type": "boolean", "description": "Tidy the new tracks and unreadable references afterwards (default true)"},
       "nets": {"type": "array", "items": {"type": "string"}, "description": "mode='polish': only these nets"},
       "while_open": WHILE_OPEN, "render": RENDER},
      destructive=True, open_world=True, group="pcb", title="Route (Freerouting / explicit)")
def pcb_route(project=None, mode=None, tracks=None, passes=30, timeout=600, ignore_nets=None, clear_existing=False,
              signal_layers=None, keep_worse=False, finish=True, while_open="refuse", render=True, _progress=None,
              polish=True, nets=None):
    pr = projmod.resolve(project)
    mode = mode or ("tracks" if tracks else "auto")
    if mode == "polish":
        from ..pcb import setup_ext
        from ..review import elegance as _el
        if nets:
            names_ = {n["name"] for n in board.read(pr, what=["nets"]).get("nets", [])}
            nets = [_resolve_net(pr, n, names_) for n in nets]
        pol = setup_ext.polish(pr, all_tracks=True, nets=nets, while_open=while_open, silk_hide="if_auto")
        d = pol.pop("drc")
        changed = [] if pol.get("reverted") else \
            [f"{k.replace('_', ' ')}: {v}" for k, v in pol.items() if k in ("merged", "chamfered", "pad_entries", "duplicate_vias") and v] + \
            ([f"re-seated references: {', '.join(pol['silk']['moved'][:20])}"] if (pol.get("silk") or {}).get("moved") else [])
        findings = _findings(d)
        if pol.get("reverted"):
            findings.insert(0, {"severity": "warning", "code": "POLISH_REVERTED", "message": pol["reverted"]})
        data = {"changed": changed, "polish": {k: v for k, v in pol.items() if k != "checkpoint"},
                "verify": {"drc": _drc_brief(d)}, "findings": findings, "elegance": _el.safe_brief(pr, d, 3)}
        return Result(data, images=_render(pr, "pcb-polish") if render else [],
                      summary=f"pcb_route polish: {'; '.join(changed) or 'nothing to change'}; DRC errors {d['errors']}, "
                              f"unrouted {d['unconnected']}")
    if mode == "tracks":
        if not tracks:
            raise ToolError("mode='tracks' needs tracks=[{from, to, ...}]", code="BAD_ARGUMENT")
        before = board.drc(pr, parity=False)
        ops = [dict(t, op="route") for t in tracks]
        res = board.run_ops(pr, ops, while_open=while_open, label="before-pcb-route")
        d = board.drc(pr, parity=True)
        changed = [f"{t['from']} → {t['to']}: {r['segments']} segments" + (f" + {r['vias']} via" if r["vias"] else "")
                   + f" on {r['net'] or 'no net'} ({r['width']} mm, {r.get('style')})"
                   for t, r in zip(tracks, res["results"])]
        findings = _findings(d)
        if d["errors"] > before["errors"]:
            findings.insert(0, {"severity": "error", "code": "ROUTE_ADDED_DRC_ERRORS",
                                "message": f"DRC errors {before['errors']} → {d['errors']} after drawing these tracks",
                                "fix": "checkpoint(action='restore') to undo, or pcb_edit delete the new uuids"})
        data = {"changed": changed, "unrouted_before": before.get("unconnected"),
                "unrouted_after": d["unconnected"], "revision": res["revision"], "verify": {"drc": _drc_brief(d)},
                "findings": findings, "uuids": [u for r in res["results"] for u in r["uuids"]]}
        return Result(data, images=_render(pr, "pcb-route") if render else [],
                      summary=f"pcb_route: {len(tracks)} connections drawn; unrouted {before.get('unconnected')} → "
                              f"{d['unconnected']}; DRC errors {before['errors']} → {d['errors']}")
    from ..pcb import route
    names = None
    if ignore_nets:
        names = {n["name"] for n in board.read(pr, what=["nets"]).get("nets", [])}
        ignore_nets = [_resolve_net(pr, n, names) for n in ignore_nets]
    try:
        rules_mod.normalize_project(pr, while_open)
    except ToolError:
        pass
    uu0 = None
    if polish:
        from ..pcb import setup_ext
        uu0 = setup_ext.track_uuids(pr)
    r = route.autoroute(pr, passes=passes, timeout=timeout, clear_existing=clear_existing, ignore_nets=ignore_nets,
                        while_open=while_open, progress=_progress, signal_layers=signal_layers, keep_worse=keep_worse,
                        finish=finish)
    d = r.pop("drc")
    if polish and not r.get("nothing_to_route") and not r.get("reverted"):
        try:
            pol = setup_ext.polish(pr, before_uuids=uu0, drc_before=d, while_open=while_open, silk_hide="if_auto")
            d = pol.pop("drc", d)
            r["polish"] = {k: v for k, v in pol.items() if k != "checkpoint"}
        except ToolError as e:       # the routed board stands; say why the clean-up did not run
            r["polish"] = {"skipped": str(e)[:200]}
    if r.get("nothing_to_route"):
        return Result(dict(r, changed=[], verify={"drc": _drc_brief(d)}, findings=_findings(d)),
                      summary="pcb_route: nothing to route (0 unrouted connections); board unchanged")
    r = {k: v for k, v in r.items() if v is not None}
    data = dict(r, changed=[] if r.get("reverted") else
                [f"autorouted: {r['tracks_added']:+d} track segments, {r['vias_added']:+d} vias "
                 f"({r.get('track_length_mm')} mm of track on the board)"]
                + ([f"removed {r['duplicates_removed']} duplicate segments already on the board"]
                   if r.get("duplicates_removed") else [])
                + ([f"guided router finished {r['guided_finish']['routed']} connections Freerouting left"]
                   if r.get("guided_finish") else []),
                verify={"drc": _drc_brief(d)}, findings=_findings(d))
    if r.get("reverted"):
        data["findings"].insert(0, {"severity": "warning", "code": "ROUTE_REVERTED", "message": r["reverted"],
                                    "fix": "route the rest by hand (pcb_route(tracks=[...])), or pcb_route(clear_existing=true) "
                                           "for a fresh full route; keep_worse=true keeps such a result"})
    if r.get("downloaded"):
        data["findings"].insert(0, {"severity": "info", "code": "ROUTER_DOWNLOADED",
                                    "message": "first use: downloaded " + "; ".join(r["downloaded"])})
    by_net = r.pop("unrouted_by_net", None)
    data.pop("unrouted_by_net", None)
    if d["unconnected"]:
        pads_by_net = {}
        for u in d.get("unconnected_top", []):
            for it in u.get("items") or []:
                m = re.search(r"Pad (\S+) \[([^\]]+)\] of (\S+)", it or "")
                if m:
                    pads_by_net.setdefault(m.group(2), []).append(f"{m.group(3)}.{m.group(1)}")
        if by_net is None:   # fall back to the nets named in KiCad's (truncated) DRC list
            by_net = {}
            for u in d.get("unconnected_top", []):
                for it in (u.get("items") or [])[:1]:
                    m = re.search(r"\[([^\]]+)\]", it or "")
                    if m:
                        by_net[m.group(1)] = by_net.get(m.group(1), 0) + 1
        data["unrouted_nets"] = dict(sorted(by_net.items(), key=lambda kv: -kv[1]))
        data["findings"] = [f for f in data["findings"] if f["code"] != "UNROUTED"]
        pour = [n for n in by_net if n in (ignore_nets or []) or placer.is_ground(n)]
        rest = [n for n in by_net if n not in pour]
        if pour:
            data["findings"].append({"severity": "info", "code": "LEFT_FOR_POUR",
                                     "message": f"{sum(by_net[n] for n in pour)} connections on {', '.join(pour)} "
                                                "are meant to be joined by a copper pour",
                                     "fix": f"pcb_pour(net='{pour[0]}', stitch=true) — stitching adds a via to every "
                                            "isolated pour fragment that holds a pad; do not autoroute pour nets again"})
        if rest:
            ex = next((pads_by_net[n][:2] for n in rest if len(pads_by_net.get(n, [])) >= 2), None)
            stubs = [uid for u in d.get("unconnected_top", []) for it, uid in zip(u.get("items") or [], u.get("uuids") or [])
                     if _COPPER_ITEM.match(it or "")]
            if ex:
                hint = f"pcb_route(tracks=[{{from:'{ex[0]}', to:'{ex[1]}'}}])"
            elif stubs:
                hint = (f"a routed track stops short of its pad: pcb_edit(ops=[{{op:'delete', uuids:{stubs[:6]}}}]) "
                        "then pcb_route()")
            else:
                hint = "pcb_route(tracks=[{from:'REF.PAD', to:'REF.PAD'}]) (pads from verify's unconnected list)"
            fix = f"{hint} for those connections, or pcb_place(auto=true, spacing=1.0, clear_tracks=true) then pcb_route()"
            why = [f for f in (r.get("guided_finish") or {}).get("failed") or [] if f.get("net") in rest]
            if why:
                fix += ("; guided_finish.failed gives the reason and position per connection — "
                        "pcb_route_guided(ripup='nets', nets=[<net>, <nets named in its reason>]) re-routes just those")
            necked = [n for n in (r.get("necked_nets") or {}) if n in rest]
            if necked:
                fix += f"; {', '.join(necked)} are routed at the fine-pitch width — give them a plane " \
                       "(pcb_pour on an inner layer) on dense boards"
            data["findings"].append({"severity": "warning", "code": "UNROUTED",
                                     "message": f"the autorouter could not finish {sum(by_net[n] for n in rest)} "
                                                f"connections: {', '.join(f'{n}×{by_net[n]}' for n in rest[:12])}",
                                     "fix": fix})
    from ..review import elegance as _el
    e_ = _el.safe_brief(pr, d, 3)
    if e_:
        data["elegance"] = e_
    summ = (f"pcb_route: unrouted {r['unrouted_before']} → {r['unrouted_after']} in {r['seconds']} s; "
            f"DRC errors {r.get('drc_errors_before')} → {d['errors']}"
            + (" — REVERTED (result was worse)" if r.get("reverted") else "") + f" (checkpoint {r['checkpoint']})")
    return Result(data, images=_render(pr, "pcb-route") if render else [], summary=summ)


# ── pcb_pour ──────────────────────────────────────────────────────────────
@tool("pcb_pour",
      """Copper pour: a zone for one net (default GND) covering the whole board outline on the chosen layers, with
clearance, thermal reliefs and optional stitching vias; then fills every zone and re-runs DRC.
After the fill every filled fragment that holds a pad of the net is made to join: a pad no thermal spoke could
reach (slotted / small-annulus pads) gets a solid connection, and an isolated fragment (e.g. an IC ground pad boxed
in by signal tracks) gets a via into the net's copper on another layer; fragments where no via fits are reported
(POUR_ISLAND) with their position. A pour on an inner layer of a 4-layer board is a plane: pcb_route() then
reserves that layer and fans the net's pads out to it with vias.
Re-running replaces the existing zone for that net/layer. remove=true deletes that net's zones instead.
fill_only=true just refills all existing zones (after moving parts or routing).
Examples: pcb_pour() (GND on F.Cu+B.Cu) · pcb_pour(net="GND", stitch=true, stitch_pitch=6) ·
pcb_pour(net="+3V3", layers=["In2.Cu"]) · pcb_pour(fill_only=true)""",
      {"project": PROJECT,
       "net": {"type": "string", "description": "Net to pour (default GND)"},
       "layers": {"type": "array", "items": {"type": "string"}, "description": "Copper layers (default [F.Cu, B.Cu])"},
       "clearance": {"type": "number", "description": "Zone clearance mm (default 0.3)"},
       "min_width": {"type": "number", "description": "Minimum fill width mm (default 0.25)"},
       "thermal_gap": {"type": "number", "description": "Thermal relief gap mm (default 0.5)"},
       "thermal_spoke": {"type": "number", "description": "Thermal spoke width mm (default 0.5)"},
       "pad_connection": {"type": "string", "enum": ["thermal", "solid", "tht_thermal", "none"],
                          "description": "tht_thermal (default: thermal reliefs on through-hole pads, solid SMD pads — reflow friendly, no starved thermals), thermal, solid, none"},
       "priority": {"type": "integer"},
       "stitch": {"type": "boolean", "description": "Add a grid of stitching vias (only where both layers are filled)"},
       "stitch_pitch": {"type": "number", "description": "Stitching via pitch mm (default 5)"},
       "remove": {"type": "boolean", "description": "Remove this net's zones"},
       "fill_only": {"type": "boolean", "description": "Only refill existing zones"},
       "while_open": WHILE_OPEN, "render": RENDER},
      destructive=False, idempotent=True, group="pcb", title="Copper pour / zones")
def pcb_pour(project=None, net="GND", layers=None, clearance=0.3, min_width=0.25, thermal_gap=0.5, thermal_spoke=0.5,
             pad_connection="tht_thermal", priority=0, stitch=False, stitch_pitch=5.0, remove=False, fill_only=False,
             while_open="refuse", render=True):
    pr = projmod.resolve(project)
    ops, repair = [], {}
    if remove:
        if net != "*":
            try:
                net = _resolve_net(pr, net)
            except ToolError:
                pass   # zones may still carry a net that no pad uses any more
        ops.append({"op": "delete", "zone_nets": [net]})
    elif not fill_only:
        net = _resolve_net(pr, net)
        ops.append({"op": "zone", "net": net, "layers": layers or ["F.Cu", "B.Cu"], "clearance": clearance,
                    "min_width": min_width, "thermal_gap": thermal_gap, "thermal_spoke": thermal_spoke,
                    "pad_connection": pad_connection, "priority": priority})
        if stitch:
            ops.append({"op": "fill"})
            ops.append({"op": "stitch", "net": net, "pitch": stitch_pitch, "clearance": max(clearance, 0.3)})
        # every filled fragment holding a pad must join: starved thermals go solid, islands get a via
        ops.append({"op": "pour_repair", "net": net, "fix_thermals": pad_connection in ("thermal", "tht_thermal")})
    ops.append({"op": "fill"})
    res = board.run_ops(pr, ops, while_open=while_open, label="before-pcb-pour")
    changed = []
    for o, r in zip(ops, res["results"]):
        if o["op"] == "zone":
            changed.append(f"{net} zone on {', '.join(z['layer'] for z in r['zones_added'])}"
                           + (f" (replaced {r['zones_replaced']})" if r["zones_replaced"] else ""))
        elif o["op"] == "stitch":
            changed.append(f"{r['vias_added']} stitching vias ({r['pruned_not_in_copper']} skipped: no copper on both sides)")
        elif o["op"] == "delete":
            changed.append(f"removed {r['zones']} {net} zones")
        elif o["op"] == "pour_repair":
            if r["solid_pads"]:
                changed.append(f"solid zone connection for {', '.join(r['solid_pads'][:8])} (no thermal spoke could form)")
            if r["island_vias"]:
                changed.append(f"{len(r['island_vias'])} vias joining isolated {net} fragments at "
                               + ", ".join(f"{'/'.join(i['pads'])} {i['at']}" for i in r["island_vias"][:6]))
            repair = r
    changed.append(f"filled {res['results'][-1]['zones_filled']} of {res['results'][-1]['zones']} copper zones")
    d = board.drc(pr, parity=True)
    for _ in range(2):      # a relief that cannot form its spokes (pad squeezed by the edge / its neighbours) goes solid
        starved = sorted({f"{m.group(2)}.{m.group(1)}" for t in d.get("top", []) if t.get("type") == "starved_thermal"
                          for it in t.get("items") or [] for m in [re.search(r"[Pp]ad (\S+) \[[^\]]*\] of (\S+)", it or "")] if m})
        if not starved or remove or pad_connection not in ("thermal", "tht_thermal"):
            break
        r2 = board.run_ops(pr, [{"op": "pads_solid", "pads": starved}, {"op": "fill"}], while_open=while_open,
                           label="pour-solid-pads")
        if not r2["results"][0]["pads"]:
            break
        changed.append(f"solid zone connection for {', '.join(r2['results'][0]['pads'])} (thermal relief could not form 2 spokes)")
        d = board.drc(pr, parity=True)
    findings = _findings(d)
    if repair.get("unfixed_islands"):
        isl = repair["unfixed_islands"]
        findings.insert(0, {"severity": "warning", "code": "POUR_ISLAND",
                            "message": f"{len(isl)} {net} pour fragments hold pads but no via fits inside: "
                                       + "; ".join(f"{'/'.join(i['pads'])} on {i['layer']} near {i['at']}" for i in isl[:5]),
                            "fix": f"move the blocking track(s) or route the pad to {net} explicitly: "
                                   f"pcb_route(tracks=[{{from:'{isl[0]['pads'][0]}', to:'<another {net} pad>'}}])"})
    data = {"changed": changed, "revision": res["revision"], "unrouted": d["unconnected"],
            "verify": {"drc": _drc_brief(d)}, "findings": findings}
    return Result(data, images=_render(pr, "pcb-pour", layers=["F.Cu", "F.SilkS", "Edge.Cuts"]) if render else [],
                  summary=f"pcb_pour: {'; '.join(changed)}; unrouted {d['unconnected']}, DRC errors {d['errors']}")


# ── pcb_edit ──────────────────────────────────────────────────────────────
EDIT_OPS = {"add_track", "add_via", "delete", "add_text", "set_field", "move", "lock", "unlock"}


@tool("pcb_edit",
      """Low-level PCB escape hatch — a batch of ops applied in one transaction (all or nothing), then DRC:
  {"op":"add_track","points":[[x,y],[x,y],...],"width":0.25,"layer":"F.Cu","net":"GND"}
  {"op":"add_via","at":[x,y],"net":"GND","size":0.6,"drill":0.3}      (or "points":[[x,y],...])
  {"op":"delete","refs":["R7"]} | {"uuids":[...]} | {"nets":["SDA"]} (tracks+vias of those nets) |
      {"all_tracks":true} | {"zone_nets":["GND"]}
  {"op":"add_text","text":"v1.0","at":[x,y],"layer":"F.SilkS","size":1.0,"rot":0}
  {"op":"set_field","ref":"U1","field":"LCSC","value":"C12345","visible":false}
  {"op":"move","ref":"R1","x":110,"y":105,"rot":90,"side":"top"}    (locked part: add "unlock":true)
  {"op":"lock"|"unlock","refs":["J1"]}    (or "all":true)
Coordinates are mm (board coordinates as in pcb_read; track/via uuids from pcb_read(what=["tracks"])).
Deleting a footprint that is in the schematic only lasts until the next pcb_sync — delete the symbol instead.
Prefer pcb_place / pcb_route / pcb_pour when they fit.""",
      {"project": PROJECT, "ops": {"type": "array", "items": {"type": "object"}},
       "while_open": WHILE_OPEN, "render": RENDER},
      required=["ops"], destructive=True, group="pcb", title="Edit PCB items")
def pcb_edit(project=None, ops=None, while_open="refuse", render=False):
    pr = projmod.resolve(project)
    wops = []
    for o in ops or []:
        k = o.get("op")
        if k not in EDIT_OPS:
            raise ToolError(f"unknown op '{k}'", code="BAD_ARGUMENT", fix=f"one of {sorted(EDIT_OPS)}")
        if k == "move":
            if "ref" not in o:
                raise ToolError("move needs a ref", code="BAD_ARGUMENT", fix='{"op":"move","ref":"R1","x":110,"y":105}')
            wops.append({"op": "place", "items": [dict({x: o[x] for x in MOVE_KEYS if x in o}, check=True)]})
        elif k in ("lock", "unlock"):
            refs_ = _ref_list(o.get("refs") if o.get("refs") is not None else o.get("ref"), "refs")
            every = bool(o.get("all")) or "*" in refs_
            if not every and not refs_:
                raise ToolError(f"{k} needs refs=[...] or all=true", code="BAD_ARGUMENT",
                                fix=f'{{"op":"{k}","refs":["U1"]}}')
            wops.append({"op": "lock", "locked": k == "lock", **({"all": True} if every else {"refs": refs_})})
        else:
            wops.append(o)
    if not wops:
        raise ToolError("ops is empty", code="BAD_ARGUMENT")
    res = board.run_ops(pr, wops, while_open=while_open, label="before-pcb-edit")
    d = board.drc(pr, parity=True)
    changed = []
    for o, r in zip(wops, res["results"]):
        if o["op"] == "place":
            p = r["placed"][0]
            changed.append((f"moved {p['ref']}" if "after" in p else f"{p['ref']} skipped ({p.get('skipped')})")
                           + ((" (now locked)" if p["locked"] else " (unlocked)") if "locked" in p else ""))
        elif o["op"] == "lock":
            verb = "locked" if r["locked"] else "unlocked"
            changed.append(f"{verb} {', '.join(r['changed'])}" if r["changed"]
                           else f"nothing to do: {', '.join(r['already'][:12]) or 'no parts'} already {verb}")
        elif o["op"] == "delete":
            changed.append("deleted " + ", ".join(f"{v} {k}" for k, v in r.items() if v and k != "footprints")
                           + (f" footprints {r['footprints']}" if r["footprints"] else ""))
        elif o["op"] in ("add_track", "add_via"):
            changed.append(f"{o['op']}: {r.get('segments', r.get('vias'))} item(s)")
        else:
            changed.append(f"{o['op']} ok")
    data = {"changed": changed, "results": res["results"], "revision": res["revision"],
            "verify": {"drc": _drc_brief(d)}, "findings": _findings(d)}
    return Result(data, images=_render(pr, "pcb-edit") if render else [],
                  summary=f"pcb_edit: {len(wops)} ops; DRC errors {d['errors']}, unrouted {d['unconnected']}")
