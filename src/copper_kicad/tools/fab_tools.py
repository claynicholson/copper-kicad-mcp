"""fab_export · export — manufacturing outputs, verified after writing."""
from __future__ import annotations

from .. import project as P
from ..mcp.registry import Result, tool
from ..review import fab as F
from ..review import fab_profiles as FP

PROJECT = {"type": "string", "description": "Project folder or .kicad_pro/.kicad_sch/.kicad_pcb path. Omit to use the active project."}


@tool("fab_export", """Produce the order pack for a PCB fab: Gerbers + Excellon drill zipped, a BOM CSV and a placement (centroid/CPL) CSV,
all in the format of a fab PROFILE. Writes to <project>/copper-out/fab/.
profile: omit to use the project's configured profile (fab.profile in the Copper config; 'jlcpcb' out of the box). Built in:
'jlcpcb' (Protel gerbers; BOM 'Comment, Designator, Footprint, LCSC Part #'; CPL 'Designator, Mid X, Mid Y, Layer, Rotation' with
JLC rotation corrections), 'pcbway', 'oshpark' (bare boards) and 'generic' (Gerber X2 + Excellon + KiCad position file + plain BOM
with MPN/Manufacturer — for any other fab). Users add their own as JSON in $COPPER_HOME/fabs/ or <project>/.copper/fabs/.
profile='list' returns every available profile with its rules and notes (nothing is exported).

It first runs ERC + DRC (zones refilled, schematic parity) and REFUSES (error FAB_BLOCKED, listing blockers with fixes) for whatever
the profile says blocks an order: ERC errors, DRC errors, unrouted nets, parity issues, no board outline, an empty board and — for
profiles that need one per part, like jlcpcb — assembled parts without the profile's part number.
Fix the blockers (each comes with the next tool call); pass waive=[codes] ONLY for things the user consciously accepts:
'erc','drc','unrouted','parity','parts','outline','empty' or 'all'. assembly=false skips BOM + placement (bare PCB order).
With assembly it also asks the parts provider for live stock of every BOM part (for `boards` boards, default 5): parts with zero /
too little stock or not listed come back in `warnings` (code 'stock': affected refs + a ready parts_alternatives fix per part). That never blocks;
waive=['stock'] acknowledges it. Offline (or with check_stock=false), `stock.not_checked` and the first order note say stock was NOT checked.
Every output is checked (non-empty, complete layer set, zip readable). Returns board size, layers, min track/drill, part counts,
`summary.assembly_fees` (Basic / Preferred / Extended counts, the fee total, `extended_refs` = which parts cause it) when the
profile has part classes, and order notes.
Examples: fab_export() · fab_export(profile='generic') · fab_export(waive=['parts']) · fab_export(assembly=false)""",
      {"project": PROJECT,
       "profile": {"type": "string", "description": "fab profile name (default: the project's configured one); 'list' shows them"},
       "waive": {"type": "array", "items": {"type": "string", "enum": sorted(F.WAIVABLE) + ["all"]}},
       "assembly": {"type": "boolean", "description": "also write BOM + placement files (default: true when the fab assembles)"},
       "boards": {"type": "integer", "minimum": 1, "description": "boards in the order, for the stock check (default 5)"},
       "check_stock": {"type": "boolean", "description": "ask the parts provider for live stock (default true)"},
       "out_dir": {"type": "string", "description": "output folder (default <project>/copper-out/fab)"},
       "layers": {"type": "array", "items": {"type": "string"}, "description": "override gerber layer list (rarely needed)"}},
      title="Fab order pack", read_only=False, destructive=False, idempotent=True, group="fab")
def fab_export(project=None, profile=None, waive=None, assembly=None, out_dir=None, layers=None, boards=None,
               check_stock=True, _progress=None):
    if str(profile or "").strip().lower() == "list":
        pr = None
        try:
            pr = P.resolve(project)
        except Exception:  # noqa: BLE001 — listing profiles needs no project
            pr = None
        profs, bad = [], []
        for p in FP.available(pr):
            try:
                profs.append(FP.describe(FP.load(p["name"], pr)))
            except Exception as e:  # noqa: BLE001
                bad.append({"name": p["name"], "file": p["path"], "error": getattr(e, "message", str(e)),
                            "fix": getattr(e, "fix", None)})
        active = FP.active_name(pr)
        d = {"active": active, "profiles": profs, "add_your_own": "copy a built-in file to $COPPER_HOME/fabs/<name>.json or "
             "<project>/.copper/fabs/<name>.json and edit it ('extends' inherits the rest); docs/FAB_PROFILES.md"}
        if bad:
            d["broken"] = bad
        return Result(d, summary=f"{len(profs)} fab profile(s); active: {active}")
    pr = P.resolve(project)
    r = F.fab_export(pr, profile=profile, out_dir=out_dir, waive=waive, layers=layers, assembly=assembly, progress=_progress,
                     check_stock=check_stock is not False, boards=boards)
    s = r["summary"]
    size = f"{s['board_mm'][0]}×{s['board_mm'][1]} mm" if s.get("board_mm") and s["board_mm"][0] else "size ?"
    line = f"{r['fab']} pack for {pr.name} ready: {r['zip']} — {s.get('copper_layers')} layers, {size}"
    if r.get("bom"):
        line += f", {r.get('placements', 0)} placements / {r.get('bom_lines', 0)} BOM lines"
    if r.get("waived"):
        line += f" (WAIVED: {', '.join(w['code'] for w in r['waived'])})"
    fees = s.get("assembly_fees") or {}
    if fees.get("extended"):
        line += f"; {fees['extended']} Extended part(s) = ${fees['estimated_usd']:g} fee"
    st = r.get("stock") or {}
    for w in r.get("warnings") or []:
        if w["code"] == "stock":
            line += (f". STOCK WARNING: {len(w['parts'])} BOM part(s) short for {st.get('boards', '?')} board(s) ({', '.join(x['part'] for x in w['parts'][:6])}) — "
                     f"{w['parts'][0]['fix']}")
    if st.get("unchecked"):
        line += f". Stock NOT checked for {st['unchecked']} of {st['lines']} BOM part(s): {st.get('not_checked')}"
    return Result(r, summary=line)


@tool("export", """Export any other manufacturing/documentation output with kicad-cli; the files are verified to exist and be non-empty.
kind: pdf_schematic | svg (schematic, one per sheet) | pcb_pdf | step | glb | vrml | ipc2581 (BOM columns from the part-number/MPN fields) | odb |
netlist (KiCad s-expr) | bom (CSV grouped by Value/Footprint/part number) | pos (CSV, mm, both sides, DNP excluded) | drill | gerbers | render3d (PNG) |
stats (board statistics JSON) | ipcd356.
out: file (or folder for svg/drill/gerbers); default <project>/copper-out/export/. For a fab's order pack use fab_export instead.
Examples: export(kind='step') · export(kind='pdf_schematic') · export(kind='pcb_pdf', layers=['F.Cu','Edge.Cuts'])""",
      {"kind": {"type": "string", "enum": F.EXPORT_KINDS},
       "out": {"type": "string"},
       "layers": {"type": "array", "items": {"type": "string"}},
       "view": {"type": "string", "enum": ["top", "bottom", "iso", "iso_bottom", "front"], "description": "render3d only"},
       "project": PROJECT},
      ["kind"], title="Export", read_only=False, destructive=False, idempotent=True, group="fab")
def export(kind, out=None, layers=None, view="iso", project=None):
    pr = P.resolve(project)
    r = F.export(pr, kind, out=out, layers=layers, view=view)
    images = []
    if r.get("image"):
        from ..mcp.registry import Image
        images = [Image(path=r["image"], caption=f"3D {view}")]
    return Result(r, images=images, summary=f"exported {kind}: {len(r['files'])} file(s), {r['total_bytes']:,} bytes → {r['path']}")
