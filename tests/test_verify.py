"""Tests for the verify area: ERC/DRC normalisation, design review, JLC fab pack,
exports, project templates, checkpoints and MCP resources/prompts.

Runs against the real KiCad install (kicad-cli). Every project is built in a
temporary directory — nothing is written into the repo.

    python -m pytest tests/test_verify.py -q
    python tests/test_verify.py            (no pytest needed)
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_TMP = Path(tempfile.mkdtemp(prefix="copper-test-verify-"))
os.environ.setdefault("COPPER_HOME", str(_TMP / "home"))

from copper_kicad import project as P  # noqa: E402
from copper_kicad import sexpr  # noqa: E402
from copper_kicad.kicad import env  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402
from copper_kicad.review import common, design_review, fab, project_info, verify  # noqa: E402


def _share() -> Path:
    return env.kicad().share


# ── fixture: a small but realistic broken schematic built from stock symbols ─────
def _lib_symbol(lib: str, name: str):
    tree = sexpr.loads((_share() / "symbols" / f"{lib}.kicad_sym").read_text(encoding="utf-8"))
    for s in sexpr.find_all(tree, "symbol"):
        if s[1] == name:
            assert sexpr.find(s, "extends") is None, f"{lib}:{name} extends another symbol"
            s = [x for x in s]
            s[1] = f"{lib}:{name}"
            return s
    raise AssertionError(f"{lib}:{name} not found")


def _pins(sym) -> dict:
    """pin number → (x, y, name) in symbol coordinates (Y up)."""
    out = {}

    def walk(node):
        for e in node:
            if isinstance(e, list) and e and e[0] == "pin":
                at = sexpr.find(e, "at")
                nm = sexpr.find(e, "name")
                num = sexpr.find(e, "number")
                out[str(num[1])] = (float(at[1]), float(at[2]), str(nm[1]) if nm else "")
            elif isinstance(e, list) and e and e[0] == "symbol":
                walk(e)
    walk(sym)
    return out


def _fmt(x) -> str:
    return sexpr.dumps(x)


def build_fixture(dirpath: Path) -> P.Project:
    """USB-C + PCF8574 I/O expander + LDO + LED, wired with labels, with deliberate mistakes:
    CC2 without Rd, no decoupling, no I2C pull-ups, LED without resistor, LDO without caps,
    a 'SDAA' label typo, unconnected expander pins, rails without PWR_FLAG."""
    r = project_info.create_project("fixture", str(dirpath), "jlc-2layer", [60, 40])
    pr = P.resolve(r["project"]["dir"])
    root_uuid = str(sexpr.value(sexpr.loads(pr.sch.read_text(encoding="utf-8")), "uuid"))
    libs = {"Device:R": _lib_symbol("Device", "R"), "Device:C": _lib_symbol("Device", "C"),
            "Device:LED": _lib_symbol("Device", "LED"),
            "Interface_Expansion:PCF8574TS": _lib_symbol("Interface_Expansion", "PCF8574TS"),
            "Connector:USB_C_Receptacle_USB2.0_16P": _lib_symbol("Connector", "USB_C_Receptacle_USB2.0_16P"),
            "Regulator_Linear:AP1117-15": _lib_symbol("Regulator_Linear", "AP1117-15"),
            "Connector_Generic:Conn_01x04": _lib_symbol("Connector_Generic", "Conn_01x04")}
    body, labels = [], []

    def place(lib_id, ref, value, x, y, nets: dict, footprint, lcsc=""):
        sym = libs[lib_id]
        pins = _pins(sym)
        props = [["property", "Reference", ref, ["at", x, y - 8, 0], ["effects", ["font", ["size", 1.27, 1.27]]]],
                 ["property", "Value", value, ["at", x, y + 8, 0], ["effects", ["font", ["size", 1.27, 1.27]]]],
                 ["property", "Footprint", footprint, ["at", x, y, 0], ["effects", ["font", ["size", 1.27, 1.27]], ["hide", sexpr.Sym("yes")]]]]
        if lcsc:
            props.append(["property", "LCSC", lcsc, ["at", x, y, 0], ["effects", ["font", ["size", 1.27, 1.27]], ["hide", sexpr.Sym("yes")]]])
        s = [sexpr.Sym("symbol"), [sexpr.Sym("lib_id"), lib_id], [sexpr.Sym("at"), x, y, 0], [sexpr.Sym("unit"), 1],
             [sexpr.Sym("exclude_from_sim"), sexpr.Sym("no")], [sexpr.Sym("in_bom"), sexpr.Sym("yes")],
             [sexpr.Sym("on_board"), sexpr.Sym("yes")], [sexpr.Sym("dnp"), sexpr.Sym("no")],
             [sexpr.Sym("uuid"), str(uuid.uuid4())]]
        for p in props:
            p[0] = sexpr.Sym("property")
            for sub in p[3:]:
                _symify(sub)
            s.append(p)
        for num in pins:
            s.append([sexpr.Sym("pin"), num, [sexpr.Sym("uuid"), str(uuid.uuid4())]])
        s.append([sexpr.Sym("instances"), [sexpr.Sym("project"), "fixture",
                                           [sexpr.Sym("path"), f"/{root_uuid}", [sexpr.Sym("reference"), ref], [sexpr.Sym("unit"), 1]]]])
        body.append(s)
        for num, net in nets.items():
            px, py, _ = pins[num]
            labels.append([sexpr.Sym("label"), net, [sexpr.Sym("at"), round(x + px, 4), round(y - py, 4), 0],
                           [sexpr.Sym("effects"), [sexpr.Sym("font"), [sexpr.Sym("size"), 1.27, 1.27]],
                            [sexpr.Sym("justify"), sexpr.Sym("left"), sexpr.Sym("bottom")]],
                           [sexpr.Sym("uuid"), str(uuid.uuid4())]])
        return pins

    usb = place("Connector:USB_C_Receptacle_USB2.0_16P", "J1", "USB_C", 50.8, 88.9,
                {"A4": "VBUS", "A9": "VBUS", "B4": "VBUS", "B9": "VBUS", "A1": "GND", "A12": "GND", "B1": "GND", "B12": "GND",
                 "SH": "GND", "A5": "CC1", "B5": "CC2", "A6": "USB_DP", "B6": "USB_DP", "A7": "USB_DN", "B7": "USB_DN"},
                "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12", "C165948")
    place("Device:R", "R1", "5.1k", 88.9, 63.5, {"1": "CC1", "2": "GND"}, "Resistor_SMD:R_0402_1005Metric", "C25905")
    place("Regulator_Linear:AP1117-15", "U2", "AMS1117-3.3", 101.6, 101.6, {"1": "GND", "2": "+3V3", "3": "VBUS"},
          "Package_TO_SOT_SMD:SOT-223-3_TabPin2", "C6186")
    exp = place("Interface_Expansion:PCF8574TS", "U1", "PCF8574TS", 152.4, 88.9, {}, "Package_SO:SSOP-20_4.4x6.5mm_P0.65mm")
    by_name = {v[2]: k for k, v in exp.items()}
    nets = {by_name["VDD"]: "+3V3", by_name["VSS"]: "GND", by_name["SDA"]: "SDA", by_name["SCL"]: "SCL"}
    # attach the nets to U1 now that we know its pin numbers
    for num, net in nets.items():
        px, py, _ = exp[num]
        labels.append([sexpr.Sym("label"), net, [sexpr.Sym("at"), round(152.4 + px, 4), round(88.9 - py, 4), 0],
                       [sexpr.Sym("effects"), [sexpr.Sym("font"), [sexpr.Sym("size"), 1.27, 1.27]],
                        [sexpr.Sym("justify"), sexpr.Sym("left"), sexpr.Sym("bottom")]], [sexpr.Sym("uuid"), str(uuid.uuid4())]])
    # I2C header (second bus member) and a pull-up whose label was mistyped 'SDAA'
    place("Connector_Generic:Conn_01x04", "J2", "I2C", 228.6, 88.9, {"1": "+3V3", "2": "GND", "3": "SDA", "4": "SCL"},
          "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
    place("Device:LED", "D1", "LED red", 203.2, 63.5, {"1": "GND", "2": "+3V3"}, "LED_SMD:LED_0603_1608Metric", "C2286")
    place("Device:R", "R2", "4.7k", 203.2, 101.6, {"1": "SDAA", "2": "+3V3"}, "Resistor_SMD:R_0402_1005Metric", "C25900")
    tree = sexpr.loads(pr.sch.read_text(encoding="utf-8"))
    ls = sexpr.find(tree, "lib_symbols")
    for lid in sorted(libs):
        ls.append(libs[lid])
    idx = tree.index(sexpr.find(tree, "sheet_instances"))
    tree[idx:idx] = labels + body
    pr.sch.write_text(sexpr.dumps(tree) + "\n", encoding="utf-8", newline="\n")
    common._NL_CACHE.clear()
    return pr


def _symify(node):
    if isinstance(node, list) and node:
        if isinstance(node[0], str):
            node[0] = sexpr.Sym(node[0])
        for e in node[1:]:
            _symify(e)


_FIX = {}


def fixture() -> P.Project:
    if "pr" not in _FIX:
        d = _TMP / "fixture"
        if d.exists():
            shutil.rmtree(d)
        _FIX["pr"] = build_fixture(d)
    return _FIX["pr"]


def demo(name: str) -> P.Project:
    src = _share() / "demos" / name
    dst = _TMP / "demos" / name
    if not dst.exists():
        shutil.copytree(src, dst)
    return P.resolve(str(dst))


# ── unit tests ───────────────────────────────────────────────────────────
def test_parse_item():
    it = common.parse_item("Symbol U1 Pin 3 [GPIO, Bidirectional, Line]")
    assert it["kind"] == "pin" and it["pin_ref"] == "U1.3" and it["pin_name"] == "GPIO" and it["pin_type"] == "Bidirectional"
    it = common.parse_item("Symbol #PWR01 Pin 1 [Power input, Line]")
    assert it["pin_ref"] == "#PWR01.1" and "pin_name" not in it and it["pin_type"] == "Power input"
    it = common.parse_item("Symbol U3A Pin 7 [~{RESET}, Input, Line]")
    assert it["ref"] == "U3" and it["pin_ref"] == "U3.7"
    it = common.parse_item("PTH pad 1 [+BATT] of J2")
    assert it["kind"] == "pad" and it["ref"] == "J2" and it["net"] == "+BATT"
    it = common.parse_item("Pad 2 [/Debugger/D-] of U3 on F.Cu")
    assert it["pin_ref"] == "U3.2" and it["net"] == "/Debugger/D-" and it["layer"] == "F.Cu"
    it = common.parse_item("Track [GND] on F.Cu, length 0.4425 mm")
    assert it["kind"] == "track" and it["net"] == "GND"
    assert common.parse_item("Footprint J4")["ref"] == "J4"
    assert common.parse_item("Label 'SDA'")["net"] == "SDA"


def test_values_and_nets():
    assert common.parse_value("4k7") == 4700 and common.parse_value("5.1k") == 5100
    assert abs(common.parse_value("100nF") - 1e-7) < 1e-15 and common.parse_value("0R") == 0
    assert common.is_ground_net("GND") and common.is_ground_net("/sub/AGND") and not common.is_ground_net("SDA")
    assert common.is_power_net("+3V3") and common.is_power_net("VBUS") and not common.is_power_net("SDA")
    assert common.lcsc_of({"LCSC Part #": "c25804"}) == "C25804" and common.lcsc_of({"Value": "C1"}) == ""


def test_jlc_placement_rules():
    J = "jlcpcb"                                    # the default fab profile: JLC rotation table + CPL conventions
    rules = fab.load_rotation_rules(profile=J)
    assert len(rules) > 40
    top = fab.placement_row({"name": "SOT-23", "rotation": 0, "layer": "top", "pos": [10, 20]}, rules, profile=J)
    assert top["rotation"] == 270 and top["layer"] == "Top" and top["y"] == -20
    plain = fab.placement_row({"name": "R_0402_1005Metric", "rotation": 90, "layer": "top", "pos": [1, 2]}, rules, profile=J)
    assert plain["rotation"] == 90 and plain["correction_source"] is None
    bottom = fab.placement_row({"name": "R_0402_1005Metric", "rotation": 30, "layer": "bottom", "pos": [1, 2]}, rules, profile=J)
    assert bottom["rotation"] == 150 and bottom["layer"] == "Bottom"
    ov = fab.placement_row({"name": "SOT-23", "rotation": 0, "layer": "top", "pos": [0, 0],
                            "fields": {"JLC Rotation Offset": "90"}}, rules, profile=J)
    assert ov["rotation"] == 90 and ov["correction_source"] == "field"
    off = fab.placement_row({"name": "X", "rotation": 90, "layer": "top", "pos": [0, 0],
                             "fields": {"JLC Position Offset": "1,0"}}, rules, profile=J)
    assert math.isclose(off["x"], 0, abs_tol=1e-6) and math.isclose(off["y"], 1, abs_tol=1e-6)  # rotated offset, Y up
    # a profile without a rotation table (generic, pcbway, oshpark) never corrects: KiCad's rotation goes out as it is
    assert fab.load_rotation_rules(profile="generic") == []
    g = fab.placement_row({"name": "SOT-23", "rotation": 0, "layer": "top", "pos": [10, 20],
                           "fields": {"JLC Rotation Offset": "90"}}, [], profile="generic")
    assert g["rotation"] == 0 and g["correction_source"] is None and g["layer"] == "top", g
    # KiCad's HRO TYPE-C-31-M-12 footprint is drawn the same way round as JLC's own footprint for C165948 (pad row
    # toward -y, A1/B12 at -x), so the CPL rotation is the board rotation. A former +180 rule flipped the receptacle.
    for rot in (0, 90, 180):
        usb = fab.placement_row({"name": "USB_C_Receptacle_HRO_TYPE-C-31-M-12", "rotation": rot, "layer": "top",
                                 "pos": [0, 0]}, rules, profile=J)
        assert usb["rotation"] == rot and usb["correction_source"] is None, usb


# ── templates ────────────────────────────────────────────────────────────
def test_project_templates_load_cleanly():
    for t, size in (("blank", None), ("jlc-2layer", [50, 30]), ("jlc-4layer", [80, 60])):
        r = project_info.create_project(f"tpl-{t}", str(_TMP / "tpl" / t), t, size)
        chk = r["kicad_check"]
        assert chk["ok"] and chk["erc_violations"] == 0, chk
        if size:
            assert chk["drc_violations"] == 0, chk
        pr = P.resolve(r["project"]["dir"])
        assert project_info.board_outline_mm(pr.pcb) == ([float(size[0]), float(size[1])] if size else None)
        assert len(fab.copper_layers(pr.pcb)) == (4 if t == "jlc-4layer" else 2)
        pro = json.loads(pr.pro.read_text(encoding="utf-8"))
        assert pro["meta"]["filename"] == f"tpl-{t}.kicad_pro"
        if t != "blank":
            assert pro["board"]["design_settings"]["rules"]["min_copper_edge_clearance"] == 0.3
            assert (pr.dir / f"tpl-{t}.kicad_dru").exists()
        # KiCad's canonical writer must not change our board file
        tmp = pr.dir / "roundtrip.kicad_pcb"
        shutil.copy2(pr.pcb, tmp)
        env.run(["pcb", "upgrade", "--force", str(tmp)], timeout=120, check=True)
        assert tmp.read_text(encoding="utf-8").replace("\r", "") == pr.pcb.read_text(encoding="utf-8").replace("\r", "")
    try:
        project_info.create_project("tpl-blank", str(_TMP / "tpl" / "blank"), "blank")
        raise AssertionError("overwrote an existing project")
    except ToolError as e:
        assert e.code == "EXISTS"


# ── ERC / review on the fixture ──────────────────────────────────────────
def test_erc_findings_and_delta():
    pr = fixture()
    e = verify.run_erc(pr)
    types = e["by_type"]
    assert types.get("pin_not_connected", 0) >= 8, types           # P0..P7, A0..A2, INT on U1
    nc = [f for f in e["violations"] if f["type"] == "pin_not_connected"]
    assert all(f["items"][0]["pin_ref"].startswith(("U1.", "J1.")) for f in nc)
    assert "sch_apply" in nc[0]["fix"] and '"no_connect"' in nc[0]["fix"]
    for f in e["violations"]:
        if f.get("at"):
            assert 0 <= f["at"][0] <= 297 and 0 <= f["at"][1] <= 210, f   # mm on an A4 sheet (KiCad's 1/100 bug fixed)
    pw = [f for f in e["violations"] if f["type"] == "power_pin_not_driven"]
    assert pw and {common.base_net(f.get("net", "")) for f in pw} >= {"GND", "VBUS"}, pw
    assert any('"op":"pwr_flag"' in f["fix"] for f in pw)       # a real sch_apply op, not prose
    # second run: nothing new
    e2 = verify.run_erc(pr)
    assert e2["new"] == 0 and e2["fixed"] == 0 and not e2["first_run"]
    # put a no-connect flag on one unconnected pin → exactly that finding is fixed
    target = nc[0]
    x, y = target["items"][0]["at"]
    txt = pr.sch.read_text(encoding="utf-8")
    flag = f'\t(no_connect\n\t\t(at {x} {y})\n\t\t(uuid "{uuid.uuid4()}")\n\t)\n'
    i = txt.index("\t(sheet_instances")
    pr.sch.write_text(txt[:i] + flag + txt[i:], encoding="utf-8", newline="\n")
    e3 = verify.run_erc(pr)
    assert e3["fixed"] >= 1 and e3["new"] == 0, (e3["fixed"], e3["new"], e3["new_findings"])
    b = verify.erc_brief(pr, limit=5)
    assert len(b["top"]) <= 5 and b["errors"] == e3["errors"]


def test_design_review_catches_the_planted_mistakes():
    pr = fixture()
    r = design_review.review(pr)
    t = {}
    for f in r["findings"]:
        t.setdefault(f["type"], []).append(f)
    assert "missing_decoupling" in t and any("U1" in f["message"] for f in t["missing_decoupling"]), t.keys()
    assert "i2c_no_pullup" in t and {common.base_net(f["net"]) for f in t["i2c_no_pullup"]} == {"SDA", "SCL"}, t.get("i2c_no_pullup")
    cc = t.get("usb_c_cc_floating", []) + t.get("usb_c_cc_no_rd", [])
    assert cc and all("CC2" in (f.get("net") or f["message"]) for f in cc), cc        # CC2 has no Rd
    assert not any("CC1" in f["message"] for f in cc)                                  # CC1 has its 5.1k
    assert "usb_no_esd" not in t          # the fixture's D+/D- reach no transceiver: nothing to protect (see test_usb_esd_*)
    assert "led_no_resistor" in t and "D1" in t["led_no_resistor"][0]["message"]
    assert "regulator_no_input_cap" in t and "regulator_no_output_cap" in t
    sp = [f for f in t.get("single_pin_net", []) if common.base_net(f["net"]) == "SDAA"]
    assert sp and common.base_net(sp[0].get("suggestion", "")) == "SDA", t.get("single_pin_net")
    assert "rail_not_driven" in t
    assert "missing_lcsc" in t and "U1" in t["missing_lcsc"][0]["items"]
    for f in r["findings"]:
        assert f["severity"] in ("error", "warning", "info") and f["fix"], f
    # no false alarms on this design
    assert "crystal_no_load_caps" not in t and "floating_enable" not in t and "net_name_collision" not in t


def test_design_review_quiet_on_demo():
    pr = demo("stickhub")
    r = design_review.review(pr)
    noisy = [f for f in r["findings"] if f["type"] in ("net_name_typo", "net_name_collision", "single_pin_net", "led_no_resistor")]
    assert not noisy, noisy
    assert r["stats"]["components"] > 50


# ── DRC / fab / export on a real KiCad demo ──────────────────────────────
def test_drc_and_fab_pack():
    pr = demo("pic_programmer")
    d = verify.run_drc(pr)
    assert d["parity_checked"] and isinstance(d["errors"], int) and d["unconnected"] == 0
    try:
        fab.fab_export(pr)
        raise AssertionError("fab_export (default profile jlcpcb) should refuse unsourced parts")
    except ToolError as e:
        assert e.code == "FAB_BLOCKED" and any(b["code"] == "parts" for b in e.data["blockers"])
        assert "LCSC number" in e.message and e.data["profile"] == "jlcpcb", e.message
        assert "pic_programmer" in e.message and e.data["project"] == "pic_programmer", e.message   # which project was gated
    r = fab.fab_export(pr, waive=["all"])
    assert r["profile"] == "jlcpcb" and Path(r["zip"]).name == "pic_programmer-JLCPCB.zip", r["zip"]
    names = zipfile.ZipFile(r["zip"]).namelist()
    exts = {Path(n).suffix.lower() for n in names}
    assert {".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gm1", ".drl"} <= exts, names
    assert len([n for n in names if not n.endswith(".drl")]) == 9
    bom = Path(r["bom"]).read_text(encoding="utf-8").splitlines()
    cpl = Path(r["placement"]).read_text(encoding="utf-8").splitlines()
    assert Path(r["bom"]).name == "pic_programmer-BOM-JLCPCB.csv" and Path(r["placement"]).name == "pic_programmer-CPL-JLCPCB.csv"
    assert bom[0] == "Comment,Designator,Footprint,LCSC Part #"
    assert cpl[0] == "Designator,Mid X,Mid Y,Layer,Rotation" and len(cpl) - 1 == r["placements"] > 40
    assert r["summary"]["board_mm"][0] > 50 and r["summary"]["copper_layers"] == 2
    assert {w["code"] for w in r["waived"]} >= {"parts"}


def test_cpl_matches_kicad_positions():
    pr = demo("stickhub")
    fps = {f["ref"]: f for f in fab.board_footprints(pr)["footprints"]}
    out = _TMP / "pos.csv"
    env.run(["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "-o", str(out), str(pr.pcb)], check=True)
    import csv
    n = 0
    for row in csv.DictReader(out.open(encoding="utf-8")):
        f = fps[row["Ref"]]
        assert abs(f["pos"][0] - float(row["PosX"])) < 1e-3 and abs(-f["pos"][1] - float(row["PosY"])) < 1e-3
        assert abs((f["rotation"] - float(row["Rot"]) + 180) % 360 - 180) < 1e-3
        assert f["layer"] == row["Side"]
        n += 1
    assert n > 50


def test_exports():
    pr = demo("pic_programmer")
    for kind in ("netlist", "pdf_schematic", "bom", "pos", "stats", "drill"):
        r = fab.export(pr, kind)
        assert r["files"] and all(f["bytes"] > 0 for f in r["files"]), r
    try:
        fab.export(pr, "nope")
        raise AssertionError
    except ToolError as e:
        assert e.code == "BAD_ARGUMENT"


# ── project info, checkpoints, resources ─────────────────────────────────
def test_summary_and_checkpoint_diff():
    pr = fixture()
    s = project_info.summary(pr)
    rails = {common.base_net(n) for n in s["power_rails"]}
    assert s["components"] == 7 and {"+3V3", "VBUS"} <= rails and s["board_mm"] == [60.0, 40.0], (s["components"], rails, s["board_mm"])
    cid = P.checkpoint(pr, "test")
    txt = pr.sch.read_text(encoding="utf-8").replace('"LED red"', '"LED green"', 1)
    pr.sch.write_text(txt, encoding="utf-8", newline="\n")
    d = project_info.diff_checkpoint(pr, cid)
    sch = [f for f in d["changed_files"] if f["file"].endswith(".kicad_sch")][0]
    assert any(c["ref"] == "D1" and "value" in c["changed"] for c in sch["symbols_changed"]), sch


def test_resources_and_prompts():
    from copper_kicad import resources
    P.activate(fixture())                  # make the fixture the active project
    lst = resources.dispatch("resources/list", {})
    assert {r["uri"] for r in lst["resources"]} >= {"copper://status", "copper://guide", "copper://project/bom"}
    g = resources.dispatch("resources/read", {"uri": "copper://guide"})["contents"][0]
    assert "sch_apply" in g["text"]
    bom = json.loads(resources.dispatch("resources/read", {"uri": "copper://project/bom"})["contents"][0]["text"])
    assert bom["parts"] == 7 and bom["unsourced"] == 2, bom
    nl = resources.dispatch("resources/read", {"uri": "copper://project/netlist"})["contents"][0]["text"]
    assert "SDA:" in nl and "U1." in nl
    bad = json.loads(resources.dispatch("resources/read", {"uri": "copper://nope"})["contents"][0]["text"])
    assert bad["error"] == "NOT_FOUND"
    pl = resources.dispatch("prompts/list", {})
    assert {p["name"] for p in pl["prompts"]} == {"design_board", "source_bom", "review_design", "release_to_fab"}
    msg = resources.dispatch("prompts/get", {"name": "design_board", "arguments": {"requirements": "USB-C LED blinker"}})
    assert "USB-C LED blinker" in msg["messages"][0]["content"]["text"]
    assert resources.dispatch("completion/complete", {})["completion"]["values"] == []


def test_tools_registered():
    from copper_kicad.mcp import registry
    from copper_kicad.tools import fab_tools, project_tools, verify_tools  # noqa: F401
    for name in ("verify", "render", "design_review", "fab_export", "export", "copper_status", "project_create",
                 "project_open", "project_summary", "checkpoint"):
        assert name in registry.TOOLS, name
    core = {n for n, t in registry.TOOLS.items() if t.core}
    assert {"verify", "copper_status", "project_summary"} <= core
    out = registry.call_tool("render", {"target": "schematic", "project": str(fixture().dir), "width": 600})
    assert not out["isError"] and any(c["type"] == "image" for c in out["content"])
    out = registry.call_tool("verify", {"what": ["erc"], "project": str(fixture().dir), "limit": 3})
    assert not out["isError"] and len(out["structuredContent"]["erc"]["findings"]) <= 3


# ── regression tests from the skeptical-tester pass ───────────────────────
def test_kicad_cli_uses_the_real_global_lib_tables():
    """env.run() sets KICAD_CONFIG_HOME=…/kicad/10.0 but KiCad appends the version → it read …/10.0/10.0 (no lib
    tables) and every stock footprint became 'configuration does not include the footprint library'."""
    pr = demo("sonde xilinx")                     # also: spaces in the project name and path
    # Under a scratch KICAD_CONFIG_HOME there are no global tables at all (KiCad's first-run setup never ran
    # there): seed KiCad's own defaults, so the test checks which folder KiCad reads, not this machine's setup.
    from copper_kicad.kicad import env as _env
    cfg, share = _env.config_dir(), _env.kicad().share
    if os.environ.get("KICAD_CONFIG_HOME") and share and not (cfg / "fp-lib-table").exists():
        cfg.mkdir(parents=True, exist_ok=True)
        for n in ("sym-lib-table", "fp-lib-table"):
            if (share / "template" / n).exists() and not (cfg / n).exists():
                shutil.copy2(share / "template" / n, cfg / n)
    e = verify.run_erc(pr)
    bad = [f for f in e["violations"] if "does not include" in f["message"]]
    assert not bad, bad[:3]
    assert e["warnings"] <= 3, e["by_type"]       # only the 2 genuinely missing Connector_Dsub footprints
    r = common.cli(["version"])
    assert r.returncode == 0 and r.stdout.strip()


def test_parse_item_kicad10_variants():
    it = common.parse_item("Symbol #PWR02 Hidden pin 1 [+12V, Power input, Line]")
    assert it["kind"] == "pin" and it["pin_ref"] == "#PWR02.1" and it["pin_name"] == "+12V", it
    it = common.parse_item("Symbol #PWR0686 Hidden pin  [P3V3, Power input, Line]")
    assert it["pin_ref"] == "#PWR0686.1" and it["pin_type"] == "Power input", it
    it = common.parse_item("Symbol U21 Hidden pin 85 [VCORE, Power input, Line]")
    assert it["pin_ref"] == "U21.85", it
    it = common.parse_item("Track (arc) [/SoM/DP2.AUX_C-] on F.Cu, length 0.3770 mm")
    assert it["kind"] == "track" and it["net"] == "/SoM/DP2.AUX_C-" and it["layer"] == "F.Cu", it
    it = common.parse_item("Zone 'p12v_vme' [P12V] on L10, priority 16")
    assert it["kind"] == "zone" and it["net"] == "P12V" and it["layer"] == "L10", it


def test_power_symbol_rail_gets_its_net_and_a_real_fix():
    pr = demo("multichannel")
    e = verify.run_erc(pr)
    pw = [f for f in e["violations"] if f["type"] == "power_pin_not_driven"]
    assert pw and all(f.get("net") for f in pw), pw                     # hidden power pins resolved to their rail
    assert any(f["net"] == "+12V" and '{"op":"pwr_flag","net":"+12V"}' in f["fix"] for f in pw), [f["fix"] for f in pw]


def test_net_classification():
    for n in ("+5V", "+3V3", "+5F", "-12V", "3V3", "3.3V", "1V8_IO", "VCC", "VDD_CORE", "VEE"):
        assert common.is_power_net(n), n
    for n in ("/graphic/14MHZOUT", "VPP_ON", "VBUS_DET", "SDA", "2N2222_B", "GND"):
        assert not common.is_power_net(n), n
    assert not common.is_ground_net("VEE") and common.is_ground_net("AGND")


def test_read_only_project_still_verifies():
    src = _share() / "demos" / "pic_programmer"      # under Program Files: not writable
    pr = P.Project(src, "pic_programmer")
    try:
        (src / ".copper-probe").write_text("")
        (src / ".copper-probe").unlink()
        # running elevated (admin, or root on Linux: CI runs this suite as an ordinary user for that reason)
        print("  note: test_read_only_project_still_verifies tested nothing: the KiCad demos folder is writable")
        return
    except OSError:
        pass
    e = verify.run_erc(pr)
    assert e["errors"] == 0 and str(P.STATE_DIR) in e["report"], e["report"]
    s = project_info.summary(pr)
    assert s["components"] > 50 and s.get("read_only")
    assert verify.last_results(pr)["erc"]["errors"] == 0


def test_create_never_overwrites_a_lone_schematic():
    d = _TMP / "lone"
    d.mkdir(exist_ok=True)
    (d / "lone.kicad_sch").write_text("(kicad_sch (version 20250114))", encoding="utf-8")
    try:
        project_info.create_project("lone", str(d.parent))
        raise AssertionError("create_project overwrote lone.kicad_sch")
    except ToolError as e:
        assert e.code == "EXISTS"
    assert (d / "lone.kicad_sch").read_text(encoding="utf-8").startswith("(kicad_sch (version 20250114))")


def test_export_out_guards():
    pr = demo("stickhub")
    before = P.revision(pr.pcb)
    try:
        fab.export(pr, "step", out=str(pr.pcb))
        raise AssertionError("export overwrote the board")
    except ToolError as e:
        assert e.code == "BAD_ARGUMENT"
    assert P.revision(pr.pcb) == before
    d = _TMP / "posdir"
    d.mkdir(exist_ok=True)
    r = fab.export(pr, "pos", out=str(d))             # a folder for a single-file kind
    assert Path(r["path"]).parent == d and Path(r["path"]).stat().st_size > 0


def test_fab_refuses_an_empty_board():
    r = project_info.create_project("emptyfab", str(_TMP), "jlc-2layer", [30, 20])
    pr = P.resolve(r["project"]["dir"])
    try:
        fab.fab_export(pr)
        raise AssertionError("fab_export produced a pack for an empty board")
    except ToolError as e:
        assert e.code == "FAB_BLOCKED" and "empty" in json.dumps(e.data["blockers"])
    from copper_kicad.mcp import registry
    out = registry.call_tool("verify", {"project": str(pr.dir)})
    assert "no parts yet" in out["structuredContent"]["next"], out["structuredContent"]["next"]


def test_compact_groups_repeats():
    from copper_kicad.tools import verify_tools as VT
    fs = [common.finding("error", "pin_not_connected", f"Pin not connected — U1.{i}", "x",
                         [{"pin_ref": f"U1.{i}", "ref": "U1"}], [1, 2]) for i in range(10)]
    fs += [common.finding("warning", "lib_footprint_mismatch", f"Footprint 'F{i}' differs — R{i}", "y", [{"ref": f"R{i}"}])
           for i in range(30)]
    fs.append(common.finding("warning", "clearance", "Clearance violation — U2", "z", [{"ref": "U2"}]))
    entries, more = VT._compact(fs, 25)
    assert len(entries) == 3 and more == 0, entries
    nc = entries[0]
    assert nc["count"] == 10 and len(nc["refs"]) == 10 and '"pins":["U1.0"' in nc["fix"], nc


def test_fixed_findings_are_readable():
    pr = fixture()
    verify.run_erc(pr)
    st = common.load_state(pr)["erc"]
    sig = st["signatures"][0]
    st["signatures"].append("/|pin_not_connected|bogus")
    st["messages"]["/|pin_not_connected|bogus"] = "Pin not connected — U9.9"
    common.save_state(pr, "erc", st)
    e = verify.run_erc(pr)
    assert e["fixed"] == 1 and e["fixed_findings"] == ["Pin not connected — U9.9"], e["fixed_findings"]
    assert sig in common.load_state(pr)["erc"]["signatures"]


def test_review_routes_fixes_to_subsheets_and_ignores_dnp():
    nl = {"components": {"U1": {"value": "MCU", "lib_id": "MCU:X", "sheet": "/power/", "fields": {}, "footprint": "x"},
                         "C1": {"value": "100n", "lib_id": "Device:C", "sheet": "/power/", "fields": {}, "footprint": "x",
                                "dnp": True}},
          "nets": {"+3V3": [{"ref": "U1", "pin": "1", "pin_name": "VDD", "pin_type": "power_in"},
                            {"ref": "C1", "pin": "1", "pin_name": "", "pin_type": "passive"}],
                   "GND": [{"ref": "U1", "pin": "2", "pin_name": "GND", "pin_type": "power_in"},
                           {"ref": "C1", "pin": "2", "pin_name": "", "pin_type": "passive"}],
                   "X": [{"ref": "U1", "pin": "3", "pin_name": "PA0", "pin_type": "bidirectional"}]}}
    r = design_review.review(None, checks=["decoupling"], include_erc=False, netlist=nl)
    dec = [f for f in r["findings"] if f["type"] == "missing_decoupling"]
    assert dec, r["findings"]                                   # the DNP cap does not count
    assert 'sch_apply(sheet="power", ops=' in dec[0]["fix"], dec[0]["fix"]


def test_resource_path_read_keeps_active_project():
    from copper_kicad import resources
    other = demo("stickhub").dir
    P.activate(fixture())
    active = P._state().get("active")
    r = resources.dispatch("resources/read", {"uri": f"copper://project/summary?path={other}"})
    assert '"StickHub"' in r["contents"][0]["text"]
    assert P._state().get("active") == active


def test_checkpoint_restore_refuses_when_open_in_kicad():
    from copper_kicad.mcp import registry
    from copper_kicad.tools import project_tools  # noqa: F401  (registers checkpoint)
    pr = fixture()
    cid = P.checkpoint(pr, "lock-test")
    lock = pr.dir / f"~{pr.sch.name}.lck"
    lock.write_text("{}", encoding="utf-8")
    try:
        out = registry.call_tool("checkpoint", {"action": "restore", "id": cid, "project": str(pr.dir)})
        assert out["isError"] and "FILE_OPEN_IN_KICAD" in out["content"][0]["text"], out
        assert "while_open" not in out["content"][0]["text"]       # checkpoint has no such argument
    finally:
        lock.unlink()


# ── regression tests: verify_core findings from the 5-tester pass ─────────────
def _nl(comps: dict, nets: dict) -> dict:
    """Tiny synthetic netlist: comps {ref: (lib_id, value)}, nets {name: [(ref, pin, pin_name, pin_type)]}."""
    c = {r: {"lib_id": lib, "value": v, "description": "", "footprint": "x:y", "fields": {}, "sheet": "/"}
         for r, (lib, v) in comps.items()}
    n = {name: [{"ref": r, "pin": p, "pin_name": pn, "pin_type": pt} for r, p, pn, pt in nodes] for name, nodes in nets.items()}
    return {"components": c, "nets": n}


def _types(r) -> dict:
    t: dict = {}
    for f in r["findings"]:
        t.setdefault(f["type"], []).append(f)
    return t


def test_cpl_never_applies_the_kicad_table_to_jlc_native_footprints():
    rules = fab.load_rotation_rules(profile="jlcpcb")
    from functools import partial
    place = partial(fab.placement_row, profile="jlcpcb")
    # EasyEDA-converted footprints share JLC's zero orientation: the board rotation goes to the CPL unchanged
    for lib, name in (("copper-lcsc", "TSSOP-20_L6.5-W4.4-P0.65-LS6.4-BL"), ("copper-lcsc", "SOT-23-6_L2.9-W1.6-P0.95-LS2.8-BR"),
                      ("copper-lcsc", "SOIC-8_L5.3-W5.3-P1.27-LS8.0-BL"), ("easyeda2kicad", "QFN-56_L7.0-W7.0-P0.40-BL-EP3.2")):
        pl = place({"lib": lib, "name": name, "rotation": 90, "layer": "top", "pos": [0, 0]}, rules)
        assert pl["rotation"] == 90 and pl["correction_source"] is None and pl["native"], (lib, name, pl)
    by_src = place({"lib": "MyLib", "name": "SOT-23", "rotation": 0, "layer": "top", "pos": [0, 0],
                                "fields": {"Source": "JLCEDA/EasyEDA Official Library"}}, rules)
    assert by_src["rotation"] == 0 and by_src["native"], by_src
    stock = place({"lib": "Package_TO_SOT_SMD", "name": "SOT-23-6", "rotation": 0, "layer": "top", "pos": [0, 0]}, rules)
    assert stock["rotation"] == 270 and stock["correction_source"] == "rule ^SOT-23" and not stock["native"], stock
    ov = place({"lib": "copper-lcsc", "name": "SOT-23-6_X", "rotation": 0, "layer": "top", "pos": [0, 0],
                            "fields": {"JLC Rotation Offset": "180"}}, rules)
    assert ov["rotation"] == 180 and ov["correction_source"] == "field", ov      # an explicit field still wins


def test_protection_ic_ground_is_its_own_reference_not_a_rail():
    """DW01A: VCC on DW_VCC, GND pin on BATN (cell negative), C3 across them — the datasheet circuit."""
    comps = {"U2": ("copper-lcsc:DW01A", "DW01A"), "C3": ("Device:C", "100nF"), "U1": ("MCU:X", "MCU"),
             "C1": ("Device:C", "100nF"), "Q1": ("copper-lcsc:FS8205A", "FS8205A")}
    nets = {"/DW_VCC": [("U2", "5", "VCC", "power_in"), ("C3", "1", "", "passive")],
            "/BATN": [("U2", "6", "GND", "power_in"), ("C3", "2", "", "passive"), ("Q1", "2", "S1", "passive")],
            "+3V3": [("U1", "1", "VDD", "power_in"), ("C1", "1", "", "passive")],
            "GND": [("U1", "2", "VSS", "power_in"), ("C1", "2", "", "passive"), ("Q1", "7", "S2", "passive")],
            "/DW_OD": [("U2", "1", "OD", "output"), ("Q1", "4", "G1", "input")],
            "/X": [("U1", "3", "PA0", "bidirectional")]}
    nl = _nl(comps, nets)
    assert not common.is_power_net("/BATN", nl["nets"]["/BATN"]) and common.is_ground_net("/BATN", nl["nets"]["/BATN"])
    r = design_review.review(None, checks=["decoupling"], include_erc=False, netlist=nl)
    assert not [f for f in r["findings"] if f["type"] == "missing_decoupling"], r["findings"]
    assert "/BATN" in r["stats"]["ground_nets"] and "/BATN" not in r["stats"]["power_rails"], r["stats"]
    # without C3 the advice must be a cap to the IC's own ground (BATN) — never BATN↔GND across the protection FETs
    nets2 = {k: [n for n in v if n[0] != "C3"] for k, v in nets.items()}
    r2 = design_review.review(None, checks=["decoupling"], include_erc=False, netlist=_nl(comps, nets2))
    dec = [f for f in r2["findings"] if f["type"] == "missing_decoupling"]
    assert len(dec) == 1 and "U2" in dec[0]["message"] and '"net":"BATN"' in dec[0]["fix"], dec
    assert not any(f["net"] == "/BATN" for f in dec), dec


def _dec(comps, nets):
    r = design_review.review(None, checks=["decoupling"], include_erc=False, netlist=_nl(comps, nets))
    return [f for f in r["findings"] if f["type"] == "missing_decoupling"], r


def test_heaters_loads_and_passives_are_not_ic_power_pins():
    """An MQ-3 on +5V: KiCad's Sensor_Gas symbols type the heater pins 'power input' and use reference U, which
    made the review demand a 100 nF on the heater. Same for an imported part with H pins on a switched node, a
    fan and an LED. The MCU next to them on the same rail must still be reported."""
    sym = _lib_symbol("Sensor_Gas", "MQ-6")
    if sym is not None:                 # the synthetic netlist below uses the stock symbol's real pin types
        found = {}

        def walk(node):
            for e in node:
                if isinstance(e, list) and e and e[0] == "pin":
                    found[str(sexpr.find(e, "name")[1])] = str(e[1])
                elif isinstance(e, list) and e and e[0] == "symbol":
                    walk(e)
        walk(sym)
        assert found.get("VH+") == "power_in" and found.get("VH-") == "power_in" and found.get("A1") == "passive", found
    comps = {"U1": ("Sensor_Gas:MQ-6", "MQ-3"), "U2": ("copper-lcsc:MQ-3", "MQ-3"), "M1": ("Motor:Fan_Tacho", "Fan"),
             "U4": ("copper-lcsc:IR204", "IR emitter"), "D1": ("Device:LED", "red"), "R1": ("Device:R", "10k"),
             "R2": ("Device:R", "330"), "U3": ("MCU:X", "MCU"), "Q1": ("Device:Q_NMOS", "2N7002")}
    nets = {"+5V": [("U1", "2", "VH+", "power_in"), ("U1", "1", "B1", "passive"), ("U1", "3", "B2", "passive"),
                    ("U2", "2", "H", "power_in"), ("U2", "1", "A", "passive"), ("M1", "1", "+", "passive"),
                    ("M1", "4", "VCC", "passive"), ("U4", "1", "A", "power_in"), ("D1", "2", "A", "passive"),
                    ("U3", "1", "VDD", "power_in")],
            "GND": [("U1", "5", "VH-", "power_in"), ("R1", "2", "", "passive"), ("M1", "2", "-", "passive"),
                    ("U3", "2", "VSS", "power_in"), ("Q1", "2", "S", "passive"), ("R2", "2", "", "passive")],
            "/HEAT_SW": [("U2", "5", "H", "power_in"), ("Q1", "3", "D", "passive")],
            "/AOUT": [("U1", "4", "A2", "passive"), ("U1", "6", "A1", "passive"), ("U2", "3", "B", "passive"),
                      ("R1", "1", "", "passive"), ("U3", "3", "PA0", "bidirectional")],
            "/TACH": [("M1", "3", "Tacho", "passive"), ("U3", "4", "PA1", "bidirectional")],
            "/LED_K": [("D1", "1", "K", "passive"), ("U4", "2", "K", "passive"), ("U4", "3", "NC", "no_connect"),
                       ("R2", "1", "", "passive")],
            "/G": [("Q1", "1", "G", "input"), ("U3", "5", "PA2", "bidirectional")]}
    dec, r = _dec(comps, nets)
    assert len(dec) == 1 and dec[0]["items"] == ["U3.1"] and dec[0]["net"] == "+5V", dec     # the MCU, and only the MCU
    assert "/HEAT_SW" not in r["stats"]["power_rails"] and "+5V" in r["stats"]["power_rails"], r["stats"]
    # net role without component context too: a heater terminal typed power_in makes no rail
    n = _nl(comps, nets)["nets"]
    assert not common.is_power_net("/HEAT_SW", n["/HEAT_SW"]) and common.is_power_net("+5V", n["+5V"])
    for name in ("H", "H1", "VH+", "VH-", "HEATER", "HTR2", "COIL+", "M+", "MOTOR-", "FIL1"):
        assert common.is_load_pin({"pin_name": name}), name
    for name in ("VM", "VS", "HV", "VDD", "HSYNC", "MOSI", "HOLD", "M0", "A", "VHV"):
        assert not common.is_load_pin({"pin_name": name}), name


def test_load_classification_keeps_real_decoupling_findings():
    """Things that look a little like a load but have electronics inside stay reported."""
    gnd_c = ("C9", "2", "", "passive")
    cases = {
        # an IC imported with every pin passive: canonical VCC + its own GND pin
        "passive-typed IC": [("U1", "1", "VCC", "passive"), ("U1", "2", "GND", "passive"), ("U1", "3", "OUT", "passive")],
        # pins left 'unspecified': unknown, so judged by the supply name
        "unspecified IC": [("U1", "1", "VDD", "unspecified"), ("U1", "2", "X", "unspecified"), ("U1", "3", "Y", "unspecified")],
        # an MQ-3 breakout (comparator on board): VCC / GND / AO / DO
        "sensor module": [("U1", "1", "VCC", "power_in"), ("U1", "2", "GND", "power_in"), ("U1", "3", "AO", "passive"),
                          ("U1", "4", "DO", "passive")],
        # a digital pot has H/L/W terminals; its VDD still needs a cap and H is not listed as a power pin
        "digital pot": [("U1", "1", "VDD", "power_in"), ("U1", "2", "VSS", "power_in"), ("U1", "3", "H", "passive"),
                        ("U1", "4", "W", "passive"), ("U1", "5", "SCK", "input")],
        # a sensor IC with an integrated heater supply pin: the logic supply is reported, the heater pin is not
        "heater on an IC": [("U1", "1", "VDD", "power_in"), ("U1", "2", "GND", "power_in"), ("U1", "3", "VH", "power_in"),
                            ("U1", "4", "SDA", "bidirectional")],
        # an op-amp drawn with V+ / V- and typed pins
        "op-amp": [("U1", "8", "V+", "power_in"), ("U1", "4", "V-", "power_in"), ("U1", "1", "~", "output"),
                   ("U1", "2", "-", "input"), ("U1", "3", "+", "input")],
    }
    for label, pins in cases.items():
        nets = {"+5V": [p for p in pins if p[2] in ("VCC", "VDD", "V+", "VH")],
                "GND": [p for p in pins if p[2] in ("GND", "VSS", "V-")] + [gnd_c]}
        for i, p in enumerate(p for p in pins if p not in nets["+5V"] and p not in nets["GND"]):
            nets[f"/S{i}"] = [p, ("R9", "1", "", "passive")]
        comps = {"U1": ("Sensor_Gas:MQ-3_module" if label == "sensor module" else "copper-lcsc:PART", "PART"),
                 "C9": ("Device:C", "100nF"), "R9": ("Device:R", "10k")}
        dec, r = _dec(comps, nets)
        assert len(dec) == 1 and dec[0]["items"] == ["U1." + nets["+5V"][0][1]], (label, dec, r["findings"])
    # and the cap that fixes it is still recognised
    nets = {"+5V": [("U1", "1", "VDD", "power_in"), ("C9", "1", "", "passive")],
            "GND": [("U1", "2", "GND", "power_in"), gnd_c], "/S": [("U1", "3", "SDA", "bidirectional"), ("R9", "1", "", "passive")]}
    dec, _ = _dec({"U1": ("copper-lcsc:PART", "PART"), "C9": ("Device:C", "100nF"), "R9": ("Device:R", "10k")}, nets)
    assert not dec, dec


def test_led_series_resistor_on_a_rail_named_node():
    """pic_programmer: R22 VCC_PIC→PWR_LED, D13 PWR_LED→GND. 'PWR_LED' is not a rail."""
    comps = {"R22": ("Device:R", "1k"), "D13": ("Device:LED", "green"), "U1": ("MCU:X", "PIC")}
    nets = {"/pic_sockets/VCC_PIC": [("R22", "1", "", "passive"), ("U1", "1", "VDD", "power_in")],
            "/pic_sockets/PWR_LED": [("R22", "2", "", "passive"), ("D13", "2", "A", "passive")],
            "GND": [("D13", "1", "K", "passive"), ("U1", "2", "VSS", "power_in")]}
    r = design_review.review(None, checks=["led_resistors"], include_erc=False, netlist=_nl(comps, nets))
    assert not r["findings"], r["findings"]
    for n in ("PWR_LED", "/a/PWR_LED", "VBUS_LED", "PWR_STAT"):
        assert not common.is_power_net(n), n
    for n in ("PWR", "PWR_5V", "+5V"):
        assert common.is_power_net(n), n
    # a real LED straight across a rail is still caught
    nets2 = {"+3V3": [("D13", "2", "A", "passive"), ("U1", "1", "VDD", "power_in")],
             "GND": [("D13", "1", "K", "passive"), ("U1", "2", "VSS", "power_in")]}
    r2 = design_review.review(None, checks=["led_resistors"], include_erc=False, netlist=_nl(comps, nets2))
    assert [f for f in r2["findings"] if f["type"] == "led_no_resistor"], r2["findings"]


def test_usb_esd_only_when_a_phy_is_behind_the_connector():
    comps = {"J2": ("Connector:USB_A", "USB-A"), "U1": ("MCU:STM32", "STM32F072")}
    dcp = {"/USB_DCP": [("J2", "2", "D-", "bidirectional"), ("J2", "3", "D+", "bidirectional")],
           "+5V": [("J2", "1", "VBUS", "power_in")], "GND": [("J2", "4", "GND", "passive")]}
    r = design_review.review(None, checks=["usb_esd"], include_erc=False, netlist=_nl(comps, dcp))
    assert not r["findings"], r["findings"]                       # charge-only port: no PHY to protect
    data = {"/D+": [("J2", "3", "D+", "bidirectional"), ("U1", "33", "PA12", "bidirectional")],
            "/D-": [("J2", "2", "D-", "bidirectional"), ("U1", "32", "PA11", "bidirectional")]}
    r2 = design_review.review(None, checks=["usb_esd"], include_erc=False, netlist=_nl(comps, data))
    f = [x for x in r2["findings"] if x["type"] == "usb_no_esd"]
    assert f and "U1" in f[0]["message"] and "MCU's" not in f[0]["message"], f


def test_jlc_class_is_shared_and_preferred_is_not_basic():
    assert design_review._jlc_class("C25905", {}) == "basic"          # 5.1k 0402, Basic
    assert design_review._jlc_class("C1570", {}) == "preferred"
    assert design_review._jlc_class("C99999999", {}) == "extended"    # not in JLC's Basic/Preferred lists → Extended
    assert design_review._jlc_class("C25905", {"JLC Class": "Extended"}) == "extended"   # the part's own field wins
    comps = {"R1": ("Device:R", "5.1k"), "C1": ("Device:C", "x"), "U1": ("MCU:X", "x")}
    nl = _nl(comps, {"A": [("R1", "1", "", "passive"), ("C1", "1", "", "passive"), ("U1", "1", "PA0", "bidirectional")]})
    for ref, lc in (("R1", "C25905"), ("C1", "C1570"), ("U1", "C99999999")):
        nl["components"][ref]["fields"]["LCSC"] = lc
    st = design_review.review(None, checks=["sourcing"], include_erc=False, netlist=nl)["stats"]
    assert (st["basic"], st["preferred"], st["extended"], st["unknown_class"]) == (1, 1, 1, 0), st
    assert st["estimated_extended_fee_usd"] == design_review.JLC_EXTENDED_FEE_USD


def _power_board(dirpath: Path, track_w: float = 0.2, vias_in_pad: int = 0, rtop: str = "22k"):
    """A minimal boost-converter board: netlist + .kicad_pcb + .kicad_pro written straight from text."""
    from types import SimpleNamespace
    dirpath.mkdir(parents=True, exist_ok=True)
    pro = {"net_settings": {"classes": [{"name": "Default", "track_width": 0.2}, {"name": "Power", "track_width": 0.8}],
                            "netclass_patterns": [{"netclass": "Power", "pattern": "GND"}, {"netclass": "Power", "pattern": "+*"}]}}
    (dirpath / "b.kicad_pro").write_text(json.dumps(pro), encoding="utf-8")
    pads = "".join(f'\t\t(pad "{i}" smd rect\n\t\t\t(at {-2.5 + (i - 1) % 4 * 1.27:.2f} {-2.6 if i <= 4 else 2.6})\n'
                   f'\t\t\t(size 0.6 1.5)\n\t\t\t(layers "F.Cu")\n\t\t\t(net "GND")\n\t\t)\n' for i in range(1, 9))
    pads += '\t\t(pad "9" smd rect\n\t\t\t(at 0 0)\n\t\t\t(size 2.4 3.3)\n\t\t\t(layers "F.Cu")\n\t\t\t(net "GND")\n\t\t)\n'
    vias = "".join(f'\t(via\n\t\t(at {130 + 0.5 * k} 120)\n\t\t(size 0.6)\n\t\t(drill 0.3)\n\t\t(net "GND")\n\t)\n'
                   for k in range(vias_in_pad))
    pcb = ('(kicad_pcb\n\t(version 20250114)\n'
           '\t(footprint "copper-lcsc:ESOP-8"\n\t\t(layer "F.Cu")\n\t\t(at 130 120)\n'
           '\t\t(property "Reference" "U1"\n\t\t)\n' + pads + '\t)\n'
           f'\t(segment\n\t\t(start 100 100)\n\t\t(end 105 100)\n\t\t(width {track_w})\n\t\t(layer "F.Cu")\n\t\t(net "/SW")\n\t)\n'
           + vias +
           '\t(zone\n\t\t(net "GND")\n\t\t(layer "B.Cu")\n\t)\n)\n')
    (dirpath / "b.kicad_pcb").write_text(pcb, encoding="utf-8")
    pr = SimpleNamespace(pcb=dirpath / "b.kicad_pcb", pro=dirpath / "b.kicad_pro", dir=dirpath)
    comps = {"U1": ("copper-lcsc:TP4056", "TP4056"), "U3": ("copper-lcsc:MT3608", "MT3608"), "L1": ("Device:L", "22uH"),
             "D1": ("Device:D_Schottky", "SS34"), "R8": ("Device:R", rtop), "R9": ("Device:R", "3k"),
             "J3": ("Connector:Conn_01x02", "battery")}
    nets = {"/SW": [("U3", "1", "SW", "power_out"), ("L1", "2", "", "passive"), ("D1", "2", "A", "passive")],
            "/VBAT": [("L1", "1", "", "passive"), ("U3", "5", "IN", "power_in"), ("J3", "1", "", "passive"),
                      ("U1", "5", "BAT", "power_out")],
            "/BATN": [("J3", "2", "", "passive")],
            "+5V": [("D1", "1", "K", "passive"), ("R8", "1", "", "passive")],
            "/FB": [("U3", "3", "FB", "input"), ("R8", "2", "", "passive"), ("R9", "1", "", "passive")],
            "GND": [("U3", "2", "GND", "power_in"), ("R9", "2", "", "passive"), ("U1", "3", "GND", "power_in")]}
    for i in range(1, 9):
        nets.setdefault(f"/U1_{i}", [("U1", str(i + 10), f"P{i}", "passive")])
    return pr, _nl(comps, nets)


def test_power_review_divider_power_path_and_thermal_pad():
    pr, nl = _power_board(_TMP / "pwr-bad", track_w=0.2, vias_in_pad=0)
    r = design_review.review(pr, checks=["regulator_setpoint", "power_path", "thermal_pad"], include_erc=False, netlist=nl)
    t = _types(r)
    assert not t.get("regulator_setpoint"), t                       # 0.6·(1+22k/3k) = 5.0 V on +5V
    sp = r["stats"]["regulator_setpoints"][0]
    assert sp["ref"] == "U3" and abs(sp["vout"] - 5.0) < 0.01 and sp["rail"] == "+5V", sp
    narrow = t.get("power_path_narrow", [])
    assert narrow and narrow[0]["net"] == "/SW" and "pcb_setup(power_nets=[\"/SW\"])" in narrow[0]["fix"], narrow
    cls = t.get("power_path_netclass", [])
    assert cls and "/BATN" in cls[0]["items"] and "Default" in cls[0]["message"], cls      # pattern misses '/BATN'
    tp = t.get("thermal_pad_no_vias", [])
    assert tp and tp[0]["severity"] == "warning" and "U1" in tp[0]["message"] and '"op":"add_via"' in tp[0]["fix"], tp
    # fixed board: wide switch node, vias in the pad, matching divider → quiet; a wrong divider → flagged
    pr2, nl2 = _power_board(_TMP / "pwr-good", track_w=0.8, vias_in_pad=3, rtop="10k")
    t2 = _types(design_review.review(pr2, checks=["regulator_setpoint", "power_path", "thermal_pad"], include_erc=False, netlist=nl2))
    assert not t2.get("power_path_narrow") and not t2.get("thermal_pad_no_vias"), t2
    rs = t2.get("regulator_setpoint", [])
    assert rs and "2.60 V" in rs[0]["message"] and "+5V" in rs[0]["message"] and "22k" in rs[0]["fix"], rs


def test_board_counts_read_kicad10_multiline_items():
    txt = ('(kicad_pcb\n\t(footprint "R"\n\t)\n\t(segment\n\t\t(start 0 0)\n\t)\n\t(segment (start 1 1))\n\t(arc\n\t)\n'
           '\t(via\n\t\t(at 1 1)\n\t)\n\t(zone\n\t\t(net "GND")\n\t)\n'
           '\t(zone\n\t\t(net "A")\n\t\t(attr\n\t\t\t(teardrop\n\t\t\t\t(type padvia)\n\t\t\t)\n\t\t)\n\t)\n)\n')
    c = project_info.board_counts(txt)
    assert c == {"footprints_on_board": 1, "tracks": 3, "vias": 1, "zones": 1, "teardrop_zones": 1}, c
    s = project_info.summary(demo("pic_programmer"))
    assert s["tracks"] > 300 and s["vias"] > 0 and s["zones"] >= 1, {k: s.get(k) for k in ("tracks", "vias", "zones")}
    assert s["nets"] >= s["connected_nets"] > 0 and "unconnected_pin_nets" in s


def test_per_call_project_does_not_switch_the_active_project():
    saved = P._state()
    try:
        a, b = fixture(), demo("stickhub")
        P.activate(a)
        P.resolve(str(b.dir))                                 # e.g. sch_read(project=B) / fab_export dry run
        assert P._state()["active"] == str(a.pro), P._state()
        assert P.resolve(None).name == a.name
        from copper_kicad.mcp import registry
        from copper_kicad.tools import project_tools  # noqa: F401
        out = registry.call_tool("project_summary", {"project": str(b.dir)})
        assert "active project is unchanged" in out["structuredContent"].get("note", ""), out["structuredContent"].get("note")
        assert P._state()["active"] == str(a.pro)
        out = registry.call_tool("project_open", {"path": str(b.dir)})   # the explicit switch
        sc = out["structuredContent"]
        assert P._state()["active"] == str(b.pro) and "by_kind" not in sc and sc.get("more"), sc
        # nothing active yet → the first per-call project becomes active (so the next call without project works)
        P._save_state({})
        P.resolve(str(a.dir))
        assert P._state().get("active") == str(a.pro)
    finally:
        P._save_state(saved)


def test_checkpoint_long_path_dedupe_and_restore():
    deep = _TMP / ("deep-" + "x" * 60) / ("nested-" + "y" * 60) / "proj"
    os.makedirs(P._long(deep), exist_ok=True)
    src = fixture()
    for f in src.dir.iterdir():
        if f.is_file() and (f.suffix in P.DESIGN_EXT or f.name in P.DESIGN_EXT):
            shutil.copy2(f, P._long(deep / f.name.replace("fixture", "proj")))
    pr = P.Project(Path(P._long(deep)) if len(str(deep)) >= 240 else deep, "proj")
    a = P.checkpoint(pr, "sch_apply-with-a-long-label-xxxxxxxxxxxxxxxxx")
    assert P.checkpoint(pr, "sch_apply") == a                       # identical files → no second copy
    assert P.checkpoint(pr, "manual", dedupe=False) != a            # an explicit checkpoint always writes
    assert len(str(pr.copper_dir / "snapshots" / a / pr.pcb.name)) > 260 or os.name != "nt"
    sch = P._long(pr.sch)
    with open(sch, "a", encoding="utf-8") as fh:
        fh.write("\n")
    restored = P.restore_checkpoint(pr, a)
    assert "proj.kicad_sch" in restored and open(sch, encoding="utf-8").read() == \
        open(P._long(src.sch), encoding="utf-8").read()
    ids = [c["id"] for c in P.list_checkpoints(pr)]
    assert any(i.endswith("before-restore") for i in ids) and not any("before-restore-2026" in i for i in ids), ids
    try:
        P.restore_checkpoint(pr, "../..")
        raise AssertionError("restored from outside the snapshot folder")
    except ToolError as e:
        assert e.code == "NOT_FOUND"


def test_lock_refusal_names_the_owner():
    pr = fixture()
    lock = pr.dir / f"~{pr.sch.name}.lck"
    lock.write_text(json.dumps({"hostname": "WS-7", "username": "alice"}), encoding="utf-8")
    try:
        P.guard_writable(pr.sch)
        raise AssertionError("wrote over an open schematic")
    except ToolError as e:
        assert e.code == "FILE_OPEN_IN_KICAD" and "alice@WS-7" in e.message and str(lock) in e.fix, (e.message, e.fix)
    finally:
        lock.unlink()


def test_erc_attributes_subsheet_items_to_their_sheet_with_exact_fixes():
    src = _share() / "demos" / "pic_programmer"
    dst = _TMP / "pic-erc"
    if not dst.exists():
        shutil.copytree(src, dst)
    pr = P.Project(dst, "pic_programmer")
    sub = dst / "pic_sockets.kicad_sch"
    wid = str(uuid.uuid4())
    txt = sub.read_text(encoding="utf-8")
    i = txt.rindex("\t(sheet_instances") if "\t(sheet_instances" in txt else txt.rindex(")")
    stray = (f'\t(wire\n\t\t(pts\n\t\t\t(xy 30.48 30.48) (xy 35.56 30.48)\n\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n'
             f'\t\t\t(type solid)\n\t\t)\n\t\t(uuid "{wid}")\n\t)\n')
    sub.write_text(txt[:i] + stray + txt[i:], encoding="utf-8", newline="\n")
    e = verify.run_erc(pr)
    hit = [f for f in e["violations"] if any(it.get("uuid") == wid for it in f.get("items", []))]
    assert hit, [(f["type"], f.get("sheet")) for f in e["violations"]][:10]
    f = hit[0]
    assert f["sheet"] == "/pic_sockets/" and f.get("sheet_file") == "pic_sockets.kicad_sch", (f["sheet"], f.get("sheet_file"))
    # the fix is a real op on the right sheet (never "edit the file by hand"), and running it removes the finding
    assert 'sch_apply(sheet="pic_sockets", ops=[{"op":"delete","dangling":true}])' in f["fix"], f["fix"]
    assert '"wire_at":[30.48, 30.48]' in f["fix"] and "direct text edit" not in f["fix"], f["fix"]
    for g in e["violations"]:
        assert "crop=[x-15" not in g["fix"], g["fix"]
    from copper_kicad.mcp import registry
    from copper_kicad.tools import sch_tools  # noqa: F401
    r = registry.call_tool("sch_apply", {"project": str(dst), "sheet": "pic_sockets", "preview": False,
                                         "ops": [{"op": "delete", "wire_at": [30.48, 30.48]}]})
    assert not r["isError"], r["content"][0]["text"][:400]
    e2 = verify.run_erc(pr)
    assert not [g for g in e2["violations"] if any(it.get("uuid") == wid for it in g.get("items", []))]


def test_unconnected_power_symbol_pin_is_deleted_not_no_connected():
    class Ctx:
        def net_of(self, _):
            return None
    for desc, ref, what in (("Symbol #FLG03 Pin 1 [pwr, Power output, Line]", "#FLG03", "PWR_FLAG"),
                            ("Symbol #PWR012 Hidden pin  [GND, Power input, Line]", "#PWR012", "power symbol")):
        fix = verify._erc_fix("pin_not_connected", [common.parse_item(desc)], "Pin not connected", Ctx())
        assert f'{{"op":"delete","ref":"{ref}"}}' in fix and what in fix, fix
        assert "no_connect" not in fix and '"op":"connect"' not in fix, fix
    real = verify._erc_fix("pin_not_connected", [common.parse_item("Symbol U1 Pin 3 [PA0, Bidirectional, Line]")], "", Ctx())
    assert '"op":"no_connect","pin":"U1.3"' in real, real
    w = dict(common.parse_item("Horizontal Wire, length 5.08 mm"), at=[50.8, 60.96])
    fix = verify._erc_fix("wire_dangling", [w], "", Ctx())
    assert '{"op":"delete","dangling":true}' in fix and '{"op":"delete","wire_at":[50.8, 60.96]}' in fix, fix


def test_drc_fix_names_exact_copper_never_whole_nets():
    def it(desc, uid):
        return dict(common.parse_item(desc), uuid=uid)
    tr = it("Track [/SDA] on F.Cu, length 3.2 mm", "aaaa-1")
    via = it("Via [GND] on F.Cu - B.Cu", "bbbb-2")
    pad = it("Pad 2 [GND] of C3 on F.Cu", "cccc-3")
    pad2 = it("Pad 1 [/SDA] of R4 on F.Cu", "dddd-4")
    zone = it("Zone [GND] on F.Cu", "eeee-5")
    assert tr["kind"] == "track" and via["kind"] == "via" and pad["kind"] == "pad" and zone["kind"] == "zone"
    for t in ("clearance", "shorting_items", "tracks_crossing", "hole_clearance"):
        fx = verify._drc_fix(t, [tr, via], "")
        assert 'pcb_edit(ops=[{"op":"delete","uuids":["aaaa-1", "bbbb-2"]}]) then pcb_route()' in fx, fx
        assert '"nets"' not in fx and "nets:" not in fx, fx
    fx = verify._drc_fix("clearance", [pad, tr], "")
    assert '"uuids":["aaaa-1"]' in fx and "cccc-3" not in fx and '"ref":"C3"' in fx and '"nets"' not in fx, fx   # the pad stays
    fx = verify._drc_fix("clearance", [pad, pad2], "")                # nothing routed to delete → move a part
    assert "pcb_edit" not in fx and 'pcb_place(moves=[{"ref":"C3"' in fx and '"nets"' not in fx, fx
    fx = verify._drc_fix("clearance", [zone, pad2], "")
    assert "pcb_pour(fill_only=true)" in fx and "pcb_edit" not in fx, fx
    assert '"uuids":["aaaa-1"]' in verify._drc_fix("track_dangling", [tr], "")
    # the real thing: KiCad's DRC items keep their uuid and kind, and no fix anywhere rips up a net
    dst = _TMP / "pic-drc"
    shutil.copytree(_share() / "demos" / "pic_programmer", dst)
    pr = P.Project(dst, "pic_programmer")
    wid = str(uuid.uuid4())
    txt = pr.pcb.read_text(encoding="utf-8")
    i = txt.index("\t(segment\n")
    bad = (f'\t(segment\n\t\t(start 110.49 124.155)\n\t\t(end 145.1 97.47)\n\t\t(width 0.8)\n\t\t(layer "F.Cu")\n'
           f'\t\t(net "VCC")\n\t\t(uuid "{wid}")\n\t)\n')
    pr.pcb.write_text(txt[:i] + bad + txt[i:], encoding="utf-8", newline="\n")     # a VCC track straight across the board
    d = verify.run_drc(pr, parity=False)
    mine = [f for f in d["violations"] if any(i.get("uuid") == wid for i in f["items"])]
    clash = [f for f in mine if f["type"] in ("clearance", "shorting_items", "tracks_crossing")]
    assert clash and all(wid in f["fix"] for f in clash), [(f["type"], f["fix"]) for f in mine][:3]
    its = [i for f in d["violations"] for i in f["items"]]
    assert its and all(i.get("kind") for i in its) and sum(1 for i in its if i.get("uuid")) >= 0.9 * len(its), its[:4]
    for f in d["violations"]:
        assert '"op":"delete","nets"' not in f["fix"], f["fix"]
        if f["type"] in ("clearance", "shorting_items", "tracks_crossing") and any(i.get("kind") == "track" for i in f["items"]):
            assert '"uuids":[' in f["fix"], f


def test_checkpoint_diff_reports_hierarchical_labels():
    src = _share() / "demos" / "pic_programmer"
    dst = _TMP / "pic-hier"
    shutil.copytree(src, dst)
    pr = P.Project(dst, "pic_programmer")
    cid = P.checkpoint(pr, "base", dedupe=False)
    sub = dst / "pic_sockets.kicad_sch"
    raw = sub.read_bytes()
    txt = sub.read_text(encoding="utf-8")
    a = txt.index('\t(hierarchical_label "VPP-MCLR"')
    b = txt.index("\n\t)\n", a) + len("\n\t)\n")
    block = txt[a:b]
    sub.write_text(txt[:a] + block.replace('"VPP-MCLR"', '"NEW_SIG"', 1) + txt[b:], encoding="utf-8", newline="\n")
    d = project_info.diff_checkpoint(pr, cid)
    e = next(f for f in d["changed_files"] if f["file"] == "pic_sockets.kicad_sch")
    assert e["hier_labels_removed"] == ["VPP-MCLR"] and e["hier_labels_added"] == ["NEW_SIG"], e
    assert "VPP-MCLR" in e["note"] and "sheet pin" in e["note"], e
    sub.write_text(txt[:a] + txt[b:], encoding="utf-8", newline="\n")                 # delete it outright
    e = next(f for f in project_info.diff_checkpoint(pr, cid)["changed_files"] if f["file"] == "pic_sockets.kicad_sch")
    assert e["hier_labels_removed"] == ["VPP-MCLR"] and e["hier_labels_added"] == [], e
    assert e["items_delta"].get("hierarchical_label") == -1, e["items_delta"]
    sub.write_bytes(raw)
    assert project_info.diff_checkpoint(pr, cid)["identical"]


def test_review_fix_ops_use_label_names_not_kicad_paths():
    """KiCad calls a local label's net '/SDA' (or '/power/SDA'); sch_apply ops want the label name 'SDA'."""
    import re as _re
    comps = {"U1": ("MCU:X", "STM32"), "U2": ("Sensor:Y", "BME280"), "J1": ("Connector:USB_C_Receptacle", "USB-C"),
             "D1": ("Device:LED", "LED"), "U3": ("Regulator_Linear:AMS1117-3.3", "AMS1117-3.3"), "R1": ("Device:R", "10k")}
    nets = {"/power/VDD_MCU": [("U1", "1", "VDD", "power_in"), ("U2", "1", "VDD", "power_in"), ("U3", "2", "VO", "power_out"),
                               ("D1", "1", "K", "passive")],
            "/power/VIN": [("U3", "3", "VI", "power_in"), ("J1", "A4", "VBUS", "passive")],
            "GND": [("U1", "2", "VSS", "power_in"), ("U2", "2", "GND", "power_in"), ("J1", "A1", "GND", "passive"),
                    ("U3", "1", "GND", "power_in")],
            "/io/SDA": [("U1", "3", "SDA", "bidirectional"), ("U2", "3", "SDA", "bidirectional")],
            "/io/SCL": [("U1", "4", "SCL", "bidirectional"), ("U2", "4", "SCL", "bidirectional")],
            "/io/CC1": [("J1", "A5", "CC1", "passive")], "/io/CC2": [("J1", "B5", "CC2", "passive")],
            "/io/LED_A": [("D1", "2", "A", "passive"), ("U1", "5", "PA5", "bidirectional")],
            "/io/LONELY": [("U1", "6", "PA6", "bidirectional")],
            "/io/EN_X": [("R1", "1", "", "passive"), ("U1", "7", "PA7", "bidirectional")]}
    r = design_review.review(None, include_erc=False, netlist=_nl(comps, nets))
    seen = 0
    for f in r["findings"]:
        fx = f.get("fix") or ""
        for call in _re.findall(r"sch_apply\(.*?\]\)", fx):
            seen += 1
            assert not _re.search(r'"net":"[^"]*/', call), (f["type"], call)
        assert not _re.search(r"(?:on|and) net /", fx), (f["type"], fx)
    types = {f["type"] for f in r["findings"]}
    assert seen >= 3 and {"missing_decoupling", "i2c_no_pullup", "single_pin_net"} <= types, (seen, sorted(types))


def test_power_class_width_is_respected_and_templates_match_local_nets():
    # jlc-4layer: Power is 0.3 mm on purpose; a power-path net IN that class at 0.3 mm is not "narrow"
    pr, nl = _power_board(_TMP / "pwr-4l", track_w=0.3, vias_in_pad=3)
    pro = json.loads(pr.pro.read_text(encoding="utf-8"))
    pro["net_settings"]["classes"][1]["track_width"] = 0.3
    pro["net_settings"]["netclass_patterns"] += [{"netclass": "Power", "pattern": "/SW"}, {"netclass": "Power", "pattern": "*VBAT*"},
                                                 {"netclass": "Power", "pattern": "/BATN"}]
    pr.pro.write_text(json.dumps(pro), encoding="utf-8")
    t = _types(design_review.review(pr, checks=["power_path"], include_erc=False, netlist=nl))
    assert not t.get("power_path_narrow") and not t.get("power_path_netclass"), t
    pr2, nl2 = _power_board(_TMP / "pwr-4l-thin", track_w=0.15, vias_in_pad=3)       # thinner than its own class → flagged
    pr2.pro.write_text(json.dumps(pro), encoding="utf-8")
    n = _types(design_review.review(pr2, checks=["power_path"], include_erc=False, netlist=nl2)).get("power_path_narrow", [])
    assert n and "0.3 mm" in n[0]["message"], n
    # shipped templates: patterns match KiCad's full names of local labels ('/VBAT'); 4-layer Power is 0.3 mm
    widths = {}
    for name in ("jlc-2layer", "jlc-4layer"):
        d = json.loads((project_info.TEMPLATES / f"{name}.kicad_pro").read_text(encoding="utf-8").replace("__NAME__", "x"))
        ns = d["net_settings"]
        b = {"classes": {c["name"]: c for c in ns["classes"]}, "assigned": {},
             "patterns": [(q["pattern"], q["netclass"]) for q in ns["netclass_patterns"]]}
        for net in ("GND", "/GND", "AGND", "+3V3", "/+5V", "/VBAT", "/power/VBUS", "VCC", "/VDD_CORE", "/VIN", "/VSYS", "V+", "/V+",
                    "/PWR_IN", "/sub/GNDA"):
            assert design_review._netclass(b, net)[0] == "Power", (name, net)
        for net in ("/SDA", "/USB_DP", "/LED", "Net-(R1-Pad1)"):
            assert design_review._netclass(b, net)[0] == "Default", (name, net)
        widths[name] = b["classes"]["Power"]["track_width"]
    assert widths == {"jlc-2layer": 0.5, "jlc-4layer": 0.3}, widths
    from copper_kicad.mcp import registry
    from copper_kicad.tools import project_tools  # noqa: F401
    assert "Power 0.3 mm" in registry.TOOLS["project_create"].description
    four = project_info.create_project("four-pw", str(_TMP / "four-pw"), "jlc-4layer", [30, 20])
    assert four["kicad_check"].get("drc_violations", 0) == 0, four["kicad_check"]


def test_default_pcb_render_has_no_fab_layer():
    from copper_kicad import render as R
    assert "F.Fab" not in R.DEFAULT_PCB_LAYERS and "B.Fab" not in R.DEFAULT_PCB_LAYERS
    pr = demo("pic_programmer")
    im = R.pcb_layers(pr.pcb, _TMP / "renders", None, width=500, name="default-layers")
    assert "Fab" not in im.caption and "F.SilkS" in im.caption and "F.Cu" in im.caption, im.caption
    explicit = R.pcb_layers(pr.pcb, _TMP / "renders", ["F.Cu", "F.Fab", "Edge.Cuts"], width=500, name="with-fab")
    assert "F.Fab" in explicit.caption, explicit.caption                 # still available on request


def test_status_reports_first_use_download_as_available():
    from copper_kicad.install import freerouting
    base = {"java": None, "java_version": None, "jar": None, "version": "2.2.3", "tools_dir": "/x/tools"}
    orig = freerouting.find
    try:
        freerouting.find = lambda: dict(base, ok=False, available=True,
                                        first_use_downloads=["Java 25 runtime (~50 MB)", "Freerouting 2.2.3 (~20 MB)"])
        t = project_info.tools_status()
        assert t["autorouting_available"] is True and t["autorouting"] == "downloads on first use", t
        assert t["autorouting_ready"] is False and "Freerouting 2.2.3" in t["note"] and "pcb_route() works" in t["note"], t
        freerouting.find = lambda: dict(base, ok=True, available=True, first_use_downloads=[], jar="/x/fr.jar", java="/x/java")
        t = project_info.tools_status()
        assert t["autorouting"] == "ready" and t["autorouting_ready"] and "note" not in t, t
        freerouting.find = lambda: dict(base, ok=False, available=False, first_use_downloads=[], problem="backend broken",
                                        fix="reinstall")
        t = project_info.tools_status()
        assert t["autorouting"] == "unavailable" and not t["autorouting_available"] and t["fix"] == "reinstall", t
    finally:
        freerouting.find = orig
    live = project_info.tools_status()                                    # the real backend, whatever is installed here
    assert live["autorouting"] in ("ready", "downloads on first use") and live["autorouting_available"] is True, live


def test_pin_conflict_with_pwr_flag_is_called_benign():
    class Ctx:
        def net_of(self, _):
            return "GND"
    items = [common.parse_item("Symbol U2 Pin 5 [SDO, Bidirectional, Line]"),
             common.parse_item("Symbol #FLG01 Pin 1 [pwr, Power output, Line]")]
    fix = verify._erc_fix("pin_to_pin", items, "Pins of type Bidirectional and Power output are connected", Ctx())
    assert "benign" in fix and "real short" not in fix and "U2.5" in fix, fix
    real = [common.parse_item("Symbol U2 Pin 5 [OUT, Output, Line]"), common.parse_item("Symbol U3 Pin 1 [OUT, Output, Line]")]
    assert "real short" in verify._erc_fix("pin_to_pin", real, "", Ctx())


def test_compact_never_merges_clearances_between_different_nets():
    from copper_kicad.tools import verify_tools as VT

    def clr(n1, n2, ref):
        items = [{"kind": "pad", "ref": ref, "pin_ref": f"{ref}.1", "net": n1}, {"kind": "track", "net": n2}]
        return common.finding("error", "clearance", "Clearance violation (netclass 'Default' clearance 0.2 mm)",
                              verify._drc_fix("clearance", items, ""), items, [1, 2], section="violations")
    fs = [clr("/BOOTSEL", "GND", "SW1"), clr("/GPIO10", "/XIN", "U1"), clr("/XIN", "GND", "Y1"), clr("/QSPI_SS", "+3V3", "U2")]
    entries, _ = VT._compact(fs, 25)
    assert len(entries) == 4 and all("count" not in e for e in entries), entries
    same = [clr("/A", "/B", f"R{i}") for i in range(5)]
    e = VT._compact(same, 25)[0]
    assert len(e) == 1 and e[0]["count"] == 5 and e[0]["nets"] == ["/A", "/B"], e
    assert "same fix for each" not in e[0]["fix"] and len(e[0]["fixes"]) == 4, e[0]   # per-ref fixes differ (nudge Rn)


def test_render_pcb_crop_layer_order_and_inner_layers():
    from copper_kicad import render as R
    order = sorted(["F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "Edge.Cuts", "F.SilkS"], key=lambda l: R._layer_rank(l, False))
    assert order == ["B.Cu", "In2.Cu", "In1.Cu", "F.Cu", "F.SilkS", "Edge.Cuts"], order
    pr = demo("pic_programmer")
    bb = project_info.board_bbox_mm(pr.pcb)
    out = _TMP / "renders"
    full = R.pcb_layers(pr.pcb, out, ["F.Cu", "B.Cu", "Edge.Cuts"], width=800, name="full")
    crop = [bb[0] + 10, bb[1] + 10, bb[0] + 40, bb[1] + 30]
    cut = R.pcb_layers(pr.pcb, out, ["F.Cu", "B.Cu", "Edge.Cuts"], width=800, crop_mm=crop, name="crop")
    from PIL import Image as PImage
    fw, fh = PImage.open(full.path).size
    cw, ch = PImage.open(cut.path).size
    assert cw < fw * 0.6 and abs(cw / ch - 34 / 24) < 0.2, (fw, fh, cw, ch)       # the 30×20 mm window (+2 mm pad)
    assert "B.Cu,F.Cu" in full.caption, full.caption                               # front copper painted last
    four = project_info.create_project("four", str(_TMP / "four"), "jlc-4layer", [30, 20])
    im = R.pcb_layers(Path(four["project"]["pcb"]), out, ["B.Cu", "F.Cu", "Edge.Cuts"], width=400, name="four")
    assert "In1.Cu" in im.caption and "In2.Cu" in im.caption, im.caption


def test_project_create_dir_is_the_folder_when_it_ends_in_the_name():
    d = _TMP / "proj dir" / "stm32 sensor"
    r = project_info.create_project("stm32 sensor", str(d), "jlc-2layer")
    assert Path(r["project"]["dir"]) == d.resolve() and r["project"]["name"] == "stm32-sensor", r["project"]
    assert "stm32-sensor" in r.get("note", ""), r


def test_record_external_drc_updates_last_results():
    pr = fixture()
    verify.record_external(pr, "drc", {"errors": 3, "warnings": 1, "unconnected": 2, "parity": 0}, source="pcb_place")
    lr = verify.last_results(pr)["drc"]
    assert lr["errors"] == 3 and lr["unconnected"] == 2 and lr["source"] == "pcb_place" and lr["stale"] is False, lr


def test_checkpoint_list_is_compact():
    from copper_kicad.mcp import registry
    from copper_kicad.tools import project_tools  # noqa: F401
    pr = fixture()
    for i in range(3):
        P.checkpoint(pr, f"c{i}", dedupe=False)
    sc = registry.call_tool("checkpoint", {"action": "list", "project": str(pr.dir)})["structuredContent"]
    assert sc["files"] and all(isinstance(c, str) for c in sc["checkpoints"]), sc


def test_bad_argument_errors_carry_fixes_and_compact_json():
    from copper_kicad.mcp import registry
    from copper_kicad.tools import verify_tools  # noqa: F401
    out = registry.call_tool("render", {"target": "nope"})
    d = out["structuredContent"]
    assert d["error"] == "BAD_ARGUMENT" and "schematic" in d["fix"], d
    assert "\n" not in out["content"][0]["text"], out["content"][0]["text"]
    d = registry.call_tool("verify", {"limit": [1]})["structuredContent"]
    assert d["error"] == "BAD_ARGUMENT" and "integer" in d["fix"], d
    d = registry.call_tool("verify", ["erc"])["structuredContent"]
    assert d["error"] == "BAD_ARGUMENT" and "object" in d["message"], d


def test_multiple_net_names_signature_is_stable():
    a = {"type": "multiple_net_names", "description": "Both 'SDA' and 'SDAA' are attached to the same items; 'SDA' will be used",
         "items": [{"description": "Symbol U1 Pin 5 [SDA, Bidirectional, Line]"}, {"description": "Label 'SDAA'"}]}
    b = {"type": "multiple_net_names", "description": "Both 'SDAA' and 'SDA' are attached to the same items; 'SDAA' will be used",
         "items": [{"description": "Symbol R2 Pin 1 [~, Passive, Line]"}, {"description": "Label 'SDA'"}]}
    assert verify._sig(a) == verify._sig(b) == "multiple_net_names|SDA|SDAA", (verify._sig(a), verify._sig(b))


def test_render_unknown_sheet_is_an_error_not_the_root():
    from copper_kicad.mcp import registry
    from copper_kicad.tools import verify_tools  # noqa: F401
    pr = demo("pic_programmer")
    out = registry.call_tool("render", {"target": "schematic", "sheet": "no_such_sheet", "project": str(pr.dir), "width": 400})
    assert out["isError"] and out["structuredContent"]["error"] == "NOT_FOUND", out["content"][0]["text"][:300]
    out = registry.call_tool("render", {"target": "schematic", "sheet": "pic_sockets", "project": str(pr.dir), "width": 400})
    imgs = [c for c in out["content"] if c["type"] == "image"]
    assert not out["isError"] and len(imgs) == 1 and "pic_sockets" in out["structuredContent"]["images"][0], out["structuredContent"]


def _fp_graphic(kind: str, body: str, layer: str = "Edge.Cuts") -> str:
    return (f'\t\t({kind}\n{body}\t\t\t(stroke\n\t\t\t\t(width 0.1)\n\t\t\t\t(type solid)\n\t\t\t)\n'
            f'\t\t\t(layer "{layer}")\n\t\t\t(uuid "{uuid.uuid4()}")\n\t\t)\n')


def test_footprint_owned_edge_cuts_are_flagged_with_a_fix():
    # a converted connector footprint (USB-C plug) carried a 'board edge here' line on Edge.Cuts: KiCad counts it
    # as board outline and only says 'invalid outline' — verify must name the footprint and the way out
    dst = _TMP / "pic-fp-edge"
    shutil.copytree(_share() / "demos" / "pic_programmer", dst)
    pr = P.Project(dst, "pic_programmer")
    clean = pr.pcb.read_text(encoding="utf-8")
    assert verify.footprint_edge_cuts(clean) == [], "the demo's own Edge.Cuts outline is board-level, not footprint-owned"
    base = verify.run_drc(pr, parity=False)
    assert "footprint_edge_cuts" not in base["by_type"] and "invalid_outline" not in base["by_type"], base["by_type"]
    i = clean.index("\t(footprint ")                      # C1 at (110.49, 78.867) rotated 180°
    j = clean.index("\t\t(fp_line", i)
    line = _fp_graphic("fp_line", "\t\t\t(start -4 3)\n\t\t\t(end 4 3)\n")
    pr.pcb.write_text(clean[:j] + line + clean[j:], encoding="utf-8", newline="\n")
    hits = verify.footprint_edge_cuts(pr.pcb.read_text(encoding="utf-8"))
    assert len(hits) == 1 and hits[0]["ref"] == "C1" and hits[0]["count"] == 1 and not hits[0]["closed"], hits
    assert hits[0]["bbox"] == [106.49, 75.867, 114.49, 75.867], hits[0]["bbox"]       # footprint rotation applied
    d = verify.run_drc(pr, parity=False)
    mine = [f for f in d["violations"] if f["type"] == "footprint_edge_cuts"]
    assert len(mine) == 1 and mine[0]["severity"] == "error" and d["errors"] > base["errors"], (mine, d["errors"])
    f = mine[0]
    assert "C1" in f["message"] and f["items"][0]["ref"] == "C1" and f["at"] == [110.49, 78.867], f
    assert 'footprint C1' in f["fix"] and '(layer "Dwgs.User")' in f["fix"] and "pcb_setup(" in f["fix"], f["fix"]
    assert "board edge belongs: y = 75.867 mm" in f["fix"], f["fix"]
    bad = [x for x in d["violations"] if x["type"] == "invalid_outline"]
    assert bad and all("footprint C1 owns Edge.Cuts" in x["fix"] for x in bad), [x["fix"] for x in d["violations"]][:4]
    # the fix the finding describes clears it, touching nothing else in the file
    r = verify.move_footprint_edge_cuts(pr)
    assert r == {"moved": {"C1": 1}, "to_layer": "Dwgs.User"}, r
    assert pr.pcb.read_text(encoding="utf-8") == clean[:j] + line.replace("Edge.Cuts", "Dwgs.User") + clean[j:]
    d2 = verify.run_drc(pr, parity=False)
    assert "footprint_edge_cuts" not in d2["by_type"] and "invalid_outline" not in d2["by_type"], d2["by_type"]
    assert d2["errors"] == base["errors"], (d2["errors"], base["errors"])
    assert verify.move_footprint_edge_cuts(pr) == {"moved": {}, "to_layer": "Dwgs.User"}
    # a closed shape may be a real cut-out the part needs: a warning, not an error; refs= limits the edit
    rect = _fp_graphic("fp_rect", "\t\t\t(start -1 -1)\n\t\t\t(end 1 1)\n")
    pr.pcb.write_text(clean[:j] + rect + clean[j:], encoding="utf-8", newline="\n")
    h = verify.footprint_edge_cuts(pr.pcb.read_text(encoding="utf-8"))
    assert len(h) == 1 and h[0]["closed"] and h[0]["kinds"] == ["fp_rect"], h
    fs = verify._fp_edge_findings(pr)
    assert len(fs) == 1 and fs[0]["severity"] == "warning" and "cut-out" in fs[0]["message"], fs
    assert verify.move_footprint_edge_cuts(pr, refs=["R99"])["moved"] == {}
    assert verify.move_footprint_edge_cuts(pr, refs=["C1"])["moved"] == {"C1": 1}
    # four lines that close are a cut-out too; three are not
    sq = [((-1, -1), (1, -1)), ((1, -1), (1, 1)), ((1, 1), (-1, 1)), ((-1, 1), (-1, -1))]

    def lines(n):
        return "".join(_fp_graphic("fp_line", f"\t\t\t(start {a[0]} {a[1]})\n\t\t\t(end {b[0]} {b[1]})\n") for a, b in sq[:n])
    assert verify.footprint_edge_cuts(clean[:j] + lines(4) + clean[j:])[0]["closed"]
    assert not verify.footprint_edge_cuts(clean[:j] + lines(3) + clean[j:])[0]["closed"]


def _numbered_demo(numbers: list[str]):
    """pic_programmer with part numbers on its first len(numbers) assembled parts (the demo has none), and
    every other part left unsourced: (project, {number: [refs]}, restore())."""
    pr = demo("pic_programmer")
    orig = fab.assembly_parts
    given: dict = {}

    def patched(pr_, fps=None, part_field="LCSC"):
        a = orig(pr_, fps, part_field)
        given.clear()
        for part, num in zip(a["parts"], numbers):
            part["part"] = num
            part["fields"] = dict(part["fields"], LCSC=num)
            given.setdefault(num, []).append(part["ref"])
        return a
    fab.assembly_parts = patched

    def restore():
        fab.assembly_parts = orig
    return pr, given, restore


def test_fab_gate_warns_about_stock_and_sums_up_fees():
    from copper_kicad.mcp import registry
    from copper_kicad.parts import common as PC
    from copper_kicad.parts import search as PS
    import copper_kicad.tools  # noqa: F401  (registers fab_export)
    # two refs share the Basic part C25905, one Preferred (C1570), one Extended (C99999999), one JLC does not list
    pr, given, restore = _numbered_demo(["C25905", "C25905", "C1570", "C99999999", "C88888888"])
    live = {"C25905": 0, "C1570": 3, "C99999999": 100000, "C88888888": None}
    asked: list = []
    orig_part, orig_http = PS.jlc_part, PC.http

    def jlc_part(code, ttl=None):
        asked.append(code)
        if live[code] is None:
            return None
        return {"lcsc": code, "stock": live[code], "class": "Extended", "mfr": "X"}

    def no_http(url, *a, **k):
        if PC.offline():
            raise PC.NetError("offline mode (COPPER_OFFLINE=1)")
        raise AssertionError(f"network access attempted: {url}")
    old_env = os.environ.pop("COPPER_OFFLINE", None)
    out = _TMP / "stockfab"
    try:
        PS.jlc_part, PC.http = jlc_part, no_http
        r = fab.fab_export(pr, waive=["parts"], run_checks=False, out_dir=str(out / "a"))
        assert sorted(asked) == sorted(live), asked                               # one lookup per unique part number
        (w,) = r["warnings"]
        by = {x["part"]: x for x in w["parts"]}
        assert w["code"] == "stock" and w["severity"] == "warning" and set(by) == {"C25905", "C1570", "C88888888"}, w
        assert by["C25905"]["status"] == "out_of_stock" and by["C25905"]["need"] == 10 and by["C25905"]["refs"] == given["C25905"]
        assert by["C1570"]["status"] == "insufficient" and (by["C1570"]["stock"], by["C1570"]["need"]) == (3, 5)
        assert by["C88888888"]["status"] == "not_listed"
        assert all(x["fix"] == f"parts_alternatives(target='{x['part']}')" for x in w["parts"]), w["parts"]
        assert set(w["refs"]) == {x for n in by for x in given[n]} and "parts_alternatives(target='C25905')" in w["fix"]
        assert "waive=['stock']" in w["fix"] and "OUT OF STOCK" in w["message"] and given["C25905"][0] in w["message"]
        st = r["stock"]
        assert (st["lines"], st["checked"], st["unchecked"], st["short"], st["boards"]) == (4, 4, 0, 3, 5), st
        assert "live" in st["source"] and not any("NOT checked" in n for n in r["summary"]["order_notes"])
        assert Path(r["zip"]).exists() and [x["code"] for x in r["waived"]] == ["parts"]      # a warning: the pack is written
        fees = r["summary"]["assembly_fees"]
        assert (fees["basic"], fees["preferred"], fees["extended"], fees["unknown"]) == (1, 1, 2, 0), fees
        assert fees["estimated_usd"] == 6.0 == r["summary"]["estimated_extended_fee_usd"] and fees["fee_per_extended_usd"] == 3.0
        assert fees["extended_refs"] == {n: given[n] for n in ("C99999999", "C88888888")}, fees
        note = next(n for n in r["summary"]["order_notes"] if n.startswith("4 unique parts"))
        assert "1 Basic + 1 Preferred (no per-part fee), 2 Extended × $3 = $6" in note and "parts_alternatives" in note, note
        # the tool's one-line summary carries both
        t = registry.call_tool("fab_export", {"project": str(pr.dir), "waive": ["erc", "drc", "parity", "unrouted", "parts"],
                                              "out_dir": str(out / "t")})
        txt = t["content"][0]["text"]
        assert not t["isError"] and "STOCK WARNING: 3 BOM part(s) short for 5 board(s)" in txt and "2 Extended part(s) = $6 fee" in txt, txt[:600]
        # the tool takes the order size (and can be told not to look)
        t = registry.call_tool("fab_export", {"project": str(pr.dir), "waive": ["all"], "out_dir": str(out / "t2"), "boards": 3})
        assert "STOCK WARNING" not in t["content"][0]["text"] and "WAIVED" in t["content"][0]["text"]     # 'all' acknowledges stock
        t = registry.call_tool("fab_export", {"project": str(pr.dir), "waive": ["erc", "drc", "parity", "unrouted", "parts"],
                                              "out_dir": str(out / "t3"), "boards": 3})
        assert "STOCK WARNING: 2 BOM part(s) short for 3 board(s)" in t["content"][0]["text"], t["content"][0]["text"][:600]
        n = len(asked)
        t = registry.call_tool("fab_export", {"project": str(pr.dir), "waive": ["erc", "drc", "parity", "unrouted", "parts"],
                                              "out_dir": str(out / "t4"), "check_stock": False})
        assert len(asked) == n and "Stock NOT checked for 4 of 4 BOM part(s): check_stock=false" in t["content"][0]["text"]
        # fewer boards → fewer shortfalls
        r = fab.fab_export(pr, waive=["parts"], run_checks=False, out_dir=str(out / "b"), boards=3)
        assert {x["part"] for x in r["warnings"][0]["parts"]} == {"C25905", "C88888888"}
        # acknowledged: no warning left, the finding is listed under waived
        r = fab.fab_export(pr, waive=["parts", "stock"], run_checks=False, out_dir=str(out / "c"))
        assert "warnings" not in r and [x["code"] for x in r["waived"]] == ["parts", "stock"] and r["stock"]["short"] == 3
        assert set(r["waived"][1]) == {"code", "message", "refs"} and "C25905" in r["waived"][1]["message"]   # kept short
        # everything in stock: no warning
        live.update({"C25905": 5000, "C1570": 5000, "C88888888": 5000})
        r = fab.fab_export(pr, waive=["parts"], run_checks=False, out_dir=str(out / "d"))
        assert "warnings" not in r and (r["stock"]["checked"], r["stock"]["short"], r["stock"]["unchecked"]) == (4, 0, 0), r["stock"]
        # the supplier goes away after the first answer: the rest is reported as NOT checked, without retrying each part
        asked.clear()

        def flaky(code, ttl=None):
            asked.append(code)
            if len(asked) > 1:
                raise PC.NetError("https://example.invalid: timed out")
            return {"lcsc": code, "stock": 0, "class": "Basic", "mfr": "X"}
        PS.jlc_part = flaky
        r = fab.fab_export(pr, waive=["parts"], run_checks=False, out_dir=str(out / "e"))
        assert len(asked) == 2 and r["stock"]["checked"] == 1 and r["stock"]["unchecked"] == 3, (asked, r["stock"])
        assert len(r["warnings"][0]["parts"]) == 1 and "unreachable" in r["stock"]["not_checked"], r["stock"]
        assert any("stock was NOT checked for 3 of 4" in n for n in r["summary"]["order_notes"][:2]), r["summary"]["order_notes"][:2]
        # offline: the real lookup path; the pack is still made and it says stock was not checked
        PS.jlc_part = orig_part
        os.environ["COPPER_OFFLINE"] = "1"
        r = fab.fab_export(pr, waive=["parts"], run_checks=False, out_dir=str(out / "f"))
        assert "warnings" not in r and Path(r["zip"]).exists(), r.get("warnings")
        assert r["stock"]["checked"] == 0 and r["stock"]["unchecked"] == 4 and "offline" in r["stock"]["not_checked"], r["stock"]
        assert r["summary"]["order_notes"][0].startswith("stock was NOT checked for 4 of 4 BOM part(s)"), r["summary"]["order_notes"][0]
        assert r["summary"]["assembly_fees"]["extended"] == 2                     # fees come from the bundled table: still there
        t = registry.call_tool("fab_export", {"project": str(pr.dir), "waive": ["all"], "out_dir": str(out / "g")})
        assert "Stock NOT checked for 4 of 4 BOM part(s)" in t["content"][0]["text"], t["content"][0]["text"][:600]
        # asked not to look
        r = fab.fab_export(pr, waive=["parts"], run_checks=False, out_dir=str(out / "h"), check_stock=False)
        assert r["stock"]["not_checked"] == "check_stock=false" and "NOT checked" in r["summary"]["order_notes"][0]
        # a bare-board order has no BOM: nothing to check, nothing said
        r = fab.fab_export(pr, run_checks=False, out_dir=str(out / "i"), assembly=False)
        assert "stock" not in r and "warnings" not in r
    finally:
        PS.jlc_part, PC.http = orig_part, orig_http
        os.environ.pop("COPPER_OFFLINE", None)
        if old_env is not None:
            os.environ["COPPER_OFFLINE"] = old_env
        restore()
    assert "stock" in fab.WAIVABLE and "stock" not in fab.FP.BLOCK_CODES          # waivable, but never a blocker


def test_stock_check_uses_the_provider_that_owns_the_number():
    from copper_kicad.parts import sourcing
    pr = demo("pic_programmer")
    # default providers (lcsc): a number it does not own is reported as not checked, never guessed
    r = sourcing.stock_check(pr, [{"part": "STK-001", "refs": ["R1", "R2"]}], part_field="Stock No")
    assert r["checked"] == 0 and r["unchecked"] == ["STK-001"] and "no active parts provider" in r["not_checked"], r
    assert sourcing.stock_check(pr, []) == {"boards": 5, "lines": 0, "checked": 0, "unchecked": [], "short": [],
                                            "unknown": [], "classes": {}}
    assert fab.stock_finding(r) is None


def _run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"PASS {n}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)
    print(f"{len(tests) - failed}/{len(tests)} passed  (scratch: {_TMP})")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
