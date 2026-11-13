"""What a model is told: a stable prefix that providers can cache, and a small volatile user turn.

    system_prefix(mode='chat') -> str        system prompt + design skill. Byte-identical between turns.
    tool_catalogue(mode='chat') -> [tool]    [{'name','description','input_schema'}], fixed order
    prefix_digest(mode='chat') -> str        sha256 of both: drivers may log it; tests assert it is stable
    focus_line(focus) -> str                 one line, about 60-150 tokens at most
    user_turn(text, focus=None, notes=()) -> str        focus line + notes + the user's words

Rules that keep the prefix cacheable:
  * nothing in ``system_prefix`` / ``tool_catalogue`` depends on the project, the time, the focus or the turn;
  * everything volatile (focus, document revision, review outcome) goes in the user turn, AFTER the cached part;
  * tools are listed in the registry's order with the slim catalogue texts (mcp/catalogue.py).

Mode 'quick' (the one-line quick edit) uses QUICK_TOOLS and no conversation history.
No model, vendor or client is named here (tests/test_neutral.py scans this package).
"""
from __future__ import annotations

import hashlib
import json

QUICK_TOOLS = ("sch_read", "sch_apply", "parts_search", "pcb_read", "pcb_edit")
HIDDEN_TOOLS = ("project_create", "project_open", "focus", "checkpoint")       # see host/live.py BLOCKED
SKILL = "kicad-design"

SYSTEM = """You are Copper, the assistant inside the Copper schematic and PCB editor (based on KiCad).
The user is looking at the open design. They point at things and ask for changes.

How pointing works
- A user message may start with a line "[focus] ...". It says what the user has selected or pointed at:
  document, sheet, parts (refs), pins (REF.PIN), nets, and a point or box in millimetres.
  "this", "these", "here" mean that focus. Empty parts with a point or box mean that AREA.
- Never ask for coordinates and never call a tool to find out what is selected. If nothing is in focus and you
  need a target, ask the user to select it.

How changes work
- Use the tools. Schematic edits land in the open document as ONE undoable change and the user gets a
  Keep / Undo bar. A tool result with {"live": ...} tells you the change is in the document and under review.
- Make the whole requested change in one batch (one sch_apply call), not one call per part.
- Tool results already carry the electrical check (ERC / DRC counts and new findings). Do not re-verify or
  render after each step. If a result lists new errors, fix them in the next call or say what is wrong.
- If a result says the document changed (STALE_BASE) or is busy (BUSY), nothing was applied: call again once.
- Board jobs (placement, routing, pours) run on the saved file; their undo is "Restore checkpoint". Say so.
- You cannot open, create or switch projects, and you cannot touch files outside the tools.

How to answer
- Short and concrete. Name parts by reference (R4, U3.PA5) so the user can click them.
- Say what you changed, then anything the user must decide. No preamble, no recap of the request.
- If the request is ambiguous in a way that changes the circuit, ask one question instead of guessing.
"""

QUICK = """Quick edit: make exactly the small change asked for, on the focused items, in one tool call if
possible. Answer in one short sentence.
"""


def _skill_body() -> str:
    try:
        from .. import skills
        s = skills.get(SKILL)
        return (s or {}).get("body", "").strip()
    except Exception:  # noqa: BLE001 — a missing skill must not break a turn
        return ""


def system_prefix(mode: str = "chat") -> str:
    """The system prompt. Stable: the same bytes for every turn of a given mode and engine version."""
    if mode == "quick":
        return SYSTEM + "\n" + QUICK
    body = _skill_body()
    return SYSTEM + ("\n# Design practice\n\n" + body + "\n" if body else "")


def tool_catalogue(mode: str = "chat") -> list[dict]:
    """The engine tools a model may call, as neutral {'name','description','input_schema'} entries."""
    from ..mcp import registry
    from .. import tools as _tools  # noqa: F401  (registers the engine tools)
    out = []
    for name, t in registry.TOOLS.items():
        if name in HIDDEN_TOOLS or (mode == "quick" and name not in QUICK_TOOLS):
            continue
        d = t.describe()
        schema = json.loads(json.dumps(d.get("inputSchema") or {"type": "object", "properties": {}}))
        (schema.get("properties") or {}).pop("project", None)          # the app owns the project
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r != "project"]
        out.append({"name": name, "description": d.get("description") or "", "input_schema": schema})
    return out


def prefix_digest(mode: str = "chat") -> str:
    blob = system_prefix(mode) + "\n" + json.dumps(tool_catalogue(mode), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _num(v) -> str:
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "?"


def _clean(v, limit: int = 80) -> str:
    """One focus value as inert text: a single line, no control characters, bounded. Names in a focus come from
    the design file (a reference, a net name, a pin name); a line break in one must not be able to start a new
    "[focus]" or "[note]" line in the prompt."""
    s = "".join(ch if ch.isprintable() else " " for ch in str(v))
    s = " ".join(s.split()).replace("[focus]", "(focus)").replace("[note]", "(note)")
    return s[:limit] + ("..." if len(s) > limit else "")


def _strs(v) -> list:
    return [x for x in v if isinstance(x, (str, int)) and not isinstance(x, bool)] if isinstance(v, list) else []


def _cap(items: list, n: int) -> str:
    items = [_clean(i) for i in items]
    return ", ".join(items[:n]) + (f" (+{len(items) - n} more)" if len(items) > n else "")


def focus_line(focus: dict | None) -> str:
    """The focus as one line, or '' when there is none.

    "[focus] schematic sheet / | selection | parts U3, C4 | pins U3.PA5(SWDIO) | nets /SWDIO, GND | box 100,50..120,70 mm"

    Whatever the object holds, the result is ONE line: values of the wrong type are left out, text is cleaned."""
    if not isinstance(focus, dict) or not focus:
        return ""
    doc = _clean(focus.get("document") or "schematic", 16)
    bits = [("board" if doc.lower() in ("board", "pcb") else "schematic")
            + (f" sheet {_clean(focus['sheet_path'])}" if isinstance(focus.get("sheet_path"), str) and focus["sheet_path"] else ""),
            _clean(focus.get("mode") or "selection", 16)]
    refs = _strs(focus.get("refs"))
    if refs:
        bits.append("parts " + _cap(refs, 24))
    pins = []
    for p in focus.get("pins") if isinstance(focus.get("pins"), list) else []:
        if isinstance(p, dict) and p.get("ref"):
            pins.append(f"{_clean(p['ref'], 32)}.{_clean(p.get('number', '?'), 16)}"
                        + (f"({_clean(p['name'], 32)})" if p.get("name") else ""))
        elif isinstance(p, str):
            pins.append(_clean(p))
    if pins:
        bits.append("pins " + ", ".join(pins[:16]) + (f" (+{len(pins) - 16} more)" if len(pins) > 16 else ""))
    nets = _strs(focus.get("nets"))
    if nets:
        bits.append("nets " + _cap(nets, 16))
    pt, box = focus.get("point_mm"), focus.get("box_mm")
    has_pt = isinstance(pt, list) and len(pt) == 2
    has_box = isinstance(box, list) and len(box) == 4
    if has_pt:
        bits.append(f"point {_num(pt[0])},{_num(pt[1])} mm")
    if has_box:
        bits.append(f"box {_num(box[0])},{_num(box[1])}..{_num(box[2])},{_num(box[3])} mm")
    items = focus.get("items") if isinstance(focus.get("items"), list) else []
    others = [i for i in items if isinstance(i, dict) and isinstance(i.get("type"), str)
              and i["type"] not in ("symbol", "footprint")]
    if others:
        kinds: dict = {}
        for i in others:
            k = _clean(i["type"], 24)
            kinds[k] = kinds.get(k, 0) + 1
        bits.append("also " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())[:8]))
    if not refs and not pins and (has_pt or has_box):
        bits.append("(an area, no part)")
    return "[focus] " + " | ".join(bits)


def user_turn(text: str, focus: dict | None = None, notes=()) -> str:
    """The user message a driver sends: focus line, then one '[note] ...' line per note, then the text."""
    lines = [focus_line(focus)] + [f"[note] {n}" for n in notes if n]
    head = "\n".join(l for l in lines if l)
    return (head + "\n\n" if head else "") + (text or "")
