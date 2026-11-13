"""sch_apply · sch_read · sch_tidy — schematic capture tools."""
from __future__ import annotations

import json
import re

from .. import project as P
from .. import render as R
from ..kicad import env
from ..mcp.registry import Image, Result, ToolError, tool
from ..sch import netlist as N
from ..sch import ops as O
from ..sch.model import Schematic

PROJECT = {"type": "string", "description": "Project folder or .kicad_pro/.kicad_sch path. Omit to use the active project."}
SHEET = {"type": "string", "description": "Sub-sheet file or sheet name (default: root sheet)."}
WHILE_OPEN = {"type": "string", "enum": ["refuse", "revert"],
              "description": "If the schematic is open in KiCad: 'refuse' (default) or 'revert' (write anyway; then use File ▸ Revert in KiCad)."}

MAX_CHANGED = 60


_ENV_TYPES = {"lib_symbol_issues", "footprint_link_issues", "lib_symbol_mismatch"}


def _erc_baseline(pr):
    """Record an ERC baseline BEFORE the first Copper edit of an existing design, so 'new' after the edit
    means 'introduced by this edit' instead of 'every pre-existing warning'."""
    try:
        from ..review import verify as V
        from ..review.common import load_state
    except ImportError:
        return
    try:
        from ..sch import model as _M
        if _M.live():
            return
        if pr.sch.exists() and not (load_state(pr) or {}).get("erc"):
            V.run_erc(pr)
    except (ToolError, OSError, ValueError):
        pass


def _erc_raw(pr) -> dict:
    """ERC through the review module (positions scaled to real mm, new/fixed deltas)."""
    from ..review import verify as V
    # the same rows V.erc_brief() hands out (severity/type/message/at/fix) plus the sheet each one is on —
    # run_erc is called once so its new/fixed delta describes THIS edit
    r = V.run_erc(pr)
    r["top"] = [{k: f.get(k) for k in ("severity", "type", "message", "at", "fix", "sheet") if f.get(k) is not None}
                for f in r.pop("violations", [])]
    r["new_findings"] = [{k: f.get(k) for k in ("severity", "type", "message", "at", "fix", "sheet") if f.get(k) is not None}
                         for f in r.get("new_findings") or []]
    return r


def _net_of(msg: str) -> str | None:
    """Label name of the net in an ERC message: '(net /power/VBAT)' → 'VBAT' (ops take label names; a pwr_flag on
    '/VBAT' used to plant a new, unconnected label literally named '/VBAT')."""
    m = re.search(r"\(net ([^)]+)\)", msg or "")
    if not m:
        return None
    net = m.group(1).strip()
    return net.rstrip("/").split("/")[-1] if net.startswith("/") else net


def _sheet_name(f: dict) -> str:
    """Sub-sheet name a finding is on ('' = root): one sch_apply call edits one sheet, so fixes are routed there."""
    return (f.get("sheet") or "").strip("/").split("/")[-1]


def _example(f: dict) -> dict:
    """One finding as the agent needs it: what, WHERE (`at` in mm, `sheet` when not the root) and its own fix —
    the fix review.verify built already names the pin and carries sheet='<name>' for sub-sheet problems."""
    e = {"message": f.get("message")}
    if f.get("at"):
        e["at"] = f["at"]
    if _sheet_name(f):
        e["sheet"] = _sheet_name(f)
    if f.get("fix"):
        e["fix"] = f["fix"]
    return e


def compact_erc(r: dict, limit: int = 8) -> dict:
    """ERC brief an agent can act on: one entry per violation TYPE (errors first) with a count and up to three
    examples, each keeping its `at` and its sheet-routed `fix` (one shared `fix` when they are identical, and one
    batched pwr_flag fix per sheet for undriven rails). Library-table noise is folded into 'environment'."""
    if not isinstance(r, dict) or "errors" not in r:
        return r
    groups: dict = {}
    for f in r.get("top") or []:
        t = f.get("type") or "?"
        g = groups.setdefault(t, {"type": t, "severity": f.get("severity"), "count": 0, "examples": []})
        g["count"] += 1
        if f.get("severity") == "error":
            g["severity"] = "error"
        if len(g["examples"]) < 3 and f.get("message") not in [e["message"] for e in g["examples"]]:
            g["examples"].append(_example(f))
        if t == "power_pin_not_driven":
            net = _net_of(f.get("message"))
            if net:
                nets = g.setdefault("_nets", {}).setdefault(_sheet_name(f), [])
                if net not in nets:
                    nets.append(net)
    for t, n in (r.get("by_type") or {}).items():       # counts beyond the fetched rows
        if t in groups:
            groups[t]["count"] = max(groups[t]["count"], n)
    env_n = sum(g["count"] for t, g in groups.items() if t in _ENV_TYPES)
    out_groups = []
    for t, g in groups.items():
        if t in _ENV_TYPES:
            continue
        nets = g.pop("_nets", None)
        fixes = [e.get("fix") for e in g["examples"]]
        if nets:
            calls = ["sch_apply(" + (f"sheet={json.dumps(sn)}, " if sn else "") + "ops=["
                     + ",".join('{"op":"pwr_flag","net":"%s"}' % n for n in ns) + "])" for sn, ns in nets.items()]
            g["fix"] = ("if the rail is supplied from off-sheet (connector, battery, USB), flag it: " + " and ".join(calls)
                        + "; otherwise connect a regulator/source output to it")
            for e in g["examples"]:
                e.pop("fix", None)
        elif fixes and fixes[0] and all(x == fixes[0] for x in fixes):
            g["fix"] = fixes[0]                           # same remedy for every example: say it once
            for e in g["examples"]:
                e.pop("fix", None)
        if g["count"] == 1:
            e = g.pop("examples")[0]
            g.update({k: v for k, v in e.items() if k != "fix" or "fix" not in g})
        out_groups.append(g)
    out_groups.sort(key=lambda g: (g["severity"] != "error", -g["count"]))
    d = {"errors": r.get("errors"), "warnings": r.get("warnings")}
    if r.get("first_run"):
        d["baseline"] = "first ERC of this project: counts include issues that existed before this edit"
    elif "new" in r:
        d["new"], d["fixed"] = r.get("new", 0), r.get("fixed", 0)
        if r.get("new_findings") is not None and (r.get("errors", 0) + r.get("warnings", 0)) > 25:
            # big existing design: show what THIS edit introduced, plus per-type totals
            newr = compact_erc({"errors": 1, "top": r["new_findings"]}, limit)
            d["new_issues"] = newr.get("issues", [])
            d["all_by_type"] = dict(sorted(((t, n) for t, n in (r.get("by_type") or {}).items() if t not in _ENV_TYPES),
                                           key=lambda kv: -kv[1])[:10])
            out_groups = []
        if r.get("fixed_findings"):
            d["fixed_examples"] = r["fixed_findings"][:3]
    if out_groups:
        d["issues"] = out_groups[:limit]
    if len(out_groups) > limit:
        d["more_types"] = len(out_groups) - limit
    if env_n:
        d["environment"] = {"count": env_n, "note": "library-table / footprint-link warnings caused by this machine's "
                            "KiCad configuration, not by the wiring", "fix": "library(action='repair', dry_run=false)"}
    if r.get("report"):
        d["report"] = r["report"]
    return d


def _erc(pr) -> dict:
    from ..sch import model as _M
    if _M.live():       # live host: the app runs ERC on the real document after the change lands (host.apply erc_new)
        return {"skipped": "live"}
    return compact_erc(_erc_raw(pr))


def _preview(pr, sch_file, region, width=1100) -> list[Image]:
    try:
        sch = Schematic.load(sch_file, pr.name)
        pw, ph = sch.page_size()
    except ToolError:
        pw, ph = 297, 210
    crop = None
    full_w = 1800
    if region:
        pad = 15.0
        x0, y0, x1, y1 = region[0] - pad, region[1] - pad, region[2] + pad, region[3] + pad
        # keep some context: at least ~90 x 60 mm around the change
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        w, h = max(x1 - x0, 90.0), max(y1 - y0, 60.0)
        if w * h < 0.45 * pw * ph:
            crop = [max(0.0, cx - w / 2), max(0.0, cy - h / 2), min(pw, cx + w / 2), min(ph, cy + h / 2)]
            # render so the CROP comes out ~width px wide (≈10+ px per mm: 1.27 mm text stays legible)
            full_w = int(min(6000, max(1500, width * pw / max(1.0, crop[2] - crop[0]))))
    imgs = R.schematic(sch_file, pr.out_dir / "preview", width=full_w, crop_mm=crop, max_sheets=1)
    for im in imgs:
        im.caption = ("changed region" if crop else "full sheet") + f" of {sch_file.name}"
    return imgs


@tool("sch_apply", """Edit the schematic with ONE atomic batch of ops (all succeed or nothing is written). The main schematic tool —
put a whole sub-circuit (or the whole design) in a single call. Creates the .kicad_sch if it doesn't exist.

Pins are 'REF.PIN' where PIN is a pin number OR name: 'U1.PA9', 'U1.12', 'J1.VBUS', 'R1.2', 'U3.~{RESET}'.
A name matching several pins (e.g. 'U1.GND', 'J1.VBUS') connects all of them. NEVER compute coordinates for wiring:
Copper anchors a short stub + label (or power symbol) on the exact pin tip, rotation/mirror aware.
Parts placed without 'at' are auto-laid-out (clusters around ICs, decaps beside their IC, R→LED chains wired,
connectors at the edges, page grows A4→A3 as needed). Give temporary 'id's to new parts and use them in pins
('led.A'); the result's 'refs' maps id → final reference. ids are single words (no '.'), unique, and must not
equal an existing reference. Net names are case-sensitive in KiCad: 'gnd' next to 'GND' is refused; KiCad's sheet-path form '/VBAT' is read as 'VBAT'.
One call edits ONE sheet (`sheet`= file or sheet name); explicit 'at'/'move' positions that would land a pin on
another pin/label/wire end are refused (that would silently short nets).

ops (objects with "op"):
- place: lib_id | lcsc ("C25804", imports via LCSC) | part (+provider: any parts provider's number) | spec ("10k 0603",
  "100nF 0402", "LED red 0603": resolved by the project's parts providers; default a JLC Basic part);
  optional ref ("R5" or "R?"), id, value, footprint, at [x,y] mm, rot 0/90/180/270, mirror x|y, unit (int|"all"; multi-unit
  parts default to all), fields {LCSC:...,MPN:...}, group (mcu|power|connector|passive), near (ref), dnp
- connect: pin | pins [..], net — power nets (GND, +3V3, +5V, VBUS, VCC …, any power-library name) become power symbols,
  others local labels; kind:'global' for nets shared between sheets ('hierarchical' needs a matching sheet pin
  on the parent sheet symbol, which Copper cannot add yet — you get a warning)
- power: pin(s), net — force a power symbol (unknown names use a VCC/GND symbol with that value)
- no_connect: pin | pins — X flag on unused pins (clears 'pin not connected' ERC)
- wire: from, to (pin refs; orthogonal L route) or points [[x,y],...]
- label: net + pin (anchored) or at [x,y] (+kind, rot)
- set_field: ref, field, value (Value, Footprint, LCSC, MPN, Datasheet, Reference, any) or fields {..}
- move: ref, at|by, rot, mirror — stubs/labels/wires on its pins follow
- delete: ref (its labels/power stubs and any wires left dangling go too) | label (net name, optional pin, kind
  local|global|hierarchical) | wire_at [x,y] (one stray segment) | dangling:true (every dangling wire run)
- rename_net: from, to — renames its labels / power symbols on this sheet (refused if 'to' already exists)
- disconnect: pin(s) — remove label/power symbol/wire/no-connect at that pin
- swap: ref + lib_id|lcsc|part and/or footprint — connections are re-attached by pin number
- text: text, at?, size
- pwr_flag: net — PWR_FLAG on a net driven only by passive/connector pins (fixes 'power pin not driven' on GND/VBUS);
  idempotent (a net that already has one is left alone)

After writing, KiCad re-saves the file, Copper re-checks every requested connection in KiCad's netlist
(`connectivity` ok/checked + mismatches), runs ERC (`verify.erc`: one entry per issue type, each example with its
`at` [x,y] mm and a ready `fix` routed to the right sheet; on big designs `new_issues` = what THIS call introduced) and returns a preview cropped to the change.
Typical next step: apply the `fix` ops from verify.erc, or sch_read(query='unconnected').
Example: ops=[{"op":"place","lib_id":"Regulator_Linear:AMS1117-3.3","id":"ldo"},
 {"op":"place","spec":"10uF 0805","id":"cin"},{"op":"connect","pins":["ldo.VI","cin.1"],"net":"VBUS"},
 {"op":"connect","pins":["ldo.GND","cin.2"],"net":"GND"},{"op":"connect","pin":"ldo.VO","net":"+3V3"}]""",
      {"project": PROJECT,
       "ops": {"type": "array", "items": {"type": "object"}, "description": "Ordered list of op objects (see description)."},
       "dry_run": {"type": "boolean", "description": "Validate and report what would change without writing."},
       "while_open": WHILE_OPEN,
       "sheet": SHEET,
       "preview": {"type": "boolean", "description": "Attach a rendered PNG (cropped to the change when small). Default true."},
       "verify": {"type": "boolean", "description": "Run ERC + netlist connectivity check after writing. Default true."},
       "layout": {"type": "string", "enum": ["auto", "keep", "pro"],
                  "description": "auto (default): a sheet only Copper arranged is re-drawn as functional blocks after a "
                                 "batch without at/rot (netlist proven identical); keep: never; pro: always try."}},
      ["ops"], title="Schematic: apply ops", destructive=True, core=True, group="schematic")
def sch_apply(ops, project=None, dry_run=False, while_open="refuse", sheet=None, preview=True, verify=True, layout="auto",
              _progress=None):
    pr = P.resolve(project, must_exist=False)
    if verify and not dry_run:
        _erc_baseline(pr)
    from ..sch.pro import tool as SP
    pre_layout = SP.pre_state(pr, sheet, ops) if not dry_run and layout != "keep" and isinstance(ops, list) else None
    r = O.apply_ops(pr, ops, dry_run=dry_run, while_open=while_open, sheet=sheet)
    from pathlib import Path
    f = Path(r["file"])
    changed = r["changed"]
    data = {"changed": changed[:MAX_CHANGED], "refs": r["refs"], "revision": r.get("revision")}
    if not dry_run and layout != "keep" and isinstance(ops, list):
        lay = SP.after_apply(pr, f, ops, pre_layout, layout, while_open, refs=r.get("refs"))
        if lay:
            data["layout"] = lay["note"]
            if lay.get("relaid"):
                data["revision"] = lay["revision"]
                r["region"] = None
    if len(changed) > MAX_CHANGED:
        data["changed_truncated"] = len(changed) - MAX_CHANGED
    if r["warnings"]:
        data["warnings"] = r["warnings"]
    if r.get("checkpoint"):
        data["checkpoint"] = r["checkpoint"]
    if r.get("created"):
        data["created"] = r["created"]
    if dry_run:
        data["dry_run"] = True
        return Result(data, summary=f"dry run: {len(changed)} change(s) would be made — nothing written")
    images = []
    findings = []
    if verify:
        if _progress:
            _progress(0.6, "netlist check")
        try:
            nl = N.export_netlist(pr)
            chk = N.check_expectations(nl, r["expect"])
            data["connectivity"] = {"checked": chk["checked"], "ok": chk["ok"]}
            if chk["mismatches"]:
                data["connectivity"]["mismatches"] = chk["mismatches"][:20]
                findings.append({"severity": "error", "code": "connectivity_mismatch",
                                 "message": f"{len(chk['mismatches'])} pin(s) did not end up on the requested net in KiCad's netlist",
                                 "fix": "sch_read(query='pins', ref=...) to inspect, then disconnect + connect"})
        except ToolError as e:
            data["connectivity"] = {"error": e.message}
        if _progress:
            _progress(0.75, "ERC")
        try:
            data["verify"] = {"erc": _erc(pr)}
        except ToolError as e:
            data["verify"] = {"erc": {"error": e.message}}
    if findings:
        data["findings"] = findings
    if preview:
        if _progress:
            _progress(0.9, "render")
        try:
            images = _preview(pr, f, r.get("region"))
        except ToolError as e:
            data["preview_error"] = e.message
    erc = (data.get("verify") or {}).get("erc") or {}
    conn = data.get("connectivity") or {}
    summ = f"{len(changed)} change(s) written to {f.name}"
    if conn.get("checked") is not None:
        summ += f"; connectivity {conn.get('ok')}/{conn.get('checked')} ok"
    if "errors" in erc:
        summ += f"; ERC {erc['errors']} error(s), {erc.get('warnings', 0)} warning(s)"
    return Result(data, images, summ)


def _netlist_safe(pr, sch=None):
    try:
        return N.export_netlist(pr, sch)
    except ToolError:
        return None


NET_NAMES_NOTE = ("KiCad's full net names, as on the board ('/SDA' = local label SDA on the root sheet, '/power/EN' = on "
                  "sheet 'power', no slash = global/power net); in sch_apply ops use the label name (last path segment)")


def _net_is(full: str, want: str) -> bool:
    """`net=` accepts KiCad's full name ('/power/EN' — exact) or a bare label name ('EN' — on any sheet)."""
    if want.startswith("/"):
        return full == want
    return full == want or full.rsplit("/", 1)[-1] == want


def _match(filter_, *vals) -> bool:
    if not filter_:
        return True
    f = str(filter_).lower()
    return any(f in str(v).lower() for v in vals if v)


@tool("sch_read", """Read the schematic compactly. Use before editing an existing design, and to find exact pin names.

query:
- 'components': ref, value, lib_id, footprint, part number (LCSC) / MPN, position, unit (filter matches ref/value/lib_id/footprint)
- 'pins': every pin of `ref` with number, name, type and the NET it is on (from KiCad's netlist) — use to pick pin names for connect
- 'nets': net → ['REF.PIN', ...] for the whole hierarchy, keyed by KiCad's full net names as on the board ('/SDA',
  '/power/EN', 'GND'); filter matches net names; `net` = one net (full or label name). Per-pin 'unconnected-(…)' nets
  are left out and counted in `excluded`
- 'labels': labels with kind and position
- 'unconnected': pins on no net and without a no-connect flag (what ERC will flag) — each with a ready no_connect/connect hint
- 'summary': counts (default): nets (all), connected_nets (≥2 pins), unconnected_pin_nets — as project_summary
- 'readability': drawing-quality metrics of the sheet (score, text overlaps, wire crossings, labels per net, paper fill)
Examples: sch_read(query='pins', ref='U1') · sch_read(query='nets', filter='USB') · sch_read(query='components', filter='C')""",
      {"project": PROJECT,
       "query": {"type": "string", "enum": ["summary", "components", "pins", "nets", "labels", "unconnected", "readability"]},
       "ref": {"type": "string", "description": "Reference for query='pins' (e.g. 'U1')."},
       "net": {"type": "string", "description": "Single net for query='nets': full name ('/power/EN') or label name ('EN')."},
       "filter": {"type": "string", "description": "Case-insensitive substring filter."},
       "sheet": SHEET,
       "limit": {"type": "integer", "description": "Max rows (default 80)."}},
      title="Schematic: read", read_only=True, idempotent=True, core=True, group="schematic")
def sch_read(project=None, query="summary", ref=None, net=None, filter=None, sheet=None, limit=80):
    pr = P.resolve(project)
    f, spath = O.sheet_file(pr, sheet)
    if not f.exists():
        raise ToolError(f"{f.name} does not exist yet", code="NOT_FOUND",
                        fix="sch_apply(ops=[{\"op\":\"place\",...}]) creates it")
    sch = Schematic.load(f, pr.name, spath)
    limit = max(1, int(limit or 80))
    if query == "readability":
        from ..sch.pro import tool as SP
        m = SP.readability(pr, sheet)
        return Result(m, summary=f"readability {m['score']}/100: {m['overlaps']} overlap(s), {m['wire_crossings']} "
                      f"crossing(s), {m['labels_per_signal_net']} labels per net on {m['paper']}")
    if query == "components":
        rows = [s.describe() for s in sch.symbols(include_power=False)
                if _match(filter, s.ref, s.value, s.lib_id, s.field("Footprint"), s.field("LCSC"))]
        rows.sort(key=lambda d: (_refkey(d["ref"]), d.get("unit", 1)))
        return Result({"components": rows[:limit], "total": len(rows)}, summary=f"{len(rows)} component(s)")
    if query == "pins":
        if not ref:
            raise ToolError("query='pins' needs ref", code="BAD_ARGUMENT", fix="sch_read(query='pins', ref='U1')")
        insts = sch.find_ref(ref)
        if not insts:
            where = O.ref_elsewhere(pr, sch, ref)
            raise ToolError(f"no symbol '{ref}' on {f.name}" + (f"; it is on sheet '{where}'" if where else ""),
                            code="REF_NOT_FOUND",
                            fix=f"sch_read(query='pins', ref='{ref}', sheet='{where}')" if where else
                            "sch_read(query='components') lists references")
        nl = _netlist_safe(pr)
        pn = N.pin_nets(nl) if nl else {}
        ctx = O.Ctx(pr, sch)
        rows = []
        for si in insts:
            for p in si.pins_world():
                if not _match(filter, p["number"], p["name"]):
                    continue
                net = pn.get((si.ref, p["number"]))
                d = {"pin": p["number"], "name": p["name"], "type": p["type"]}
                if len(insts) > 1:
                    d["unit"] = si.unit
                if net is not None:
                    d["net"] = None if N.is_unconnected(net) else net
                st = ctx.net_at_pin(p["xy"], si)[1]
                if st == "no_connect":
                    d["no_connect"] = True
                if p.get("hidden"):
                    d["hidden"] = True
                if p.get("alternates"):
                    d["alternates"] = p["alternates"][:8]
                d["side"] = {"left": "L", "right": "R", "top": "T", "bottom": "B"}[
                    {(-1, 0): "left", (1, 0): "right", (0, -1): "top", (0, 1): "bottom"}.get(tuple(p["out"]), "left")]
                rows.append(d)
        rows.sort(key=lambda d: _refkey(d["pin"]))
        return Result({"ref": ref, "lib_id": insts[0].lib_id, "value": insts[0].value, "pins": rows[:limit],
                       "total": len(rows), **({} if nl else {"note": "netlist unavailable; nets omitted"})},
                      summary=f"{ref}: {len(rows)} pin(s)")
    if query == "nets":
        nl = N.export_netlist(pr)
        allnets = nl["nets"]
        unconn = sum(1 for n in allnets if N.is_unconnected(n))
        out = {}
        for name, nodes in allnets.items():
            if N.is_unconnected(name):
                continue
            if net and not _net_is(name, net):
                continue
            if not _match(filter, name):
                continue
            out[name] = [f"{n['ref']}.{n['pin']}" + (f"({n['pin_name']})" if n['pin_name'] and n['pin_name'] != n['pin'] else "")
                         for n in nodes]
        keys = sorted(out)[:limit]
        multi = sum(1 for v in out.values() if len(v) >= 2)
        d = {"nets": {k: out[k] for k in keys}, "total": len(out), "multi_pin": multi,
             "all_nets": len(allnets), "names": NET_NAMES_NOTE}
        excl = []
        if unconn:
            excl.append(f"{unconn} single-pin 'unconnected-(…)' net(s) KiCad makes for pins on no net "
                        f"(sch_read(query='unconnected') lists those pins)")
        if (net or filter) and len(allnets) - unconn - len(out):
            excl.append(f"{len(allnets) - unconn - len(out)} net(s) not matching " + ("net=" + repr(net) if net else "filter=" + repr(filter)))
        if len(out) > len(keys):
            excl.append(f"{len(out) - len(keys)} more beyond limit={limit} (raise limit or use filter)")
        if excl:
            d["excluded"] = excl
        if len(O.sheet_files(pr)) > 1:
            d["scope"] = "whole hierarchy (every sheet), not just this sheet"
        return Result(d, summary=f"{len(keys)} of {len(allnets)} net(s) shown ({multi} multi-pin"
                      + (f"; {unconn} unconnected-pin nets excluded" if unconn else "") + ")")
    if query == "labels":
        rows = [{"net": l["net"], "kind": l["kind"], "at": l["at"]} for l in sch.labels() if _match(filter, l["net"])]
        return Result({"labels": rows[:limit], "total": len(rows)}, summary=f"{len(rows)} label(s)")
    if query == "unconnected":
        nl = N.export_netlist(pr)
        ctx = O.Ctx(pr, sch)
        rows = []
        single = {}
        for name, nodes in nl["nets"].items():
            if len(nodes) == 1:
                single[(nodes[0]["ref"], nodes[0]["pin"])] = name
        for si in sch.symbols(include_power=False):
            for p in si.pins_world():
                key = (si.ref, p["number"])
                if key not in single:
                    continue
                if ctx.net_at_pin(p["xy"], si)[1] == "no_connect":
                    continue
                if p["type"] == "no_connect":
                    continue
                if not _match(filter, si.ref, p["name"]):
                    continue
                pin = f"{si.ref}.{p['number']}"
                rows.append({"pin": pin, "name": p["name"], "type": p["type"],
                             **({"net": single[key]} if not N.is_unconnected(single[key]) else {}),
                             "fix": f'{{"op":"no_connect","pin":"{pin}"}} or {{"op":"connect","pin":"{pin}","net":"..."}}'})
        return Result({"unconnected": rows[:limit], "total": len(rows)}, summary=f"{len(rows)} unconnected pin(s)")
    # summary
    syms = sch.symbols(include_power=False)
    labels = sch.labels()
    nl = _netlist_safe(pr)
    d = {"file": str(f), "paper": sch.paper, "components": len({s.ref for s in syms}),
         "power_symbols": len([s for s in sch.symbols() if s.is_power]), "labels": len(labels),
         "wires": len(sch.items("wire")), "no_connects": len(sch.items("no_connect")),
         "sheets": [str(x.name) for x in O.sheet_files(pr)]}
    if nl:
        # same three numbers project_summary reports: every KiCad net, those joining ≥2 pins, and the per-pin
        # 'unconnected-(…)' nets KiCad creates for pins on no net
        d["nets"] = len(nl["nets"])
        d["connected_nets"] = sum(1 for v in nl["nets"].values() if len(v) >= 2)
        d["unconnected_pin_nets"] = sum(1 for n in nl["nets"] if N.is_unconnected(n))
        if len(d["sheets"]) > 1:
            d["components_all_sheets"] = len(nl["components"])
            d["note"] = "components/labels/wires count this sheet; nets and with_part_number cover the whole hierarchy"
        try:        # the project's part-number field (LCSC with the default parts provider)
            from ..parts import providers as _PV
            _field = _PV.for_pr(pr)[0] and _PV.part_field(*_PV.for_pr(pr))
        except ToolError:
            _field = "LCSC"
        if _field:
            _n = sum(1 for c in nl['components'].values() if _PV.part_number(c['fields'], _field))
            d["with_part_number"] = f"{_n}/{len(nl['components'])} ({_field})"
    return Result(d, summary=f"{d['components']} components, {d.get('connected_nets', '?')} connected nets "
                  f"({d.get('nets', '?')} incl. single-pin) on {sch.paper}")


def _refkey(r: str):
    import re
    m = re.match(r"([A-Za-z#_]*)(\d*)(.*)", str(r))
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, m.group(3))


@tool("sch_tidy", """Re-draw a schematic sheet so it reads like a hand-drawn professional sheet WITHOUT changing the circuit: titled
functional blocks in signal-flow columns, canonical drawings per idiom (LDO, op-amp loop, crystal, LED chain), support
parts wired at the pin they serve, supplies up and grounds down, design notes, smallest paper that fits. Nothing is
written unless KiCad's netlist of the WHOLE project is identical before and after and ERC reports nothing new. A sheet
wired by hand is left alone (`style_used: 'kept'`) unless force=true; sheets with sheet symbols, buses or several
instances are refused. style 'pro2' (default); when the checks refuse it a simpler style is drawn: `style_used` says
which, `fallback` says why.
Example: sch_tidy() · sch_tidy(sheet='power.kicad_sch', dry_run=true) · sch_tidy(blocks=[{"id":"U2","title":"MCU"}])""",
      {"project": PROJECT, "sheet": SHEET,
       "dry_run": {"type": "boolean"},
       "while_open": WHILE_OPEN,
       "preview": {"type": "boolean", "description": "Attach the re-laid-out sheet image (default true)."},
       "style": {"type": "string", "enum": ["pro", "pro2", "labels"],
                 "description": "pro2 (default): idiom templates, flow columns, notes; pro: blocks + wires; "
                                "labels: every connection a pin label."},
       "symfix": {"type": "string", "enum": ["off", "hint", "auto"],
                  "description": "pro2: overprinting pin texts. hint (default) reports them; auto redraws the "
                                 "sheet's copy of Copper-imported symbols only (never KiCad stock); off: silent."},
       "route": {"type": "string", "enum": ["auto", "off"], "description": "off: no routed wires inside blocks."},
       "blocks": {"type": "array", "items": {"type": "object"},
                  "description": "Block hints: {id: anchor ref, title?, note?, members? [refs]}."},
       "frames": {"type": "boolean", "description": "Block frames + titles (default true)."},
       "title_block": {"type": "object", "description": "title, date, rev, company, comment1..9 (title/date auto-filled when empty)."},
       "paper": {"type": "string", "description": "A4, A3 ... (default: smallest that fits)."},
       "force": {"type": "boolean", "description": "Also re-lay a hand-wired sheet."}},
      title="Schematic: tidy layout", destructive=True, group="schematic",
      help="""## What the drawing looks like (style pro2, the default)
Titled functional blocks (power input / regulator / MCU / connectors) in signal-flow columns left to right; canonical
drawings per idiom (LDO / boost line, op-amp loop, crystal, LED chain, divider); support parts wired at the pin they
serve (pull-ups above, pull-downs and RC caps below, series parts and LED chains in line); decoupling caps at their
IC; supplies pointing up and grounds down; names only where a net leaves its block; generated design notes (LED
currents, FB-divider Vout, RC corners) and the rails with their sources in the title block; smallest paper that fits.

## What is proven before anything is written
KiCad's own netlist of the WHOLE project is identical before and after (every component field, net name, pin);
PWR_FLAGs, hierarchical / global labels and no-connect flags are still on the same nets; ERC reports nothing new.
`connectivity` in the result says what was proven. Refused cleanly (nothing written): sheets containing hierarchical
sheet symbols, buses, or used by several sheet instances.

## Styles and the fallback ladder
`pro2` (default) -> `pro2-safe` (no routed wires) -> `pro` (the older block engine) -> `pro-plain` -> `labels` (every
connection a pin label). Each candidate goes through the same checks; the first that passes is written.
`style_used` names it. When it is not the style asked for, `fallback` is one sentence with the reason the better ones
were refused and `fallbacks` lists every refused style. style='pro' and style='labels' ask for the older styles.
`style_used: 'kept'` = a hand-wired sheet was left alone (force=true re-lays it); `'unchanged'` = already laid out.

## Other result fields
`notes` (design notes written), `texts` (Reference / Value texts re-seated), `hard` (rule violations left on the
sheet), `readability` before / after, `blocks`, `symfix` / `hint` (pin texts of a library symbol that overprint).

## symfix
hint (default) reports symbols whose pin texts overprint (stacked pins, numbers wider than the pin). auto redraws the
sheet's embedded copy of a Copper-imported (copper-lcsc) symbol - same pins, names and types, only positions / name
offset - when the gate and ERC accept it. KiCad stock and user library symbols are never redrawn; they get the hint.

## Examples
sch_tidy() · sch_tidy(style='pro') · sch_tidy(sheet='power.kicad_sch', dry_run=true) · sch_tidy(force=true, symfix='auto')
""")
def sch_tidy(project=None, sheet=None, dry_run=False, while_open="refuse", preview=True, style="pro2", route="auto",
             blocks=None, frames=True, title_block=None, paper=None, force=False, symfix="hint"):
    pr = P.resolve(project)
    from ..sch.pro import tool as SP
    if not dry_run:
        _erc_baseline(pr)
    r = SP.tidy(pr, sheet, dry_run=dry_run, while_open=while_open, style=style, route_mode=route, blocks=blocks,
                frames=frames, title_block=title_block, paper=paper, force=force, symfix=symfix)
    images = []
    summ = SP.summary(r)
    wrote = bool(r.get("revision")) and not dry_run
    if wrote:
        try:
            erc = _erc(pr)
        except ToolError as e:
            erc = {"error": e.message}
        r["verify"] = {"erc": erc}
        if "errors" in erc:
            summ += f"; ERC {erc['errors']} error(s), {erc.get('warnings', 0)} warning(s)" + (
                f" ({erc['new']} new)" if erc.get("new") else "")
    if preview and wrote:
        from pathlib import Path
        f, _ = O.sheet_file(pr, sheet)
        try:
            images = _preview(pr, Path(f), None)
        except ToolError as e:
            r["preview_error"] = e.message
    return Result(r, images, summ)
