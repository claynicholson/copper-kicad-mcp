"""The commands the spine itself needs: /help, /focus and /tool. Product commands (/verify, /tidy, /route, /pour,
/export, part search) are separate modules in this package; see ``__init__`` for how to add one.
"""
from __future__ import annotations

from . import COMMANDS, command
from .. import prompt


@command("help", "List the commands", usage="/help", needs_project=False)
def help_(turn, args):
    rows = [f"{c.usage or '/' + c.name}  -  {c.title}" for c in COMMANDS.values()]
    turn.text("\n".join(rows) + "\n")


@command("focus", "Show what is selected or pointed at", usage="/focus", needs_project=False)
def focus(turn, args):
    f = turn.focus
    line = prompt.focus_line(f)
    if not line:
        turn.text("Nothing is selected. Click a part, a pin or a wire, or drag a box.\n")
        return
    turn.text(line[len("[focus] "):] + "\n")
    target = {k: f[k] for k in ("refs",) if f.get(k)}
    if f.get("box_mm"):
        target["box"] = f["box_mm"]
    uuids = [i["uuid"] for i in f.get("items") or [] if isinstance(i, dict) and i.get("uuid")]
    if uuids and not target.get("refs"):
        target["uuids"] = uuids
    if target:
        turn.reveal(document=f.get("document"), **target)


@command("tool", "Run one engine tool by name", usage='/tool {"tool": "sch_read", "arguments": {}}',
         args={"tool": {"type": "string", "description": "engine tool name"},
               "arguments": {"type": "object", "description": "its arguments"},
               "summary": {"type": "string", "description": "text for the review bar when the tool edits"}},
         required=("tool",), needs_project=False)
def tool(turn, args):
    """The generic door: every engine tool, routed exactly like a model's tool call (schematic edits become one
    host.apply). Product commands are thin wrappers around the same ``turn.call_tool``."""
    out = turn.call_tool(args["tool"], args.get("arguments") or {}, summary=args.get("summary"))
    if out["ok"]:
        turn.text((out["text"].splitlines() or ["done"])[0][:400] + "\n")
        data = out.get("data") or {}
        items = data.get("findings")
        if isinstance(items, list) and items:
            turn.findings("schematic" if out["route"] == "schematic" else "board" if out["route"] == "board"
                          else (turn.focus or {}).get("document") or "schematic",
                          [i for i in items if isinstance(i, dict)], source="engine")
    else:
        err = out["error"] or {}
        turn.text(f"{args['tool']} failed: {err.get('message', 'error')}"
                  + (f"\nFix: {err['fix']}" if err.get("fix") else "") + "\n")
