"""copper_status · project_create · project_open · project_summary · checkpoint"""
from __future__ import annotations

import os

from .. import project as P
from ..mcp.registry import Result, ToolError, tool
from ..review import project_info as I

PROJECT = {"type": "string", "description": "Project folder or .kicad_pro/.kicad_sch/.kicad_pcb path. Omit to use the active project."}


@tool("copper_status", """Start here. Shows Copper + KiCad versions and installs, what works on this machine (kicad-cli, KiCad's pcbnew Python for
board edits, live IPC socket, Freerouting/Java for autorouting), the active project (+ whether it's open in KiCad, last ERC/DRC counts),
recent projects, tool modules that failed to load, `session` (what this session's Copper calls cost so far) and `next`: the single
most useful next call. detail='credits' returns the per-tool table instead: calls, result bytes, images, estimated tokens.""",
      {"detail": {"type": "string", "enum": ["status", "credits"], "description": "credits = per-tool cost of this session"}},
      title="Copper status", read_only=True, idempotent=True, core=True, group="project",
      help="""## Reading the result
        - capabilities.kicad_cli: needed for everything (ERC/DRC, renders, exports).
        - capabilities.pcbnew_worker.ok: KiCad's own Python can import pcbnew; every pcb_* edit needs it.
        - capabilities.routing.autorouting: 'ready' (Freerouting + Java are installed) or 'downloads on first use'
          (the first pcb_route fetches what `first_route_downloads` lists into `tools_dir`, about 120 MB, needs network;
          `copper freerouting --install` does it ahead of time). In both cases `autorouting_available` is true and
          pcb_route can be called. `autorouting_ready` is true only once everything is on disk.
        - capabilities.ipc: KiCad's live API socket (only while KiCad runs with the API enabled).
        - active_project.last_verify: last ERC/DRC counts; `stale` means the file changed since.
        - tool_load_errors: tool modules that failed to import (run `copper doctor`).
        - config: the active parts providers (in order) and fab profile for the active project, and the config files that
          set them ($COPPER_HOME/config.json, <project>/.copper/config.json). A broken file shows here with its line and fix.
        - session: calls, errors, result kB and images so far, with ESTIMATED tokens (text bytes / 4, image pixels / 750)
          for the tool catalogue, arguments, results and images. `reread_per_turn` is what every further turn re-reads.
          It counts Copper's own traffic only: the assistant's prompt and reasoning are not in it.
          copper_status(detail='credits') lists every tool; past sessions are counters in $COPPER_HOME/sessions/.""")
def copper_status(detail="status"):
    from ..mcp import ledger
    led = ledger.current()
    if detail == "credits":
        t = led.table()
        e = t["est_tokens"]
        return Result(t, summary=f"this session: {sum(r[1] for r in t['rows'])} Copper calls, about {e['total']} tokens "
                                 f"(catalogue {e['catalogue']}, arguments {e['args']}, results {e['results']}, images {e['images']})")
    d = I.status()
    d["session"] = led.digest()
    k = (d["kicad"].get("active") or {}).get("version") or "not found"
    a = d.get("active_project") or {}
    c = d.get("config") or {}
    cfg = (f" · parts: {'+'.join(c.get('parts_providers') or [])} · fab: {c.get('fab_profile')}" if c.get("fab_profile")
           else f" · CONFIG ERROR: {c.get('message')}" if c.get("error") else "")
    if str(d["next"]).startswith("fix the Copper config first"):
        cfg = cfg.split(" · CONFIG ERROR")[0] + " · CONFIG " + ("ERROR" if c.get("error") else "PROBLEM")   # `next` says what
    return Result(d, summary=f"Copper {d['copper']} · KiCad {k} · project: {a.get('name') or 'none'}{cfg} → {d['next']}")


@tool("project_create", """Create a new KiCad 10 project (.kicad_pro, .kicad_sch, .kicad_pcb) and make it active. Refuses to overwrite an existing project.
fab: the fab profile whose design rules the project gets and remembers (default: the configured one, 'jlcpcb' out of the box;
also 'pcbway', 'oshpark', 'generic' or your own). template: '2layer' (default) | '4layer' | 'blank' (KiCad defaults, no fab
rules); 'jlc-2layer' / 'jlc-4layer' = the JLCPCB profile (1.6 mm FR4, Default 0.2 mm / Power 0.5 mm; 4-layer JLC04161H-7628,
Default 0.15 mm / Power 0.3 mm so power can leave fine-pitch pads).
board_size: [width_mm, height_mm] draws a rectangular outline at (100,100). dir: the parent folder (project goes in dir/<name>), or the
project folder itself when its last component equals `name`; default Documents/Copper/<name>. `note` says when the name was made file-safe.
KiCad itself re-loads the files (ERC + DRC) before success is reported.
Example: project_create(name='usb-sensor', board_size=[50,30])""",
      {"name": {"type": "string"}, "dir": {"type": "string", "description": "parent folder, or the project folder itself if it ends in <name>"},
       "template": {"type": "string", "enum": I.TEMPLATE_NAMES},
       "fab": {"type": "string", "description": "fab profile name (fab_export(profile='list') shows them)"},
       "board_size": {"type": "array", "items": {"type": "number"}, "description": "[width_mm, height_mm]"},
       "title": {"type": "string"}, "company": {"type": "string"}, "rev": {"type": "string"}},
      ["name"], title="Create project", group="project",
      help="""## Fab profiles and templates
        Design rules come from a fab profile (data, not code: docs/FAB_PROFILES.md). `fab` picks it; without `fab` the
        Copper config's fab.profile is used ('jlcpcb' unless changed). A profile other than the configured one is written
        to <project>/.copper/config.json, so pcb_setup, verify and fab_export all follow it.
        - 2layer / 4layer: the layer-count skeleton with the profile's rules (track/space, vias, edge clearance, a
          .kicad_dru block). For the jlcpcb profile these are exactly jlc-2layer / jlc-4layer.
        - jlc-2layer: JLCPCB 1.6 mm FR4 stackup; rules 0.127 mm track/space, 0.3 mm drill, 0.5 mm via, 0.3 mm edge clearance;
          net classes Default 0.2 mm and Power 0.5 mm; a .kicad_dru with JLC hole rules.
        - jlc-4layer: JLC04161H-7628 stackup (F.Cu / In1.Cu / In2.Cu / B.Cu), 0.1 mm track/space, 0.2 mm drill;
          net classes Default 0.15 mm and Power 0.3 mm (enough for about 1 A on an outer layer, and narrow enough to leave
          0.4-0.5 mm pitch QFN pads; pour the inner layers as GND/power planes with pcb_pour).
        - blank: KiCad defaults, no fab rules.

        The Power class is assigned by net-name pattern. KiCad matches a pattern against the FULL net name and a local
        label's net is '/VBAT', so the patterns cover both forms: GND, *GND, GND*, */GND*, +*, */+*, *VBUS*, *VCC*, *VDD*,
        *VIN*, *VBAT*, *VSYS*, V+, */V+, *PWR*. Add other high-current nets with pcb_setup(power_nets=['/SW','/BATN']).

        ## Names and folders
        File names avoid characters KiCad and fabs dislike: 'my board' becomes 'my-board' and the result's `note` says so.
        project_create(name='amp', dir='D:/boards')       -> D:/boards/amp/amp.kicad_pro
        project_create(name='amp', dir='D:/boards/amp')   -> D:/boards/amp/amp.kicad_pro (not amp/amp)

        ## Examples
        project_create(name='usb-sensor', board_size=[50,30])
        project_create(name='rp2040-dev', template='4layer', board_size=[40,40], title='RP2040 dev board', rev='A')
        project_create(name='breakout', fab='oshpark', board_size=[20,15])""")
def project_create(name, dir=None, template=None, board_size=None, title=None, company="", rev="1", fab=None):
    r = I.create_project(name, dir, template, board_size, title, company, rev, fab=fab)
    chk = r["kicad_check"]
    what = r["template"] + (f", {r['fab']} rules" if r.get("fab") else "")
    return Result(r, summary=(f"created {r['project']['name']} ({what}) in {r['project']['dir']} — KiCad loaded it: "
                              f"{chk.get('erc_violations', 0)} ERC / {chk.get('drc_violations', 0)} DRC violations"
                              + (f" — NOTE: {r['note']}" if r.get("note") else "")))


@tool("project_open", """Make an existing KiCad project the active one (all other tools then default to it). path: project folder,
.kicad_pro, .kicad_sch or .kicad_pcb. Returns a short overview + next steps (project_summary has the full digest).
A `project` argument on any other tool affects only that call; project_open is what changes the active project.
Example: project_open(path='C:/Users/me/Documents/KiCad/robot/robot.kicad_pro')""",
      {"path": {"type": "string"},
       "allow_format_upgrade": {"type": "boolean", "description": "accept a one-way save in a pre-release KiCad's format"}},
      ["path"], title="Open project", idempotent=True, group="project",
      help="""## File formats
        `format` in the result shows the KiCad format of the design files (sch / pcb stamps and the series they
        belong to), `mixed` when sheets differ, and `active_kicad`: the KiCad Copper uses for this project.
        - A design saved by an older KiCad is opened as it is. The first edit saves the edited file in the active
          KiCad's format (exactly what KiCad itself does on save); Copper takes a checkpoint first and says so once
          (`format_upgraded`).
        - `format_problem` (FORMAT_TOO_NEW) means a sheet or the board was saved by a KiCad newer than the active
          one. Every tool refuses to work on it until that KiCad is installed; checkpoint(action='restore') still works.
        - When the only usable KiCad is a pre-release (nightly / release candidate), saving would make the files
          unreadable in the stable KiCad, so edits answer FORMAT_UPGRADE_BLOCKED until you open the project with
          allow_format_upgrade=true (remembered per project; false takes the consent back).""")
def project_open(path, allow_format_upgrade=None):
    pr = P.resolve(path, activate=False, format_check=False)
    if not pr.sch.exists() and not pr.pcb.exists():
        raise ToolError(f"{pr.dir} has no {pr.name}.kicad_sch or .kicad_pcb", code="NOT_FOUND",
                        fix="project_create(name=..., dir=...) to start a new project")
    P.activate(pr)
    fmt, problem = _format_info(pr, allow_format_upgrade)
    if problem:      # the project cannot be read by the active KiCad: say so instead of failing, so it can be restored
        return Result({"project": pr.to_dict(), "format": fmt, "format_problem": problem,
                       "next": [problem.get("fix") or "install the KiCad that saved this project",
                                "checkpoint(action='list') to restore an older state"]},
                      summary=f"opened {pr.name}, but it cannot be used yet: {problem.get('message')}")
    d = I.summary(pr)
    keep = ("project", "components", "nets", "connected_nets", "part_field", "sourced_pct", "board_mm", "copper_layers", "tracks", "vias",
            "open_in_kicad", "read_only", "next", "netlist_error")
    out = {k: d[k] for k in keep if k in d}
    out["sheets"] = len(d.get("sheets", []))
    out["last_verify"] = {k: {kk: v.get(kk) for kk in ("errors", "warnings", "unconnected", "stale") if kk in v}
                          for k, v in (d.get("last_verify") or {}).items()}
    out["more"] = "project_summary() for parts by kind, ICs, rails, missing part numbers/footprints, config and checkpoints"
    if fmt:
        out["format"] = fmt
    if d.get("netlist_error"):       # never report "0 components" for a design KiCad could not read
        return Result(out, summary=f"opened {pr.name} (now the active project), but its schematic could not be read: "
                                   f"{d['netlist_error'].get('message')}")
    return Result(out, summary=f"opened {pr.name} (now the active project): {d.get('components', 0)} components, "
                               f"{d.get('connected_nets', d.get('nets', 0))} connected nets, "
                               f"{len(d.get('sheets', []))} sheet(s), board {'yes' if pr.pcb.exists() else 'no'}")


def _format_info(pr, allow_format_upgrade=None):
    """(format report, FORMAT_TOO_NEW problem or None) + record the user's answer about pre-release formats."""
    try:
        from ..kicad import compat, env
        problem = P.format_preflight(pr, check=False)
        inst = env.kicad()
        if allow_format_upgrade is not None:
            compat.allow_format_upgrade(pr.dir, inst.series if allow_format_upgrade else None)
        fmt = compat.format_report(pr, inst)
        if inst.ver is not None and inst.ver.prerelease:
            fmt["prerelease"] = True
            fmt["allow_format_upgrade"] = compat._pstate(pr.dir).get("allow_format") == inst.series
        return fmt, problem
    except Exception:  # noqa: BLE001 — KiCad missing etc.: copper_status reports that
        return {}, None


@tool("project_summary", """Compact digest of the active (or given) project: sheets, components by kind, ICs, nets, power rails, % of parts with a
part number (and which are missing one), missing footprints, board size/layers/tracks/vias/zones, the active parts providers + fab
profile (`config`), last ERC/DRC counts (and whether they are stale), and next steps.
Read-only and cheap; call it when you (re)start work on a design.""",
      {"project": PROJECT}, title="Project summary", read_only=True, idempotent=True, core=True, group="project")
def project_summary(project=None):
    pr = P.resolve(project)
    d = I.summary(pr)
    if project and not P.is_active(pr):
        d["note"] = "summary of a project other than the active one (the active project is unchanged)"
    lv = d.get("last_verify", {})
    e = lv.get("erc") or {}
    return Result(d, summary=(f"{pr.name}: {d.get('components', 0)} parts, {d.get('nets', 0)} nets, "
                              + (f"{d.get('part_field') or 'sourced'} {d['sourced_pct']}%, " if "sourced_pct" in d else "")
                              +
                              f"ERC {e.get('errors', '?')}E/{e.get('warnings', '?')}W" + (" (stale)" if e.get("stale") else "") +
                              f" → {d['next'][0] if d['next'] else 'ok'}"))


@tool("checkpoint", """Snapshot / list / diff / restore the project's design files (.kicad_sch, .kicad_pcb, .kicad_pro, rules, lib tables) in .copper/snapshots.
action: 'create' (label optional) · 'list' · 'diff' (id optional, default newest: what changed since) · 'restore' (id required;
snapshots the current state first, refuses if the file is open in KiCad). Mutating tools snapshot by themselves before they write.
Example: checkpoint(action='diff')""",
      {"action": {"type": "string", "enum": ["create", "list", "diff", "restore"]},
       "label": {"type": "string"}, "id": {"type": "string"}, "project": PROJECT},
      ["action"], title="Checkpoints", destructive=True, group="project",
      help="""## What diff reports
        Per changed file: lines added/removed, plus
        - schematics: symbols_added / symbols_removed / symbols_changed (lib_id, value, footprint, lcsc, at),
          items_delta (wires, labels, global and hierarchical labels, junctions, no_connects) and
          hier_labels_added / hier_labels_removed by name. A removed hierarchical label cuts the sheet off from the
          parent's sheet pin of that name, so check it was intended.
        - boards: footprints_added / removed / moved / swapped and items_delta (tracks, vias, zones, arcs).

        ## Examples
        checkpoint(action='create', label='before-routing')
        checkpoint(action='list')
        checkpoint(action='diff')                                      # since the newest snapshot
        checkpoint(action='diff', id='20261109-181500-before-routing')
        checkpoint(action='restore', id='20261109-181500-before-routing')
        A restore first snapshots the current state as '<time>-before-restore', so it can itself be undone.""")
def checkpoint(action, label="", id=None, project=None):
    pr = P.resolve(project, format_check=False)     # a project the active KiCad cannot read must stay restorable
    try:
        return _checkpoint(pr, action, label, id)
    except PermissionError as e:
        raise ToolError(f"cannot write snapshots in {pr.dir}: {e.strerror or e}", code="READ_ONLY",
                        fix="copy the project to a writable folder and project_open(path=<copy>)")


def _checkpoint(pr, action, label, id):
    if action == "create":
        cid = P.checkpoint(pr, label or "manual", dedupe=False)
        return Result({"created": cid}, summary=f"checkpoint {cid}")
    if action == "list":
        cps = P.list_checkpoints(pr)
        # compact: ids only; the file set is listed once (and per entry only where it differs)
        common = max((tuple(c["files"]) for c in cps), key=lambda t: sum(1 for c in cps if tuple(c["files"]) == t), default=())
        rows = [c["id"] if tuple(c["files"]) == common else {"id": c["id"], "files": c["files"]} for c in cps[:40]]
        d = {"checkpoints": rows, "total": len(cps), "files": list(common)}
        if len(cps) > 40:
            d["more"] = f"{len(cps) - 40} older checkpoint(s) not shown"
        return Result(d, summary=f"{len(cps)} checkpoint(s), newest first")
    if action == "diff":
        d = I.diff_checkpoint(pr, id)
        return Result(d, summary=f"since {d['checkpoint']}: " + ("no changes" if d["identical"] else
                                                                ", ".join(f["file"] for f in d["changed_files"]) + " changed"))
    if not id:
        raise ToolError("restore needs id", code="BAD_ARGUMENT", fix="checkpoint(action='list') to see ids")
    src = pr.copper_dir / "snapshots" / id
    if os.path.isdir(P._long(src)):
        # check every file first so a half-restored project can't happen
        busy = [n for n in P.snapshot_files(src) if P.is_open_in_kicad(pr.dir / n)]
        if busy:
            raise ToolError(f"{', '.join(busy)} open in KiCad — restoring now would be overwritten when KiCad saves",
                            code="FILE_OPEN_IN_KICAD",
                            fix=f"close the schematic/board editor in KiCad (no need to save), then checkpoint(action='restore', id='{id}')")
    restored = P.restore_checkpoint(pr, id)
    return Result({"restored": restored, "from": id,
                   "note": "the previous state was snapshotted first; if KiCad has these files open use File ▸ Revert"},
                  summary=f"restored {len(restored)} file(s) from {id}")


@tool("copper_help", """Worked examples, op catalogues and argument details for one Copper tool: the long-form help that is kept out of
the tool list. Call it before your first non-trivial use of a tool, or when a call came back BAD_ARGUMENT / BAD_OP.
Omit `tool` to list every tool with a one-line summary. Example: copper_help(tool='sch_apply')""",
      {"tool": {"type": "string", "description": "tool name, e.g. 'pcb_route'"}},
      title="Tool help", read_only=True, idempotent=True, group="project")
def copper_help(tool=None):   # noqa: A002
    from ..mcp import registry
    if not tool:
        rows = registry.help_index()
        return Result({"tools": [dict({"tool": r["tool"], "summary": r["summary"]}, **({"more_help": True} if r["help_chars"] else {}))
                                 for r in rows]},
                      summary=f"{len(rows)} tools: copper_help(tool='<name>') for examples")
    name = tool.strip().strip("`' ").replace("-", "_")
    return registry.help_text(name)
