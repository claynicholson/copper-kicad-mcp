"""MCP resources + prompts for Copper.

copper://help/<tool> serves the long examples tool modules register with
``tool(..., help=…)`` / ``registry.add_help`` (see mcp/registry.py) so they
do not have to live in tools/list.

dispatch(method, params) handles resources/list, resources/templates/list,
resources/read, prompts/list, prompts/get and completion/complete.
Resources are read-only views of the active project (or ?path=...).
"""
from __future__ import annotations

import json
import urllib.parse

from . import DISPLAY_NAME, __version__
from . import project as P
from .mcp.registry import ToolError

GUIDE = f"""# {DISPLAY_NAME} {__version__} — how to drive KiCad with Copper

Copper is an MCP server: it exposes tools, it never calls a language model, and it works the same with any
MCP client and any model. Nothing below assumes a parts vendor or a PCB fab either: both are configuration.

## The loop
1. `copper_status` → KiCad found? project active? which parts providers and fab profile (`config`)? Then
   `project_create(name, board_size=[w,h])` (add `fab='<profile>'` for another fab) or `project_open(path)`.
2. `project_summary` to see what exists.
3. Parts: `parts_search` asks the project's parts providers in order and says which one answered → `parts_import(part=…)`
   when KiCad has no fitting symbol/footprint. Placing by spec (`{{"op":"place","spec":"10k 0603"}}`) does both for you.
4. Schematic: `sch_apply` with BIG batches. Connect pins to nets by NAME — `{{"op":"connect","pin":"U1.PA9","net":"USB_DP"}}`.
   Power nets (GND, +3V3, VBUS …) become power symbols automatically. Mark unused pins `{{"op":"no_connect","pin":"U1.7"}}`.
5. Every `sch_apply` result already carries KiCad's ERC and the netlist check of what you asked for, each finding with a `fix` that
   names the exact next call: a separate `verify()` after every batch is not needed (it costs a whole extra turn).
6. `design_review()` — decoupling, pull-ups, crystal caps, USB-C CC 5.1k, ESD, regulator caps, enable pins, LED resistors, part sourcing.
7. `render(target='schematic')` when you need to LOOK (pictures are attached only on request); `crop=[x0,y0,x1,y1]` (mm) zooms on a finding's `at`.
8. Board: `board_build(width, height)` does sync, outline (with the fab profile's design rules), placement, routing, pour
   and DRC in ONE call and returns one report with a small picture (call it again if it returns `pending`). The single
   steps (`pcb_sync`, `pcb_setup`, `pcb_place`, `pcb_route`, `pcb_pour`) are for fixing what it reports.
9. `fab_export()` → Gerbers+drill zip, BOM and placement file in the format of the project's fab profile. It refuses while
   whatever that profile says blocks an order remains (ERC/DRC errors, unrouted nets, and for profiles that need them, parts
   without a part number); waive=[…] only for things the user has consciously accepted.

## Parts providers and fab profiles
* `parts.providers` in the Copper config is an ordered list. Built in: `lcsc` (LCSC parts for JLCPCB assembly: offline Basic-parts
  table, live stock, EasyEDA symbol/footprint/3D import — the default), `local` (the user's own catalogue file, CSV or JSON, fully
  offline), `kicad` (generic library parts: value + footprint only). Third-party providers load from Python entry points or a path
  in the config. Every tool takes `provider=` to ask one directly.
* `fab.profile` names a fab profile: `jlcpcb` (default), `pcbway`, `oshpark`, `generic` (Gerber X2 + Excellon + KiCad position file +
  plain BOM), or a JSON file the user added. The profile holds design-rule minimums, file naming, BOM/placement columns, rotation
  corrections and what blocks an order. `fab_export(profile='list')` shows them.
* Config layers: built-in defaults < `$COPPER_HOME/config.json` < `<project>/.copper/config.json` < the call's own arguments.
* Whatever the provider, a placed part also carries `MPN` and `Manufacturer` when known, so a design can be re-sourced elsewhere.

## Conventions
* Tool descriptions are short on purpose. `copper_help(tool='sch_apply')` (or the resource `copper://help/sch_apply`) returns the
  worked examples and op catalogue for a tool; read it before the first non-trivial call.
* Pin refs are `REF.PIN` (number or name): `U1.12`, `U1.PA9`, `J1.VBUS`, `R3.2`. Coordinates are mm, 1.27 mm grid.
* With the defaults (lcsc + jlcpcb) every part carries an `LCSC` field (C-number); prefer JLC Basic parts, each unique Extended part
  costs ≈ $3 per order; rules are track/space ≥ 0.127 mm, drill ≥ 0.3 mm, via 0.6/0.3 mm, copper ≥ 0.3 mm from the edge.
  Other profiles have their own numbers: read them from `fab_export(profile='list')`, never from memory.
* Decoupling: 100 nF per IC power pin + 10 µF bulk per rail; I²C 4.7k pull-ups; USB-C sink: 5.1k on CC1 and CC2 separately; ESD on USB D+/D-.
* Mutating tools snapshot first — `checkpoint(action='diff'|'restore')` undoes anything.
* Direct edits to .kicad_sch/.kicad_pcb text are allowed for anything the tools don't cover — run `verify` afterwards.
* If the schematic is open in KiCad, close it (KiCad 9/10 have no live schematic API) or use File ▸ Revert after Copper edits.
"""

RESOURCES = [
    {"uri": "copper://status", "name": "status", "title": "Copper status",
     "description": "KiCad installs, capabilities, active project, parts providers + fab profile and next step (JSON)",
     "mimeType": "application/json"},
    {"uri": "copper://guide", "name": "guide", "title": "How to use Copper",
     "description": "Workflow, conventions, parts providers and fab profiles for driving KiCad through Copper", "mimeType": "text/markdown"},
    {"uri": "copper://help", "name": "help", "title": "Tool help index",
     "description": "Every tool with a one-line summary; read copper://help/<tool> for its long examples (JSON)",
     "mimeType": "application/json"},
    {"uri": "copper://session/credits", "name": "session-credits", "title": "Session credit meter",
     "description": "What this session's Copper calls cost: calls, result bytes, images and estimated tokens per tool (JSON)",
     "mimeType": "application/json"},
    {"uri": "copper://project/summary", "name": "project-summary", "title": "Active project summary",
     "description": "Sheets, components, nets, rails, sourced %, board, config, last ERC/DRC (JSON)", "mimeType": "application/json"},
    {"uri": "copper://project/bom", "name": "project-bom", "title": "Active project BOM",
     "description": "Grouped bill of materials with part numbers, MPN and manufacturer (JSON)", "mimeType": "application/json"},
    {"uri": "copper://project/netlist", "name": "project-netlist", "title": "Active project netlist",
     "description": "Compact netlist: one line per net, REF.PIN(name) members (text)", "mimeType": "text/plain"},
    {"uri": "copper://project/erc", "name": "project-erc", "title": "Active project ERC",
     "description": "Latest ERC counts and findings with fix hints (JSON)", "mimeType": "application/json"},
]

TEMPLATES = [
    {"uriTemplate": "copper://project/{section}{?path}", "name": "project-section",
     "title": "Project section for any project",
     "description": "section = summary | bom | netlist | erc | drc; path = project folder or .kicad_pro (default: active project)",
     "mimeType": "application/json"},
    {"uriTemplate": "copper://help/{tool}", "name": "tool-help", "title": "Help for one tool",
     "description": "Worked examples, op catalogues and argument details for a tool (kept out of tools/list to keep it small)",
     "mimeType": "text/markdown"},
]


# ── prompts ──────────────────────────────────────────────────────────────
PROMPTS = {
    "design_board": {
        "title": "Design a board from requirements",
        "description": "Full loop: requirements → sourced schematic → verified PCB → the fab's order pack",
        "arguments": [{"name": "requirements", "description": "What the board must do (interfaces, power, size, constraints)", "required": True},
                      {"name": "name", "description": "Project name (optional)", "required": False}],
    },
    "source_bom": {
        "title": "Source the BOM",
        "description": "Give every part a part number from the project's parts providers and report cost/stock risks",
        "arguments": [],
    },
    "review_design": {
        "title": "Review the design",
        "description": "ERC, DRC and engineering review, then fix what is safe to fix and report the rest",
        "arguments": [{"name": "focus", "description": "Optional focus: power, usb, mcu, layout …", "required": False}],
    },
    "release_to_fab": {
        "title": "Release to the fab",
        "description": "Final checks and the Gerber/BOM/placement pack for the project's fab profile",
        "arguments": [{"name": "profile", "description": "Fab profile (default: the project's configured one)", "required": False}],
    },
}


def _fab_ctx(profile: str | None = None) -> dict:
    """What the prompts say about the active project's vendor setup. Defaults when nothing is configured or readable."""
    ctx = {"fab": "JLCPCB", "profile": "jlcpcb", "providers": ["lcsc"], "field": "LCSC", "lcsc": True, "classes": True}
    try:
        from . import config
        from .parts import providers as PV
        from .review import fab_profiles as FP
        pr = None
        try:
            pr = P.resolve(None)
        except ToolError:
            pr = None
        cfg = config.load(pr)
        prof = FP.active(pr, profile)
        provs = PV.active(cfg)
        field = (prof.get("bom") or {}).get("part_field") or PV.part_field(cfg, provs)
        ctx.update(fab=prof["label"], profile=prof["name"], providers=cfg.providers, field=field,
                   lcsc=cfg.providers[:1] == ["lcsc"], classes=bool((prof.get("part_classes") or {}).get("field")))
    except Exception:  # noqa: BLE001 — a prompt must always render
        pass
    return ctx


def _prompt_text(name: str, args: dict) -> str:
    c = _fab_ctx(args.get("profile") if name == "release_to_fab" else None)
    provs = " → ".join(c["providers"])
    pick = ("Prefer JLC Basic parts (no $3 setup fee) and note the LCSC C-number, package and Basic/Extended class."
            if c["lcsc"] else
            f"Parts come from the providers [{provs}] in that order; note each part's provider, part number and MPN.")
    number = (f"its {c['field']} field" if c["field"] else "a Value and a Footprint (no supplier number is needed)")
    if name == "design_board":
        req = args.get("requirements") or "(ask the user for requirements first)"
        nm = args.get("name") or "a short kebab-case name derived from the requirements"
        return f"""Design a manufacturable KiCad board with Copper and get it ready to order from {c['fab']} (fab profile '{c['profile']}').

REQUIREMENTS:
{req}

Work through these steps, calling the tools yourself and showing me the renders at each milestone:
1. Plan. Restate the requirements as a block list (power input/protection, regulators with currents, MCU/main IC, interfaces, connectors,
   indicators, test points). Pick concrete parts with parts_search for every non-trivial part. {pick}
   Keep passives 0402/0603.
2. copper_status (it shows the active parts providers and fab profile), then project_create(name={nm!r},
   board_size=[w,h] estimated from the requirements).
3. Schematic in big batches with sch_apply: place every part of a block, then connect pins to nets BY NAME (power nets → power symbols).
   Include: 100 nF per IC power pin + 10 µF bulk per rail, regulator input/output caps per datasheet, pull-ups on I²C/EN/RESET,
   5.1k on each USB-C CC pin (sink), ESD on USB data lines, series resistors on LEDs, a PWR_FLAG on every rail fed from a connector,
   and no_connect on every unused pin. Every symbol gets a footprint and {number}.
4. Each sch_apply result carries KiCad's ERC: fix every error using the findings' `fix` hints. render(target='schematic') once the
   schematic is complete, to check readability.
5. design_review() and resolve every error/warning (or explain why it is a false positive).
6. Board: board_build(width, height) runs sync, outline, placement, routing, ground pour and DRC in one call and shows the
   result; fix what its report lists (pcb_place moves, pcb_route(tracks=[...]), pcb_pour) until 0 errors, 0 unrouted.
7. render(target='pcb3d', view='iso') and show me. Then fab_export() and summarise board size, layer count, part count, any fee
   estimate and the order notes it returns (they say what to check on the fab's site).
Never claim a step worked without the tool output proving it."""
    if name == "source_bom":
        if c["lcsc"]:
            how = """2. parts_source_bom() to assign LCSC numbers automatically (Basic > Preferred > Extended, in stock, footprint-compatible).
   For anything it cannot place, use parts_search with the value + package (e.g. '100nF 0402 16V', 'AMS1117-3.3 SOT-223')
   and set it with sch_apply(ops=[{"op":"set_field","ref":"C3","field":"LCSC","value":"C1525"}]).
3. Check every IC/connector's footprint matches the chosen part's package (parts_get shows the package); swap footprints with
   sch_apply {"op":"swap"} or parts_import when needed.
4. For each Extended part, try parts_alternatives(target='C…') for a Basic substitute; keep Extended only when necessary and say why.
5. verify() then design_review(checks=['sourcing','footprints']) and report: sourced %, unique parts, Basic vs Extended counts and the
   estimated Extended setup fee."""
        else:
            how = f"""2. parts_source_bom() to match every component against the providers [{provs}] (MPN field first, then value + package).
   For anything it cannot place, use parts_search with the value + package or the MPN and set the fields it reports with
   sch_apply(ops=[{{"op":"set_field","ref":"C3","fields":{{"{c['field'] or 'MPN'}":"…"}}}}]).
3. Check every IC/connector's footprint matches the chosen part's package (parts_get shows the package); swap footprints with
   sch_apply {{"op":"swap"}} when needed.
4. For out-of-stock parts try parts_alternatives(target=<part number>).
5. verify() then design_review(checks=['sourcing','footprints']) and report: sourced %, unique parts, and which provider each came from."""
        return f"""Make every part on the active project orderable (parts providers: {provs}; fab profile: {c['profile']}).

1. project_summary → note parts without a part number and parts with no footprint.
{how}"""
    if name == "review_design":
        focus = args.get("focus")
        return f"""Review the active KiCad project like a senior hardware engineer{' with a focus on ' + focus if focus else ''}.

1. project_summary for context.
2. verify(what=['erc','drc','review'], limit=60). Group findings by root cause, not by message.
3. render(target='schematic') (and render(target='pcb') if a board exists); look for unreadable or suspicious areas and crop into them.
4. For each issue: severity, why it matters on a real board, and the exact fix (the `fix` hint, adapted). Apply fixes that are
   unambiguous (missing no_connects, PWR_FLAGs, decoupling caps, pull-ups, CC resistors); ask me before changing topology or parts.
5. Re-run verify and report what is fixed and what remains, with checkpoint(action='diff') as the change log."""
    if name == "release_to_fab":
        prof_arg = f"profile='{c['profile']}'" if args.get("profile") else ""
        need = (f"4. Every assembled part must have {number} (parts_source_bom if not)." if c["field"] else
                "4. Every assembled part must have a Value and a Footprint.")
        return f"""Prepare the active project for an order with {c['fab']} (fab profile '{c['profile']}').

1. checkpoint(action='create', label='pre-release').
2. verify(what=['erc','drc']) must show 0 ERC errors, 0 DRC errors, 0 unrouted, 0 parity issues. Fix or explain every remaining warning.
3. design_review() — no errors; warnings either fixed or explicitly accepted by me.
{need}
5. render(target='pcb3d', view='iso') and render(target='pcb', view='bottom') — show me both.
6. fab_export({prof_arg}). Do not waive anything I have not approved. Report the zip/BOM/placement paths, board size, layers,
   min track/drill, part counts, any fee estimate and the order notes, and remind me of whatever those notes say to check on the
   fab's own site before paying."""
    raise ToolError(f"unknown prompt {name}", code="NOT_FOUND")


# ── resource reads ───────────────────────────────────────────────────────
def _project(path: str | None):
    """Resolve without side effects: reading copper://project/…?path=X never switches
    (or sets) the active project."""
    if not path:
        return P.resolve(None)
    return P.resolve(path, activate=False)


def _bom(pr) -> dict:
    """Grouped BOM. `part` is the project's part-number field (LCSC with the default provider); MPN and
    Manufacturer ride along whenever the schematic has them."""
    from .parts import providers as PV
    from .review.common import field_ci, load_netlist, natural_key
    from .review.design_review import _sourcing_context
    src = _sourcing_context(pr)
    field = src["field"]
    nl = load_netlist(pr)
    groups: dict = {}
    for ref, c in nl["components"].items():
        if ref.startswith("#") or c.get("exclude_from_bom"):
            continue
        f = c.get("fields", {})
        num = PV.part_number(f, field)
        key = (c.get("value", ""), c.get("footprint", ""), num, PV.part_number(f, "MPN"),
               field_ci(f, "Manufacturer", "Mfr", "Mfg"), bool(c.get("dnp")))
        groups.setdefault(key, []).append(ref)
    rows = []
    for k, v in groups.items():
        row = {"refs": sorted(v, key=natural_key), "qty": len(v), "value": k[0], "footprint": k[1], "part": k[2] or None,
               "mpn": k[3] or None, "manufacturer": k[4] or None, "dnp": k[5]}
        rows.append({a: b for a, b in row.items() if b is not None or a == "part"})
    rows.sort(key=lambda r: natural_key(r["refs"][0]))
    return {"project": pr.name, "part_field": field, "lines": len(rows), "parts": sum(r["qty"] for r in rows),
            "unsourced": sum(r["qty"] for r in rows if not r["part"] and not r["dnp"]) if src["required"] else 0, "bom": rows}


def _netlist_text(pr) -> str:
    from .review.common import load_netlist, natural_key
    nl = load_netlist(pr)
    lines = [f"# {pr.name}: {len(nl['components'])} components, {len(nl['nets'])} nets"]
    for name in sorted(nl["nets"], key=natural_key):
        nodes = nl["nets"][name]
        mem = " ".join(f"{n['ref']}.{n['pin']}" + (f"({n['pin_name']})" if n.get("pin_name") and n["pin_name"] != n["pin"] else "")
                       for n in sorted(nodes, key=lambda n: natural_key(n["ref"])))
        lines.append(f"{name}: {mem}")
    return "\n".join(lines)


def _erc(pr) -> dict:
    from .review.common import load_state
    from .project import revision
    st = load_state(pr).get("erc")
    if st and st.get("revision") == revision(pr.sch):
        return {"source": "last verify", **{k: st.get(k) for k in ("ts", "errors", "warnings", "top")}}
    from .review.verify import erc_brief
    return {"source": "fresh run", **erc_brief(pr, limit=25)}


def _drc(pr) -> dict:
    from .review.common import load_state
    from .project import revision
    st = load_state(pr).get("drc")
    if st and st.get("revision") == revision(pr.pcb):
        return {"source": "last verify", **{k: st.get(k) for k in ("ts", "errors", "warnings", "unconnected", "parity", "top")}}
    return {"source": "none", "note": "no current DRC result — call verify(what=['drc'])"}


def read(uri: str) -> dict:
    u = urllib.parse.urlparse(uri)
    q = urllib.parse.parse_qs(u.query)
    path = (q.get("path") or [None])[0]
    if u.scheme != "copper":
        raise ToolError(f"unknown resource {uri}", code="NOT_FOUND")
    where = (u.netloc + u.path).strip("/")
    if where == "guide":
        return {"uri": uri, "mimeType": "text/markdown", "text": GUIDE}
    if where == "help" or where.startswith("help/"):
        from . import tools as _tools  # noqa: F401 — make sure every tool (and its help) is registered
        from .mcp import registry
        name = where[5:].strip("/")
        if not name:
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps({"tools": registry.help_index(),
                                        "read": "copper://help/<tool> (or the copper_help tool) for examples and details"},
                                       indent=1)}
        return {"uri": uri, "mimeType": "text/markdown", "text": registry.help_text(name)}
    if where == "session/credits":
        from .mcp import ledger
        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(ledger.current().table(), indent=1)}
    if where == "status":
        from .review.project_info import status
        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(status(), indent=1, default=str)}
    if where.startswith("project/"):
        section = where.split("/", 1)[1]
        pr = _project(path)
        if section == "summary":
            from .review.project_info import summary
            data = summary(pr)
        elif section == "bom":
            data = _bom(pr)
        elif section == "netlist":
            return {"uri": uri, "mimeType": "text/plain", "text": _netlist_text(pr)}
        elif section == "erc":
            data = _erc(pr)
        elif section == "drc":
            data = _drc(pr)
        else:
            raise ToolError(f"unknown project section '{section}'", code="NOT_FOUND")
        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data, indent=1, default=str)}
    raise ToolError(f"unknown resource {uri}", code="NOT_FOUND")


def dispatch(method: str, params: dict | None) -> dict:
    params = params or {}
    if method == "resources/list":
        return {"resources": RESOURCES}
    if method == "resources/templates/list":
        return {"resourceTemplates": TEMPLATES}
    if method == "resources/read":
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError("resources/read needs a 'uri' string (see resources/list)")
        try:
            return {"contents": [read(uri)]}
        except ToolError as e:
            # resources/read has no isError — return the error as readable JSON content
            return {"contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps(e.to_dict(), separators=(",", ":"), ensure_ascii=False)}]}
    if method == "prompts/list":
        return {"prompts": [{"name": k, **v} for k, v in PROMPTS.items()]}
    if method == "prompts/get":
        name = params.get("name")
        if name not in PROMPTS:
            raise ValueError(f"unknown prompt '{name}' (have: {', '.join(PROMPTS)})")
        args = params.get("arguments") or {}
        missing = [a["name"] for a in PROMPTS[name]["arguments"] if a.get("required") and not args.get(a["name"])]
        if missing:
            raise ValueError(f"prompt '{name}' needs argument(s) {missing}")
        return {"description": PROMPTS[name]["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": _prompt_text(name, args)}}]}
    if method == "completion/complete":
        return {"completion": {"values": [], "total": 0, "hasMore": False}}
    raise ValueError(f"unsupported method {method}")
