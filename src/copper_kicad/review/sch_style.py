"""Schematic drawing style: the rules for a schematic a stranger can read "in the blink of an eye".

The rules come from the KiCon talk on drawing good schematics (https://www.youtube.com/watch?v=X0hd_v8qRiY),
including the suggestions made in its Q&A, and the references it names: Tufte's *The Visual Display of
Quantitative Information*, the schematic appendix of *The Art of Electronics*, IEEE Std 315 and Olin Lathrop's
"Rules and guidelines for drawing good schematics" on Electrical Engineering Stack Exchange. Where they disagree
with an older Copper habit, these rules win. docs/SCHEMATIC_STYLE.md is the long form of `RULES`.

`check(pr)` measures the rules that can be measured from the saved sheet files alone (no KiCad call, no netlist):
it is the `schematic_style` pass of design_review. Rules that need judgement (is this sheet a block diagram, does
the flow read left to right) stay in the guidance; the pass only reports what it can show on the drawing.
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
     "Ground and negative-supply symbols always point down; positive-supply symbols always point up. Never "
     "sideways, never upside down."),
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
     "discrete package and must be drawn on discrete transistors."),
    ("Y3", "Hidden duplicate power pins",
     "Hide (or stack) duplicate supply and ground pins so a symbol shows one of each, unless the design is "
     "safety-critical, where every pin is shown."),
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
     "Turn on inter-sheet references (Schematic Setup > General > Formatting) so every global label says which "
     "sheet(s) its net goes to."),
    ("C6", "At most three wires at a junction",
     "A junction joins at most three wires. Never draw a four-way junction: stagger it into two tees."),
    ("C7", "Sense wires at 45 degrees",
     "A sense (Kelvin) wire leaves the conductor it senses at 45 degrees at the exact sense point."),
    ("P1", "Descriptive rail names",
     "Name rails after their source or use: VUSB, VBAT, +3V3_MCU, not VCC or VDD."),
    ("P2", "Rail specs on the sheet",
     "Annotate each rail with min / typ / max voltage and typical / maximum current, e.g. \"VUSB 4.5 / 5.0 / "
     "5.5 V, 35 mA typ, 500 mA max\", or at least \"9-19 V in\" and \"3V3, 2 A max\"."),
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
     "(\"10k 1% 0.1W 0402\"). Check the package can dissipate the rating. Not needed for ICs."),
    ("D4", "Standard-parts note on every sheet",
     "Every sheet carries a note with the defaults (\"Unless noted: resistors 1% 0.063W 0402; capacitors X7R 16V "
     "0402\") so parts that follow them need only their value."),
    ("K1", "Checklist",
     "Before release: power is distributed through power symbols, every supply points the right way, important "
     "nets are descriptively named, MOSFETs are oriented correctly for their body diode, every IC has its bypass "
     "capacitors, and the design fails safe, with modules (power supplies, I/O) grouped separately."),
]

RULE_TITLE = {r[0]: r[1] for r in RULES}

# rails whose name says nothing about where they come from or what they are (rule P1)
GENERIC_RAILS = {"VCC", "VDD", "VEE", "V+", "V-", "VS", "VS+", "VS-", "PWR", "POWER", "VSUPPLY", "SUPPLY", "VPOS",
                 "VNEG", "VPP", "VDC", "DC", "V"}
_NEG_RE = re.compile(r"^(?:-|VEE|VNEG|V-|VSS-|NEG)", re.I)
_PASSIVE_REF = re.compile(r"^(R|C|L|FB|D|RN|CN)\d", re.I)
_TOL_RE = re.compile(r"\d\s*%|±|\+/-")
_RATING_RE = re.compile(r"\d\s*(?:m|µ|u)?W\b|\b\d+/\d+\s*W\b|\d\s*k?V\b|\d\s*(?:m|µ|u)?A\b", re.I)
_PKG_RE = re.compile(r"\b(?:0075|01005|0201|0402|0603|0805|1206|1210|1812|2010|2512|SOD-?\d+\w*|SMA|SMB|SMC|"
                     r"SOT-?\d+\w*|DO-?\d+\w*|THT|axial|radial)\b", re.I)
_STD_NOTE_RE = re.compile(r"unless\s+(?:otherwise\s+)?(?:noted|stated|specified)|standard\s+parts|all\s+resistors|"
                          r"all\s+capacitors|default\s+parts", re.I)
_VOLT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*V\b|\b\d+V\d+\b", re.I)
_AMP_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:m|µ|u)?A\b")
_TP_REF = re.compile(r"^TP\d", re.I)
_Q_LIB = re.compile(r"transistor|mosfet|bjt|jfet|igbt|^Device:Q_|:Q_", re.I)
_EARTH_LIB = re.compile(r"^power:(?:Earth|Earth_Protective|Earth_Clean|Chassis)", re.I)

NEAR_LABEL_MM = 30.0          # two same-name local labels closer than this: the wire would have been short (C2)
CROWDED_PARTS = 45            # parts (power symbols excluded) beyond which one sheet is crammed (G3)
ROOT_CIRCUIT_PARTS = 4        # circuit parts tolerated beside sheet symbols on a root sheet (G4)


def _rule(f: dict, rid: str) -> dict:
    f["rule"] = rid
    f["section"] = f"{rid} {RULE_TITLE[rid]}"
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


def _body_box(sch: Schematic, s) -> list:
    ls = sch.lib_symbol(s.lib_name)
    if ls is None:
        return s.body_bbox()
    return G.bbox_world(s.at, s.matrix, LIBS.unit_bbox(ls, s.unit, s.body_style, include_pins=False))


def _is_flag(s) -> bool:
    return s.lib_id.split(":")[-1] == "PWR_FLAG" or s.ref.startswith("#FLG")


def _sheet_name(pr, f: Path) -> str:
    return "" if f == pr.sch else f.stem


# ── per-sheet checks ──────────────────────────────────────────────────────
def power_direction(sch: Schematic) -> list[dict]:
    """F2: ground / negative symbols point down, positive supplies point up."""
    bad = []
    for s in sch.symbols():
        if not s.is_power or _is_flag(s):
            continue
        pw = s.pins_world()
        if not pw:
            continue
        bb = _body_box(sch, s)
        px, py = pw[0]["xy"]
        dx, dy = (bb[0] + bb[2]) / 2 - px, (bb[1] + bb[3]) / 2 - py
        name = str(s.value)
        down = is_ground_net(name) or bool(_NEG_RE.match(name))
        if abs(dx) > abs(dy) + 0.01:
            bad.append((s, name, "sideways", down))
        elif (dy > 0) != down:
            bad.append((s, name, "upside down", down))
    if not bad:
        return []
    refs = [s.ref for s, *_ in bad]
    said = "; ".join(f"{s.ref} {name} {how}" for s, name, how, _ in bad[:6])
    return [_rule(finding(
        "warning", "power_symbol_direction",
        f"{len(bad)} power symbol(s) point the wrong way ({said}{' …' if len(bad) > 6 else ''}): ground and "
        "negative-supply symbols always point down, positive-supply symbols always point up",
        fix=f'sch_apply(ops=[{{"op":"move","ref":"{refs[0]}","rot":0}}]) per symbol (rot 0 stands a stock power '
            "symbol upright; its stub follows), or sch_tidy() to re-draw the sheet",
        items=refs[:30], at=list(bad[0][0].at)), "F2")]


def junctions(sch: Schematic) -> list[dict]:
    """C6: a point where four or more wire ends / pin tips meet."""
    segs = [(_k(w["a"]), _k(w["b"])) for w in sch.wires()]
    deg: dict = {}
    for a, b in segs:
        if a == b:
            continue
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    tips = {_k(p["xy"]) for s in sch.symbols() for p in s.pins_world()}     # stacked pins are one connection
    for k in tips:
        if k in deg:
            deg[k] += 1
    for k in list(deg):
        for a, b in segs:
            if k in (a, b):
                continue
            if (a[0] == b[0] == k[0] and min(a[1], b[1]) < k[1] < max(a[1], b[1])) or \
                    (a[1] == b[1] == k[1] and min(a[0], b[0]) < k[0] < max(a[0], b[0])):
                deg[k] += 2
    out = []
    for k, n in sorted(deg.items()):
        if n >= 4:
            at = [k[0] / 100, k[1] / 100]
            out.append(_rule(finding(
                "warning", "four_way_junction",
                f"{n} wires meet at one junction ({at[0]:.2f}, {at[1]:.2f}): at most three wires may meet, a four-way "
                "junction reads like a crossing",
                fix="stagger it into two tees at least 2.54 mm apart: move one branch's wire along the trunk "
                    "(sch_apply delete wire_at + wire points), keeping every pin on the same net",
                at=at), "C6"))
    return out


def label_hops(sch: Schematic) -> list[dict]:
    """C1/C2: local labels standing in for a short wire on the same sheet."""
    by: dict = {}
    for l in sch.labels():
        if l["kind"] == "local":
            by.setdefault(l["net"], []).append(l["at"])
    pins = [(p["xy"], f"{s.ref}.{p['name'] if p['name'] not in ('', '~') else p['number']}")
            for s in sch.symbols(include_power=False) for p in s.pins_world()]

    def pin_near(xy):
        best = min(pins, key=lambda q: math.dist(q[0], xy), default=None)
        return best[1] if best and math.dist(best[0], xy) <= 7.62 else None
    hops, same_sheet = [], []
    for net, pts in sorted(by.items()):
        if len(pts) < 2 or is_power_net(net) or is_ground_net(net):
            continue
        same_sheet.append(net)
        close = [(a, b) for i, a in enumerate(pts) for b in pts[i + 1:] if math.dist(a, b) < NEAR_LABEL_MM]
        if close:
            a, b = min(close, key=lambda ab: math.dist(*ab))
            hops.append((net, a, b, pin_near(a), pin_near(b)))
    out = []
    if hops:
        calls = [f'sch_apply(ops=[{{"op":"disconnect","pin":"{pb}"}},{{"op":"wire","from":"{pa}","to":"{pb}"}}])'
                 for _, _, _, pa, pb in hops if pa and pb and pa != pb]
        said = ", ".join(f"{n} ({math.dist(a, b):.0f} mm)" for n, a, b, _, _ in hops[:8])
        f = finding(
            "warning", "label_between_neighbours",
            f"{len(hops)} net(s) jump by label between neighbouring points on one sheet: {said}"
            f"{' …' if len(hops) > 8 else ''}. Draw the wire: a label hop must be searched for, a wire is seen",
            fix=calls[0] if calls else "sch_apply wire op between the two labelled pins, then delete the second "
                                       "label (keep one as the net's name)",
            at=list(hops[0][1]), net=hops[0][0], items=[x for h in hops for x in h[3:] if x][:30])
        if len(calls) > 1:
            f["fixes"] = calls[1:8]
        out.append(_rule(f, "C2"))
    far = [n for n in same_sheet if n not in {h[0] for h in hops}]
    if len(far) >= 3:
        out.append(_rule(finding(
            "info", "labels_instead_of_wires",
            f"{len(far)} nets are joined only by repeated local labels on this sheet ({', '.join(far[:8])}"
            f"{' …' if len(far) > 8 else ''}): wire every one that can be drawn without an awkward run",
            fix="sch_tidy() wires in-block nets for a Copper-drawn sheet; otherwise sch_apply wire ops pin to pin",
            items=far[:20]), "C1"))
    return out


def rail_names(sch: Schematic) -> list[dict]:
    """P1: generic rail names (VCC, VDD ...)."""
    names = set()
    for s in sch.symbols():
        if s.is_power and not _is_flag(s):
            names.add(str(s.value))
    for l in sch.labels():
        names.add(l["net"])
    out = []
    for n in sorted(names):
        if n.lstrip("/").upper() in GENERIC_RAILS:
            out.append(_rule(finding(
                "warning", "generic_rail_name",
                f"rail '{n}' says nothing about its source or voltage: name it after what feeds it (VUSB, VBAT, "
                "+3V3_MCU, +12V_IN)",
                fix=f'sch_apply(ops=[{{"op":"rename_net","from":"{n}","to":"<descriptive name>"}}]) on every sheet '
                    "that uses it",
                net=n), "P1"))
    return out


def earth_symbols(sch: Schematic) -> list[dict]:
    """P4: an earth / chassis symbol on circuit common, or 0 V and GND side by side."""
    out = []
    vals = {}
    for s in sch.symbols():
        if s.is_power and not _is_flag(s):
            vals.setdefault(str(s.value).upper(), []).append(s)
    for s in [s for v in vals.values() for s in v]:
        if _EARTH_LIB.match(s.lib_id) and str(s.value).upper() in ("GND", "0V", "GNDD", "DGND", "AGND"):
            out.append(_rule(finding(
                "info", "earth_symbol_on_common",
                f"{s.ref} draws an earth/chassis symbol for {s.value}: circuit common is not earth",
                fix=f'sch_apply(ops=[{{"op":"swap","ref":"{s.ref}","lib_id":"power:GND"}}]) or name the net EARTH / '
                    "CHASSIS if it really is earth",
                items=[s.ref], at=list(s.at), net=str(s.value)), "P4"))
    if "0V" in vals and "GND" in vals:
        out.append(_rule(finding(
            "info", "zero_volt_and_gnd",
            "this sheet uses both 0V and GND: if they are one reference use one name; if not, add a note that says "
            "what each is (power-supply return, circuit common, chassis)",
            fix='sch_apply(ops=[{"op":"text","text":"0V = <what>; GND = <what>"}])'), "P4"))
    return out


def floating_inputs(sch: Schematic) -> list[dict]:
    """D2: an input pin with a no-connect flag and no note saying it is intentional."""
    ncs = {_k([float(n[1][1]), float(n[1][2])]) for n in sch.items("no_connect") if sexpr.find(n, "at") is not None
           for n in [[n[0], sexpr.find(n, "at")]]}
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
        fix=f'sch_apply(ops=[{{"op":"text","text":"{", ".join(hits[:8])}: intentionally left floating (<why>)"}}])',
        items=hits[:20]), "D2")]


def passives(sch: Schematic) -> list[dict]:
    """D3/D4: passives without tolerance / rating / package, and the per-sheet standard-parts note."""
    parts = [s for s in sch.symbols(include_power=False) if _PASSIVE_REF.match(s.ref) and not s.dnp]
    if not parts:
        return []
    texts = [t for t, _ in _texts(sch)]
    out = []
    has_note = any(_STD_NOTE_RE.search(t) for t in texts)
    if not has_note:
        out.append(_rule(finding(
            "info", "no_standard_parts_note",
            f"this sheet has {len(parts)} passive(s) and no standard-parts note: say once what every resistor and "
            "capacitor is unless marked otherwise",
            fix='sch_apply(ops=[{"op":"text","text":"Unless noted: resistors 1% 0.063W 0402; capacitors X7R 16V '
                '0402"}]) with the defaults this sheet really uses'), "D4"))
    weak = []
    for s in parts:
        f = s.fields
        blob = " ".join([s.value] + [v for k, v in f.items() if k not in ("Reference", "Footprint", "Datasheet",
                                                                          "Description", "ki_fp_filters")])
        pkg = bool(_PKG_RE.search(blob)) or bool(_PKG_RE.search(f.get("Footprint", "")))
        miss = []
        if not _TOL_RE.search(blob) and not has_note:
            miss.append("tolerance")
        if not _RATING_RE.search(blob) and not has_note:
            miss.append("rating")
        if not pkg:
            miss.append("package")
        if miss:
            weak.append((s.ref, miss))
    if weak:
        what = sorted({m for _, ms in weak for m in ms})
        out.append(_rule(finding(
            "info", "passive_spec_incomplete",
            f"{len(weak)} passive(s) lack {', '.join(what)} ({', '.join(r for r, _ in weak[:10])}"
            f"{' …' if len(weak) > 10 else ''}): a passive reads value, tolerance, power/voltage rating, package",
            fix=f'sch_apply(ops=[{{"op":"set_field","ref":"{weak[0][0]}","fields":{{"Tolerance":"1%","Power":"0.063W"}}}}]) '
                "per part (capacitors: Voltage), or one standard-parts note for the common case",
            items=[r for r, _ in weak][:30]), "D3"))
    return out


def symbols_drawn(sch: Schematic) -> list[dict]:
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
            if re.match(r"^Q\d", ref) and not any(g[0] == "circle" for g in graphics):
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
            fix="re-draw the library symbol by function in KiCad's symbol editor (or pick a functional stock symbol "
                "with sch_apply swap); keep pin numbers, move pins", items=pkg[:20]), "Y1"))
    if nocirc:
        out.append(_rule(finding(
            "info", "transistor_without_envelope",
            f"discrete transistor(s) {', '.join(nocirc[:8])} drawn without the envelope circle (IEEE 315: the circle "
            "marks a discrete package)",
            fix='sch_apply(ops=[{"op":"swap","ref":"' + nocirc[0] + '","lib_id":"Device:Q_NPN_BEC"}]) or another '
                "stock Device:Q_* symbol (they carry the circle), pins re-attached by number", items=nocirc[:20]), "Y2"))
    if hollow:
        out.append(_rule(finding(
            "info", "symbol_body_unfilled",
            f"{', '.join(hollow[:8])} {'has' if len(hollow) == 1 else 'have'} a transparent body: fill symbol bodies "
            "with the background colour",
            fix="symbol editor: body rectangle Fill = 'Body background colour' (fill type background)",
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


def test_points(sch: Schematic) -> list[dict]:
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
        fix=f'sch_apply(ops=[{{"op":"text","text":"{bare[0]}: expect <3.30 V ±2% / waveform, scope setting>"}}]) '
            "beside each, or a field 'Expected' on the test point",
        items=bare[:20]), "D1")]


# ── whole-project checks ─────────────────────────────────────────────────
def intersheet_refs(pr, sheets: list) -> list[dict]:
    """C5: global labels without inter-sheet references switched on."""
    if not any(s.items("global_label") for _, s in sheets):
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
        fix="Schematic Setup > General > Formatting > 'Show inter-sheet references' (or set "
            "schematic.drawing.intersheets_ref_show = true in the .kicad_pro with KiCad closed)"), "C5")]


def rail_specs(pr, sheets: list) -> list[dict]:
    """P2: rails without a min/typ/max voltage or current note anywhere in the design."""
    rails: dict = {}
    for f, s in sheets:
        for sym in s.symbols():
            if sym.is_power and not _is_flag(sym):
                n = str(sym.value)
                if not is_ground_net(n):
                    rails.setdefault(n, f)
    if not rails:
        return []
    texts = [t for _, s in sheets for t, _ in _texts(s)]
    bare = []
    for n, f in sorted(rails.items()):
        key = re.escape(n.lstrip("+"))
        said = [t for t in texts if re.search(rf"(?<![\w]){key}(?![\w])", t, re.I)]
        ok = any(_AMP_RE.search(t) or len(_VOLT_RE.findall(t)) >= 2 for t in said)
        if not ok:
            bare.append(n)
    if not bare:
        return []
    return [_rule(finding(
        "info", "rail_without_spec",
        f"rail(s) {', '.join(bare[:10])} carry no voltage range or current: a reader needs min / typ / max volts "
        "and the current it can deliver",
        fix=f'sch_apply(ops=[{{"op":"text","text":"{bare[0]}: <min> / <typ> / <max> V, <typ> mA typ, <max> A max"}}]) '
            "beside each rail's source",
        items=bare[:20]), "P2")]


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
                fix="move whole functional blocks to hierarchical sub-sheets in KiCad (Place > Hierarchical Sheet), "
                    "then sch_tidy each sheet", sheet=_sheet_name(pr, f) or None), "G3"))
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
                    fix="move those parts into the sub-sheet they belong to (cut / paste in KiCad keeps the "
                        "netlist when the labels go with them), then verify", items=parts[:20]), "G4"))
    return out


SHEET_CHECKS = (power_direction, junctions, label_hops, rail_names, earth_symbols, floating_inputs, passives,
                symbols_drawn, test_points)
PROJECT_CHECKS = (intersheet_refs, rail_specs, structure)


def check(pr) -> list[dict]:
    """Every measurable rule over every sheet of the project; findings carry `rule` (id) and `section`."""
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
        for fn in SHEET_CHECKS:
            try:
                fs = fn(s)
            except Exception as e:  # noqa: BLE001 — one heuristic must never sink the review
                fs = [finding("info", "review_check_failed", f"style check {fn.__name__} crashed on {f.name}: "
                                                              f"{type(e).__name__}: {e}", fix="report it; ignore this note")]
            for x in fs:
                if name:
                    x.setdefault("sheet", name)
                    if x.get("fix", "").startswith("sch_apply(ops="):
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
    """The rules as a compact Markdown list (for help pages)."""
    return "\n".join(f"- **{rid} {title}.** {text}" for rid, title, text in RULES)
