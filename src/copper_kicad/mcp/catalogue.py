"""What tools/list says: the same tools, in fewer bytes.

Every client loads the whole catalogue up front and re-reads it on every turn, so each byte here is paid
many times. ``Tool.describe`` hands its plain description to :func:`shape`, which

* gives shared parameters (``project``, ``while_open``, ``render`` / ``preview``) ONE short description,
  identical in every tool;
* keeps the annotation hints that carry information and drops the ones that restate a default
  (``title`` is already top-level; ``readOnlyHint`` / ``idempotentHint`` only when true; ``destructiveHint``
  and ``openWorldHint`` are always stated for tools that write, because their protocol default is true);
* swaps a long description for a short one from :data:`SLIM`. The owner's full text is not lost: it moves
  to the help page (``copper://help/<tool>`` and ``copper_help(tool=…)``), next to the tool's worked examples.

Rule for what stays in the catalogue: text that prevents a wrong FIRST call (what the tool is for, the
argument shapes, one example). Reference material (edge cases, second examples, rationale) is help.

A slim text is tied to the tool's argument names (and, for sch_apply, its op names). When the owner adds or
renames an argument or an op, the slim text is stale: :func:`shape` then falls back to the owner's full
description instead of hiding the change, and :func:`stale` lists it so the slim text can be re-written.
``COPPER_CATALOGUE=full`` switches the whole module off.
"""
from __future__ import annotations

import os
import re

from . import compact

SHARED_PARAMS = {
    "project": "project path (default: active)",
    "while_open": "refuse (default) | revert",
}
IMAGE_PARAM = {"final": "attach a PNG (default false)", "never": "attach a PNG (default false)",
               "auto": "attach a PNG (default true)"}

# The tools a client should keep loaded when it defers the rest (a hint one vendor's clients read and others
# ignore; nothing depends on it): the short path, plus looking and help. Overrides the tools' own core= flags.
CORE = ("project_create", "sch_apply", "sch_read", "board_build", "verify", "render", "copper_help", "fab_export")

# name -> (signature, slim description). signature = sorted argument names joined by "," (see _sig).
SLIM: dict[str, tuple[str, str]] = {}


def slim(name: str, sig: str, text: str) -> None:
    SLIM[name] = (sig, text.strip())


def full_mode() -> bool:
    return os.environ.get("COPPER_CATALOGUE", "").strip().lower() == "full"


_OP_LINE = re.compile(r"^- (\w+):", re.M)


def _sig(tool) -> str:
    names = sorted(tool.schema.get("properties", {}))
    if tool.name == "sch_apply":
        names += ["op:" + o for o in sorted(set(_OP_LINE.findall(tool.description)))]
    return ",".join(names)


def slim_for(tool) -> str | None:
    """The slim description for a tool, or None when there is none or it no longer matches the tool."""
    if full_mode():
        return None
    entry = SLIM.get(tool.name)
    if not entry or entry[0] != _sig(tool):
        return None
    return entry[1]


def stale() -> dict:
    """Slim texts whose tool changed its arguments since they were written: {tool: current signature}."""
    from . import registry
    return {n: _sig(registry.TOOLS[n]) for n, (sig, _t) in SLIM.items()
            if n in registry.TOOLS and sig != _sig(registry.TOOLS[n])}


def shape(tool, description: str) -> dict:
    """The tools/list entry for one tool."""
    desc = description
    schema = tool.schema
    if not full_mode():
        s = slim_for(tool)
        if s:
            desc = s
        props = {}
        mode = compact.image_mode() if compact.enabled() else "auto"
        for k, v in schema.get("properties", {}).items():
            if k in SHARED_PARAMS and isinstance(v, dict) and v.get("type") == "string":
                v = dict(v, description=SHARED_PARAMS[k])
            elif k in ("render", "preview") and isinstance(v, dict) and v.get("type") == "boolean" \
                    and tool.name not in compact.IMAGE_EXEMPT:
                v = dict(v, description=IMAGE_PARAM[mode])
            props[k] = v
        schema = dict(schema, properties=props)
    title = tool.title or tool.name.replace("_", " ").title()
    if full_mode():
        ann = {"title": title, "readOnlyHint": tool.read_only, "destructiveHint": tool.destructive,
               "idempotentHint": tool.idempotent, "openWorldHint": tool.open_world}
    else:
        ann = {"readOnlyHint": True} if tool.read_only else {"destructiveHint": bool(tool.destructive)}
        if tool.idempotent:
            ann["idempotentHint"] = True
        ann["openWorldHint"] = bool(tool.open_world)
    d = {"name": tool.name, "title": title, "description": desc, "inputSchema": schema, "annotations": ann}
    if (tool.core if full_mode() else tool.name in CORE):
        d["_meta"] = {"anthropic/alwaysLoad": True}
    return d


def moved_to_help(tool) -> str | None:
    """The owner's full description when tools/list shows a slim one (so the help page must carry it)."""
    return tool.description.strip() if slim_for(tool) else None


# ── slim texts ────────────────────────────────────────────────────────────
# Written against the tools as of 2026-11-09. Each keeps: purpose, argument shapes, one example.

slim("sch_apply",
     "dry_run,layout,ops,preview,project,sheet,verify,while_open,op:connect,op:delete,op:disconnect,op:label,op:move,"
     "op:no_connect,op:place,op:power,op:pwr_flag,op:rename_net,op:set_field,op:swap,op:text,op:wire",
     """Edit the schematic with ONE atomic batch of ops (all succeed or nothing is written). Put a whole sub-circuit, or the
whole design, in a single call. Creates the .kicad_sch if missing. One call edits one sheet.
Pins are 'REF.PIN', PIN = number or name ('U1.PA9', 'J1.VBUS', 'R1.2'); a name on several pins connects them all.
Never compute coordinates. In a block prefer `wire` pin to pin; `connect` by NET NAME (label or power symbol on
the pin) for power, off-sheet nets, awkward runs. New parts take an 'id' (one word) used in pins ('led.A');
`refs` maps id to reference. Parts without
'at' are laid out automatically, and a sheet only Copper has arranged is re-drawn as titled functional blocks
(layout='keep' never does that; nothing is redrawn unless the netlist is proven identical).
ops (objects with "op"):
- place: lib_id | lcsc "C25804" | part (+provider) | spec "10k 0402" (resolved by the parts providers); id, ref, value, footprint,
  fields {LCSC:..}, near (ref/id), group mcu|power|connector|passive, at [x,y], rot, mirror, unit, dnp
- connect: pin | pins [..], net (GND, +3V3, VBUS: power symbols; kind:'global' between sheets)
- power: pin(s), net · no_connect: pin | pins · pwr_flag: net · wire: from, to | points · label: net + pin | at
- set_field: ref, field, value | fields · move: ref, at | by, rot, mirror · swap: ref + lib_id | lcsc | part | footprint
- delete: ref | label | wire_at [x,y] | dangling:true · disconnect: pin(s) · rename_net: from, to · text: text, at
The result has what changed, the netlist check of every connection and KiCad's ERC with a `fix` per issue:
no verify call.
Example: ops=[{"op":"place","lib_id":"Regulator_Linear:AMS1117-3.3","id":"ldo"},{"op":"place","spec":"10uF 0805","id":"cin"},
{"op":"connect","pins":["ldo.VI","cin.1"],"net":"VBUS"},{"op":"connect","pins":["ldo.GND","cin.2"],"net":"GND"}]""")

slim("sch_read", "filter,limit,net,project,query,ref,sheet",
     """Read the schematic compactly: before editing an existing design, and to find exact pin names.
query: summary (default, counts) | components (ref, value, lib_id, footprint, LCSC, position) | pins (every pin of `ref`
with number, name, type and its net) | nets (net -> ['REF.PIN', ...], KiCad's full net names) | labels |
unconnected (pins ERC will flag, each with a ready fix). filter = case-insensitive substring.
Example: sch_read(query='pins', ref='U1') · sch_read(query='nets', filter='USB')""")

slim("sch_tidy", "blocks,dry_run,force,frames,paper,preview,project,route,sheet,style,title_block,while_open",
     """Re-draw a schematic sheet so it reads like a hand-drawn professional sheet WITHOUT changing the circuit: titled
functional blocks in signal-flow order, support parts wired at the pin they serve, decoupling caps at their IC,
supplies up and grounds down, smallest paper that fits, title block filled. Nothing is written unless KiCad's netlist
of the whole project is identical before and after and ERC reports nothing new. A sheet wired by hand is left alone
unless force=true; sheets with sheet symbols, buses or several instances are refused.
blocks=[{id: anchor ref, title, note, members}] are optional hints; style 'pro' (default) | 'labels'.
Example: sch_tidy() · sch_tidy(sheet='power.kicad_sch', dry_run=true) · sch_tidy(blocks=[{"id":"U2","title":"MCU"}])""")

slim("pcb_read", "drc,limit,net,project,refs,source,what",
     """Read the PCB. With no arguments: a digest (footprint, track, via and unrouted counts, outline size, layers).
what: any of summary | footprints (ref, value, position mm, rotation, side) | nets | pads | tracks (every segment
with its uuid; combine with net) | widths (per-net copper summary) | zones | drawings.
refs: only these footprints. net: only that net. drc=true also runs KiCad DRC. source='live' reads the board open
in the running KiCad editor.
Example: pcb_read(what=["footprints"], refs=["U1"]) · pcb_read(what=["nets"], net="GND")""")

slim("pcb_sync", "project,remove_extra,render,update_footprints,while_open",
     """Update the PCB from the schematic (KiCad's "Update PCB from Schematic"); creates the .kicad_pcb if missing. Adds
missing footprints beside the outline, updates fields, swaps changed footprints in place, assigns every pad's net
and keeps existing positions. Copper follows renamed nets; copper left from deleted parts is reported.
remove_extra=true deletes footprints (and their orphaned copper) that are no longer in the schematic.
Refuses if the schematic has unannotated or duplicate references. board_build() runs this and the following steps.
Example: pcb_sync()""")

slim("pcb_setup",
     "auto_size,default_track,height,hole_inset,hole_net,holes,layers,origin,outline_axis,outline_clearance,"
     "outline_dry_run,outline_file,outline_flip,outline_holes,outline_keep_position,outline_layer,outline_mode,"
     "outline_pick,outline_units,polygon,power_nets,power_track,project,radius,render,rules,silk_text,via,while_open,width",
     """Board setup: outline, corner mounting holes, copper layer count, fab design rules + net classes, silkscreen text
size. Only what you pass is changed. Outline, one of: width x height mm rectangle (optional radius; REPLACES the
existing outline), polygon, auto_size=true (smallest comfortable rectangle for the parts, unrouted boards only), or
outline_file='case.step' (STEP, DXF, SVG, STL; 'auto' = the one file in mechanical/): the board body or an
enclosure's cavity with its arcs, cutouts and mounting holes; an uncertain pick lists candidates
(outline_pick), outline_dry_run=true only analyses. First setup applies the design rules of the
project's fab profile (rules='fab'). power_nets adds nets / patterns to the Power net class.
Examples: pcb_setup(width=40, height=30) · pcb_setup(width=50, height=50, radius=2, holes='M3', layers=4) ·
pcb_setup(outline_file='mechanical/case.step', outline_dry_run=true) · pcb_setup(power_nets=['VBAT','SW'], power_track=0.8)""")

slim("pcb_place",
     "auto,candidates,clear_tracks,dry_run,edge_clearance,elegance,keep,moves,overhang,project,refs,render,spacing,"
     "tidy_silk,while_open",
     """Place footprints. auto=true: connectivity-driven placement inside the outline (connectors on the edges, decoupling
caps at their IC pin, crystal at the MCU, RF antennas on an edge), courtyard-legal on a 0.5 mm grid; several
candidates are scored for elegance and the best is written. Locked parts and keep=[..] stay put; refs=[..] places
only those around what is there. On a routed board, placing everything is refused unless clear_tracks=true.
overhang={"J1": 1.2} lets a side-entry connector stick out past the edge.
moves=[{ref, x, y, rot, side, lock}]: exact positions in mm (board coordinates, as pcb_read), applied after auto
placement. Returns the ratsnest length before/after and the elegance score.
Examples: pcb_place(auto=true) · pcb_place(moves=[{"ref":"J1","x":101,"y":115,"rot":90}])""")

slim("pcb_route",
     "clear_existing,finish,ignore_nets,keep_worse,mode,nets,passes,polish,project,render,signal_layers,timeout,tracks,"
     "while_open",
     """Route the board. mode='auto' (default without tracks): autoroute every unrouted connection with Freerouting (Java and
the router are downloaded once into $COPPER_HOME/tools on first use). Existing tracks and vias are kept; a result
that is worse than before is reverted; the new tracks are then polished (polish=false skips that). Can take minutes
on dense boards. 2 layers: pcb_route(ignore_nets=["GND"]) then pcb_pour(net="GND", stitch=true). 4 layers: pour the
inner planes first.
tracks=[{"from":"U1.3","to":"C3.1","width":0.3,"layer":"F.Cu","waypoints":[[x,y]]}]: explicit connections; refuses to
join different nets or to cross other nets' copper.
pcb_route_guided is the built-in deterministic router (no download); board_build() picks for you.
Examples: pcb_route() · pcb_route(tracks=[{"from":"J1.1","to":"U1.3","width":0.5}])""")

slim("pcb_pour",
     "clearance,fill_only,layers,min_width,net,pad_connection,priority,project,remove,render,stitch,stitch_pitch,"
     "thermal_gap,thermal_spoke,while_open",
     """Copper pour: a zone for one net (default GND) over the whole outline on the chosen layers (default F.Cu + B.Cu), then
fill every zone and re-run DRC. Pads the fill cannot reach get a solid connection or a via; what still cannot join
is reported with its position. stitch=true adds stitching vias. A pour on an inner layer is a plane.
Re-running replaces the net's zone; remove=true deletes it; fill_only=true just refills after moving or routing.
Examples: pcb_pour() · pcb_pour(net="GND", stitch=true) · pcb_pour(net="+3V3", layers=["In2.Cu"]) · pcb_pour(fill_only=true)""")

slim("pcb_route_plan", "heatmap,nets,project,ripup",
     """Analyse the board for routing (read-only) and return what you need to write a plan for pcb_route_guided: board and
rules, a net table (kind, pads, unrouted, class), pours, keep-outs, a congestion map with hot spots, and
`suggested_plan` (the built-in heuristics as an editable plan) with `plan_schema`. Skip it to route with defaults.
heatmap=true attaches the congestion picture. ripup='all' analyses as if all unlocked tracks were removed.
Example: pcb_route_plan() -> edit suggested_plan -> pcb_route_guided(plan=...)""")

slim("pcb_route_guided", "compare,detail,dry_run,nets,plan,project,render,ripup,while_open",
     """Built-in deterministic router (no download): same plan + same board gives the same copper; KiCad's DRC judges the
result and copper it rejects is removed again. No plan = built-in heuristics.
plan (all optional, mm, net globs ok): order [nets], global {grid, via_cost, style, layers, time_budget_s,
engine: "v2" (default) | "guided"},
nets {"<name>": {priority, width, layers, avoid:[[x0,y0,x1,y1]], corridor:[[x,y],..], max_vias, skip}},
diff_pairs [{p, n, gap, width}], regions [{rect, cost | forbid:true, layers, nets}]. Unknown fields are rejected.
ripup: none (default, keep existing tracks) | nets (re-route `nets`) | all. dry_run / compare never write.
Returns routed/total, failed [{net, from, to, reason, where, blocked_by}], per-net length and vias, DRC counts,
findings with fixes. A net with a pour only gets the pads the pour misses connected; ground without one is left
for pcb_pour. Routing nothing leaves the board untouched.
Examples: pcb_route_guided() · pcb_route_guided(ripup="all", plan={"order":["/USB_D?","*"]})""")

slim("focus", "action,box,document,launch,near,project,radius,render,sheet,side,source,wait",
     """What the user is pointing at. Call it when they say "this part", "here", "that corner" instead of guessing.
action: get (default; the latest click / box in the Copper Canvas or selection in KiCad's PCB editor; wait=N waits up
to N s for their next one) | resolve (no human: what is inside box=[x0,y0,x1,y1] or nearest near=[x,y] mm on
document='schematic' | 'pcb') | open (start the Copper Canvas viewer and return its URL) | clear.
Returns focus {document, mode, box_mm | point_mm, refs, pins, nets, age_s}, items with value / footprint / position,
and a cropped PNG with the focus outlined. Empty refs with a box or point means the user means that AREA.
Use refs / pins in sch_apply, pcb_place, pcb_route; box_mm in render(crop=...).
Examples: focus() · focus(wait=60) · focus(action='resolve', document='pcb', box=[100,100,120,115])""")

slim("parts_search", "basic_only,category,in_stock,limit,live,min_stock,package,provider,query",
     """Find parts. The project's parts providers are asked in order and the first with results answers (`provider` in the
result says which; 'local' = the user's own catalogue file, 'kicad' = generic library parts). Results are ranked by
spec match, then by the provider's part class (parts without a setup fee first), stock and price.
query: a value spec ('10k 0603', '100nF 16V 0402 X7R', 'LED red 0603', '12MHz crystal', 'LDO 3.3V SOT-223'), an MPN
('AMS1117-3.3') or a category phrase ('USB-C 16P', 'Header 1x4 2.54mm').
Each result: provider, id (the provider's part number), mpn, package, stock, price, match ('partial' = not every
term confirmed) and `next` = the exact sch_apply op. Passives by spec and known part numbers need no search:
sch_apply place resolves and imports them itself.
Example: parts_search(query='USB-C 16P', limit=5)""")

slim("parts_source_bom", "allow_extended,basic_only,boards,live,overwrite,project,provider,while_open,write",
     """BOM sourcing pass: give every schematic component (all sheets) a part from the project's parts providers. Run it once
the schematic has values and footprints, before the fab pack. Per component it decides from an existing MPN field,
then value + footprint package, then an MPN in the footprint name; existing part numbers are kept (overwrite=true
re-picks) and checked against stock and package. Writes the provider's part-number field plus MPN / Manufacturer
(write=false reports only). Returns summary (sourced %, parts by class, setup-fee and cost estimates), `lines`
(decided), `manual` (could not decide: reason + options) and `stock_risks`.
Example: parts_source_bom(write=false) to preview, then parts_source_bom()""")

slim("parts_import", "lcsc,part,prefer,preview,project,provider,with_3d",
     """Make a provider's part usable in this project: returns the `lib_id` + `footprint` for sch_apply place. sch_apply
{"op":"place","part":...} calls this by itself; use it directly to inspect a part first or to force a source.
prefer='auto' (default) uses KiCad's stock symbol + footprint when it verifiably fits, else converts the provider's
symbol, footprint and 3D model into project libraries (registered in the project's lib tables); 'easyeda' forces the
provider's drawing, 'stock' fails if there is no stock fit. Footprint pads are checked against symbol pins and
KiCad itself loads the result. preview=true returns pictures of the symbol and footprint.
Example: parts_import(part='C165948')""")

slim("fab_export", "assembly,layers,out_dir,profile,project,waive",
     """Produce the order pack for a PCB fab in <project>/copper-out/fab/: Gerbers + drill zipped, a BOM CSV and a placement
CSV, in the format of a fab PROFILE (omit `profile` for the project's configured one; profile='list' shows every
available profile with its rules and exports nothing; 'generic' suits any fab).
It first runs ERC + DRC and REFUSES (FAB_BLOCKED, each blocker with its fix) on ERC errors, DRC errors, unrouted nets,
parity issues, no outline, an empty board and, where the profile needs them, parts without a part number.
waive=[codes] ONLY for what the user consciously accepts: 'erc','drc','unrouted','parity','parts','outline','empty',
'all'. assembly=false skips BOM + placement (bare boards). Returns paths, board facts, fee estimate and order notes.
Examples: fab_export() · fab_export(profile='generic') · fab_export(assembly=false)""")

slim("library", "action,dry_run,filters,fp_id,kind,lib,lib_id,limit,pins,project,query",
     """Search and inspect the KiCad symbol and footprint libraries this machine has (global + project tables): find a valid
lib_id / footprint before sch_apply place, and read exact pin names.
action: search_symbols (query = part number, name or keywords) | search_footprints (query; filters = footprint globs) |
symbol_info (lib_id -> pins with number, name, type, unit; default footprint) | footprint_info (fp_id -> pads,
courtyard, 3D model) | list (libraries in the active tables) | repair (fix "library not found": rebuilds missing or
stale global tables; dry_run defaults to TRUE).
Examples: library(action='search_symbols', query='USB C receptacle') · library(action='symbol_info', lib_id='Device:R')""")

slim("project_create", "board_size,company,dir,fab,name,rev,template,title",
     """Create a new KiCad project (.kicad_pro, .kicad_sch, .kicad_pcb) and make it active. Refuses to overwrite an existing
project. template: '2layer' (default) | '4layer' | 'blank' (KiCad defaults, no fab rules). fab: the fab profile whose
design rules the project gets (default: the configured one). board_size: [width_mm, height_mm] draws a rectangular
outline. dir: the parent folder (the project goes in dir/<name>), or the project folder itself when it ends in
<name>; default Documents/Copper/<name>. KiCad re-loads the files (ERC + DRC) before success is reported.
Example: project_create(name='usb-sensor', board_size=[50,30])""")
