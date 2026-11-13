"""Schematic drawing-style rules (review/sch_style.py): the `schematic_style` pass of design_review.

    python tests/test_sch_style.py            (or: python -m pytest tests/test_sch_style.py)

No KiCad needed: the pass reads the saved sheet files only.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-home-"))

from copper_kicad.project import Project  # noqa: E402
from copper_kicad.review import design_review, sch_style  # noqa: E402

LIBS = """
  (symbol "power:GND" (power) (pin_names (offset 0) hide)
    (property "Reference" "#PWR" (at 0 -6.35 0)) (property "Value" "GND" (at 0 -3.81 0))
    (symbol "GND_0_1" (polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))
      (stroke (width 0) (type default)) (fill (type none))))
    (symbol "GND_1_1" (pin power_in line (at 0 0 270) (length 0) (name "GND") (number "1"))))
  (symbol "power:VCC" (power) (pin_names (offset 0) hide)
    (property "Reference" "#PWR" (at 0 -3.81 0)) (property "Value" "VCC" (at 0 3.81 0))
    (symbol "VCC_0_1" (polyline (pts (xy 0 0) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))
      (circle (center 0 1.905) (radius 0.635) (stroke (width 0) (type default)) (fill (type none))))
    (symbol "VCC_1_1" (pin power_in line (at 0 0 90) (length 0) (name "VCC") (number "1"))))
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
"""


def _sym(lib, ref, value, x, y, rot=0, extra=""):
    return (f'(symbol (lib_id "{lib}") (at {x} {y} {rot}) (unit 1) (uuid "u-{ref}")\n'
            f'  (property "Reference" "{ref}" (at {x} {y - 3} 0)) (property "Value" "{value}" (at {x} {y + 3} 0)){extra})')


def _sheet(body: str, uuid="root") -> str:
    return (f'(kicad_sch (version 20250114) (generator "eeschema") (uuid "{uuid}") (paper "A4")\n'
            f'(lib_symbols {LIBS})\n{body}\n(sheet_instances (path "/" (page "1"))))\n')


def _wire(a, b):
    return f"(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) (uuid \"w{a[0]}{a[1]}{b[0]}{b[1]}\"))"


def _project(body: str, pro: dict | None = None, name="t") -> Project:
    d = Path(tempfile.mkdtemp(prefix="copper-style-"))
    (d / f"{name}.kicad_sch").write_text(_sheet(body), encoding="utf-8")
    (d / f"{name}.kicad_pro").write_text(json.dumps(pro or {}), encoding="utf-8")
    return Project(d, name)


BAD = "\n".join([
    _sym("power:GND", "#PWR01", "GND", 50, 50, 90),                      # sideways ground
    _sym("power:VCC", "#PWR02", "VCC", 60, 50, 180),                     # supply upside down, generic name
    _sym("Device:R", "R1", "10k", 80, 50),
    _sym("Mine:Q_bare", "Q1", "BC847", 120, 50),
    _sym("Mine:DIP8", "U1", "DIP8", 160, 50),
    _wire((100, 100), (110, 100)), _wire((110, 100), (120, 100)), _wire((110, 90), (110, 100)), _wire((110, 100), (110, 110)),
    '(label "SIG" (at 80 46.19 0) (uuid "l1"))', '(label "SIG" (at 90 46.19 0) (uuid "l2"))',
    '(global_label "BUS" (shape input) (at 30 30 0) (uuid "g1"))',
])

GOOD = "\n".join([
    _sym("power:GND", "#PWR01", "GND", 50, 60, 0),
    _sym("power:VCC", "#PWR02", "VUSB", 50, 40, 0),
    _sym("Device:R", "R1", "10k 1% 0.063W 0402", 80, 50),
    _wire((100, 100), (110, 100)), _wire((110, 100), (120, 100)), _wire((110, 90), (110, 100)),      # a tee
    '(label "SIG" (at 80 46.19 0) (uuid "l1"))',
    '(global_label "BUS" (shape input) (at 30 30 0) (uuid "g1"))',
    '(text "VUSB: 4.5 / 5.0 / 5.5 V, 35 mA typ, 500 mA max" (at 20 20 0) (uuid "t1"))',
    '(text "Unless noted: resistors 1% 0.063W 0402" (at 20 25 0) (uuid "t2"))',
])


def _types(fs):
    return {f["type"] for f in fs}


def test_rule_table_is_complete():
    ids = [r[0] for r in sch_style.RULES]
    assert len(ids) == len(set(ids)) and len(ids) >= 28, ids
    for rid in ("F1", "F2", "Y1", "Y2", "Y3", "Y4", "G1", "G3", "G4", "C1", "C2", "C5", "C6", "C7", "P1", "P2", "P4",
                "D1", "D2", "D3", "D4", "K1"):
        assert rid in ids, rid
    doc = (ROOT / "docs" / "SCHEMATIC_STYLE.md").read_text(encoding="utf-8")
    for rid, title, _ in sch_style.RULES:
        assert f"**{rid} " in doc, f"docs/SCHEMATIC_STYLE.md does not list rule {rid} {title}"


def test_bad_sheet_is_flagged():
    fs = sch_style.check(_project(BAD))
    t = _types(fs)
    want = {"power_symbol_direction", "four_way_junction", "label_between_neighbours", "generic_rail_name",
            "passive_spec_incomplete", "no_standard_parts_note", "transistor_without_envelope", "package_shaped_symbol",
            "symbol_body_unfilled", "intersheet_refs_off", "rail_without_spec"}
    assert want <= t, sorted(want - t)
    assert "review_check_failed" not in t, [f["message"] for f in fs if f["type"] == "review_check_failed"]
    for f in fs:
        assert f["fix"] and f["rule"] in sch_style.RULE_TITLE and f["section"].startswith(f["rule"]), f
    pd = next(f for f in fs if f["type"] == "power_symbol_direction")
    assert set(pd["items"]) == {"#PWR01", "#PWR02"}, pd


def test_good_sheet_is_quiet():
    pr = _project(GOOD, {"schematic": {"drawing": {"intersheets_ref_show": True}}})
    fs = sch_style.check(pr)
    assert not fs, [(f["type"], f["message"]) for f in fs]


def test_runs_inside_design_review():
    assert "schematic_style" in design_review.CHECKS
    nl = {"components": {}, "nets": {}}
    r = design_review.review(_project(BAD), checks=["schematic_style"], include_erc=False, netlist=nl)
    assert r["checks_run"] == ["schematic_style"] and "power_symbol_direction" in _types(r["findings"]), r
    r0 = design_review.review(None, checks=["schematic_style"], include_erc=False, netlist=nl)   # no files: skipped
    assert not r0["findings"]


def test_templates_show_intersheet_refs():
    for f in (ROOT / "src" / "copper_kicad" / "data" / "templates").glob("*.kicad_pro"):
        d = json.loads(f.read_text(encoding="utf-8"))
        assert d["schematic"]["drawing"]["intersheets_ref_show"] is True, f.name


def test_guidance_carries_the_rules():
    skill = (ROOT / "src" / "copper_kicad" / "skills" / "kicad-design" / "SKILL.md").read_text(encoding="utf-8")
    for phrase in ("wires", "VUSB", "min / typ / max", "four-way", "standard-parts", "block diagram", "IEEE 315"):
        assert phrase in skill, phrase
    assert skill == (ROOT / "plugin" / "skills" / "kicad-design" / "SKILL.md").read_text(encoding="utf-8")


def main():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception:  # noqa: BLE001
            bad += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
