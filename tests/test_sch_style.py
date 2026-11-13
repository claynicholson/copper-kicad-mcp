"""Schematic drawing-style rules (review/sch_style.py): the `schematic_style` pass of design_review.

    python tests/test_sch_style.py            (or: python -m pytest tests/test_sch_style.py)

Most tests need no KiCad: the pass reads the saved sheet files only. The tool-path tests at the end build a sheet
with Copper's own sch_apply + sch_tidy and are skipped when KiCad is not installed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-home-"))

from copper_kicad.project import Project  # noqa: E402
from copper_kicad.review import design_review, sch_style  # noqa: E402
from copper_kicad.sch.model import Schematic  # noqa: E402


try:
    import pytest
    Skip = pytest.skip.Exception
except ImportError:          # the plain runner below
    class Skip(Exception):
        pass


LIBS = """
  (symbol "power:GND" (power) (pin_names (offset 0) hide)
    (property "Reference" "#PWR" (at 0 -6.35 0)) (property "Value" "GND" (at 0 -3.81 0))
    (symbol "GND_0_1" (polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))
      (stroke (width 0) (type default)) (fill (type none))))
    (symbol "GND_1_1" (pin power_in line (at 0 0 270) (length 0) (name "GND") (number "1"))))
  (symbol "power:Earth" (power) (pin_names (offset 0) hide)
    (property "Reference" "#PWR" (at 0 -6.35 0)) (property "Value" "Earth" (at 0 -3.81 0))
    (symbol "Earth_0_1" (polyline (pts (xy -0.635 -1.905) (xy 0.635 -1.905)) (stroke (width 0) (type default)) (fill (type none)))
      (polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0) (type default)) (fill (type none))))
    (symbol "Earth_1_1" (pin power_in line (at 0 0 270) (length 0) (name "Earth") (number "1"))))
  (symbol "power:VCC" (power) (pin_names (offset 0) hide)
    (property "Reference" "#PWR" (at 0 -3.81 0)) (property "Value" "VCC" (at 0 3.81 0))
    (symbol "VCC_0_1" (polyline (pts (xy 0 0) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))
      (circle (center 0 1.905) (radius 0.635) (stroke (width 0) (type default)) (fill (type none))))
    (symbol "VCC_1_1" (pin power_in line (at 0 0 90) (length 0) (name "VCC") (number "1"))))
  (symbol "power:-5V" (power) (pin_names (offset 0) hide)
    (property "Reference" "#PWR" (at 0 -3.81 0)) (property "Value" "-5V" (at 0 3.81 0))
    (symbol "-5V_0_1" (polyline (pts (xy 0 0) (xy 0 1.27) (xy -0.762 1.27) (xy 0 2.54) (xy 0.762 1.27) (xy 0 1.27))
      (stroke (width 0) (type default)) (fill (type none))))
    (symbol "-5V_1_1" (pin power_in line (at 0 0 90) (length 0) (name "-5V") (number "1"))))
  (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0))
    (property "Reference" "R" (at 2.032 0 90)) (property "Value" "R" (at 0 0 90))
    (symbol "R_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none))))
    (symbol "R_1_1" (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
      (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))))
  (symbol "Mine:Q_bare"
    (property "Reference" "Q" (at 5.08 1.27 0)) (property "Value" "Q" (at 5.08 -1.27 0))
    (symbol "Q_bare_0_1" (polyline (pts (xy 0.635 1.905) (xy 0.635 -1.905)) (stroke (width 0.5) (type default)) (fill (type none))))
    (symbol "Q_bare_1_1" (pin input line (at -5.08 0 0) (length 5.08) (name "B") (number "1"))
      (pin passive line (at 2.54 5.08 270) (length 2.54) (name "C") (number "2"))
      (pin passive line (at 2.54 -5.08 90) (length 2.54) (name "E") (number "3"))))
  (symbol "Transistor_BJT:DTC114T"
    (property "Reference" "Q" (at 5.08 1.27 0)) (property "Value" "DTC114T" (at 5.08 -1.27 0))
    (symbol "DTC114T_0_1" (polyline (pts (xy 0.635 1.905) (xy 0.635 -1.905)) (stroke (width 0.5) (type default)) (fill (type none))))
    (symbol "DTC114T_1_1" (pin input line (at -5.08 0 0) (length 5.08) (name "B") (number "1"))
      (pin passive line (at 2.54 5.08 270) (length 2.54) (name "C") (number "2"))
      (pin passive line (at 2.54 -5.08 90) (length 2.54) (name "E") (number "3"))))
  (symbol "Mine:DIP8"
    (property "Reference" "U" (at 0 8 0)) (property "Value" "DIP8" (at 0 -8 0))
    (symbol "DIP8_0_1" (rectangle (start -5.08 6.35) (end 5.08 -6.35) (stroke (width 0.254) (type default)) (fill (type none))))
    (symbol "DIP8_1_1"
      (pin input line (at -7.62 3.81 0) (length 2.54) (name "A") (number "1"))
      (pin input line (at -7.62 1.27 0) (length 2.54) (name "B") (number "2"))
      (pin input line (at -7.62 -1.27 0) (length 2.54) (name "C") (number "3"))
      (pin power_in line (at -7.62 -3.81 0) (length 2.54) (name "GND") (number "4"))
      (pin output line (at 7.62 -3.81 180) (length 2.54) (name "D") (number "5"))
      (pin output line (at 7.62 -1.27 180) (length 2.54) (name "E") (number "6"))
      (pin output line (at 7.62 1.27 180) (length 2.54) (name "F") (number "7"))
      (pin power_in line (at 7.62 3.81 180) (length 2.54) (name "VCC") (number "8"))))
  (symbol "Mine:OPA"
    (property "Reference" "U" (at 0 8 0)) (property "Value" "OPA" (at 0 -8 0))
    (symbol "OPA_0_1" (rectangle (start -5.08 5.08) (end 5.08 -5.08) (stroke (width 0.254) (type default)) (fill (type background))))
    (symbol "OPA_1_1"
      (pin output line (at 7.62 0 180) (length 2.54) (name "OUT") (number "1"))
      (pin input line (at -7.62 2.54 0) (length 2.54) (name "-") (number "2"))
      (pin input line (at -7.62 0 0) (length 2.54) (name "+") (number "3"))
      (pin input line (at -7.62 -2.54 0) (length 2.54) (name "-") (number "6"))))
  (symbol "Graphic:Logo" (power)
    (property "Reference" "#LOGO" (at 0 3 0)) (property "Value" "~" (at 0 -3 0))
    (symbol "Logo_0_1" (circle (center 0 0) (radius 2) (stroke (width 0) (type default)) (fill (type none)))))
  (symbol "Mine:TP"
    (property "Reference" "TP" (at 0 3 0)) (property "Value" "TP" (at 0 -3 0))
    (symbol "TP_0_1" (circle (center 0 2.54) (radius 0.762) (stroke (width 0) (type default)) (fill (type none))))
    (symbol "TP_1_1" (pin passive line (at 0 0 90) (length 1.778) (name "1") (number "1"))))
"""


def _sym(lib, ref, value, x, y, rot=0, extra=""):
    return (f'(symbol (lib_id "{lib}") (at {x} {y} {rot}) (unit 1) (uuid "u-{ref}")\n'
            f'  (property "Reference" "{ref}" (at {x} {y - 3} 0)) (property "Value" "{value}" (at {x} {y + 3} 0)){extra})')


def _fp(fp):
    return f' (property "Footprint" "{fp}" (at 0 0 0))'


def _sheet(body: str, uuid="root") -> str:
    return (f'(kicad_sch (version 20250114) (generator "eeschema") (uuid "{uuid}") (paper "A4")\n'
            f'(lib_symbols {LIBS})\n{body}\n(sheet_instances (path "/" (page "1"))))\n')


def _wire(a, b):
    return f"(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) (uuid \"w{a[0]}{a[1]}{b[0]}{b[1]}\"))"


def _label(net, x, y, i):
    return f'(label "{net}" (at {x} {y} 0) (uuid "l{i}-{net}"))'


def _text(t, i):
    return f'(text "{t}" (at 20 {20 + 5 * i} 0) (uuid "t{i}"))'


def _project(body: str, pro: dict | None = None, name="t", subs: dict | None = None) -> Project:
    d = Path(tempfile.mkdtemp(prefix="copper-style-"))
    (d / f"{name}.kicad_sch").write_text(_sheet(body), encoding="utf-8")
    for fn, b in (subs or {}).items():
        (d / fn).write_text(_sheet(b, uuid=f"sub-{fn}"), encoding="utf-8")
    (d / f"{name}.kicad_pro").write_text(json.dumps(pro or {}), encoding="utf-8")
    return Project(d, name)


def _subsheet(fn, i=1):
    return (f'(sheet (at {100 + 30 * i} 150) (size 20 20) (uuid "s{i}") '
            f'(property "Sheetname" "{Path(fn).stem}" (at 100 149 0)) (property "Sheetfile" "{fn}" (at 100 171 0)))')


def _types(fs):
    return {f["type"] for f in fs}


def _one(fs, t):
    hits = [f for f in fs if f["type"] == t]
    assert len(hits) == 1, (t, [(f["type"], f["message"]) for f in fs])
    return hits[0]


QUIET_NOTES = [_text("VUSB: 4.5 / 5.0 / 5.5 V, 35 mA typ, 500 mA max", 1),
               _text("Unless noted: resistors 1% 0.063W 0402", 2)]

BAD = "\n".join([
    _sym("power:GND", "#PWR01", "GND", 50, 50, 90),                      # sideways ground
    _sym("power:VCC", "#PWR02", "VCC", 60, 50, 180),                     # supply upside down, generic name
    _sym("Device:R", "R1", "10k", 80, 50),                               # no package anywhere
    _sym("Mine:Q_bare", "Q1", "BC847", 120, 50),
    _sym("Mine:DIP8", "U1", "DIP8", 160, 50),
    _wire((100, 100), (110, 100)), _wire((110, 100), (120, 100)), _wire((110, 90), (110, 100)), _wire((110, 100), (110, 110)),
    _label("SIG", 80, 46.19, 1), _label("SIG", 90, 46.19, 2),
])

GOOD = "\n".join([
    _sym("power:GND", "#PWR01", "GND", 50, 60, 0),
    _sym("power:VCC", "#PWR02", "VUSB", 50, 40, 0),
    _sym("power:-5V", "#PWR03", "-5V", 60, 40, 0),                       # stock negative rail as drawn: up
    _sym("power:Earth", "#PWR04", "EARTH", 70, 60, 0),                   # earth symbol drawn down, named earth
    _sym("Device:R", "R1", "10k", 80, 50, extra=_fp("Resistor_SMD:R_0402_1005Metric")),
    _sym("Transistor_BJT:DTC114T", "Q2", "DTC114T", 120, 50),           # stock pre-biased: no circle by design
    _wire((100, 100), (110, 100)), _wire((110, 100), (120, 100)), _wire((110, 90), (110, 100)),      # a tee
    _sym("Device:R", "R2", "10k", 140, 50, extra=_fp("Resistor_SMD:R_0402_1005Metric")),
    _wire((140, 46.19), (140, 40)), _wire((140, 40), (160, 40)),
    _label("CLK", 140, 40, 1), _label("CLK", 160, 40, 2),               # one wire named at both ends (C3, not C2)
    _label("DC", 80, 46.19, 3),                                          # a display D/C line, not a rail
    '(global_label "BUS" (shape input) (at 30 30 0) (uuid "g1"))',      # single sheet: no inter-sheet refs needed
    _text("-5V: -5.2 / -5.0 / -4.8 V, 20 mA max", 3),
    *QUIET_NOTES,
])


# ── no-KiCad tests ────────────────────────────────────────────────────────
def test_rule_table_is_complete():
    ids = [r[0] for r in sch_style.RULES]
    assert len(ids) == len(set(ids)) and len(ids) >= 28, ids
    for rid in ("F1", "F2", "Y1", "Y2", "Y3", "Y4", "G1", "G3", "G4", "C1", "C2", "C5", "C6", "C7", "P1", "P2", "P4",
                "D1", "D2", "D3", "D4", "K1"):
        assert rid in ids, rid
    assert set(sch_style.CHECKED) <= set(ids)


def test_docs_match_the_rule_table():
    doc = (ROOT / "docs" / "SCHEMATIC_STYLE.md").read_text(encoding="utf-8").splitlines()
    for line in sch_style.rules_markdown().splitlines():
        assert line in doc, f"docs/SCHEMATIC_STYLE.md is out of step with RULES: {line[:80]}"


def test_rules_are_served_by_copper_help():
    from copper_kicad import tools  # noqa: F401  (registers the tools)
    from copper_kicad.mcp import registry
    h = registry.help_text("design_review")
    assert "**C2 No label hops between neighbours.**" in h and "label_between_neighbours" in h, h[-400:]


def test_bad_sheet_is_flagged():
    fs = sch_style.check(_project(BAD))
    t = _types(fs)
    want = {"power_symbol_direction", "four_way_junction", "label_between_neighbours", "generic_rail_name",
            "passive_spec_incomplete", "no_standard_parts_note", "transistor_without_envelope", "package_shaped_symbol",
            "symbol_body_unfilled", "rail_without_spec"}
    assert want <= t, sorted(want - t)
    assert "review_check_failed" not in t, [f["message"] for f in fs if f["type"] == "review_check_failed"]
    for f in fs:
        assert f["fix"] and f["rule"] in sch_style.CHECKED and f["type"] in sch_style.CHECKED[f["rule"]], f
        assert f["severity"] in ("warning", "info"), f
    pd = _one(fs, "power_symbol_direction")
    assert set(pd["items"]) == {"#PWR01", "#PWR02"} and pd["severity"] == "warning", pd
    assert "#PWR01 GND sideways" in pd["message"] and "#PWR02 VCC upside down" in pd["message"], pd
    assert "4 wires" not in _one(fs, "four_way_junction")["message"]


def test_good_sheet_is_quiet():
    pr = _project(GOOD, {"schematic": {"drawing": {"intersheets_ref_show": True}}})
    fs = sch_style.check(pr)
    assert not fs, [(f["type"], f["message"]) for f in fs]


def test_every_checked_type_is_covered_here():
    """Each finding type the pass can report has a test in this file that makes it fire."""
    src = Path(__file__).read_text(encoding="utf-8")
    want = re.search(r"want = \{(.*?)\}", src, re.S).group(1)          # test_bad_sheet_is_flagged
    for rid, types in sch_style.CHECKED.items():
        for t in types:
            assert f'"{t}")' in src or f'"{t}"' in want, f"{rid} {t} has no test"


def test_fixes_never_change_the_netlist_or_invent_data():
    fs = sch_style.check(_project(BAD))
    for f in fs:
        fix = f["fix"]
        assert '"disconnect"' not in fix and "Tolerance" not in fix and '"Power"' not in fix, f
        assert "Device:Q_NPN_BEC" not in fix and "<" not in fix, f      # no made-up lib ids, no placeholders to paste
        for m in re.finditer(r'"op":"(\w+)"', fix):
            assert m.group(1) in ("move", "wire", "swap"), f


def test_power_symbol_direction_follows_the_library_drawing():
    body = "\n".join([_sym("power:-5V", "#PWR01", "-5V", 50, 50, 0), _sym("power:Earth", "#PWR02", "EARTH", 60, 50, 0),
                      _sym("power:GND", "#PWR03", "GND", 70, 50, 0), _sym("power:-5V", "#PWR04", "-5V", 80, 50, 180),
                      _sym("power:GND", "#PWR05", "GND", 90, 50, 0).replace("(unit 1)", "(mirror x) (unit 1)")])
    pd = _one(sch_style.check(_project(body)), "power_symbol_direction")
    assert set(pd["items"]) == {"#PWR04", "#PWR05"}, pd       # stock -5V and earth as drawn are fine


def test_four_way_counts_wires_not_pins():
    # three wires and a resistor pin meet at R1.1 (80, 46.19): not a four-way junction
    tip = (80, 46.19)
    body = "\n".join([_sym("Device:R", "R1", "10k", 80, 50), _wire(tip, (70, 46.19)), _wire(tip, (90, 46.19)),
                      _wire(tip, (80, 40)), '(junction (at 80 46.19) (diameter 0) (color 0 0 0 0) (uuid "j1"))'])
    assert "four_way_junction" not in _types(sch_style.check(_project(body)))
    # a wire passing through a junction dot plus two ends there is four arms
    body = "\n".join([_wire((100, 100), (120, 100)), _wire((110, 100), (110, 90)), _wire((110, 100), (110, 110)),
                      '(junction (at 110 100) (diameter 0) (color 0 0 0 0) (uuid "j2"))'])
    f = _one(sch_style.check(_project(body)), "four_way_junction")
    assert "(110.00, 100.00)" in f["message"], f
    # two wires crossing at a junction dot, no wire ending there: four arms too
    body = "\n".join([_wire((100, 100), (120, 100)), _wire((110, 90), (110, 110)),
                      '(junction (at 110 100) (diameter 0) (color 0 0 0 0) (uuid "j3"))'])
    f = _one(sch_style.check(_project(body)), "four_way_junction")
    assert "(110.00, 100.00)" in f["message"], f


def test_label_hop_fix_wires_pins_already_on_the_net():
    # SIG: R1.1 labelled at its tip; R2.1 wired up to a label 6 mm away. R9.1 sits closer to that label but is not
    # on SIG: the fix must name R2.1 (found through the wire), never the nearest pin.
    body = "\n".join([_sym("Device:R", "R1", "10k", 80, 50), _sym("Device:R", "R2", "10k", 100, 50),
                      _sym("Device:R", "R9", "10k", 104, 43.81),
                      _wire((100, 46.19), (100, 40)), _label("SIG", 80, 46.19, 1), _label("SIG", 100, 40, 2),
                      '(no_connect (at 104 40) (uuid "nc1"))'])
    f = _one(sch_style.check(_project(body)), "label_between_neighbours")
    assert f["fix"] == 'sch_apply(ops=[{"op":"wire","from":"R1.1","to":"R2.1"}]) joins two pins already on SIG ' \
                      "(the netlist does not change); the labels can stay as names", f
    assert f["rule"] == "C2" and f["severity"] == "info"


def test_label_hop_fix_joins_two_different_parts():
    # U1.2 and U1.6 are both '-' (common inside); each is wired to one SIG label, and R3.1 shares the first wire.
    # The nearest pair would wire U1 to itself, which reads as a bypass: the fix must join R3.1 and U1.6
    body = "\n".join([_sym("Mine:OPA", "U1", "OPA", 150, 50), _sym("Device:R", "R3", "10k", 120, 50),
                      _wire((120, 47.46), (142.38, 47.46)), _wire((120, 46.19), (120, 47.46)),
                      _wire((142.38, 52.54), (135, 52.54)),
                      _label("SIG", 135, 47.46, 1), _label("SIG", 135, 52.54, 2)])
    f = _one(sch_style.check(_project(body)), "label_between_neighbours")
    assert '"from":"R3.1","to":"U1.6"' in f["fix"], f


def test_label_hop_fix_uses_pin_numbers():
    # an op-amp with two pins named '-': the fix must say U1.6, not the ambiguous U1.-
    body = "\n".join([_sym("Mine:OPA", "U1", "OPA", 150, 50), _sym("Device:R", "R3", "10k", 135, 50),
                      _label("BUF", 142.38, 52.54, 1), _label("BUF", 135, 46.19, 2)])
    f = _one(sch_style.check(_project(body)), "label_between_neighbours")
    assert '"U1.6"' in f["fix"] and '"R3.1"' in f["fix"] and "U1.-" not in f["fix"], f


def test_label_hop_without_a_pin_has_no_ops():
    body = "\n".join([_label("SIG", 80, 40, 1), _label("SIG", 90, 40, 2), _sym("Device:R", "R1", "10k", 200, 150)])
    f = _one(sch_style.check(_project(body)), "label_between_neighbours")
    assert "sch_apply(" not in f["fix"], f


def test_label_hop_names_the_net_its_fix_wires():
    # AAA hops between two labels with no pins (sorted first); SIG hops between pins: the finding's net, place and
    # fix all describe SIG
    body = "\n".join([_sym("Device:R", "R1", "10k", 80, 50), _sym("Device:R", "R2", "10k", 100, 50),
                      _label("SIG", 80, 46.19, 1), _label("SIG", 100, 46.19, 2),
                      _label("AAA", 20, 20, 3), _label("AAA", 30, 20, 4)])
    f = _one(sch_style.check(_project(body)), "label_between_neighbours")
    assert f["net"] == "SIG" and '"from":"R1.1","to":"R2.1"' in f["fix"] and "already on SIG" in f["fix"], f
    assert f["at"] in ([80, 46.19], [100, 46.19]), f


def test_labels_instead_of_wires():
    body = "\n".join(_label(n, x, y, i) for i, (n, x, y) in enumerate(
        [("A1", 20, 20), ("A1", 200, 150), ("A2", 20, 30), ("A2", 200, 160), ("A3", 20, 40), ("A3", 200, 170)]))
    body += "\n" + _sym("Device:R", "R1", "10k", 100, 100)
    f = _one(sch_style.check(_project(body)), "labels_instead_of_wires")
    assert set(f["items"]) == {"A1", "A2", "A3"} and f["rule"] == "C1", f


def test_engine_sheet_skips_the_wiring_rules():
    from copper_kicad.sch.pro import emit
    body = "\n".join([_sym("Device:R", "R1", "10k", 80, 50), _sym("Device:R", "R2", "10k", 100, 50),
                      _label("SIG", 80, 46.19, 1), _label("SIG", 100, 46.19, 2),
                      _sym("power:GND", "#PWR01", "GND", 50, 50, 90),
                      _sym("power:VCC", "#PWR02", "VUSB", 60, 50, 180),
                      _wire((100, 100), (110, 100)), _wire((110, 100), (120, 100)), _wire((110, 90), (110, 100)),
                      _wire((110, 100), (110, 110))])
    pr = _project(body)
    dg = emit.digest(Schematic.load(pr.sch))
    txt = pr.sch.read_text(encoding="utf-8").replace("(sheet_instances", f'(group "copper:layout:{dg}" (uuid "g0") (members))\n(sheet_instances')
    pr.sch.write_text(txt, encoding="utf-8")
    assert sch_style.layout_state(Schematic.load(pr.sch)) == "engine"
    fs = sch_style.check(pr)
    t = _types(fs)
    assert not t & {"label_between_neighbours", "labels_instead_of_wires", "four_way_junction"}, t
    pd = _one(fs, "power_symbol_direction")                  # upside down still counts; sideways is the engine's call
    assert pd["items"] == ["#PWR02"], pd


def test_generic_rail_names_are_power_symbols_only_and_merged():
    sub = "\n".join([_sym("power:VCC", "#PWR11", "VCC", 50, 40, 0), _label("DC", 80, 40, 1)])
    root = "\n".join([_sym("power:VCC", "#PWR01", "VCC", 50, 40, 0), _label("SUPPLY", 80, 40, 1), _subsheet("a.kicad_sch")])
    fs = sch_style.check(_project(root, subs={"a.kicad_sch": sub}))
    f = _one(fs, "generic_rail_name")
    assert f["items"] == ["VCC"] and "2 sheets" in f["message"] and "<" not in f["fix"], f


def test_rail_without_spec():
    f = _one(sch_style.check(_project(_sym("power:VCC", "#PWR01", "+3V3", 50, 40, 0))), "rail_without_spec")
    assert f["items"] == ["+3V3"], f
    ok = "\n".join([_sym("power:VCC", "#PWR01", "+3V3", 50, 40, 0), _text("+3V3: 3.2 / 3.3 / 3.4 V, 600 mA max", 1)])
    assert "rail_without_spec" not in _types(sch_style.check(_project(ok)))
    logo = "\n".join([ok, _sym("Graphic:Logo", "#LOGO1", "~", 200, 150), _sym("Graphic:Logo", "#LOGO2", "Logo_X", 210, 150)])
    assert not _types(sch_style.check(_project(logo))) & {"rail_without_spec", "generic_rail_name"}


def test_earth_and_zero_volt():
    body = "\n".join([_sym("power:Earth", "#PWR01", "GND", 50, 50, 0), _sym("power:GND", "#PWR02", "0V", 60, 50, 0)])
    fs = sch_style.check(_project(body))
    f = _one(fs, "earth_symbol_on_common")
    assert f["items"] == ["#PWR01"] and '"lib_id":"power:GND"' in f["fix"], f
    _one(fs, "zero_volt_and_gnd")


def test_floating_input():
    body = "\n".join([_sym("Mine:DIP8", "U1", "DIP8", 160, 50), '(no_connect (at 152.38 46.19) (uuid "nc1"))'])
    f = _one(sch_style.check(_project(body)), "floating_input_unexplained")
    assert f["items"] == ["U1.A"], f
    ok = body + "\n" + _text("U1.A: intentionally left floating", 1)
    assert "floating_input_unexplained" not in _types(sch_style.check(_project(ok)))


def test_passives_package_from_stock_footprint_names():
    for fp in ("Resistor_SMD:R_0402_1005Metric", "Capacitor_SMD:C_0603_1608Metric", "easyeda2kicad:R0402",
               "LED_SMD:LED_0603_1608Metric", "Diode_SMD:D_SOD-123", "Diode_SMD:D_SMA", "Resistor_SMD:R_0402",
               "Capacitor_THT:CP_Radial_D5.0mm_P2.50mm", "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"):
        assert sch_style._PKG_RE.search(fp), fp
    for v in ("10k", "100nF", "C25804", "BAT54"):
        assert not sch_style._PKG_RE.search(v), v
    body = "\n".join([_sym("Device:R", "R1", "10k", 80, 50, extra=_fp("Resistor_SMD:R_0402_1005Metric")),
                      _sym("Device:R", "R2", "10k", 90, 50), _sym("Device:R", "CN1", "USB-C", 100, 50)])
    fs = sch_style.check(_project(body))
    assert _one(fs, "passive_spec_incomplete")["items"] == ["R2"]       # CN1 is a connector, R1 has its footprint
    _one(fs, "no_standard_parts_note")
    noted = body + "\n" + _text("Unless noted: resistors 1% 0.063W 0402", 1)
    assert "no_standard_parts_note" not in _types(sch_style.check(_project(noted)))
    # two sheets without the note: one finding naming both
    f = _one(sch_style.check(_project(body + "\n" + _subsheet("a.kicad_sch"), subs={"a.kicad_sch": body})),
             "no_standard_parts_note")
    assert set(f["items"]) == {"root", "a"} and "sheet" not in f, f


def test_symbols_drawn():
    body = "\n".join([_sym("Mine:Q_bare", "Q1", "BC847", 120, 50), _sym("Transistor_BJT:DTC114T", "Q2", "DTC114T", 140, 50),
                      _sym("Mine:DIP8", "U1", "DIP8", 160, 50), _sym("Mine:OPA", "U2", "OPA", 200, 50)])
    fs = sch_style.check(_project(body))
    assert _one(fs, "transistor_without_envelope")["items"] == ["Q1"]
    assert _one(fs, "package_shaped_symbol")["items"] == ["U1"]
    assert _one(fs, "symbol_body_unfilled")["items"] == ["U1"]            # U2's body has a background fill


def test_test_points():
    tp = _sym("Mine:TP", "TP1", "TP", 50, 50)
    _one(sch_style.check(_project(tp)), "test_point_no_expectation")
    assert "test_point_no_expectation" not in _types(sch_style.check(_project(tp + "\n" + _text("TP1: 3.3 V", 1))))


def test_project_structure_checks():
    sub = '(global_label "BUS" (shape input) (at 30 30 0) (uuid "g2"))'
    root = "\n".join([_subsheet("a.kicad_sch"), '(global_label "BUS" (shape input) (at 30 30 0) (uuid "g1"))',
                      *[_sym("Device:R", f"R{i}", "10k", 20 + 6 * i, 50, extra=_fp("Resistor_SMD:R_0402_1005Metric"))
                        for i in range(1, 6)]])
    fs = sch_style.check(_project(root, subs={"a.kicad_sch": sub}))
    _one(fs, "intersheet_refs_off")
    assert _one(fs, "root_not_block_diagram")["items"] == ["R1", "R2", "R3", "R4", "R5"]
    on = sch_style.check(_project(root, {"schematic": {"drawing": {"intersheets_ref_show": True}}},
                                  subs={"a.kicad_sch": sub}))
    assert "intersheet_refs_off" not in _types(on)
    crowded = "\n".join(_sym("Device:R", f"R{i}", "10k", 20 + 5 * (i % 50), 50 + 10 * (i // 50),
                             extra=_fp("Resistor_SMD:R_0402_1005Metric")) for i in range(1, 47))
    _one(sch_style.check(_project(crowded)), "sheet_crowded")


def test_sub_sheet_fixes_name_the_sheet():
    sub = "\n".join([_sym("Device:R", "R1", "10k", 80, 50), _sym("Device:R", "R2", "10k", 100, 50),
                     _label("SIG", 80, 46.19, 1), _label("SIG", 100, 46.19, 2)])
    fs = sch_style.check(_project(_subsheet("power.kicad_sch"), subs={"power.kicad_sch": sub}))
    f = _one(fs, "label_between_neighbours")
    assert f["sheet"] == "power" and f["fix"].startswith('sch_apply(sheet="power", ops='), f


def test_hand_drawn_power_direction_fix_is_a_sentence():
    fs = sch_style.check(_project(_sym("power:VCC", "#PWR11", "VUSB", 60, 50, 180)))
    f = _one(fs, "power_symbol_direction")
    assert f["severity"] == "warning" and "sch_apply(" not in f["fix"] and "rot 0" in f["fix"], f


def test_negative_rail_pointing_down_is_info():
    f = _one(sch_style.check(_project(_sym("power:-5V", "#PWR01", "-5V", 80, 50, 180))), "power_symbol_direction")
    assert f["items"] == ["#PWR01"] and f["severity"] == "info", f


def test_no_tidy_fix_where_sch_tidy_refuses():
    """A stub-and-label sheet sch_tidy cannot re-lay (here: it has a bus) gets plain fixes, never sch_tidy()."""
    body = "\n".join([_sym("Device:R", "R1", "10k", 80, 50), _sym("Device:R", "R2", "10k", 100, 50),
                      _label("SIG", 80, 46.19, 1), _label("SIG", 100, 46.19, 2),
                      '(bus (pts (xy 150 150) (xy 170 150)) (uuid "b1"))'])
    sch = Schematic.load(_project(body).sch)
    assert sch_style.tidy_refuses(sch)
    assert sch_style.layout_state(sch) != "machine"
    assert not any(f["fix"].startswith("sch_tidy") for f in sch_style.check(_project(body))), "sch_tidy offered"


def test_style_is_opt_in_and_kept_apart():
    assert "schematic_style" in design_review.CHECKS and "schematic_style" not in design_review.DEFAULT_CHECKS
    nl = {"components": {}, "nets": {}}
    r = design_review.review(_project(BAD), include_erc=False, netlist=nl)
    assert "schematic_style" not in r["checks_run"] and "style" not in r, r
    r = design_review.review(_project(BAD), checks=["schematic_style"], include_erc=False, netlist=nl)
    assert r["checks_run"] == ["schematic_style"] and not r["findings"] and r["counts"]["warning"] == 0, r
    assert "power_symbol_direction" in _types(r["style"]), r
    r0 = design_review.review(None, checks=["schematic_style"], include_erc=False, netlist=nl)   # no files: skipped
    assert not r0["findings"] and "style" not in r0


def test_style_listing_keeps_the_rule_id():
    from copper_kicad.tools import verify_tools as VT
    fs = sch_style.check(_project(BAD))
    entries = VT._listing(fs, 40)["findings"]
    assert entries and all(e.get("rule") for e in entries), entries


def test_templates_show_intersheet_refs():
    for f in (ROOT / "src" / "copper_kicad" / "data" / "templates").glob("*.kicad_pro"):
        d = json.loads(f.read_text(encoding="utf-8"))
        assert d["schematic"]["drawing"]["intersheets_ref_show"] is True, f.name


def test_guidance():
    for name in ("kicad-design", "design-review"):
        a = (ROOT / "src" / "copper_kicad" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert a == (ROOT / "plugin" / "skills" / name / "SKILL.md").read_text(encoding="utf-8"), name
        assert "SCHEMATIC_STYLE.md" not in a and "not optional" not in a, name
    skill = (ROOT / "src" / "copper_kicad" / "skills" / "kicad-design" / "SKILL.md").read_text(encoding="utf-8")
    for phrase in ('checks=["schematic_style"]', "min / typ / max", "never hide them", "`power` op", "+3V3"):
        assert phrase in skill, phrase
    assert "J1.S1" not in skill


# ── through the real tools on a Copper-made sheet (needs KiCad) ─────────────
def _kicad_project(name: str):
    from copper_kicad.kicad import env as _env
    try:
        if not _env.kicad().cli:
            raise Skip("kicad-cli not found")
    except Exception as e:  # noqa: BLE001
        raise Skip(f"no KiCad: {e}")
    cfg, share = _env.config_dir(), _env.kicad().share
    if os.environ.get("KICAD_CONFIG_HOME") and share:
        cfg.mkdir(parents=True, exist_ok=True)
        for n in ("sym-lib-table", "fp-lib-table"):
            if (share / "template" / n).exists() and not (cfg / n).exists():
                shutil.copy2(share / "template" / n, cfg / n)
    from copper_kicad import tools  # noqa: F401
    from copper_kicad.mcp import registry
    d = Path(tempfile.mkdtemp(prefix="copper-style-tool-"))
    r = registry.call_tool("project_create", {"name": name, "dir": str(d), "template": "jlc-2layer"})
    assert not r.get("isError"), r
    return registry, next(d.rglob(f"{name}.kicad_pro"))


def _skill_example() -> list:
    skill = (ROOT / "src" / "copper_kicad" / "skills" / "kicad-design" / "SKILL.md").read_text(encoding="utf-8")
    return json.loads(re.search(r"```json\n(\{\"ops\": \[.*?\]\})\n```", skill, re.S).group(1))["ops"]


DRAWING_TYPES = {"power_symbol_direction", "four_way_junction", "label_between_neighbours", "labels_instead_of_wires",
                 "generic_rail_name", "passive_spec_incomplete", "package_shaped_symbol", "symbol_body_unfilled",
                 "transistor_without_envelope", "earth_symbol_on_common", "floating_input_unexplained"}


def test_skill_example_applies_and_copper_output_is_quiet():
    registry, pro = _kicad_project("ex")
    p = str(pro)
    r = registry.call_tool("sch_apply", {"project": p, "ops": _skill_example(), "preview": False})
    assert not r.get("isError"), r["content"][0]["text"][:800]
    sc = r["structuredContent"]
    assert sc["connectivity"]["ok"] == sc["connectivity"]["checked"], sc["connectivity"]
    nets = registry.call_tool("sch_read", {"project": p, "query": "nets"})["structuredContent"]["nets"]
    assert sorted(nets["/CC1"]) == ["J1.A5(CC1)", "R1.1"] and sorted(nets["/CC2"]) == ["J1.B5(CC2)", "R2.1"], nets
    assert "R1.2" in nets["GND"] and len(nets["VBUS"]) == 4, nets
    for step in ("apply", "tidy"):
        if step == "tidy":
            t = registry.call_tool("sch_tidy", {"project": p, "preview": False})
            assert not t.get("isError"), t["content"][0]["text"][:600]
        d = registry.call_tool("design_review", {"project": p})["structuredContent"]
        assert "style" not in d and "schematic_style" not in str(d.get("checks_run", "")), d
        s = registry.call_tool("design_review", {"project": p, "checks": ["schematic_style"]})["structuredContent"]
        st = (s.get("style") or {}).get("findings") or []
        assert not s.get("findings"), s
        assert not [f for f in st if f["severity"] != "info"], (step, st)
        assert not _types(st) & DRAWING_TYPES, (step, [(f["type"], f["message"]) for f in st])
        assert all(f.get("rule") for f in st), st
    v = registry.call_tool("verify", {"project": p, "what": ["review"]})["structuredContent"]
    rv = v.get("review") or {}
    assert "style" not in rv and not [f for f in rv.get("findings") or [] if f.get("rule")], v


FP = "Resistor_SMD:R_0402_1005Metric"


def test_keep_layout_sheet_gets_a_working_wire_fix():
    """A label pair drawn with layout='keep': the C2 fix is a wire between pins already on the net, it applies, and
    afterwards KiCad's netlist is the same and the finding is gone."""
    registry, pro = _kicad_project("hop")
    p = str(pro)
    ops = [{"op": "place", "lib_id": "Device:R", "ref": "R1", "at": [100, 100], "footprint": FP},
           {"op": "place", "lib_id": "Device:R", "ref": "R2", "at": [115.24, 100], "footprint": FP},
           {"op": "connect", "pins": ["R1.2", "R2.2"], "net": "MID"},
           {"op": "connect", "pin": "R1.1", "net": "+3V3"}, {"op": "connect", "pin": "R2.1", "net": "GND"}]
    r = registry.call_tool("sch_apply", {"project": p, "ops": ops, "layout": "keep", "preview": False})
    assert not r.get("isError"), r["content"][0]["text"][:600]
    before = registry.call_tool("sch_read", {"project": p, "query": "nets"})["structuredContent"]["nets"]
    st = registry.call_tool("design_review", {"project": p, "checks": ["schematic_style"]})["structuredContent"]["style"]
    hop = next(f for f in st["findings"] if f["type"] == "label_between_neighbours")
    m = re.match(r"sch_apply\(ops=(\[.*\])\)", hop["fix"])
    assert m, hop
    r = registry.call_tool("sch_apply", {"project": p, "ops": json.loads(m.group(1)), "layout": "keep", "preview": False})
    assert not r.get("isError"), r["content"][0]["text"][:600]
    after = registry.call_tool("sch_read", {"project": p, "query": "nets"})["structuredContent"]["nets"]
    assert {k: sorted(v) for k, v in after.items()} == {k: sorted(v) for k, v in before.items()}, (before, after)
    st = registry.call_tool("design_review", {"project": p, "checks": ["schematic_style"]})["structuredContent"]["style"]
    assert "label_between_neighbours" not in _types(st.get("findings") or []), st
    # R2.1 faces up, so layout='keep' drew its GND upside down: on this stub-and-label sheet the fix is sch_tidy(),
    # and after it the drawing rules are quiet
    pd = next(f for f in st["findings"] if f["type"] == "power_symbol_direction")
    assert pd["severity"] == "info" and pd["fix"].startswith("sch_tidy()"), pd
    t = registry.call_tool("sch_tidy", {"project": p, "preview": False})
    assert not t.get("isError"), t["content"][0]["text"][:600]
    st = registry.call_tool("design_review", {"project": p, "checks": ["schematic_style"]})["structuredContent"]["style"]
    assert not _types(st.get("findings") or []) & DRAWING_TYPES, st


def main():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = skipped = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Skip as e:
            skipped += 1
            print(f"skip {name}: {e}")
        except Exception:  # noqa: BLE001
            bad += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - bad - skipped}/{len(tests)} passed" + (f" ({skipped} skipped)" if skipped else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
