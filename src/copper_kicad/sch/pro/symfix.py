"""Symbols whose pin texts overprint (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md section 8).

Two library defects no placement can remove:

* stacked pins - two or more VISIBLE pins on one coordinate (USB-C "A4" on "B9"): their numbers print on top of
  each other;
* over-long pin numbers - a number such as "A1B12" on a 2.54 mm pin is wider than the pin and runs into the pin
  name inside the body.

Level 1 (always): detect, reserve the number strip as an obstacle, say so in a `hint`.
Level 2 (symfix="auto"): the sheet's EMBEDDED copy of the library symbol is replaced by a derived one under the
same key (the instance, its lib_id and every field stay as they are). The design asked for a separate "<name>_cu"
symbol selected with `lib_name`; measured on KiCad 10 (bench/sch/symfix_probe.py --lib-name) that changes the
netlist - kicad-cli writes the lib_name into the component's libsource and libparts - so G1 refuses it, always.
Replacing the embedded copy in place leaves the exported netlist byte-identical. In the derived symbol

* the 2nd..nth pin of a stack moves to the free rows next to it (2.54 mm pitch) and the body rectangle grows to
  hold them - the FIRST pin keeps its tip, so a later "Update symbols from library" drops the others back onto a
  point that is still wired;
* `pin_names (offset ..)` grows until the names clear the long numbers (only when both sides' names still fit).

Nothing else changes: no pin is hidden, retyped, renamed or merged, and no library file is touched. The gate
(gate.symfix_ok) accepts the derived symbol only when every pin's (number, name, type, shape, unit, body
style, hidden, alternates, length, direction) and every property / flag equals the original's.
"""
from __future__ import annotations

import copy
import math
import re

from ... import sexpr
from ...sexpr import Sym
from ..model import Schematic, text_width
from ...parts import importer as _importer

# Owner rule "default symbols first" (docs/internal/SCHEMATIC_PRO_V2_DESIGN.md, 2026-11-11): a symbol of a KiCad stock library
# - or of any library the user maintains - is NEVER redrawn. Level 2 is confined to the symbols Copper generated
# itself (the conversions made by parts/importer.py, library nickname importer.LIB); everything else gets the
# Level 1 hint and stays as its library draws it.
GENERATED_LIBS = frozenset({_importer.LIB})


def may_redraw(key: str) -> bool:
    """Level 2 is allowed for this lib_symbols key: only Copper-generated symbols, never a stock / user library."""
    k = str(key)
    return ":" in k and k.split(":", 1)[0] in GENERATED_LIBS


SUFFIX = "_cu"
PITCH = 2.54
CLEAR = 0.6               # air between a pin number and the pin name it would run into


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _yes(node, key) -> bool:
    for e in node[1:]:
        if e == key:
            return True
        if isinstance(e, list) and e and e[0] == key:
            return len(e) == 1 or str(e[1]) == "yes"
    return False


def item_name(key: str) -> str:
    return str(key).split(":", 1)[-1]


def _units(ls):
    """[(sub symbol node, unit, style)]"""
    out = []
    for sub in sexpr.find_all(ls, "symbol"):
        m = re.match(r".*_(\d+)_(\d+)$", str(sub[1]))
        out.append((sub, int(m.group(1)) if m else 0, int(m.group(2)) if m else 0))
    return out


def _pin(p):
    at = sexpr.find(p, "at") or [Sym("at"), 0, 0, 0]
    nm, no = sexpr.find(p, "name"), sexpr.find(p, "number")
    font = sexpr.find(sexpr.find(no, "effects") or [], "font") if no is not None else None
    sz = sexpr.find(font, "size") if font else None
    return {"node": p, "x": _f(at[1]), "y": _f(at[2]), "angle": int(round(_f(at[3]) if len(at) > 3 else 0)) % 360,
            "length": _f(sexpr.value(p, "length", 2.54)), "number": str(no[1]) if no is not None and len(no) > 1 else "",
            "name": str(nm[1]) if nm is not None and len(nm) > 1 else "", "hidden": _yes(p, "hide"),
            "num_size": _f(sz[1], 1.27) if sz and len(sz) > 1 else 1.27}


def _name_offset(ls):
    pn = sexpr.find(ls, "pin_names")
    if pn is None:
        return 0.508, False
    off = sexpr.find(pn, "offset")
    return (_f(off[1], 0.508) if off is not None and len(off) > 1 else 0.508), _yes(pn, "hide")


def _numbers_hidden(ls) -> bool:
    pn = sexpr.find(ls, "pin_numbers")
    return pn is not None and _yes(pn, "hide")


def _refkey(s):
    m = re.match(r"([A-Za-z]*)(\d*)(.*)", str(s))
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, m.group(3))


def defects(ls) -> dict:
    """{'stacks': [[pin numbers]], 'long': [pin numbers], 'need_offset': mm} of one embedded library symbol."""
    stacks, long_, need = [], [], 0.0
    if ls is None or _numbers_hidden(ls):
        return {"stacks": [], "long": [], "need_offset": 0.0}
    off, names_hidden = _name_offset(ls)
    for sub, _u, _s in _units(ls):
        at: dict = {}
        for p in sexpr.find_all(sub, "pin"):
            q = _pin(p)
            if q["hidden"]:
                continue
            at.setdefault((round(q["x"], 3), round(q["y"], 3), q["angle"]), []).append(q)
            tw = text_width(q["number"], q["num_size"])
            over = (tw - q["length"]) / 2
            if over > 0.25 and q["name"] not in ("", "~") and not names_hidden and off > 0 and over + 0.2 > off:
                long_.append(q["number"])
                need = max(need, over + CLEAR)
        for qs in at.values():
            if len(qs) > 1:
                stacks.append(sorted((q["number"] for q in qs), key=_refkey))
    return {"stacks": sorted(stacks), "long": sorted(long_, key=_refkey), "need_offset": round(need, 3)}


def scan(sch: Schematic) -> dict:
    """{lib_symbols key: defects} for the symbols placed on the sheet that have one."""
    out = {}
    for key in sorted({s.lib_name for s in sch.symbols(include_power=False)}):
        d = defects(sch.lib_symbol(key))
        if d["stacks"] or d["long"]:
            out[key] = d
    return out


def hints(sch: Schematic) -> list:
    """Level 1: one line per symbol that still overprints on this sheet."""
    refs: dict = {}
    for s in sch.symbols(include_power=False):
        refs.setdefault(s.lib_name, []).append(s.ref)
    out = []
    for key, d in scan(sch).items():
        who = ", ".join(sorted(set(refs.get(key, [])), key=_refkey)[:4])
        what = []
        if d["stacks"]:
            what.append("stacked pins " + "; ".join("/".join(st) for st in d["stacks"][:4]))
        if d["long"]:
            what.append("pin numbers wider than their pins (" + ", ".join(d["long"][:6]) + ")")
        fix = ("sch_tidy(symfix='auto') draws them apart, or edit the symbol in its library" if may_redraw(key) else
               "it is a stock / user library symbol and is left exactly as its library draws it (symfix never "
               "redraws those)")
        out.append(f"{who}: the library symbol {item_name(key)} has " + " and ".join(what) +
                   " - the numbers overprint; " + fix)
    return out


def number_strips(sch: Schematic) -> list:
    """Sheet boxes of the pin numbers that reach beyond their pin (obstacles for wires ends, labels and texts)."""
    out = []
    for s in sch.symbols(include_power=False):
        ls = sch.lib_symbol(s.lib_name)
        if ls is None or _numbers_hidden(ls):
            continue
        for p in s.pins_world():
            if p.get("hidden"):
                continue
            tw = text_width(str(p["number"]))
            ln = float(p.get("length", 2.54) or 2.54)
            if tw <= ln + 0.5:
                continue
            ox, oy = p["out"]
            mx, my = p["xy"][0] - ox * ln / 2, p["xy"][1] - oy * ln / 2
            if oy == 0:
                out.append([mx - tw / 2, my - 1.75, mx + tw / 2, my - 0.2])
            else:
                out.append([mx - 1.75, my - tw / 2, mx - 0.2, my + tw / 2])
    return out


# ───────────────────────── Level 2: the derived symbol ─────────────────────────
def _fan(ls) -> int:
    """Move the 2nd..nth pin of every visible stack to free neighbouring rows; grow the body rectangle. -> number
    of pins moved (0 when no stack could be fanned)."""
    moved = 0
    for sub, unit, style in _units(ls):
        pins = [_pin(p) for p in sexpr.find_all(sub, "pin")]
        at: dict = {}
        for q in pins:
            if not q["hidden"]:
                at.setdefault((round(q["x"], 3), round(q["y"], 3), q["angle"]), []).append(q)
        taken = {(round(q["x"], 3), round(q["y"], 3), q["angle"]) for q in pins}
        rects = []
        for s2, u2, st2 in _units(ls):
            if u2 in (0, unit) and st2 in (0, style):
                rects += sexpr.find_all(s2, "rectangle")
        for key in sorted(at):
            qs = sorted(at[key], key=lambda q: _refkey(q["number"]))
            if len(qs) < 2:
                continue
            x, y, ang = key
            horiz = ang in (0, 180)                      # the pin runs along x: its row neighbours differ in y
            ok = None
            for sg in (-1, 1):                           # lib coordinates are y-up: -1 = down the sheet first
                spots = [(x, round(y + sg * i * PITCH, 3), ang) if horiz else (round(x + sg * i * PITCH, 3), y, ang)
                         for i in range(1, len(qs))]
                clash = False
                for sp in spots:
                    for t in taken:
                        if t[2] == ang and abs(t[0] - sp[0]) < PITCH - 0.01 and abs(t[1] - sp[1]) < PITCH - 0.01:
                            clash = True
                if not clash:
                    ok = spots
                    break
            if ok is None:
                continue
            # the body edge the pins end on: a rectangle that holds the pin's inner end
            ix = x + {0: 1, 180: -1}.get(ang, 0) * qs[0]["length"]
            iy = y + {90: 1, 270: -1}.get(ang, 0) * qs[0]["length"]
            host = None
            for r in rects:
                a, b = sexpr.find(r, "start"), sexpr.find(r, "end")
                if a is None or b is None:
                    continue
                x0, x1 = sorted((_f(a[1]), _f(b[1])))
                y0, y1 = sorted((_f(a[2]), _f(b[2])))
                if x0 - 0.02 <= ix <= x1 + 0.02 and y0 - 0.02 <= iy <= y1 + 0.02:
                    host = (r, a, b)
                    break
            if host is None:
                continue
            for q, sp in zip(qs[1:], ok):
                a = sexpr.find(q["node"], "at")
                a[1], a[2] = _num(sp[0]), _num(sp[1])
                taken.add(sp)
                moved += 1
            _r, a, b = host
            last = ok[-1]
            if horiz:
                lo, hi = min(_f(a[2]), _f(b[2])), max(_f(a[2]), _f(b[2]))
                lo, hi = min(lo, last[1] - PITCH / 2), max(hi, last[1] + PITCH / 2)
                if _f(a[2]) <= _f(b[2]):
                    a[2], b[2] = _num(lo), _num(hi)
                else:
                    a[2], b[2] = _num(hi), _num(lo)
            else:
                lo, hi = min(_f(a[1]), _f(b[1])), max(_f(a[1]), _f(b[1]))
                lo, hi = min(lo, last[0] - PITCH / 2), max(hi, last[0] + PITCH / 2)
                if _f(a[1]) <= _f(b[1]):
                    a[1], b[1] = _num(lo), _num(hi)
                else:
                    a[1], b[1] = _num(hi), _num(lo)
    return moved


def _num(v):
    v = round(float(v), 4)
    return int(v) if v == int(v) else v


def _names_fit(ls, off: float) -> bool:
    """With the names `off` inside the body edge: do the names of two pins that face each other still fit?"""
    for sub, unit, style in _units(ls):
        pins = [_pin(p) for p in sexpr.find_all(sub, "pin")]
        pins = [q for q in pins if not q["hidden"] and q["name"] not in ("", "~")]
        for i, a in enumerate(pins):
            for b in pins[i + 1:]:
                if {a["angle"], b["angle"]} == {0, 180} and abs(a["y"] - b["y"]) < 1.9:
                    span = abs(a["x"] - b["x"]) - a["length"] - b["length"]
                elif {a["angle"], b["angle"]} == {90, 270} and abs(a["x"] - b["x"]) < 1.9:
                    span = abs(a["y"] - b["y"]) - a["length"] - b["length"]
                else:
                    continue
                if text_width(a["name"]) + text_width(b["name"]) + 2 * off + 0.4 > span:
                    return False
    return True


def derive(ls, new_name: str | None = None):
    """-> (derived lib symbol tree, {'fanned': pins moved, 'offset': new name offset or None}) or None when nothing
    can be improved. new_name renames the tree and its units (only the probe uses that)."""
    d = defects(ls)
    if not d["stacks"] and not d["long"]:
        return None
    t = copy.deepcopy(ls)
    old = item_name(str(t[1]))
    info = {"fanned": 0, "offset": None}
    if d["stacks"]:
        info["fanned"] = _fan(t)
    if d["long"]:
        off, _hid = _name_offset(t)
        want = math.ceil((d["need_offset"] - 1e-6) / 0.254) * 0.254
        if want > off + 0.01 and _names_fit(t, want):
            pn = sexpr.find(t, "pin_names")
            if pn is None:
                pn = [Sym("pin_names")]
                t.insert(2, pn)
            o = sexpr.find(pn, "offset")
            if o is None:
                pn.insert(1, [Sym("offset"), _num(want)])
            else:
                o[1:] = [_num(want)]
            info["offset"] = round(want, 3)
    if not info["fanned"] and info["offset"] is None:
        return None
    if new_name:
        t[1] = new_name
        for sub in sexpr.find_all(t, "symbol"):
            nm = str(sub[1])
            if nm.startswith(old + "_"):
                sub[1] = new_name + nm[len(old):]
    return t, info


def apply(sch: Schematic, lib_name: bool = False) -> dict:
    """Level 2 on a loaded sheet, in place: the embedded library symbol of every placed part that overprints and
    can be improved is replaced by its derived form. -> {lib_symbols key: {'refs': [...], 'fanned': n, 'offset':
    mm | None}}. lib_name=True is the variant KiCad's netlist does not survive (kept for the probe and the tests):
    a separate "<name>_cu" symbol that the instances select with `lib_name`."""
    out = {}
    by: dict = {}
    for s in sch.symbols(include_power=False):
        by.setdefault(s.lib_name, []).append(s)
    for key in sorted(by):
        ls = sch.lib_symbol(key)
        if ls is None or sexpr.find(ls, "extends") is not None or not may_redraw(key):
            continue
        new = item_name(key) + SUFFIX
        if lib_name and (item_name(key).endswith(SUFFIX) or sch.lib_symbol(new) is not None):
            continue
        got = derive(ls, new if lib_name else None)
        if got is None:
            continue
        tree, info = got
        sch.embed_tree(new if lib_name else key, tree)
        if lib_name:
            for s in by[key]:
                ln = sexpr.find(s.node, "lib_name")
                if ln is None:
                    s.node.insert(1, [Sym("lib_name"), new])
                else:
                    ln[1:] = [new]
        out[new if lib_name else key] = dict(info, **{"from": key, "refs": sorted({s.ref for s in by[key]}, key=_refkey)})
    return out


# ───────────────────────── the equivalence the gate asks for ─────────────────────────
def _pin_tuple(sub_unit, sub_style, p):
    q = _pin(p)
    alts = sorted(sexpr.dumps(a) for a in sexpr.find_all(p, "alternate"))
    return (q["number"], q["name"], str(p[1]) if len(p) > 1 else "", str(p[2]) if len(p) > 2 and not isinstance(p[2], list) else "",
            sub_unit, sub_style, q["hidden"], tuple(alts), round(q["length"], 4), q["angle"],
            sexpr.dumps(sexpr.find(p, "name") or []), sexpr.dumps(sexpr.find(p, "number") or []))


def signature(ls) -> dict:
    """Everything of an embedded library symbol that symfix may NOT change."""
    pins = []
    graphics = 0
    for sub, u, s in _units(ls):
        for p in sexpr.find_all(sub, "pin"):
            pins.append(_pin_tuple(u, s, p))
        graphics += sum(1 for g in sub[2:] if isinstance(g, list) and g and g[0] != "pin")
    head = []
    for e in ls[2:]:
        if not isinstance(e, list) or not e or e[0] == "symbol":
            continue
        if e[0] == "pin_names":
            head.append(("pin_names", _yes(e, "hide")))          # the offset is the one thing that may differ
            continue
        head.append(sexpr.dumps(e))
    if sexpr.find(ls, "pin_names") is None:
        head.append(("pin_names", False))
    return {"pins": sorted(pins), "head": sorted(head, key=str), "units": sorted((u, s) for _n, u, s in _units(ls)),
            "graphics": graphics}


def equivalent(orig, derived) -> list:
    """Reasons why `derived` is NOT an admissible symfix of `orig` ([] = admissible)."""
    if orig is None or derived is None:
        return ["symbol missing"]
    a, b = signature(orig), signature(derived)
    out = []
    if str(orig[1]) != str(derived[1]):
        out.append("the symbol's name differs")
    if a["pins"] != b["pins"]:
        na, nb = {p[0]: p for p in a["pins"]}, {p[0]: p for p in b["pins"]}
        bad = sorted(set(na) ^ set(nb)) or sorted(k for k in na if na[k] != nb[k])
        out.append("pins differ (" + ", ".join(bad[:4]) + ")")
    if a["head"] != b["head"]:
        out.append("properties or flags differ")
    if a["units"] != b["units"] or a["graphics"] != b["graphics"]:
        out.append("units or graphics differ")
    return out
