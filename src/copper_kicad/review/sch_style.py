"""Schematic drawing style: rules for a schematic a stranger can read "in the blink of an eye".

The rules come from the KiCon talk on drawing good schematics (https://www.youtube.com/watch?v=X0hd_v8qRiY),
including the suggestions made in its Q&A, and the references it names: Tufte's *The Visual Display of
Quantitative Information*, the schematic appendix of *The Art of Electronics*, IEEE Std 315 and Olin Lathrop's
"Rules and guidelines for drawing good schematics" on Electrical Engineering Stack Exchange.

`check(pr)` measures the rules that can be measured from the saved sheet files alone (no KiCad call, no netlist):
it is the `schematic_style` pass of design_review, run only when asked for (checks=['schematic_style']). Rules that
need judgement stay in the guidance. Every finding is advice: the pass never reports an error, and a `fix` either
leaves the netlist as it is or is a plain sentence.

On a sheet whose drawing is still exactly what Copper's layout engine produced, the wiring rules (C1, C2, C6 and
sideways power symbols) are not checked: there the engine draws in-block nets as wires and puts a label only where a
net leaves its block, by design.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .. import sexpr
from ..kicad import libs as LIBS
from ..sch import symbols as G
from ..sch.model import Schematic
from .common import finding, is_ground_net, is_power_net

# ── the rules ─────────────────────────────────────────────────────────────
# id, short title, the rule as the agent should apply it. Ordered as a reader meets them.
RULES: list[tuple[str, str, str]] = [
    ("S0", "Blink test",
     "A reader should recognise every circuit on a sheet in about 100 ms. Standardise, simplify, modularise and "
     "communicate function. Draw the classic design patterns (divider, RC filter, follower, inverter, LDO, crystal) "
     "in their textbook shape so they are recognised, not decoded. Apply every rule with judgement: follow it until "
     "it plainly makes the sheet worse, then break it and say why in a note."),
    ("F1", "Left-to-right flow",
     "Signal and power flow left to right: inputs on the left, outputs on the right. When that cannot hold, give "
     "the path enough whitespace that the eye can follow it. Enables, soft-start and other control inputs sit with "
     "the inputs; a switcher's feedback pin on the right is fine (it belongs to the output circuit)."),
    ("F2", "Supplies up, grounds down",
     "Ground symbols point down and supply symbols point up, the way the stock power symbols are drawn. Never "
     "sideways, never upside down. KiCad's stock negative-rail symbols (-5V, VEE) point up; keep them as drawn."),
    ("F3", "Show direction",
     "Where direction is not obvious (bidirectional or RF chains, TX and RX paths) draw arrows or note which way "
     "the signal flows."),
    ("Y1", "Functional symbols",
     "Symbols are drawn by function, not as the package pinout: supplies on top, grounds at the bottom, inputs "
     "left, outputs right, related pins (crystal, bus, port) together. The layout shows the package; cross-probe "
     "from the schematic instead of pasting a pinout image. Only RF parts whose schematic is the layout are "
     "exempt."),
    ("Y2", "IEEE 315 symbols",
     "Use IEEE Std 315 symbol conventions. Every mark has a meaning: the envelope circle on a transistor marks a "
     "discrete package, so draw it on discrete transistors."),
    ("Y3", "Stacked duplicate power pins",
     "Stack duplicate supply and ground pins (same position, one visible) so a symbol shows one of each, unless "
     "the design is safety-critical, where every pin is shown. Do not hide them: in KiCad a hidden power-input pin "
     "joins a global net named after the pin."),
    ("Y4", "Filled symbol bodies",
     "Symbol bodies have a background fill, not a transparent body."),
    ("G1", "Whitespace groups",
     "Whitespace is the strongest separator. Group the parts of one function and separate groups with space. "
     "Boxes are visual noise: use a frame only when the sheet is tight."),
    ("G2", "Titled blocks",
     "Give every functional group a title (\"USB-C input\", \"3V3 LDO\")."),
    ("G3", "Minimal but not crammed sheets",
     "Use as few sheets as you can without cramming: one sheet per functional module (power supply, MCU, "
     "connectors), no paging through eight sheets for nothing. Prefer 11x17 in (tabloid / A3-class) sheets; the "
     "schematic is read on screen. When a sheet runs out of whitespace, add a sheet."),
    ("G4", "Block diagram on the root sheet",
     "On a multi-sheet design the root sheet is a block diagram made of hierarchical sheet symbols, wired in "
     "signal-flow order, with no circuitry of its own."),
    ("C1", "Wires over labels",
     "Connect with wires whenever you can: a wire is seen in 100 ms, a label must be searched for. Labels are for "
     "off-sheet connections and for runs that would be truly awkward to draw."),
    ("C2", "No label hops between neighbours",
     "Never connect two nearby symbols with a pair of labels. Draw the wire."),
    ("C3", "Name wires too",
     "Put a net label on important wired nets as well, so a reader can match names and double-check."),
    ("C4", "Global and hierarchical labels",
     "To the reader global and hierarchical labels are equal: use hierarchical labels for sheets on the block "
     "diagram, global labels for nets that truly go everywhere."),
    ("C5", "Inter-sheet references",
     "On a multi-sheet design turn on inter-sheet references (Schematic Setup > General > Formatting) so every "
     "global label says which sheet(s) its net goes to."),
    ("C6", "At most three wires at a junction",
     "A junction joins at most three wires. Never draw a four-way junction: stagger it into two tees."),
    ("C7", "Sense wires at 45 degrees",
     "A sense (Kelvin) wire leaves the conductor it senses at 45 degrees at the exact sense point."),
    ("P1", "Descriptive rail names",
     "Name rails after their voltage or source (+3V3, +5V, VBUS, +BATT), not a bare VCC or VDD. Names from "
     "KiCad's power library become power symbols with `connect`; a custom name such as +3V3_MCU needs the "
     "`power` op."),
    ("P2", "Rail specs on the sheet",
     "Annotate each rail with min / typ / max voltage and typical / maximum current, e.g. \"VBUS 4.75 / 5.0 / "
     "5.25 V, 500 mA max\", or at least \"9-19 V in\" and \"3V3, 2 A max\"."),
    ("P3", "Power through power symbols",
     "Distribute power with power symbols (or wires), never with ordinary net labels."),
    ("P4", "0 V is not earth",
     "Circuit common (0 V / GND), chassis and earth are different nets with different symbols. Do not draw the "
     "earth symbol for circuit common; name what each reference is."),
    ("D1", "Notes everywhere",
     "Use text notes freely: what a test point should read (voltage, waveform, scope setting), why a value was "
     "chosen, what the block does."),
    ("D2", "Call out the weird",
     "Say so in a note when something is intentional but odd: pins left floating on purpose, inputs shorted on "
     "purpose, a part fitted only for one variant."),
    ("D3", "Full passive specs",
     "Resistors, capacitors, inductors and diodes carry value, tolerance, power or voltage rating, and package "
     "(\"10k 1% 0.063W 0402\"). Check the package can dissipate the rating. Not needed for ICs."),
    ("D4", "Standard-parts note on every sheet",
     "Every sheet with passives carries a note with the defaults it really uses (\"Unless noted: resistors 1% "
     "0.063W 0402; capacitors X7R 16V 0402\") so parts that follow them need only their value."),
    ("K1", "Checklist",
     "Before release: power is distributed through power symbols, every supply points the right way, important "
     "nets are descriptively named, MOSFETs are oriented correctly for their body diode, every IC has its bypass "
     "capacitors, and the design fails safe, with modules (power supplies, I/O) grouped separately."),
]

RULE_TITLE = {r[0]: r[1] for r in RULES}

# rule id -> the finding types the pass reports for it (the rest is guidance only)
CHECKED: dict[str, tuple[str, ...]] = {
    "F2": ("power_symbol_direction",), "Y1": ("package_shaped_symbol",), "Y2": ("transistor_without_envelope",),
    "Y4": ("symbol_body_unfilled",), "G3": ("sheet_crowded",), "G4": ("root_not_block_diagram",),
    "C1": ("labels_instead_of_wires",), "C2": ("label_between_neighbours",), "C5": ("intersheet_refs_off",),
    "C6": ("four_way_junction",), "P1": ("generic_rail_name",), "P2": ("rail_without_spec",),
    "P4": ("earth_symbol_on_common", "zero_volt_and_gnd"), "D1": ("test_point_no_expectation",),
    "D2": ("floating_input_unexplained",), "D3": ("passive_spec_incomplete",), "D4": ("no_standard_parts_note",),
}

# what the pass measures when it is less than the whole rule
CHECK_NOTE = {"D3": "the package only"}

# power-symbol values that say nothing about the rail (rule P1)
GENERIC_RAILS = {"VCC", "VDD", "VEE", "V+", "V-", "VS", "VS+", "VS-", "PWR", "POWER", "VSUPPLY", "SUPPLY", "VPOS",
                 "VNEG"}
_PASSIVE_REF = re.compile(r"^(R|C|L|FB|D|RN)\d", re.I)
# a size code or package name anywhere in a value, field or footprint name ("R_0402_1005Metric", "R0402", "D_SOD-123")
_PKG_RE = re.compile(r"(?<![0-9A-Za-z])(?:[A-Z]{1,3}_?)?(?:0075|01005|0201|0402|0603|0805|1008|1206|1210|1806|1812|"
                     r"2010|2220|2512)(?![0-9])|SOD-?\d+|SOT-?\d+|DO-?\d+|(?<![A-Za-z])SM[ABC](?![A-Za-z])|"
                     r"MELF|THT|axial|radial", re.I)
_STD_NOTE_RE = re.compile(r"unless\s+(?:otherwise\s+)?(?:noted|stated|specified)|standard\s+parts|all\s+resistors|"
                          r"all\s+capacitors|default\s+parts", re.I)
_VOLT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*V\b|\b\d+V\d+\b", re.I)
_AMP_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:m|µ|u)?A\b")
_TP_REF = re.compile(r"^TP\d", re.I)
_Q_LIB = re.compile(r"transistor|mosfet|bjt|jfet|igbt|^Device:Q_|:Q_", re.I)
# stock symbols drawn without an envelope circle on purpose: pre-biased "digital" transistors, thyristors, triacs
# (parts with more or fewer than three pins, such as dual FETs, are not judged either)
_Q_NO_CIRCLE_OK = re.compile(r"^Triac_Thyristor:|:(?:DT[A-D]\d|Q_SCR|Q_Triac|Q_Thyristor)", re.I)
_EARTH_LIB = re.compile(r"^power:(?:Earth|Earth_Protective|Earth_Clean|Chassis)", re.I)

# a 'machine' sheet is Copper's own stub-and-label drawing (sch_apply layout='keep', or older Copper): its layout
# engine applies the drawing rules when it redraws the sheet, so that is the fix offered there
TIDY_FIX = ("sch_tidy() redraws this stub-and-label sheet: in-block nets as wires, power symbols upright, no "
            "four-way junctions")
NEAR_LABEL_MM = 30.0          # two same-name local labels closer than this: the wire would have been short (C2)
CROWDED_PARTS = 45            # parts (power symbols excluded) beyond which one sheet is crammed (G3)
ROOT_CIRCUIT_PARTS = 4        # circuit parts tolerated beside sheet symbols on a root sheet (G4)


def _rule(f: dict, rid: str) -> dict:
    f["rule"] = rid
    return f


def _k(p) -> tuple:
    return (int(round(float(p[0]) * 100)), int(round(float(p[1]) * 100)))


def _texts(sch: Schematic) -> list[tuple[str, list]]:
    out = []
    for kind in ("text", "text_box"):
        for n in sch.items(kind):
            a = sexpr.find(n, "at")
            out.append((str(n[1]) if len(n) > 1 else "", [float(a[1]), float(a[2])] if a is not None else None))
    tb = sexpr.find(sch.tree, "title_block")
    if tb is not None:
        for e in tb[1:]:
            if isinstance(e, list) and len(e) > 1 and str(e[0]) in ("comment", "title"):
                out.append((str(e[-1]), None))
    return out


def _is_flag(s) -> bool:
    return s.lib_id.split(":")[-1] == "PWR_FLAG" or s.ref.startswith("#FLG")


def _is_rail(s) -> bool:
    """A power symbol that names a rail: not a PWR_FLAG, and not a pinless graphic (logos flagged as power)."""
    return (s.is_power and not _is_flag(s) and str(s.value).strip() not in ("", "~")
            and bool(s.unit_pins()))


def _sheet_name(pr, f: Path) -> str:
    return "" if f == pr.sch else f.stem


def tidy_refuses(sch: Schematic) -> bool:
    """True for sheets sch_tidy will not re-lay (buses, sheet symbols, directive labels, rule areas, shared sheets)."""
    return bool(any(sch.items(k) for k in ("bus", "bus_entry", "sheet", "directive_label", "netclass_flag", "rule_area"))
                or getattr(sch, "other_paths", None))


def layout_state(sch: Schematic) -> str:
    """'engine' when the sheet's drawing is still exactly what Copper's layout engine produced (text notes added
    later do not count), else the engine's own reading: 'machine' (Copper's stub-and-label style, only when sch_tidy
    can re-lay it), 'hand', 'empty'."""
    try:
        from ..sch.pro import emit as EM
        if EM.is_engine_layout(sch):
            return "engine"
        from ..sch.pro import engine as EN
        state = EN.sheet_state(sch)
    except Exception:  # noqa: BLE001 — without the engine every sheet is treated as hand-drawn
        return "hand"
    return "hand" if state == "machine" and tidy_refuses(sch) else state


def _connectivity(sch: Schematic):
    """Union-find over the wiring of one sheet: wire ends join their segment; a label anywhere on a wire joins it;
    a wire end or pin tip on the middle of another wire joins it only at a junction dot (KiCad's rule)."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    segs = [(_k(w["a"]), _k(w["b"])) for w in sch.wires()]
    hz: dict = {}                                      # y -> horizontal segments, x -> vertical ones
    vt: dict = {}
    for a, b in segs:
        union(a, b)
        if a[1] == b[1] and a[0] != b[0]:
            hz.setdefault(a[1], []).append((min(a[0], b[0]), max(a[0], b[0]), a))
        elif a[0] == b[0] and a[1] != b[1]:
            vt.setdefault(a[0], []).append((min(a[1], b[1]), max(a[1], b[1]), a))

    def through(k) -> list:
        """Segments whose interior (not their ends) passes through point k, as one end of each."""
        return [e for lo, hi, e in hz.get(k[1], ()) if lo < k[0] < hi] + \
               [e for lo, hi, e in vt.get(k[0], ()) if lo < k[1] < hi]
    juncs = {_k([float(a[1]), float(a[2])]) for n in sch.items("junction") for a in [sexpr.find(n, "at")] if a is not None}
    labels = {_k(l["at"]) for l in sch.labels()}
    for k in juncs | labels:
        for e in through(k):
            union(k, e)
    return find, segs, juncs, through


# ── per-sheet checks ──────────────────────────────────────────────────────
def _negative(value) -> bool:
    v = str(value).upper()
    return v.startswith("-") or v in ("VEE", "V-", "VNEG")


def power_direction(sch: Schematic, state: str = "hand") -> list[dict]:
    """F2: a power symbol drawn sideways, or upside down against its library drawing."""
    bad = []
    for s in sch.symbols():
        if not s.is_power or _is_flag(s):
            continue
        ls = sch.lib_symbol(s.lib_name)
        pins = s.unit_pins()
        if ls is None or len(pins) != 1:
            continue
        lb = LIBS.unit_bbox(ls, s.unit, s.body_style, include_pins=False)
        ldx, ldy = (lb[0] + lb[2]) / 2 - pins[0]["at"][0], (lb[1] + lb[3]) / 2 - pins[0]["at"][1]
        if abs(ldy) <= abs(ldx) + 0.01:
            continue                                   # a symbol that is not drawn up or down in its library
        want_down = ldy < 0                            # library y points up
        bb = G.bbox_world(s.at, s.matrix, lb)
        px, py = s.pins_world()[0]["xy"]
        dx, dy = (bb[0] + bb[2]) / 2 - px, (bb[1] + bb[3]) / 2 - py
        if abs(dx) > abs(dy) + 0.01:
            if state != "engine":                      # the engine's last resort on a crowded rail, by design
                bad.append((s, "sideways"))
        elif (dy > 0) != want_down:
            bad.append((s, "upside down"))
    if not bad:
        return []
    said = "; ".join(f"{s.ref} {s.value} {how}" for s, how in bad[:6])
    first = bad[0][0]
    if state == "machine":
        fix = TIDY_FIX
    elif any(s.mirror for s, _ in bad):
        fix = "re-place each listed symbol unmirrored at rot 0, or flip it back in KiCad"
    else:
        fix = ("turn each listed symbol back to rot 0, the way its library draws it: in KiCad, or with sch_apply move "
               "ref + rot 0 per symbol where its pin has room (a refused move changes nothing)")
    # a negative rail drawn pointing down is a common convention: advice only
    upside = any(how == "upside down" and not _negative(s.value) for s, how in bad)
    return [_rule(finding(
        "warning" if upside and state != "machine" else "info", "power_symbol_direction",
        f"{len(bad)} power symbol(s) point the wrong way ({said}{' …' if len(bad) > 6 else ''}): grounds point down, "
        "supplies up, never sideways",
        fix=fix, items=[s.ref for s, _ in bad][:30], at=list(first.at)), "F2")]


def junctions(sch: Schematic, state: str = "hand") -> list[dict]:
    """C6: a point where four or more wires meet (pins are not wires)."""
    if state == "engine":
        return []
    _find, segs, juncs, through = _connectivity(sch)
    deg: dict = {}
    for a, b in segs:
        if a == b:
            continue
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    for k in juncs:                                    # a wire passing through a junction dot is two arms
        n = 2 * len(through(k))
        if n:
            deg[k] = deg.get(k, 0) + n
    pts = [[k[0] / 100, k[1] / 100] for k, n in sorted(deg.items()) if n >= 4]
    if not pts:
        return []
    said = ", ".join(f"({x:.2f}, {y:.2f})" for x, y in pts[:4])
    return [_rule(finding(
        "info" if state == "machine" else "warning", "four_way_junction",
        f"{len(pts)} junction(s) where four wires meet: {said}{' …' if len(pts) > 4 else ''}. A four-way junction "
        "reads like a crossing",
        fix=TIDY_FIX if state == "machine" else
            "stagger each into two tees at least 2.54 mm apart (in KiCad, or sch_apply delete wire_at + wire points); "
            "keep every pin on its net",
        at=pts[0]), "C6")]


def label_hops(sch: Schematic, state: str = "hand") -> list[dict]:
    """C1/C2: local labels standing in for a short wire on the same sheet."""
    if state in ("engine", "empty"):
        return []
    find, _segs, _j, _o = _connectivity(sch)
    by: dict = {}
    for l in sch.labels():
        if l["kind"] == "local":
            by.setdefault(l["net"], []).append(l["at"])
    pins_at: dict = {}                                 # wire group -> [(xy, "REF.NUMBER")]
    for s in sch.symbols(include_power=False):
        for p in s.pins_world():
            pins_at.setdefault(find(_k(p["xy"])), []).append((p["xy"], f"{s.ref}.{p['number']}"))

    def pins_for(a, b):
        """Two pins on the labels' net, one wired to each label, on two different parts (a wire between two pins of
        one part reads as a bypass), the closest such pair; (None, None) when there is none."""
        ca, cb = pins_at.get(find(_k(a)), [])[:60], pins_at.get(find(_k(b)), [])[:60]
        pairs = [(x, y) for x in ca for y in cb if x[1].split(".")[0] != y[1].split(".")[0]]
        if not pairs:
            return None, None
        x, y = min(pairs, key=lambda xy: math.dist(xy[0][0], xy[1][0]))
        return x[1], y[1]
    hops, unwired = [], []
    for net, pts in sorted(by.items()):
        if len(pts) < 2 or is_power_net(net) or is_ground_net(net):
            continue
        groups = {find(_k(p)) for p in pts}
        if len(groups) < 2:
            continue                                   # every label sits on one wired run: C3, not a hop
        unwired.append(net)
        close = [(a, b) for i, a in enumerate(pts) for b in pts[i + 1:]
                 if find(_k(a)) != find(_k(b)) and math.dist(a, b) < NEAR_LABEL_MM]
        if close:
            a, b = min(close, key=lambda ab: math.dist(*ab))
            hops.append((net, a, b, *pins_for(a, b)))
    out = []
    tidy = state == "machine"
    if hops:
        said = ", ".join(f"{n} ({math.dist(a, b):.0f} mm)" for n, a, b, _, _ in hops[:8])
        h0 = next((h for h in hops if h[3] and h[4]), hops[0])     # net, at and fix all from one hop
        if h0[3] and h0[4]:
            fix = (f'sch_apply(ops=[{{"op":"wire","from":"{h0[3]}","to":"{h0[4]}"}}]) joins two pins already on '
                   f"{h0[0]} (the netlist does not change); the labels can stay as names")
        elif tidy:
            fix = TIDY_FIX
        else:
            fix = "draw a wire between the two labelled points in KiCad; the labels can stay as names"
        out.append(_rule(finding(
            "info", "label_between_neighbours",
            f"{len(hops)} net(s) jump by label between neighbouring points on one sheet: {said}"
            f"{' …' if len(hops) > 8 else ''}. A wire is seen at once; a label must be searched for",
            fix=fix, at=list(h0[1]), net=h0[0]), "C2"))
    far = [n for n in unwired if n not in {h[0] for h in hops}]
    if len(far) >= 3:
        out.append(_rule(finding(
            "info", "labels_instead_of_wires",
            f"{len(far)} nets are joined only by repeated local labels on this sheet ({', '.join(far[:8])}"
            f"{' …' if len(far) > 8 else ''}): wire the ones that can be drawn without an awkward run",
            fix=TIDY_FIX if tidy else
                "sch_apply wire from / to (pin numbers) for each short run; labels stay for nets that leave the block",
            items=far[:20]), "C1"))
    return out


def earth_symbols(sch: Schematic, state: str = "hand") -> list[dict]:
    """P4: an earth / chassis symbol on circuit common, or 0 V and GND side by side."""
    out = []
    vals: dict = {}
    for s in sch.symbols():
        if s.is_power and not _is_flag(s):
            vals.setdefault(str(s.value).upper(), []).append(s)
    for s in [s for v in vals.values() for s in v]:
        if _EARTH_LIB.match(s.lib_id) and str(s.value).upper() in ("GND", "0V", "GNDD", "DGND", "AGND"):
            out.append(_rule(finding(
                "info", "earth_symbol_on_common",
                f"{s.ref} draws an earth/chassis symbol for {s.value}: circuit common is not earth",
                fix=f'sch_apply(ops=[{{"op":"swap","ref":"{s.ref}","lib_id":"power:GND"}}]) (same net, ground '
                    "symbol), or name the net EARTH / CHASSIS if it really is earth",
                items=[s.ref], at=list(s.at), net=str(s.value)), "P4"))
    if "0V" in vals and "GND" in vals:
        out.append(_rule(finding(
            "info", "zero_volt_and_gnd",
            "this sheet uses both 0V and GND: if they are one reference use one name; if not, add a note that says "
            "what each is (power-supply return, circuit common, chassis)",
            fix="a text note (sch_apply text op) saying what 0V and GND each are"), "P4"))
    return out


def floating_inputs(sch: Schematic, state: str = "hand") -> list[dict]:
    """D2: an input pin with a no-connect flag and no note saying it is intentional."""
    ncs = {_k([float(a[1]), float(a[2])]) for n in sch.items("no_connect") for a in [sexpr.find(n, "at")]
           if a is not None}
    hits = []
    for s in sch.symbols(include_power=False):
        for p in s.pins_world():
            if p["type"] == "input" and _k(p["xy"]) in ncs and not re.fullmatch(r"N\.?C\.?|DNC|NC\d*", p["name"] or "", re.I):
                hits.append(f"{s.ref}.{p['name'] or p['number']}")
    if not hits:
        return []
    texts = " ".join(t for t, _ in _texts(sch))
    if re.search(r"float|unused|intentional|left open|not connected", texts, re.I):
        return []
    return [_rule(finding(
        "info", "floating_input_unexplained",
        f"input pin(s) {', '.join(hits[:8])} are flagged no-connect with no note: say why an input is left floating",
        fix="a text note (sch_apply text op) beside the part saying why these inputs may float",
        items=hits[:20]), "D2")]


def _passives(sch: Schematic) -> list:
    return [s for s in sch.symbols(include_power=False) if _PASSIVE_REF.match(s.ref) and not s.dnp]


def passives(sch: Schematic, state: str = "hand") -> list[dict]:
    """D3: passives with no package anywhere (no footprint, and none named in the value or a field).
    Tolerance and rating are the note's job: which parts differ from it cannot be read from the drawing."""
    parts = _passives(sch)
    if not parts:
        return []
    out = []
    weak = [s.ref for s in parts if not (s.fields.get("Footprint") or "").strip()
            and not any(_PKG_RE.search(v or "") for v in [s.value, *s.fields.values()])]
    if weak:
        out.append(_rule(finding(
            "info", "passive_spec_incomplete",
            f"{len(weak)} passive(s) show no package ({', '.join(weak[:10])}{' …' if len(weak) > 10 else ''}): "
            "no footprint, and neither the value nor a field names one",
            fix="give each its real footprint (sch_apply set_field Footprint) or put the package in its value",
            items=weak[:30]), "D3"))
    return out


def symbols_drawn(sch: Schematic, state: str = "hand") -> list[dict]:
    """Y1 package-order IC symbols, Y2 discrete transistors without the envelope circle, Y4 unfilled bodies."""
    pkg, nocirc, hollow = [], [], []
    seen = set()
    for s in sch.symbols(include_power=False):
        if s.lib_name in seen:
            continue
        ls = sch.lib_symbol(s.lib_name)
        if ls is None:
            continue
        seen.add(s.lib_name)
        ref = s.ref
        graphics = [g for sub in sexpr.find_all(ls, "symbol") for g in sub[2:] if isinstance(g, list) and g]
        if re.match(r"^Q\d", ref) or _Q_LIB.search(s.lib_id):
            if re.match(r"^Q\d", ref) and not any(g[0] == "circle" for g in graphics) \
                    and not _Q_NO_CIRCLE_OK.search(s.lib_id) and len(s.unit_pins()) == 3:
                nocirc.append(ref)
            continue
        if not re.match(r"^U\d", ref):
            continue
        rects = [g for g in graphics if g[0] == "rectangle"]
        if rects:
            fills = [str(sexpr.value(sexpr.find(g, "fill") or [], "type", "none")) for g in rects]
            if all(f == "none" for f in fills):
                hollow.append(ref)
        if _package_order(s.unit_pins()):
            pkg.append(ref)
    out = []
    if pkg:
        out.append(_rule(finding(
            "info", "package_shaped_symbol",
            f"{', '.join(pkg[:8])} {'is' if len(pkg) == 1 else 'are'} drawn in package pin order: draw the symbol by "
            "function (supplies top, grounds bottom, inputs left, outputs right, related pins together)",
            fix="no Copper op: re-draw the library symbol by function in KiCad's symbol editor (keep the pin numbers, "
                "move the pins)", items=pkg[:20]), "Y1"))
    if nocirc:
        out.append(_rule(finding(
            "info", "transistor_without_envelope",
            f"discrete transistor(s) {', '.join(nocirc[:8])} drawn without the envelope circle (IEEE 315: the circle "
            "marks a discrete package)",
            fix="swap to the stock Transistor_BJT / Transistor_FET symbol of the same type and pin order (they carry "
                "the circle); check the pin numbers match before sch_apply swap", items=nocirc[:20]), "Y2"))
    if hollow:
        out.append(_rule(finding(
            "info", "symbol_body_unfilled",
            f"{', '.join(hollow[:8])} {'has' if len(hollow) == 1 else 'have'} a transparent body: fill symbol bodies "
            "with the background colour",
            fix="no Copper op: in KiCad's symbol editor set the body rectangle's fill to the background colour",
            items=hollow[:20]), "Y4"))
    return out


def _package_order(pins: list[dict]) -> bool:
    """True when the numbered pins run round the body like the package: down the left side, along the bottom, up
    the right side, back along the top (DIP / QFP order). Needs 8+ numbered pins on at least two sides."""
    side = {0: "L", 180: "R", 90: "B", 270: "T"}       # lib angle: 0 = pin points right = sits on the left side
    rows = []
    for p in pins:
        if p.get("hidden") or not str(p["number"]).isdigit():
            continue
        sd = side.get(p["angle"])
        if sd is None:
            continue
        x, y = p["at"]
        key = {"L": (0, -y), "B": (1, x), "R": (2, y), "T": (3, -x)}[sd]
        rows.append((key, int(p["number"])))
    if len(rows) < 8 or len({k[0] for k, _ in rows}) < 2:
        return False
    nums = [n for _, n in sorted(rows)]
    start = nums.index(min(nums))
    nums = nums[start:] + nums[:start]
    rising = sum(1 for a, b in zip(nums, nums[1:]) if b == a + 1)
    return rising >= 0.9 * (len(nums) - 1)


def test_points(sch: Schematic, state: str = "hand") -> list[dict]:
    """D1: test points without a note of what to expect there."""
    tps = [s for s in sch.symbols(include_power=False) if _TP_REF.match(s.ref)]
    if not tps:
        return []
    texts = " ".join(t for t, _ in _texts(sch))
    bare = [s.ref for s in tps if not re.search(rf"\b{re.escape(s.ref)}\b", texts)
            and not any(k.lower() in ("expect", "expected", "reading") for k in s.fields)]
    if not bare:
        return []
    return [_rule(finding(
        "info", "test_point_no_expectation",
        f"test point(s) {', '.join(bare[:8])} have no note of what they should read",
        fix="a text note per test point with the real expected reading (sch_apply text op), or an 'Expected' field "
            "on the test point (sch_apply set_field)",
        items=bare[:20]), "D1")]


# ── whole-project checks ─────────────────────────────────────────────────
def intersheet_refs(pr, sheets: list) -> list[dict]:
    """C5: global labels on a multi-sheet design without inter-sheet references switched on."""
    if len(sheets) < 2 or not any(s.items("global_label") for _, s in sheets):
        return []
    try:
        d = json.loads(Path(pr.pro).read_text(encoding="utf-8"))
        on = bool(((d.get("schematic") or {}).get("drawing") or {}).get("intersheets_ref_show"))
    except (OSError, ValueError):
        on = False
    if on:
        return []
    return [_rule(finding(
        "info", "intersheet_refs_off",
        "the design has global labels but inter-sheet references are off: a reader cannot see which sheet a label "
        "goes to",
        fix="no Copper op: Schematic Setup > General > Formatting > 'Show inter-sheet references' in KiCad"), "C5")]


def rail_names(pr, sheets: list) -> list[dict]:
    """P1: power symbols with a generic rail name (VCC, VDD ...), once for the whole design."""
    names: dict = {}
    for f, s in sheets:
        for sym in s.symbols():
            if _is_rail(sym) and str(sym.value).upper() in GENERIC_RAILS:
                names.setdefault(str(sym.value), set()).add(_sheet_name(pr, f) or "root")
    if not names:
        return []
    said = ", ".join(f"{n} ({len(v)} sheet{'s' if len(v) > 1 else ''})" for n, v in sorted(names.items()))
    n0 = sorted(names)[0]
    return [_rule(finding(
        "info", "generic_rail_name",
        f"rail name(s) {said} say nothing about the rail's voltage or source",
        fix=f"rename on each sheet that uses it: sch_apply rename_net from '{n0}' to the rail's real name "
            "(+3V3, +5V, VBUS, +BATT)",
        items=sorted(names)[:20], net=n0), "P1")]


def rail_specs(pr, sheets: list) -> list[dict]:
    """P2: rails without a min/typ/max voltage or current note anywhere in the design."""
    rails: dict = {}
    for f, s in sheets:
        for sym in s.symbols():
            if _is_rail(sym):
                n = str(sym.value)
                if not is_ground_net(n):
                    rails.setdefault(n, f)
    if not rails:
        return []
    texts = [t for _, s in sheets for t, _ in _texts(s)]
    bare = []
    for n in sorted(rails):
        key = re.escape(n.lstrip("+"))
        said = [t for t in texts if re.search(rf"(?<![\w]){key}(?![\w])", t, re.I)]
        if not any(_AMP_RE.search(t) or len(_VOLT_RE.findall(t)) >= 2 for t in said):
            bare.append(n)
    if not bare:
        return []
    return [_rule(finding(
        "info", "rail_without_spec",
        f"rail(s) {', '.join(bare[:10])} carry no voltage range or current: a reader needs min / typ / max volts "
        "and the current it can deliver",
        fix="a text note beside each rail's source with its real values from the datasheet (sch_apply text op), "
            "e.g. 'VBUS: 4.75 / 5.0 / 5.25 V, 500 mA max'",
        items=bare[:20]), "P2")]


def standard_parts_notes(pr, sheets: list) -> list[dict]:
    """D4: sheets with passives but no standard-parts note, once for the whole design."""
    bare = [(_sheet_name(pr, f) or "root", len(_passives(s))) for f, s in sheets
            if _passives(s) and not any(_STD_NOTE_RE.search(t) for t, _ in _texts(s))]
    if not bare:
        return []
    said = ", ".join(f"{n} ({k} passive{'s' if k > 1 else ''})" for n, k in bare[:8])
    return [_rule(finding(
        "info", "no_standard_parts_note",
        f"{len(bare)} sheet(s) with passives have no standard-parts note: {said}{' …' if len(bare) > 8 else ''}. "
        "Say once per sheet what every resistor and capacitor is unless marked otherwise",
        fix="a text note per sheet (sch_apply text op) with the defaults its parts really have, e.g. "
            "'Unless noted: resistors 1% 0.063W 0402; capacitors X7R 16V 0402'",
        items=[n for n, _ in bare][:20]), "D4")]


def structure(pr, sheets: list) -> list[dict]:
    """G3 crowded sheets, G4 root sheet as a block diagram."""
    out = []
    for f, s in sheets:
        n = len([x for x in s.symbols(include_power=False) if not x.ref.startswith("#")])
        if n > CROWDED_PARTS:
            out.append(_rule(finding(
                "info", "sheet_crowded",
                f"{n} parts on one sheet ({s.paper}): split it by function onto more sheets (11x17 in / A3 each), "
                "with a block diagram on the root",
                fix="no Copper op: move whole functional blocks to hierarchical sub-sheets in KiCad (Place > "
                    "Hierarchical Sheet)", sheet=_sheet_name(pr, f) or None), "G3"))
    if len(sheets) > 1:
        root = next((s for f, s in sheets if f == pr.sch), None)
        if root is not None and root.items("sheet"):
            parts = [x.ref for x in root.symbols(include_power=False)
                     if not x.ref.startswith("#") and not re.match(r"^(H|MH|LOGO|G|FID)\d", x.ref)]
            if len(parts) > ROOT_CIRCUIT_PARTS:
                out.append(_rule(finding(
                    "info", "root_not_block_diagram",
                    f"the root sheet holds {len(parts)} parts beside its sheet symbols: keep the root a block "
                    "diagram (sheet symbols wired in signal-flow order) and move the circuitry into the sheets",
                    fix="no Copper op: cut / paste those parts into their sub-sheet in KiCad with their labels, "
                        "then verify", items=parts[:20]), "G4"))
    return out


SHEET_CHECKS = (power_direction, junctions, label_hops, earth_symbols, floating_inputs, passives, symbols_drawn,
                test_points)
PROJECT_CHECKS = (intersheet_refs, rail_names, rail_specs, standard_parts_notes, structure)


def check(pr) -> list[dict]:
    """Every measurable rule over every sheet of the project; findings carry `rule` (the rule id)."""
    if pr is None:
        return []
    sheets = []
    files = pr.sheets() if hasattr(pr, "sheets") else [pr.sch]
    for f in files:
        try:
            sheets.append((f, Schematic.load(f)))
        except Exception:  # noqa: BLE001 — an unreadable sheet is verify's business, not style's
            continue
    out: list[dict] = []
    for f, s in sheets:
        name = _sheet_name(pr, f)
        state = layout_state(s)
        for fn in SHEET_CHECKS:
            try:
                fs = fn(s, state)
            except Exception as e:  # noqa: BLE001 — one heuristic must never sink the review
                fs = [finding("info", "review_check_failed", f"style check {fn.__name__} crashed on {f.name}: "
                                                              f"{type(e).__name__}: {e}", fix="report it; ignore this note")]
            for x in fs:
                if name:
                    x.setdefault("sheet", name)
                    if (x.get("fix") or "").startswith("sch_apply(ops="):
                        x["fix"] = x["fix"].replace("sch_apply(ops=", f'sch_apply(sheet="{name}", ops=', 1)
            out += fs
    for fn in PROJECT_CHECKS:
        try:
            out += fn(pr, sheets)
        except Exception as e:  # noqa: BLE001
            out.append(finding("info", "review_check_failed", f"style check {fn.__name__} crashed: {type(e).__name__}: {e}",
                               fix="report it; ignore this note"))
    return out


def rules_markdown() -> str:
    """The rules as a Markdown list, with the finding types the pass reports (help page and docs)."""
    def line(rid, title, text):
        c = CHECKED.get(rid)
        note = f" ({CHECK_NOTE[rid]})" if rid in CHECK_NOTE else ""
        return f"- **{rid} {title}.** {text}" + (f" *Checked: {', '.join(f'`{t}`' for t in c)}{note}.*" if c else "")
    return "\n".join(line(*r) for r in RULES)
