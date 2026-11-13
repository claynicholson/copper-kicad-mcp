"""Commands that work on the design without a model: /verify, /tidy, /parts, /export and the detailed /focus.

Every command maps to engine tools and uses the focus as context:

    /verify [erc|drc|all]            ERC / DRC of the LIVE document (host.verify), else the engine's checks on a
                                     shadow checkout (schematic) or the saved board file. Emits `findings`.
    /tidy [pro|labels] [force] [preview]
                                     sch_tidy on the focus' sheet; lands as ONE proposal through host.apply.
    /parts <query>                   parts_search; with no query, looks up the selected part.
    /export <kind>                   export / fab_export on the saved files (asks to save first); stoppable.
    /focus [detail]                  what is pointed at; `detail` reads values, pins and nets from the engine.

Schematic changes never touch the user's files here: `turn.call_tool` routes them through the shadow checkout
and `host.apply` (host/live.py).
"""
from __future__ import annotations

import re
from pathlib import Path

from ...host.client import HostError
from ..drivers.base import Cancelled
from .. import jobs, prompt
from . import builtin, command                      # builtin first: this module replaces its /focus
from ._util import (NOT_THERE, TEXT_ARG, document, findings, first_line, flags, focus_of, save_documents,
                    say_failure, sheet_arg)

_BASELINES: dict = {}                               # (project_dir, document) -> host.verify baseline token
_CHECK = {"erc": ("schematic", "ERC"), "drc": ("board", "DRC")}


# ── /verify ───────────────────────────────────────────────────────────────
def _has_board(turn) -> bool:
    try:
        return any(Path(turn.project_dir).glob("*.kicad_pcb"))
    except OSError:
        return False


def _check_live(turn, doc: str) -> dict | None:
    """ERC / DRC by the app on the live document. None when this app cannot (then the engine checks files)."""
    if "host.verify" not in turn.host.methods:
        return None
    key = (turn.project_dir, doc)
    try:
        res = turn.host.verify(doc, _BASELINES.get(key))
    except HostError as e:
        if e.code in NOT_THERE:
            return None
        raise
    if isinstance(res.get("baseline"), str):
        _BASELINES[key] = res["baseline"]
    counts = res.get("counts") if isinstance(res.get("counts"), dict) else {}
    items = findings(res.get("findings"))
    out = {"items": items, "live": True,
           "counts": {"errors": int(counts.get("errors", sum(f["severity"] == "error" for f in items))),
                      "warnings": int(counts.get("warnings", sum(f["severity"] == "warning" for f in items)))}}
    for k in ("unrouted", "unconnected"):
        if isinstance(counts.get(k), int):
            out["counts"]["unrouted"] = counts[k]
    if isinstance(res.get("new"), list):
        out["new"] = len(res["new"])
    return out


def _check_files(turn, call: str, check: str, doc: str) -> dict:
    """The engine's verify in a child process: ERC on a shadow checkout of the live schematic (unsaved edits
    included), DRC on the board file as last saved."""
    project, note = turn.project_dir, None
    if check == "erc":
        try:
            project = str(turn.server.live.checkout("schematic", turn.host).dir)
        except HostError as e:
            if e.code not in NOT_THERE:
                raise                                       # the schematic is not open in the app: the file is it
    else:
        note = "checked the board file as last saved"
    out = jobs._child(turn, call, "verify", {"what": [check], "project": project, "limit": 100}, "disk")
    if not out["ok"]:
        return {"error": out["error"]}
    block = (out.get("data") or {}).get(check) or {}
    res = {"items": findings(block.get("findings")), "live": False, "note": note,
           "counts": {"errors": int(block.get("errors", 0)), "warnings": int(block.get("warnings", 0))}}
    if check == "drc":
        res["counts"]["unrouted"] = int(block.get("unconnected", 0))
    return res


def _verify_one(turn, check: str) -> None:
    doc, label = _CHECK[check]
    call = turn.tool_start("verify", {"what": check}, label)
    try:
        res = _check_live(turn, doc)
        if res is None:
            res = _check_files(turn, call, check, doc)
    except Cancelled:
        turn.tool_end(call, False, error={"code": "CANCELLED", "message": "cancelled"})
        raise
    except HostError as e:
        res = {"error": {"code": e.code, "message": e.message}}
    if res.get("error"):
        turn.tool_end(call, False, error=res["error"])
        say_failure(turn, label, res)
        return
    c, items = res["counts"], res["items"]
    line = f"{label}: {c['errors']} error(s), {c['warnings']} warning(s)"
    if "unrouted" in c:
        line += f", {c['unrouted']} unrouted"
    if res.get("new") is not None:
        line += f"; {res['new']} new since the last check"
    sel = [r for r in focus_of(turn, doc).get("refs") or [] if isinstance(r, str)]
    if sel and items:
        hit = [f for f in items if set(f.get("refs") or []) & set(sel)]
        if hit:
            items = hit + [f for f in items if f not in hit]
            line += f"; {len(hit)} on your selection"
    turn.tool_end(call, True, summary=line)
    turn.text(line + "." + (f" ({res['note']})" if res.get("note") else "") + "\n")
    turn.findings(doc, items, source=check, counts=c)


@command("verify", "Check the design: electrical rules (ERC) and board rules (DRC)", usage="/verify [erc|drc|all]",
         args={"what": {"type": "string", "enum": ["erc", "drc", "all"],
                        "description": "default: erc in the schematic editor, drc in the board editor"}},
         positional=("what",))
def verify(turn, args):
    what = args.get("what") or ("drc" if document(turn) == "board" else "erc")
    checks = ["erc", "drc"] if what == "all" else [what]
    if "drc" in checks and not _has_board(turn):
        if what == "drc":
            turn.text("This project has no board file yet, so there is nothing for DRC to check.\n")
            return
        checks.remove("drc")
    for check in checks:
        turn.check_cancel()
        _verify_one(turn, check)


# ── /tidy ─────────────────────────────────────────────────────────────────
_TIDY_FLAGS = {"pro": ("style", "pro"), "labels": ("style", "labels"), "force": ("force", True),
               "preview": ("dry_run", True), "dry": ("dry_run", True)}


@command("tidy", "Re-draw the schematic sheet as tidy functional blocks (one undo step)",
         usage="/tidy [pro|labels] [force] [preview]",
         args={"style": {"type": "string", "enum": ["pro", "labels"], "description": "pro: blocks and wires; labels: every connection a label"},
               "force": {"type": "boolean", "description": "also re-lay a hand-wired sheet"},
               "dry_run": {"type": "boolean", "description": "report what would change, change nothing"},
               "sheet": {"type": "string", "description": "sheet name or file (default: the sheet in focus)"},
               "text": TEXT_ARG},
         positional=("text",), documents=("schematic",))
def tidy(turn, args):
    typed, rest = flags(args.get("text"), _TIDY_FLAGS)
    if rest:
        turn.text(f"/tidy does not know {' '.join(rest)!r}. Usage: /tidy [pro|labels] [force] [preview]\n")
        return
    a = dict(typed, **{k: args[k] for k in ("style", "force", "dry_run", "sheet") if k in args})
    if "sheet" not in a:
        sheet = sheet_arg(focus_of(turn, "schematic"))
        if sheet:
            a["sheet"] = sheet
    a["preview"] = False
    out = turn.call_tool("sch_tidy", a, summary="Tidy the sheet layout", title="Tidy the schematic")
    if not out["ok"]:
        say_failure(turn, "Tidy", out)
        return
    line = first_line(out)
    p = out.get("proposal")
    if not p and "force=true" in line:                    # the engine left a hand-drawn sheet alone
        turn.text(line.split(" Pass force=true")[0] + "\nTo re-lay it anyway: /tidy force\n")
        return
    turn.text(line + "\n")
    if p:
        if p.get("netlist_ok") is False:
            turn.text("Warning: the app reports that the connections changed. Undo is the safe choice.\n")
        else:
            turn.text("It is on the canvas for review: Keep or Undo.\n")
    elif not a.get("dry_run"):
        turn.text("Nothing changed.\n")


# ── /parts ────────────────────────────────────────────────────────────────
def _query_from_focus(turn) -> tuple[str | None, str | None]:
    """(query, note) for the one selected part: its part number when it has one, else value + package."""
    refs = [r for r in focus_of(turn).get("refs") or [] if isinstance(r, str)]
    if len(refs) != 1 or not turn.project_dir:
        return None, None
    ref = refs[0]
    out = turn.call_tool("sch_read", {"query": "components", "filter": ref}, title=f"Look up {ref}")
    comp = next((c for c in ((out.get("data") or {}).get("components") or []) if c.get("ref") == ref), None) if out["ok"] else None
    if comp is None:
        return None, None
    if comp.get("mpn"):
        return str(comp["mpn"]), f"{ref} is {comp.get('value', '?')}, part {comp['mpn']}"
    m = re.search(r"_(\d{4})_", str(comp.get("footprint") or ""))
    q = " ".join(x for x in (str(comp.get("value") or ""), m.group(1) if m else "") if x)
    return (q or None), f"{ref} is {comp.get('value', '?')} with no part number yet"


@command("parts", "Search for a real part (stock, price, package)", usage="/parts <value, part number or keywords>",
         args={"query": {"type": "string", "description": "e.g. '10k 0603', 'AMS1117-3.3', 'usb-c connector'"},
               "package": {"type": "string"}, "category": {"type": "string"},
               "limit": {"type": "integer", "description": "max results (default 6)"},
               "offline": {"type": "boolean", "description": "use only the data shipped with Copper, no supplier lookup"}},
         positional=("query",), needs_project=False)
def parts(turn, args):
    query, note = (args.get("query") or "").strip(), None
    if not query:
        query, note = _query_from_focus(turn)
        if not query:
            turn.text("Type what to look for, for example /parts 10k 0603. With one part selected, /parts looks that part up.\n")
            return
    a = {"query": query, "limit": max(1, min(int(args.get("limit") or 6), 20))}
    for k in ("package", "category"):
        if args.get(k):
            a[k] = args[k]
    if args.get("offline"):
        a["live"] = False
    out = turn.call_tool("parts_search", a, title=f"Search parts: {query}")
    if not out["ok"]:
        say_failure(turn, "Part search", out)
        return
    rows = [r for r in ((out.get("data") or {}).get("results") or []) if isinstance(r, dict)]
    if note:
        turn.text(note + ".\n")
    if not rows:
        turn.text(f"No part found for {query!r}.\n")
        return
    lines = []
    for r in rows:
        price = str(r.get("price_usd") or "").split("·")[0].strip()
        bits = [str(r.get("id") or r.get("mpn") or "?"),str(r.get("mpn") or r.get("mfr_part") or ""),
                str(r.get("package") or ""), str(r.get("class") or ""),
                f"stock {r['stock']:,}" if isinstance(r.get("stock"), int) else "", price]
        lines.append("  ".join(b for b in bits if b) + (f"\n    {str(r['description'])[:110]}" if r.get("description") else ""))
    stock_note = next((str(r["stock_note"]) for r in rows if r.get("stock_note")), None)
    turn.text(f"{len(rows)} part(s) for {query!r}:\n" + "\n".join(lines) + "\n" + (f"({stock_note})\n" if stock_note else ""))


# ── /export ───────────────────────────────────────────────────────────────
_SCH_KINDS = ("pdf_schematic", "svg", "netlist", "bom")
_PCB_KINDS = ("pcb_pdf", "step", "glb", "vrml", "ipc2581", "odb", "pos", "drill", "gerbers", "render3d", "stats", "ipcd356")
_ALIASES = {"gerber": "gerbers", "3d": "step", "schematic": "pdf_schematic", "board": "pcb_pdf", "fabrication": "fab",
            "order": "fab"}


@command("export", "Write output files: PDF, netlist, BOM, gerbers, STEP, or the fab order pack",
         usage="/export <pdf|netlist|bom|gerbers|drill|pos|step|fab|...>",
         args={"kind": {"type": "string", "description": "fab, pdf, " + ", ".join(_SCH_KINDS + _PCB_KINDS)},
               "out": {"type": "string", "description": "output file or folder (default: <project>/copper-out)"}},
         positional=("kind",))
def export(turn, args):
    kind = str(args.get("kind") or "").strip().lower()
    kind = _ALIASES.get(kind, kind)
    if kind == "pdf":
        kind = "pcb_pdf" if document(turn) == "board" else "pdf_schematic"
    if kind not in _SCH_KINDS + _PCB_KINDS + ("fab",):
        turn.text(("" if not kind else f"/export does not know {kind!r}. ")
                  + "Kinds: fab (the order pack), pdf, " + ", ".join(_SCH_KINDS + _PCB_KINDS) + "\n")
        return
    if kind != "fab" and kind in _PCB_KINDS and not _has_board(turn):
        turn.text("This project has no board file yet.\n")
        return
    docs = ("schematic", "board") if kind == "fab" else ("schematic",) if kind in _SCH_KINDS else ("board",)
    note = save_documents(turn, docs, f"Export {kind}")
    if kind == "fab":
        a = {"out_dir": args["out"]} if args.get("out") else {}
        out = jobs.run_tool(turn, "fab_export", a, title="Fab order pack")
    else:
        a = dict({"kind": kind}, **({"out": args["out"]} if args.get("out") else {}))
        out = jobs.run_tool(turn, "export", a, title=f"Export {kind}")
    if not out["ok"]:
        say_failure(turn, f"Export {kind}", out)
        items = findings((out.get("data") or {}).get("findings") or (out.get("data") or {}).get("blockers"))
        if items:
            turn.findings("board", items, source="engine")
        return
    data = out.get("data") or {}
    turn.text(first_line(out, 600) + "\n")
    files = [f"{f.get('name')} ({f.get('bytes'):,} bytes)" for f in data.get("files") or []
             if isinstance(f, dict) and f.get("name") and isinstance(f.get("bytes"), int)]
    if len(files) > 1:
        turn.text("\n".join(files[:40]) + "\n")
    if note:
        turn.text(note + "\n")
    items = findings(data.get("findings"))
    if items:
        turn.findings("board" if kind == "fab" or kind in _PCB_KINDS else "schematic", items, source="engine")


# ── /focus ────────────────────────────────────────────────────────────────
def _detail_schematic(turn, f: dict) -> None:
    refs = [r for r in f.get("refs") or [] if isinstance(r, str)]
    for ref in refs[:4]:
        out = turn.call_tool("sch_read", {"query": "pins", "ref": ref}, title=f"Read {ref}")
        d = out.get("data") or {}
        if not out["ok"] or not d.get("pins"):
            turn.text(f"{ref}: {(out.get('error') or {}).get('message') or 'no pins found'}\n")
            continue
        rows = [f"  {p.get('pin', '?'):>4}  {str(p.get('name') or ''):<12} {p.get('net') or '(not connected)'}"
                for p in d["pins"][:48]]
        more = f"\n  ... {len(d['pins']) - 48} more pins" if len(d["pins"]) > 48 else ""
        turn.text(f"{ref}  {d.get('value', '')}  ({d.get('lib_id', '')})\n" + "\n".join(rows) + more + "\n")
    if len(refs) > 4:
        turn.text(f"... and {len(refs) - 4} more part(s): select fewer to see their pins.\n")
    if not refs:
        for net in [n for n in f.get("nets") or [] if isinstance(n, str)][:3]:
            out = turn.call_tool("sch_read", {"query": "nets", "net": net}, title=f"Read net {net}")
            pins = ((out.get("data") or {}).get("nets") or {}).get(net) if out["ok"] else None
            turn.text(f"net {net}: " + (", ".join(pins) if pins else "nothing connected, or not a net of this schematic") + "\n")


def _detail_board(turn, f: dict) -> None:
    refs = [r for r in f.get("refs") or [] if isinstance(r, str)]
    if not refs:
        return
    out = turn.call_tool("pcb_read", {"refs": refs[:12], "what": ["footprints"]}, title="Read the board parts")
    if not out["ok"]:
        say_failure(turn, "Reading the board", out)
        return
    for fp in (out.get("data") or {}).get("footprints") or []:
        turn.text(f"{fp.get('ref')}  {fp.get('value', '')}  {fp.get('footprint', '')}  at {fp.get('x')},{fp.get('y')} mm, "
                  f"{fp.get('side', 'top')}, rotated {fp.get('rot', 0)}"
                  + (f", part {fp['mpn']}" if fp.get("mpn") else "") + "\n")
    turn.text("(the board file as last saved)\n")


def _detail_area(turn, f: dict, doc: str) -> None:
    """A point or a box with no part in the focus: ask the engine what is there."""
    a = {"action": "resolve", "document": "pcb" if doc == "board" else "schematic", "render": False}
    if isinstance(f.get("box_mm"), list) and len(f["box_mm"]) == 4:
        a["box"] = f["box_mm"]
    elif isinstance(f.get("point_mm"), list) and len(f["point_mm"]) == 2:
        a["near"] = f["point_mm"]
    else:
        return
    project = turn.project_dir
    if doc == "schematic":
        if f.get("sheet_path"):
            a["sheet"] = f["sheet_path"]
        try:
            project = str(turn.server.live.checkout("schematic", turn.host).dir)
        except HostError as e:
            if e.code not in NOT_THERE:
                turn.text(f"Could not read the live schematic: {e.message}\n")
                return
    out = jobs.run_tool(turn, "focus", a, project=project, title="Look at this area")
    if not out["ok"]:
        say_failure(turn, "Looking at this area", out)
        return
    items = [i for i in (out.get("data") or {}).get("items") or [] if isinstance(i, dict) and i.get("ref")]
    if not items:
        turn.text("Nothing is there: no part within reach of that spot.\n")
        return
    for i in items[:20]:
        turn.text(f"{i['ref']}  {i.get('value', '')}  {i.get('footprint', '')}"
                  + (f"  nets {', '.join(i['nets'][:8])}" if i.get("nets") else "") + "\n")
    if doc == "board":
        turn.text("(the board file as last saved)\n")
    turn.reveal(document=doc, refs=[i["ref"] for i in items[:20]])


@command("focus", "Show what is selected or pointed at", usage="/focus [detail]",
         args={"detail": {"type": "boolean", "description": "also read values, pins and nets from the design"},
               "text": TEXT_ARG},
         positional=("text",), needs_project=False)
def focus(turn, args):
    typed, rest = flags(args.get("text"), {"detail": ("detail", True), "pins": ("detail", True)})
    if rest:
        turn.text(f"/focus does not know {' '.join(rest)!r}. Usage: /focus [detail]\n")
        return
    builtin.focus(turn, {})
    f = focus_of(turn)
    if not (args.get("detail") or typed.get("detail")) or not prompt.focus_line(f):
        return
    if not turn.project_dir:
        turn.text("Open a project to see the details.\n")
        return
    doc = document(turn)
    if f.get("refs") or (doc == "schematic" and f.get("nets")):
        (_detail_board if doc == "board" else _detail_schematic)(turn, f)
    else:
        _detail_area(turn, f, doc)
