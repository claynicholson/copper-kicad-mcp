"""Glue between the schematic tools (tools/sch_tools.py) and the layout engine: compact results, the readability
query, and the rule for when sch_apply may re-lay a sheet on its own."""
from __future__ import annotations

from pathlib import Path

from ...mcp.registry import ToolError
from .. import ops as O
from ..model import Schematic
from . import engine

_STRUCTURAL = {"place", "connect", "power", "delete", "swap", "disconnect", "no_connect", "pwr_flag", "rename_net"}


def compact(r: dict) -> dict:
    """Engine result -> what an agent needs (well under 1.2 kB): what was done, the proof, the numbers."""
    out = {"file": Path(r["file"]).name, "style_used": r.get("style_used")}
    for k in ("paper", "reason", "connectivity", "revision", "checkpoint", "hint"):
        if r.get(k):
            out[k] = r[k] if k != "hint" else str(r[k])[:420]
    # pro2 extras, each one short: the design notes that were written, texts that were re-seated, symbols whose
    # embedded copy was redrawn so that their pin texts no longer overprint
    if r.get("notes"):
        out["notes"] = r["notes"]["placed"]
    tx = r.get("text") or {}
    if tx.get("moved") or tx.get("unplaced"):
        out["texts"] = {"moved": tx.get("moved", 0), **({"unplaced": tx["unplaced"][:6]} if tx.get("unplaced") else {})}
    if r.get("symfix"):
        out["symfix"] = sorted({x for v in r["symfix"].values() for x in v.get("refs", [])})[:8]
    m = r.get("metrics") or {}
    if m.get("hard"):
        out["hard"] = m["hard"][:6]
    if r.get("blocks"):
        out["blocks"] = [f"{b['title']} ({b['parts']})" for b in r["blocks"]][:10]
    if r.get("loose_parts"):
        out["loose_parts"] = r["loose_parts"][:12]
    rb = (r.get("readability") or {})
    b, a = rb.get("before"), rb.get("after")
    if b:
        # "pro2" (the default of sch_tidy) reports the exemplar-calibrated score v2 it is measured by; the older
        # styles report the composite they were tuned on (score_v1) - both are in sch_read query=readability
        key = "score" if str(r.get("style_used", "")).startswith("pro2") else "score_v1"
        rd = {"before": b.get(key, b["score"])}
        if a:
            rd["after"] = a.get(key, a["score"])
            for k in ("labels", "overlaps", "wire_crossings"):
                rd[k] = [b[k], a[k]]
        out["readability"] = rd
    if r.get("orphan_graphics"):
        out["orphan_graphics"] = r["orphan_graphics"]
    un = r.get("user_notes") or {}
    if un.get("moved") or un.get("left_in_place"):
        out["your_notes"] = (f"{un.get('moved', 0)} text note(s) moved under the blocks" + (
            f"; {un['left_in_place']} other graphic item(s) left where they were - they may now sit on top of the "
            f"new drawing: look at the render" if un.get("left_in_place") else ""))
    if r.get("tried"):
        out["fallbacks"] = [f"{t['style']}: " + (t["refused"] if isinstance(t["refused"], str) else "; ".join(t["refused"][:2]))[:160]
                            for t in r["tried"]][:3]
    if r.get("dry_run"):
        out["dry_run"] = True
    return out


DEFAULT_STYLE = "pro2"        # what sch_tidy draws when the caller names no style ("pro" and "labels" stay selectable)


def fallback(requested: str, r: dict) -> str | None:
    """One sentence when the sheet was written in another style than the one asked for: which, and why the gate or
    the layout refused the better ones (None when the requested style was used or nothing was drawn)."""
    su = r.get("style_used")
    if su in (None, "kept", "unchanged", requested) or not r.get("tried"):
        return None                                   # e.g. route='off': "pro-plain" is the first rung, not a fallback
    why = []
    for t in r["tried"]:
        ref = t["refused"] if isinstance(t["refused"], str) else "; ".join(t["refused"][:2])
        why.append(f"{t['style']} refused ({' '.join(str(ref).split())[:110]})")
    return (f"asked for {requested}, drawn as {su}: " + "; ".join(why[:2]))[:300]


def tidy(pr, sheet=None, **kw) -> dict:
    kw.setdefault("style", DEFAULT_STYLE)
    r = engine.relayout(pr, sheet, **kw)
    out = compact(r)
    fb = fallback(kw["style"], r)
    if fb:
        out["fallback"] = fb
    return out


def summary(d: dict) -> str:
    su = d.get("style_used")
    if su == "kept":
        return f"{d['file']} left as it is: {d.get('reason', '')}"
    if su == "unchanged":
        return f"{d['file']} is already laid out; nothing to improve"
    rd = d.get("readability") or {}
    s = f"{d['file']} re-laid ({su}, {d.get('paper')}); connectivity {d.get('connectivity', '?')}"
    if d.get("fallback"):
        s += f"; {d['fallback']}"
    if "after" in rd:
        s += f"; readability {rd['before']} -> {rd['after']}"
    return s + (" (dry run - nothing written)" if d.get("dry_run") else "")


def readability(pr, sheet=None) -> dict:
    m = engine.readability(pr, sheet)
    m["overlap_list"] = m.get("overlap_list", [])[:8]
    return m


# ── implicit re-layout after sch_apply ──
def pre_state(pr, sheet, ops) -> str | None:
    """State of the target sheet BEFORE a batch: 'empty' | 'engine' | 'other' (None when it cannot be read)."""
    try:
        sheets = {op.get("sheet") for op in ops if isinstance(op, dict) and op.get("sheet")}
        f, spath = O.sheet_file(pr, sheet or (sheets.pop() if len(sheets) == 1 else None))
        if not f.exists():
            return "empty"
        st = engine.sheet_state(Schematic.load(f, pr.name, spath))
        return st if st in ("empty", "engine") else "other"
    except (ToolError, OSError, ValueError):
        return None


def wants_layout(ops) -> bool:
    """True when the batch changes the circuit and the caller chose no position anywhere."""
    structural = False
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind in _STRUCTURAL:
            structural = True
        # a position anywhere = the caller is arranging the sheet. `wire` from/to pins and `label` on a pin carry
        # no position (they only say what is connected), so they do not switch the re-layout off
        if kind in ("move", "text") or any(k in op for k in ("at", "rot", "by", "points", "mirror")):
            return False
        if kind in ("wire", "label"):
            structural = True
    return structural


def near_hints(ops, refs: dict | None) -> dict:
    """{new ref: ref it was placed `near`} of a batch (ids resolved through the batch's id -> ref map)."""
    refs = refs or {}
    out = {}
    for op in ops:
        if isinstance(op, dict) and op.get("op") == "place" and op.get("near"):
            r = refs.get(str(op.get("id"))) or op.get("ref")
            n = refs.get(str(op["near"]), str(op["near"]))
            if r and "?" not in str(r):
                out[str(r)] = n
    return out


def after_apply(pr, file: Path, ops, pre: str | None, mode: str, while_open: str = "refuse",
                refs: dict | None = None) -> dict | None:
    """Re-lay the sheet once after a batch when nobody arranged it by hand. Returns {'note', 'relaid', ...}."""
    if mode == "keep":
        return None
    if mode != "pro":
        if pre not in ("empty", "engine"):
            return {"note": "kept (this sheet was not laid out by Copper's layout engine or was edited by hand since - "
                            "run sch_tidy to re-lay it)", "relaid": False} if pre == "other" and wants_layout(ops) else None
        if not wants_layout(ops):
            return None
    try:
        # `pre` engine / empty: everything on the sheet was drawn by Copper before this batch and the batch chose
        # no position, so there is no hand drawing to protect - the stubs sch_apply just added must not make the
        # sheet look "hand-wired" (that silently disabled the re-layout for every edit after the first)
        r = engine.relayout(pr, str(file), while_open=while_open, implicit=True,
                            force=(mode == "pro" or pre in ("empty", "engine")), near=near_hints(ops, refs))
    except ToolError as e:
        return {"note": f"kept ({e.code}: {e.message[:140]})", "relaid": False}
    su = r.get("style_used")
    if su in ("kept", "unchanged") or not r.get("revision"):
        return {"note": f"kept ({r.get('reason') or 'already laid out'})", "relaid": False}
    rb = r.get("readability") or {}
    return {"note": f"re-laid ({su}, {r.get('paper')}; netlist identical; readability "
                    f"{(rb.get('after') or {}).get('score')}) - positions changed, read them with sch_read",
            "relaid": True, "revision": r["revision"]}
