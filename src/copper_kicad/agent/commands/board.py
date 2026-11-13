"""Board jobs without a model: /route, /pour and /undo-checkpoint.

Board tools do not produce patches yet, so these run as FILE JOBS (agent/jobs.py, HOST_PROTOCOL.md 5.9): the app is
asked, saves, a checkpoint is taken, the engine tool runs on the saved board file in a child process with a
progress row, and the app reloads the board. Stop kills the job and puts the board files back byte for byte.
A finished job is undone with /undo-checkpoint (a reload clears the editor's own undo history).

    /route [net ...] [reroute] [freerouting] [preview]     nets default to the nets in focus
    /pour [net] [remove|refill] [stitch]                   net defaults to the one net in focus, else GND
    /undo-checkpoint [list|<id>]                           restore the board from before the last job; again = redo
"""
from __future__ import annotations

import time

from .. import jobs
from . import command
from ._util import TEXT_ARG, findings, first_line, flags, focus_of, say_failure

_ROUTE_FLAGS = {"reroute": ("reroute", True), "freerouting": ("router", "freerouting"), "guided": ("router", "guided"),
                "preview": ("dry_run", True), "dry": ("dry_run", True), "all": ("all", True)}
_POUR_FLAGS = {"remove": ("remove", True), "refill": ("fill_only", True), "stitch": ("stitch", True)}


def _focus_nets(turn) -> list[str]:
    f = focus_of(turn)
    if f.get("mode") not in (None, "selection"):
        return []
    return [n for n in f.get("nets") or [] if isinstance(n, str) and n and not n.startswith("unconnected-")]


def _after_job(turn, what: str, out: dict) -> bool:
    if not out["ok"]:
        say_failure(turn, what, out)
        return False
    turn.text(first_line(out) + "\n")
    if out.get("reload_error"):
        turn.text(f"The board file changed but the editor could not re-read it ({out['reload_error']['message']}). "
                  "Use File > Revert in the board editor.\n")
    if out.get("checkpoint"):
        turn.text("Undo it with /undo-checkpoint.\n")
    items = findings((out.get("data") or {}).get("findings"))
    if items:
        turn.findings("board", items, source="engine")
    return True


@command("route", "Route the board's unrouted connections (or just some nets)",
         usage="/route [net ...] [reroute] [freerouting] [preview]",
         args={"nets": {"type": "array", "items": {"type": "string"},
                        "description": "only these nets (default: the nets in focus, else all)"},
               "reroute": {"type": "boolean", "description": "rip up and route again (those nets, or everything)"},
               "router": {"type": "string", "enum": ["guided", "freerouting"],
                          "description": "guided (built in, default) or freerouting"},
               "dry_run": {"type": "boolean", "description": "route a scratch copy and report, change nothing"},
               "text": TEXT_ARG},
         positional=("text",), documents=("board",))
def route(turn, args):
    typed, words = flags(args.get("text"), _ROUTE_FLAGS)
    opt = dict(typed, **{k: args[k] for k in ("reroute", "router", "dry_run") if k in args})
    nets = list(args.get("nets") or words) or ([] if opt.get("all") else _focus_nets(turn))
    if opt.get("router") == "freerouting":
        if opt.get("dry_run"):
            turn.text("The freerouting router has no preview; use /route preview for the built-in one.\n")
            return
        if nets:
            turn.text("The freerouting router always works on the whole board; the net list is ignored.\n")
        a = {"mode": "auto"}
        if opt.get("reroute"):
            a["clear_existing"] = True
        out = jobs.board_job(turn, "pcb_route", a, "Autoroute the board (Freerouting)")
    else:
        a = {"ripup": ("nets" if nets else "all") if opt.get("reroute") else "none"}
        if nets:
            a["nets"] = nets
        if opt.get("dry_run"):
            a["dry_run"] = True
        what = (", ".join(nets[:4]) + (" ..." if len(nets) > 4 else "")) if nets else "the board"
        out = jobs.board_job(turn, "pcb_route_guided", a, ("Re-route " if opt.get("reroute") else "Route ") + what)
    if _after_job(turn, "Routing", out) and len(nets) == 1:
        turn.reveal(document="board", net=nets[0])


@command("pour", "Add, refill or remove a copper pour (ground by default)", usage="/pour [net] [remove|refill] [stitch]",
         args={"net": {"type": "string", "description": "net to pour (default: the one net in focus, else GND)"},
               "remove": {"type": "boolean", "description": "remove this net's zones"},
               "fill_only": {"type": "boolean", "description": "only refill the existing zones"},
               "stitch": {"type": "boolean", "description": "add stitching vias"},
               "layers": {"type": "array", "items": {"type": "string"},
                          "description": "copper layers (default: both outer layers)"},
               "text": TEXT_ARG},
         positional=("text",), documents=("board",))
def pour(turn, args):
    typed, words = flags(args.get("text"), _POUR_FLAGS)
    if len(words) > 1:
        turn.text("One net at a time. Usage: /pour [net] [remove|refill] [stitch]\n")
        return
    a = dict(typed, **{k: args[k] for k in ("net", "remove", "fill_only", "stitch", "layers") if k in args})
    if words:
        a.setdefault("net", words[0])
    if "net" not in a and not a.get("fill_only"):
        nets = _focus_nets(turn)
        if len(nets) == 1:
            a["net"] = nets[0]
    net = a.get("net") or "GND"
    label = "Refill the copper zones" if a.get("fill_only") else f"Remove the {net} pour" if a.get("remove") \
        else f"Pour copper on {net}"
    out = jobs.board_job(turn, "pcb_pour", a, label)
    if _after_job(turn, label, out) and not a.get("remove") and not a.get("fill_only"):
        turn.reveal(document="board", net=net)


@command("undo-checkpoint", "Put the board back as it was before the last Copper board job (run again to redo)",
         usage="/undo-checkpoint [list|<id>]",
         args={"id": {"type": "string", "description": "a checkpoint id from /undo-checkpoint list (default: the newest)"},
               "text": TEXT_ARG},
         positional=("text",), documents=("board",))
def undo_checkpoint(turn, args):
    word = str(args.get("id") or args.get("text") or "").strip()
    if word == "list":
        cps = jobs.checkpoints(turn.project_dir)
        if not cps:
            turn.text("No board job checkpoints yet. /route and /pour take one before they change the board.\n")
            return
        turn.text("\n".join(f"{c['id']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(c.get('created', 0)))}  "
                            f"{'redo' if c.get('kind') == 'redo' else 'before'}: {c.get('label', '')}" for c in cps) + "\n")
        return
    out = jobs.restore(turn, word or None)
    if not out["ok"]:
        say_failure(turn, "Restore", out)
        return
    d = out.get("data") or {}
    line = first_line(out)
    turn.text(line[:1].upper() + line[1:] + ".\n")
    if d.get("restored"):
        if out.get("reload_error"):
            turn.text(f"The editor could not re-read the board ({out['reload_error']['message']}). Use File > Revert.\n")
        turn.text("Run /undo-checkpoint again to undo that.\n" if d.get("redone") else "Run /undo-checkpoint again to redo.\n")
