"""library — KiCad symbol/footprint libraries: search, inspect, list, repair."""
from __future__ import annotations

from pathlib import Path

from .. import project as P
from ..kicad import libs
from ..mcp.registry import Result, ToolError, tool


def _pdir(project):
    if not project:
        try:
            return P.resolve(None).dir
        except ToolError:
            return None
    return P.resolve(project).dir


@tool("library", """Search and inspect the KiCad symbol & footprint libraries this machine actually has (global + project tables,
incl. the project libraries parts_import creates). Use it to find a valid lib_id / footprint before sch_apply place, and to read exact pin names.

action:
- search_symbols: query = part number, name or keywords ("AMS1117", "STM32F103C8", "ldo 3.3v", "Device:R"). Ranked exact name >
  prefix > keyword/description. Returns lib_id, description, default footprint, pin count, units.
- search_footprints: query ("R_0603", "SOT-23", "USB C receptacle"); optional filters = ki_fp_filters globs ("R_* SOT?23*").
- symbol_info: lib_id → pins (number, name, electrical type, alternates, unit), properties, default footprint, `extends` parent
  (derived symbols are flattened like KiCad does).
- footprint_info: fp_id ("Package_TO_SOT_SMD:SOT-223-3_TabPin2") → pads, pad count, courtyard size, 3D model.
- list: libraries in the active tables (kind='sym'|'fp') with scope and whether the files exist.
- repair: fix "library not found" problems — creates missing global sym/fp-lib-table from KiCad's templates (what KiCad's
  first run does) or replaces a table whose entries point at a removed KiCad. dry_run defaults to TRUE; pass dry_run=false to write.
Examples: library(action='search_symbols', query='USB C receptacle') · library(action='symbol_info', lib_id='MCU_ST_STM32F1:STM32F103C8Tx')""",
      {"action": {"type": "string", "enum": ["search_symbols", "search_footprints", "symbol_info", "footprint_info", "list", "repair"]},
       "query": {"type": "string"},
       "lib_id": {"type": "string", "description": "Symbol 'Library:Name' for symbol_info."},
       "fp_id": {"type": "string", "description": "Footprint 'Library:Name' for footprint_info."},
       "lib": {"type": "string", "description": "Restrict a search to one library nickname."},
       "filters": {"type": "string", "description": "search_footprints: space-separated globs, e.g. a symbol's fp_filters."},
       "kind": {"type": "string", "enum": ["sym", "fp"], "description": "list: which tables (default sym)."},
       "limit": {"type": "integer", "description": "Max results (default 15)."},
       "pins": {"type": "boolean", "description": "symbol_info: include pins (default true)."},
       "dry_run": {"type": "boolean", "description": "repair: report only (default true)."},
       "project": {"type": "string", "description": "Project whose project-level lib tables are included (default: active)."}},
      ["action"], title="KiCad libraries", read_only=False, idempotent=True, group="library")
def library(action, query=None, lib_id=None, fp_id=None, lib=None, filters=None, kind="sym", limit=15, pins=True,
            dry_run=True, project=None):
    pdir = _pdir(project)
    limit = max(1, int(limit or 15))
    if action == "search_symbols":
        if not query:
            raise ToolError("search_symbols needs query", code="BAD_ARGUMENT", fix="library(action='search_symbols', query='AMS1117')")
        r = libs.search_symbols(query, pdir, limit, lib=lib)
        if not r:
            return Result({"results": [], "hint": "no match — try fewer/other keywords, or parts_search for a supplier's part "
                                                  "(parts_import creates symbol+footprint)"}, summary="no symbols found")
        return Result({"results": r}, summary=f"{len(r)} symbol(s); best: {r[0]['lib_id']}")
    if action == "search_footprints":
        if not query and not filters:
            raise ToolError("search_footprints needs query or filters", code="BAD_ARGUMENT")
        r = libs.search_footprints(query or "", pdir, limit, lib=lib, filters=filters)
        return Result({"results": r}, summary=f"{len(r)} footprint(s)" + (f"; best: {r[0]['fp_id']}" if r else ""))
    if action == "symbol_info":
        lid = lib_id or query
        if not lid:
            raise ToolError("symbol_info needs lib_id", code="BAD_ARGUMENT")
        s = libs.read_symbol(lid, pdir)
        d = s.to_dict(pins=pins if pins is not None else True)
        return Result(d, summary=f"{lid}: {d.get('pin_count', '?')} pins, {d['units']} unit(s)")
    if action == "footprint_info":
        fid = fp_id or query
        if not fid:
            raise ToolError("footprint_info needs fp_id", code="BAD_ARGUMENT")
        d = libs.read_footprint(fid, pdir)
        pads = d["pads"]
        if len(pads) > 80:
            d["pads"] = pads[:80]
            d["pads_truncated"] = len(pads) - 80
        return Result(d, summary=f"{fid}: {d['pad_count']} pads")
    if action == "list":
        rows = libs.list_libraries(kind or "sym", pdir)
        if query:
            rows = [r for r in rows if query.lower() in r["name"].lower()]
        missing = [r["name"] for r in rows if not r["exists"]]
        out = {"count": len(rows), "missing": missing,
               "libraries": [{k: r[k] for k in ("name", "scope", "exists")} for r in rows[: max(limit, 300)]]}
        if missing:
            out["fix"] = "library(action='repair', dry_run=false)"
        return Result(out, summary=f"{len(rows)} {kind} libraries, {len(missing)} missing")
    if action == "repair":
        r = libs.repair_tables(dry_run=True if dry_run is None else bool(dry_run), project_dir=pdir)
        s = "tables OK" if r.get("ok") else f"{len(r['actions'])} action(s), {len(r['problems'])} problem(s)" + (
            " — dry run, pass dry_run=false to apply" if r["dry_run"] and r["actions"] else "")
        return Result(r, summary=s)
    raise ToolError(f"unknown action {action}", code="BAD_ARGUMENT")
