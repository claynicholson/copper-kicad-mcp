"""Parts engine tests. Run with `python -m pytest tests/test_parts.py` or
`python tests/test_parts.py` (no pytest needed). Network tests skip
themselves when JLC/EasyEDA are unreachable; KiCad tests skip without
kicad-cli."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

# Copper's state (active project, recent list) must never land in the real ~/.copper: project.STATE_DIR is
# fixed when copper_kicad is first imported, so this has to come before that import.
__import__("os").environ.setdefault("COPPER_HOME", __import__("tempfile").mkdtemp(prefix="copper-test-home-"))
from copper_kicad.parts import basic, common as C, easyeda as E, importer, search  # noqa: E402


class Skip(Exception):
    pass


def skip(msg):
    try:
        import pytest  # type: ignore
        pytest.skip(msg)
    except ImportError:
        raise Skip(msg)


def _kicad():
    try:
        from copper_kicad.kicad import env
        env.kicad()
        return env
    except Exception as e:  # noqa: BLE001
        skip(f"KiCad not available: {e}")


def _tmp_home():
    d = tempfile.mkdtemp(prefix="copper-test-home-")
    os.environ.setdefault("COPPER_HOME", d)
    return d


# ── units / packages ─────────────────────────────────────────────────────
def test_parse_si():
    assert C.parse_si("10k", "ohm") == 10000
    assert C.parse_si("4k7", "ohm") == 4700
    assert C.parse_si("0R", "ohm") == 0
    assert abs(C.parse_si("100nF", "F") - 1e-7) < 1e-15
    assert abs(C.parse_si("4u7", "F") - 4.7e-6) < 1e-12
    assert C.parse_si("32.768kHz", "Hz") == 32768
    assert C.parse_si("100nF", "ohm") is None
    assert C.fmt_si(4700, "ohm") == "4.7k"
    assert C.fmt_si(1e-7, "F") == "100nF"


def test_norm_pkg():
    assert C.norm_pkg("SOT-23-3L") == "SOT-23"
    assert C.norm_pkg("SMA(DO-214AC)") == "SMA"
    assert C.norm_pkg("R_0603_1608Metric") == "0603"
    assert C.norm_pkg("0603x4") != "0603"          # resistor array is not a 0603 chip
    assert C.norm_pkg("SO-8") == C.norm_pkg("SOIC-8") == "SOIC-8"
    assert C.norm_pkg("SOIC-8-208mil") == "SOIC-8W"
    assert C.lcsc_norm("25804") == "C25804"
    assert C.lcsc_norm("https://www.lcsc.com/product-detail/x_C7519.html") == "C7519"


# ── spec parsing / offline pick ──────────────────────────────────────────
def test_parse_spec():
    s = basic.parse_spec("100nF 16V 0402 X7R")
    assert s["kind"] == "capacitor" and abs(s["value"] - 1e-7) < 1e-15 and s["voltage"] == 16
    assert s["package"] == "0402" and s["dielectric"] == "X7R"
    s = basic.parse_spec("10k 0603 1%")
    assert s["kind"] == "resistor" and s["value"] == 10000 and s["tolerance"] == 0.01
    s = basic.parse_spec("LED red 0603")
    assert s["kind"] == "led" and s["color"] == "red"
    s = basic.parse_spec("USB-C 16pin")
    assert s["kind"] == "usb_connector" and s["pins"] == 16
    assert basic.parse_spec("AMS1117-3.3")["mpn"] == "AMS1117-3.3"
    assert basic.parse_spec("LDO 3.3V")["vout"] == 3.3


def test_pick_offline():
    assert basic.meta()["count"] > 1000, "bundled basic_parts table missing"
    cases = {"10k 0603": "C25804", "LED red 0603": "C2286", "AMS1117-3.3": "C6186", "1N4148W": "C81598",
             "green LED 0805": "C2297", "12MHz crystal": "C9002"}
    for spec, want in cases.items():
        p = basic.pick(spec)
        assert p and p["lcsc"] == want, (spec, p and p["lcsc"])
    p = basic.pick("100nF 0402")
    assert p["basic"] and p["package"] == "0402" and p["value"] == "100nF"
    p = basic.pick("4.7uF 0805")
    assert p["basic"] and p["value"] == "4.7uF" and C.norm_pkg(p["package"]) == "0805"
    p = basic.pick("USB-C 16pin")
    assert p and p["kind"] == "usb_connector"
    assert basic.pick("blorf") is None


def test_search_offline_ranking():
    old = os.environ.get("COPPER_OFFLINE")
    os.environ["COPPER_OFFLINE"] = "1"
    try:
        r = search.search("10k 0603", limit=5)
        assert r["results"][0]["lcsc"] == "C25804" and r["results"][0]["class"] == "Basic"
        assert all(x["package"] == "0603" for x in r["results"])
        assert r["sources"]["seed"].startswith("ok")  # jlc may still answer from the disk cache
        r = search.search("SS34", limit=5)
        classes = [x["class"] for x in r["results"]]
        assert classes == sorted(classes, key=lambda c: C.CLASS_RANK[c])
    finally:
        if old is None:
            os.environ.pop("COPPER_OFFLINE", None)
        else:
            os.environ["COPPER_OFFLINE"] = old


# ── converter on a synthetic EasyEDA component (offline) ─────────────────
def synthetic_component() -> dict:
    sym_shapes = [
        "R~-20~-20~2~2~40~40~#880000~1~0~none~gge1~0~",
        "P~show~1~1~-30~-10~180~gge2~0^^-30~-10^^M -30 -10 h 10~#880000^^1~-17~-6~0~IN~start~~~#0000FF^^1~-24~-11~0~1~end~~~#0000FF^^0~-27~-10^^0~M -20 -7 L -17 -10 L -20 -13",
        "P~show~2~2~30~-10~0~gge3~0^^30~-10^^M 30 -10 h -10~#880000^^1~17~-6~0~OUT~end~~~#0000FF^^1~24~-11~0~2~start~~~#0000FF^^0~27~-10^^0~M 20 -13 L 17 -10 L 20 -7",
        "P~show~4~3~0~30~270~gge4~0^^0~30^^M 0 30 v -10~#880000^^1~3~17~270~GND~start~~~#0000FF^^1~-1~26~270~3~end~~~#0000FF^^1~0~23^^0~M 3 20 L 0 17 L -3 20",
        "A~M -10 0 A 10 10 0 0 1 10 0~~#880000~1~0~none~gge5~0",
        "C~0~5~3~#880000~1~0~none~gge6~0",
    ]
    fp_shapes = [
        "PAD~RECT~3990~3000~4~6~1~~1~0~3988 2997 3992 2997 3992 3003 3988 3003~0~gge10~0~~Y~0~0~0.2~3990,3000",
        "PAD~POLYGON~4010~3000~4~6~1~~2~0~4008 2997 4012 2997 4012 3003 4010 3004 4008 3003~0~gge11~0~~Y~0~0~0.2~4010,3000",
        "PAD~OVAL~4000~3020~6~10~11~~3~1.5~4000 3017 4000 3023~0~gge12~5~4000 3018.5 4000 3021.5~Y~0~0~0.2~4000,3020",
        "HOLE~4000~2985~2~gge13~0",
        "TRACK~1~3~~3980 2990 4020 2990~gge14~0",
        "ARC~1~3~~M 3985 3010 A 5 5 0 0 1 3995 3010~~gge15~0",
    ]
    return {"title": "TEST-PART", "dataStr": {"head": {"x": 0, "y": 0, "c_para": {"pre": "U?", "Manufacturer Part": "TEST-PART"}},
                                             "shape": sym_shapes},
            "packageDetail": {"title": "TEST-PKG", "uuid": "deadbeef",
                              "dataStr": {"head": {"x": 4000, "y": 3000}, "shape": fp_shapes}}}


def test_convert_synthetic():
    comp = synthetic_component()
    sym, info = E.build_symbol(comp, "TEST-PART_C1", {"Value": "TEST-PART", "LCSC": "C1"}, footprint="x:TEST-PKG")
    assert info["pins"] == {"1": "IN", "2": "OUT", "3": "GND"}
    pins = {}
    for unit in [e for e in sym if isinstance(e, list) and e and e[0] == "symbol"]:
        for p in [e for e in unit if isinstance(e, list) and e and e[0] == "pin"]:
            at = next(e for e in p if isinstance(e, list) and e[0] == "at")
            num = next(e for e in p if isinstance(e, list) and e[0] == "number")[1]
            pins[num] = (str(p[1]), str(p[2]), at[1], at[2], at[3])
    # left pin points right into the body (0°), right pin 180°, bottom pin up (90°); Y flipped; 10 units = 2.54 mm
    assert pins["1"] == ("input", "line", -7.62, 2.54, 0), pins["1"]
    assert pins["2"] == ("output", "line", 7.62, 2.54, 180), pins["2"]
    assert pins["3"][0] == "power_in" and pins["3"][1] == "inverted" and pins["3"][2:] == (0, -7.62, 90), pins["3"]
    text, meta = E.build_footprint(comp, "TEST-PKG", "d", "t")
    assert meta["pads"] == ["1", "2", "3"] and meta["through_hole"]
    assert '(pad "1" smd rect' in text and "(at -2.54 0)" in text and "(size 1.016 1.524)" in text
    assert '(pad "2" smd custom' in text and "gr_poly" in text           # non-rectangular polygon → custom pad
    assert '(pad "3" thru_hole oval' in text and "(drill oval 0.762 1.27)" in text  # vertical slot
    assert "np_thru_hole" in text and "F.CrtYd" in text and "fp_arc" in text
    cx0, cy0, cx1, cy1 = meta["courtyard_mm"]
    assert cx0 < -3 and cx1 > 3 and cy1 > 6.3


def test_convert_synthetic_kicad_loads():
    env = _kicad()
    from copper_kicad import sexpr
    from copper_kicad.sexpr import Sym
    comp = synthetic_component()
    tmp = Path(tempfile.mkdtemp(prefix="copper-test-conv-"))
    try:
        sym, _ = E.build_symbol(comp, "TEST-PART_C1", {"Value": "TEST-PART"}, footprint="t:TEST-PKG")
        lib = [Sym("kicad_symbol_lib"), [Sym("version"), E.SYM_VERSION], [Sym("generator"), "copper_kicad"], sym]
        (tmp / "t.kicad_sym").write_text(sexpr.dumps(lib) + "\n", encoding="utf-8")
        (tmp / "t.pretty").mkdir()
        text, _ = E.build_footprint(comp, "TEST-PKG", "d", "t")
        (tmp / "t.pretty" / "TEST-PKG.kicad_mod").write_text(text, encoding="utf-8")
        (tmp / "s").mkdir(); (tmp / "f").mkdir()
        env.run(["sym", "export", "svg", "-o", str(tmp / "s"), str(tmp / "t.kicad_sym")])
        env.run(["fp", "export", "svg", "-o", str(tmp / "f"), str(tmp / "t.pretty")])
        assert list((tmp / "s").glob("*.svg")), "kicad-cli could not load the generated symbol"
        assert list((tmp / "f").glob("*.svg")), "kicad-cli could not load the generated footprint"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_obj_to_wrl_and_transform():
    obj = "newmtl a\nKd 0.1 0.2 0.3\nd 0\nv 0 0 0\nv 2 0 0\nv 2 1 0\nv 0 1 1\nusemtl a\nf 1 2 3\nf 1 3 4\n"
    wrl, bbox = E.obj_to_wrl(obj)
    assert wrl.startswith("#VRML V2.0") and "coordIndex" in wrl and bbox == (0, 0, 0, 2, 1, 1)
    verts = E.parse_obj(obj)[0]
    off, rot = E.model_transform({"origin_mm": (5.0, -1.0), "z_mm": 0.0, "rotation": [0, 0, 0]}, verts)
    assert off == [4.0, -1.5, 0.0] and rot == [0, 0, 0]


# ── KiCad stock mapping ─────────────────────────────────────────────────
def test_stock_mapping():
    _kicad()
    rec = basic.by_lcsc("C25804")
    sc = importer.stock_candidate(rec)
    assert sc["lib_id"] == "Device:R" and sc["footprint"] == "Resistor_SMD:R_0603_1608Metric"
    ok, notes, _ = importer.verify_stock(sc["lib_id"], sc["footprint"], rec, None)
    assert ok, notes
    assert "Regulator_Linear:AMS1117-3.3" in importer.find_stock_symbols("AMS1117-3.3")
    assert "Power_Protection:USBLC6-2SC6" in importer.find_stock_symbols("USBLC6-2SC6")
    rec = basic.by_lcsc("C6186")
    ok, notes, sym = importer.verify_stock("Regulator_Linear:AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", rec, None)
    assert ok, notes
    # a wrong footprint must be rejected (pins without pads)
    ok, notes, _ = importer.verify_stock("Regulator_Linear:AMS1117-3.3", "Resistor_SMD:R_0603_1608Metric", rec, None)
    assert not ok


def _project():
    tmp = Path(tempfile.mkdtemp(prefix="copper-test-proj-"))
    (tmp / "t.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp / "t.kicad_sch").write_text('(kicad_sch (version 20250114) (generator "eeschema") (uuid "8a1f3b52-0f6e-4c43-9d2a-0d2b5d3b7a01") (paper "A4") (lib_symbols))\n',
                                     encoding="utf-8")
    from copper_kicad.project import Project
    return Project(tmp, "t")


def test_resolve_stock_passive():
    _kicad()
    pr = _project()
    try:
        r = importer.resolve_lcsc(pr, "C25804", live=False)
        assert r["source"] == "kicad-stock" and r["lib_id"] == "Device:R" and r["value"] == "10k"
        assert r["fields"]["LCSC"] == "C25804" and r["fields"]["JLC Class"] == "Basic" and r["basic"]
        assert not (pr.dir / "libs").exists()
    finally:
        shutil.rmtree(pr.dir, ignore_errors=True)


# ── network: live JLC + EasyEDA import ───────────────────────────────────
def test_live_search():
    try:
        rows = search.jlc_query("C25804", page_size=5, in_stock=False)
    except C.NetError as e:
        skip(f"JLC API unreachable: {e}")
    assert rows and rows[0]["lcsc"] == "C25804" and rows[0]["basic"] and rows[0]["stock_source"] == "jlc-assembly"
    r = search.search("10k 0603", limit=5)
    assert r["results"][0]["lcsc"] == "C25804"
    r = search.search("ESP32-C3", limit=8)
    assert any("ESP32-C3" in x["mfr_part"] for x in r["results"])


def test_easyeda_import_usb_c():
    env = _kicad()
    try:
        E.fetch("C165948")
    except C.NetError as e:
        skip(f"EasyEDA unreachable: {e}")
    pr = _project()
    try:
        r = importer.resolve_lcsc(pr, "C165948", prefer="easyeda", with_3d=True)
        assert r["source"] == "easyeda" and r["lib_id"].startswith("copper-lcsc:")
        assert r["pin_pad_mismatch"] == [], r["pin_pad_mismatch"]
        assert {"A5", "B5", "A1B12"} <= set(r["pins"])
        assert r["pads"] >= 16
        v = importer.verify_import(pr, r)
        assert v["ok"], v["errors"]
        tables = [l.name for l in env.lib_tables("fp", pr.dir) if l.scope == "project"]
        assert "copper-lcsc" in tables
        fp = (pr.dir / "libs" / "copper-lcsc.pretty" / (r["footprint"].split(":")[1] + ".kicad_mod")).read_text(encoding="utf-8")
        assert "${KIPRJMOD}/libs/copper-lcsc.3dshapes/" in fp or "no 3D" in " ".join(r["warnings"])
        # idempotent: second import rewrites nothing
        r2 = importer.resolve_lcsc(pr, "C165948", prefer="easyeda", with_3d=True)
        assert r2["files_written"] == [], r2["files_written"]
    finally:
        shutil.rmtree(pr.dir, ignore_errors=True)


# ── regressions found by the skeptical-tester pass ───────────────────────
def test_regress_spec_parsing():
    # '10 kOhm' used to parse as a bare '10' + keyword 'kohm' and pick a 100nF capacitor
    p = basic.pick("10 kOhm 0603")
    assert p and p["lcsc"] == "C25804", p
    assert basic.parse_spec("4.7 kΩ 0402")["value"] == 4700
    # a unit-less '470' substring-matched '0402WGF4701TCE' (a 4.7k part)
    p = basic.pick("470 0402")
    assert p and p["value"] == "470" and p["package"] == "0402", p
    s = basic.parse_spec("47")
    assert s["kind"] == "resistor" and s["value"] == 47
    assert abs(basic.parse_spec("100mR 1206")["value"] - 0.1) < 1e-12
    s = basic.parse_spec("600R 0603 ferrite")
    assert s["kind"] == "ferrite" and s["value"] == 600
    # '5V zener' returned a 12 V zener (voltage was ignored for diodes)
    p = basic.pick("5V zener SOD-323")
    assert p is None or "5V" in p["mfr"].upper().replace("V1", "V"), p
    for c in basic.candidates("5.1V zener")[:5]:
        assert c["kind"] == "zener" and abs((c["typed"].get("V") or 5.1) - 5.1) < 0.5, c["mfr"]
    assert basic.pick("12V TVS") is None or basic.pick("12V TVS")["kind"] in ("tvs", "esd")
    s = basic.parse_spec("2.54mm header 1x4")
    assert s["pitch"] == 2.54 and s["kind"] == "header" and "mpn" not in s
    assert basic.pick("0603") is None
    # European decimal comma ('5,6K' was split into '5' + '6K' → a 6k part)
    assert basic.parse_spec("5,6K")["value"] == 5600 and basic.parse_spec("4,7uF 0805")["value"] == 4.7e-6


def test_regress_footprint_package():
    from copper_kicad.parts import sourcing as S
    cases = {"Resistor_SMD:R_0603_1608Metric": "0603", "footprints:1005_C": "0402", "footprints:R_1005_C": "0402",
             "C_0402": "0402", "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal": "DO-35",
             "Package_TO_SOT_THT:TO-92_HandSolder": "TO-92", "Package_DIP:DIP-8_W7.62mm_LongPads": "DIP-8",
             "footprints:USB_A_PCB_traces_small": None,          # 'small' is not an SMA diode package
             "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm": "SMD3225-4P", "Crystal_4-SMD_2.5x2mm": "SMD2520-4P",
             "Package_TO_SOT_SMD:SOT-23-5": "SOT-23-5", "Diode_SMD:D_SMA": "SMA",
             "Package_QFP:LQFP-48_7x7mm_P0.5mm": "LQFP-48"}
    for fp, want in cases.items():
        assert S.footprint_package(fp.split(":")[-1]) == want, (fp, S.footprint_package(fp.split(":")[-1]))


def test_regress_sourcing_decisions_offline():
    from copper_kicad.parts import sourcing as S
    pol = dict(S.DEFAULT_POLICY, live=False)
    # DNP / exclude_from_bom come from the netlist as empty-valued properties
    assert S.skip_reason("R1", {"footprint": "R_0603_1608Metric", "fields": {"dnp": ""}, "dnp": True}) == "DNP"
    assert S.skip_reason("C38", {"footprint": "CP_Elec_6.3x11", "fields": {"exclude_from_bom": ""}}) == "excluded from BOM"
    assert S.skip_reason("LOGO1", {"footprint": "footprints:Creative_Commons_CC_Mask", "fields": {}})
    assert S.skip_reason("H1", {"footprint": "footprints:Plain_Hole_3mm", "fields": {}})
    assert S.skip_reason("R2", {"footprint": "Resistor_SMD:R_0603_1608Metric", "fields": {}}) is None
    # bare ohm values on a resistor
    sp = S.infer_spec({"ref": "R7", "value": "470", "footprint": "footprints:R_1005_C", "lib_id": "lib:RSMALL"})
    assert sp["value"] == 470 and sp["package"] == "0402"
    rec, why, _, _ = S._choose(sp, pol, False)
    assert rec and rec["lcsc"] == "C25117", (rec and rec["lcsc"], why)
    # '470' on a capacitor is ambiguous → manual, never a resistor
    sp = S.infer_spec({"ref": "C9", "value": "470", "footprint": "Capacitor_SMD:C_0603_1608Metric", "lib_id": "Device:C"})
    rec, why, _, _ = S._choose(sp, pol, False)
    assert rec is None and "unit" in why
    # 1N4148 on a DO-35 footprint must not become the SOD-123 1N4148W
    sp = S.infer_spec({"ref": "D1", "value": "1N4148", "footprint": "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal",
                       "lib_id": "Device:D"})
    rec, why, _, _ = S._choose(sp, pol, False)
    assert rec is None or "DO-35" in (rec.get("package") or ""), rec
    # the designator vetoes nonsense: J7 'USB6' must not get an ESD chip (USB6B1RL)
    sp = S.infer_spec({"ref": "J7", "value": "USB6", "footprint": "Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal",
                       "lib_id": "Connector:Conn_01x04"})
    rec, why, _, _ = S._choose(sp, pol, False)
    assert rec is None or rec["kind"] in ("connector", "header"), rec
    # an MPN field wins over everything (offline table hit)
    sp = S.infer_spec({"ref": "R1", "value": "10k", "footprint": "footprints:R_1005_C", "lib_id": "lib:RSMALL",
                       "fields": {"MPN": "0402WGF1002TCE"}})
    rec, why, _, _ = S._choose(sp, pol, False)
    assert rec and rec["lcsc"] == "C25744" and "MPN field" in why
    assert S._mpn_eq("SM04B-SRSS-TB(LF)(SN) ", "SM04B-SRSS-TB(LF)(SN)")
    assert S._mpn_eq("TPS63020DSJR", "TPS63020DSJ") is True        # reel suffix
    assert not S._mpn_eq("LM358DR2G", "LM358")
    # existing_lcsc ignores junk
    assert S.existing_lcsc({"LCSC": "C25804"}) == "C25804" and S.existing_lcsc({"LCSC": "~"}) is None


def test_regress_locked_package_mismatch_and_no_footprint():
    from copper_kicad.parts import sourcing as S
    from copper_kicad.project import Project
    orig = S._components
    S._components = lambda pr: {
        "R1": {"value": "10k", "footprint": "Resistor_SMD:R_0402_1005Metric", "lib_id": "Device:R",
               "fields": {"LCSC": "C25804"}, "sheet": "/"},                     # C25804 is 0603
        "R2": {"value": "10k", "footprint": "", "lib_id": "Device:R", "fields": {}, "sheet": "/"}}
    try:
        r = S.source_bom(Project(Path(tempfile.gettempdir()), "none"), {"live": False, "write": False})
    finally:
        S._components = orig
    by = {l["refs"][0]: l for l in r["lines"] + r["manual"]}
    assert "0402" in by["R1"].get("warning", "") and by["R2"]["status"] == "manual"
    assert C.lcsc_norm("C 25804") == C.lcsc_norm("LCSC: C25804") == "C25804"


def _demo(name):
    env = _kicad()
    src = Path(env.kicad().share) / "demos" / name
    if not src.exists():
        skip(f"KiCad demo {name} not installed")
    dst = Path(tempfile.mkdtemp(prefix="copper test ")) / f"{name} copy"   # spaces in the path on purpose
    shutil.copytree(src, dst)
    return env, dst


def test_regress_bom_hierarchical_multi_instance():
    """source_bom used to send every set_field to the ROOT sheet → REF_NOT_FOUND on the first sub-sheet part
    (complex_hierarchy: ampli_ht.kicad_sch instantiated twice)."""
    env, d = _demo("complex_hierarchy")
    try:
        sub = d / "ampli_ht.kicad_sch"
        t = sub.read_text(encoding="utf-8")
        t = t.replace("Resistor_THT:R_Axial_DIN0204_L3.6mm_D1.6mm_P7.62mm_Horizontal", "Resistor_SMD:R_0603_1608Metric")
        sub.write_text(t, encoding="utf-8")
        from copper_kicad import project as P
        from copper_kicad.parts import sourcing as S
        from copper_kicad.sch.netlist import export_netlist
        pr = P.resolve(str(d))
        before = export_netlist(pr)
        # root sheet locked by KiCad → refuse before writing anything (KiCad only locks the root file)
        lock = d / f"~{pr.sch.name}.lck"
        lock.write_text("{}", encoding="utf-8")
        h0 = sub.read_bytes()
        try:
            S.source_bom(pr, {"live": False, "write": True})
            assert False, "expected FILE_OPEN_IN_KICAD"
        except Exception as e:  # noqa: BLE001
            assert getattr(e, "code", "") == "FILE_OPEN_IN_KICAD", e
        assert sub.read_bytes() == h0
        lock.unlink()
        r = S.source_bom(pr, {"live": False, "write": True})
        assert r["written"]["sheets_written"] == ["ampli_ht.kicad_sch"], r["written"]
        s = r["summary"]
        assert sum(s["unique_by_class"].values()) == s["unique_lcsc"]
        after = export_netlist(pr)
        # both instances of every resistor carry the field (fields are shared by instances)
        for ref, c in after["components"].items():
            if ref.startswith("R") and not ref.startswith("RV"):
                assert c["fields"].get("LCSC", "").startswith("C"), (ref, c["fields"])
        assert after["components"]["R204"]["fields"]["LCSC"] == after["components"]["R304"]["fields"]["LCSC"] == basic.pick("470 0603")["lcsc"]
        # connectivity untouched
        assert {k: sorted((n["ref"], n["pin"]) for n in v) for k, v in before["nets"].items()} == \
               {k: sorted((n["ref"], n["pin"]) for n in v) for k, v in after["nets"].items()}
        # idempotent
        r2 = S.source_bom(pr, {"live": False, "write": True})
        assert r2["summary"]["newly_assigned"] == 0 and r2["written"].get("ops", 0) == 0
    finally:
        shutil.rmtree(d.parent, ignore_errors=True)


def test_regress_bom_dnp_and_mpn_fields():
    """StickHub: DNP (netlist 'dnp' property with empty value) was not skipped; MPN fields were ignored."""
    env, d = _demo("stickhub")
    try:
        sch = d / "StickHub.kicad_sch"
        t = sch.read_text(encoding="utf-8")
        i = t.index('(property "Reference" "R1"')
        j = t.rindex("(dnp no)", 0, i)
        sch.write_text(t[:j] + "(dnp yes)" + t[j + len("(dnp no)"):], encoding="utf-8")
        from copper_kicad import project as P
        from copper_kicad.parts import sourcing as S
        r = S.source_bom(P.resolve(str(d)), {"live": False, "write": False})
        sk = r["summary"]["skipped"]
        assert "R1" in sk.get("DNP", []), sk
        assert "C38" in sk.get("excluded from BOM", []) and "LOGO1" in sk.get("mechanical / not assembled", [])
        by_ref = {ref: l for l in r["lines"] + r["manual"] for ref in l["refs"]}
        assert by_ref["R2"]["lcsc"] == "C25741" and "MPN field" in by_ref["R2"]["reason"]   # 0402WGF1003TCE
        assert by_ref["C2"]["lcsc"] and by_ref["C2"]["package"] == "0402"                  # '1005_C' footprint
        assert by_ref["J1"]["status"] == "manual"
        assert "jlc_class" not in by_ref["J1"]
    finally:
        shutil.rmtree(d.parent, ignore_errors=True)


def test_regress_tool_surface():
    import copper_kicad.tools  # noqa: F401  (registers tools)
    from copper_kicad.mcp import registry as R
    out = R.call_tool("parts_alternatives", {"lcsc": "C25804", "limit": 2})   # 'lcsc' alias accepted
    assert not out["isError"], out
    out = R.call_tool("parts_alternatives", {})
    assert out["isError"] and out["structuredContent"]["error"] == "BAD_ARGUMENT"
    old = os.environ.get("COPPER_OFFLINE")
    os.environ["COPPER_OFFLINE"] = "1"
    try:
        out = R.call_tool("parts_search", {"query": "10k 0603", "limit": 8})
        txt = out["content"][0]["text"]
        assert len(txt) < 6000, len(txt)          # compact: ~500 chars per result
        res = out["structuredContent"]["results"]
        assert res[0]["lcsc"] == "C25804" and "datasheet" not in res[0] and "setup_fee" not in res[0]
    finally:
        if old is None:
            os.environ.pop("COPPER_OFFLINE", None)
        else:
            os.environ["COPPER_OFFLINE"] = old



# ── regression: tester findings (stm32-sensor, power-board, edit-existing, rp2040) ──
def _r(**kw):
    kw.setdefault("stock", 100000)
    kw.setdefault("price", [{"qty": 1, "usd": 0.1}])
    return search._rec(**kw)


_HDR_M = dict(category="Connectors", subcategory="Pin Headers", package="Plugin,P=2.54mm")
_HDR_F = dict(category="Connectors", subcategory="Female Headers", package="Plugin,P=2.54mm")
_USBC = dict(category="Connectors", subcategory="USB Connectors", package="SMD")


def test_regress_header_queries():
    """'Header 1x5 2.54mm' returned a female header + USB-C receptacles; 'Header 2x10' returned USB-C first."""
    s = basic.parse_spec("Header 1x5 2.54mm")
    assert s["kind"] == "header" and s["pins"] == 5 and s["rows"] == 1 and s["pitch"] == 2.54
    assert search._jlc_keyword(s, "Header 1x5 2.54mm") == "1x5P 2.54mm"     # JLC wording; raw text gave 0 rows
    assert search._jlc_keyword(basic.parse_spec("Header 2x10"), "Header 2x10") == "2x10P"
    male = _r(lcsc="C1", mfr="HX PZ2.54-1x5P ZZ", description="1x5P 2.54mm Pin Header Through Hole", **_HDR_M)
    fem = _r(lcsc="C2", mfr="2.54-1*5PFemale", description="1x5P 2.54mm Square Hole Through Hole", **_HDR_F)
    usbc = _r(lcsc="C3", mfr="TYPE-C 6P", description="6P Female Surface Mount Type-C", **_USBC)
    assert basic.spec_matches(male, s) == 0 and basic.spec_matches(fem, s) == 1      # pins preferred
    assert basic.spec_matches(usbc, s) == 9                                           # never a USB-C
    assert basic.spec_matches(usbc, basic.parse_spec("Header 2x10")) == 9
    pm = basic.parse_spec("pin header male 1x5 2.54mm")
    assert pm["kind"] == "header" and pm["gender"] == "male" and basic.spec_matches(fem, pm) == 9
    fs = basic.parse_spec("female header 1x5")
    assert fs["gender"] == "female" and basic.spec_matches(male, fs) == 9 and basic.spec_matches(fem, fs) == 0
    # known item: '2.54mm header 1x10' used to pick C60563 (2x5 right-angle)
    s10 = basic.parse_spec("2.54mm header 1x10")
    two5 = _r(lcsc="C60563", mfr="2.54-2*5P", description="2x5P 2.54mm Bend Pin Header", **_HDR_M)
    assert basic.spec_matches(two5, s10) == 9
    bent = _r(lcsc="C5", mfr="1x10P-R", description="1x10P 2.54mm Right Angle Pin Header", **_HDR_M)
    straight = _r(lcsc="C6", mfr="1x10P-V", description="1x10P 2.54mm Pin Header", **_HDR_M)
    assert basic.spec_matches(bent, s10) == 1 and basic.spec_matches(straight, s10) == 0
    p = basic.pick("2.54mm header 1x10", live=False)
    assert p is None or (p["lcsc"] != "C60563" and "2x5" not in (p.get("description") or "").lower()), p


def test_regress_usb_a():
    """USB-A queries returned nothing or Type-C/micro-USB/pin headers."""
    for q in ("USB-A", "USB A female", "USB-A female", "USB Type-A receptacle", "Type-A female", "USB-AF"):
        s = basic.parse_spec(q)
        assert s.get("kind") == "usb_connector" and s.get("usb") == "a", (q, s)
    assert search._jlc_keyword(basic.parse_spec("USB-A"), "USB-A") == "Type-A"
    a = _r(lcsc="C46407", mfr="USB-A-F-90", description="4P Female Right Angle Type-A USB 2.0", **_USBC)
    c = _r(lcsc="C456012", mfr="TYPE-C 6P", description="6P Female Surface Mount Type-C", **_USBC)
    s = basic.parse_spec("USB A female")
    assert basic.spec_matches(a, s) == 0 and basic.spec_matches(c, s) == 9
    assert basic.parse_spec("USB-C 16P")["usb"] == "c"          # unchanged


def test_regress_crystal_package_and_load_cap():
    """'8MHz crystal 3225' dropped the package; package='3225' matched nothing; load_pF was in farads."""
    s = basic.parse_spec("8MHz crystal 3225")
    assert s["kind"] == "crystal" and s["package"] == "SMD3225" and s["value"] == 8e6
    assert "3225" in search._jlc_keyword(s, "8MHz crystal 3225")
    assert basic.pkg_match("SMD3225-4P", "3225") and basic.pkg_match("SMD3225-4P", "SMD3225")
    assert not basic.pkg_match("SMD5032-2P", "3225") and not basic.pkg_match("HC-49S", "SMD3225")
    t = C.typed_fields("crystal", {"Load Capacitance": "20pF", "Frequency": "12MHz"})
    assert t["load_pF"] == 20 and t["Hz"] == 12e6
    assert C.typed_fields("crystal", {}, "12MHz 9pF 60Ω SMD3225-4P")["load_pF"] == 9


def test_regress_inductor_current():
    """'22uH inductor 3A' ranked a 600 mA part first; unparsed currents passed as exact matches."""
    t = C.typed_fields("inductor", {"Current - Saturation(Isat)": "600mA", "Inductance": "22uH"})
    assert abs(t["A"] - 0.6) < 1e-9
    t = C.typed_fields("inductor", {"Current - Saturation(Isat)": "2.2A", "Current Rating": "1.9A", "Inductance": "22uH"})
    assert t["A"] == 1.9                                                   # usable = min(Irms, Isat)
    assert C.desc_currents("2.2A 2.2A 22uH 85.8mΩ") == [2.2, 2.2] and C.desc_currents("1A 2V@(1A) 8mA") == [1.0, 0.008]
    ind = dict(category="Inductors, Coils, Chokes", subcategory="Power Inductors", package="SMD,8x8mm")
    small = _r(lcsc="C88254", mfr="SWPA4026S220MT", description="214mΩ 22uH 600mA Magnetic Shielded Inductor", **ind)
    big = _r(lcsc="C10165", mfr="SMDRH105R-220MT", description="22uH 3.2A 3.3A 58mΩ Shielded", **ind)
    unk = _r(lcsc="C9", mfr="X", description="22uH Power Inductor", **ind)
    s = basic.parse_spec("22uH inductor 3A")
    assert s["current"] == 3.0
    assert basic.spec_matches(small, s) == 9 and basic.spec_matches(big, s) == 0 and basic.spec_matches(unk, s) == 1
    # plain '22uH': a 5 mA multilayer chip (Preferred) must not outrank power inductors
    chip = _r(lcsc="C32375", mfr="SDFL2012T220KTF", preferred=True, category="Inductors, Coils, Chokes",
              subcategory="Inductors (SMD)", package="0805", description="22uH 5mA Multilayer inductor",
              attributes={"Inductance": "22uH", "Current Rating": "5mA"})
    sp = basic.parse_spec("22uH")
    assert sorted([chip, big], key=lambda r: basic._rank(dict(r, _score=0), sp))[0]["lcsc"] == "C10165"


def test_regress_mpn_ranking_and_numeric_mpn():
    """'MT3608' ranked MT3608B first; '7805 TO-220' parsed as a 7805 Ω resistor; BOM U3 '7805' found nothing."""
    dc = dict(category="Power Management (PMIC)", subcategory="DC-DC Converters", package="SOT-23-6")
    exact = _r(lcsc="C84817", mfr="MT3608", stock=34000, **dc)
    other = _r(lcsc="C19189893", mfr="MT3608B", stock=79000, **dc)
    sp = basic.parse_spec("MT3608")
    ranked = sorted([other, exact], key=lambda r: search.rank_key(r, basic.spec_matches(r, sp), sp))
    assert ranked[0]["lcsc"] == "C84817"
    s = basic.parse_spec("7805 TO-220")
    assert s.get("mpn") == "7805" and s.get("kind") != "resistor" and s["package"] == "TO-220"
    assert basic.parse_spec("470 0603")["kind"] == "resistor"            # bare ohms on a chip package unchanged
    assert basic.parse_spec("470")["value"] == 470
    ldo = dict(category="Power Management (PMIC)", subcategory="Linear Voltage Regulators (LDO)", package="TO-220")
    assert basic.spec_matches(_r(lcsc="C347289", mfr="L7805CV(UMW)", **ldo), s) == 0
    assert basic.spec_matches(_r(lcsc="C1", mfr="L78051", **ldo), s) != 0        # 78051 is not a 7805
    assert basic.mpn_family("78l05", "l78l05acz") and basic.mpn_family("7805", "mc7805ctg")
    assert not basic.mpn_family("7805", "78051") and not basic.mpn_family("ams1117", "xams1117")
    from copper_kicad.parts import sourcing as S
    sp = S.infer_spec({"ref": "U3", "value": "7805", "footprint": "Package_TO_SOT_THT:TO-220-3_Horizontal_TabDown",
                       "lib_id": "pic_programmer:7805"})
    assert sp.get("mpn") == "7805" and "value" not in sp and sp["package"] == "TO-220"
    sp = S.infer_spec({"ref": "R3", "value": "470", "footprint": "Resistor_SMD:R_0603_1608Metric", "lib_id": "Device:R"})
    assert sp["value"] == 470 and sp["kind"] == "resistor"


def test_regress_spec_pick_shares_search_ranking():
    """spec '100nF 0402' placed C307331 while parts_search ranked C1525 first; '27R 0402' fell back to a
    generic symbol ('parts engine unavailable') although parts_search had in-stock parts; Extended picks were silent."""
    old = os.environ.get("COPPER_OFFLINE")
    os.environ["COPPER_OFFLINE"] = "1"
    try:
        p = basic.pick("100nF 0402", live=False)
        r = search.search("100nF 0402", limit=3, live=False)
        assert p["lcsc"] == r["results"][0]["lcsc"], (p["lcsc"], r["results"][0]["lcsc"])
    finally:
        if old is None:
            os.environ.pop("COPPER_OFFLINE", None)
        else:
            os.environ["COPPER_OFFLINE"] = old
    # LED colours with no Basic 0603 part: the pick says so and names the Basic part in another package
    p = basic.pick("LED green 0603", live=False)
    assert p and p["class"] != "Basic" and "C2297" in p.get("warning", "") and "0805" in p["warning"], p
    assert basic.pick("LED red 0603", live=False)["lcsc"] == "C2286" and "warning" not in basic.pick("LED red 0603", live=False)
    # live fallback when the offline table has no acceptable part
    fake = _r(lcsc="C25100", mfr="0402WGF270JTCE", category="Resistors", subcategory="Chip Resistor - Surface Mount",
              package="0402", description="27Ω ±1% 0402", attributes={"Resistance": "27Ω"}, source="jlc")
    orig = search.search
    search.search = lambda *a, **k: {"results": [], "records": [dict(fake, _score=0)]}
    try:
        p = basic.pick("27R 0402")
        assert p and p["lcsc"] == "C25100" and p["source"] == "live JLC search" and "Extended" in p["warning"], p
    finally:
        search.search = orig


def test_regress_tht_passives():
    """THT passives were all 'manual' with no options and an R_0603 hint even for caps; THT semis were assigned."""
    from copper_kicad.parts import sourcing as S
    g = S.tht_geometry("Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
    assert g == {"style": "axial", "D": 2.5, "L": 6.3, "P": 10.16}
    assert S.tht_geometry("CP_Radial_D6.3mm_P2.50mm")["D"] == 6.3 and S.tht_geometry("R_0603_1608Metric") is None
    res = dict(category="Resistors", subcategory="Through Hole Resistors")
    r207 = _r(lcsc="C119347", mfr="MF1/4W-10KΩ±1% T", package="Plugin,D2.3xL6.5mm", description="10kΩ 250mW Metal Film",
              attributes={"Resistance": "10kΩ"}, **res)
    assert S._geom_score(g, r207) == 0
    assert S._geom_score(S.tht_geometry("R_Axial_DIN0204_L3.6mm_D1.6mm_P7.62mm_Horizontal"), r207) is None
    elec = _r(lcsc="C2835673", mfr="X", category="Capacitors", subcategory="Aluminum Electrolytic Capacitors - Leaded",
              package="Plugin,D6.3xL11mm", description="22uF 25V")
    assert S._geom_score(S.tht_geometry("C_Axial_L12.0mm_D6.5mm_P20.00mm_Horizontal"), elec) is None   # radial ≠ axial
    assert S._geom_score(S.tht_geometry("CP_Radial_D6.3mm_P2.50mm"), elec) == 1                       # pitch unknown
    disc = _r(lcsc="C254085", mfr="CC1H104", category="Capacitors", subcategory="Through Hole Ceramic Capacitors",
              package="Plugin,P=5.08mm", description="100nF 50V")
    assert S._geom_score(S.tht_geometry("C_Disc_D5.1mm_W3.2mm_P5.00mm"), disc) == 0
    chip = _r(lcsc="C14663", mfr="CC0603", category="Capacitors", subcategory="MLCC - SMD/SMT", package="0603")
    assert S._geom_score(S.tht_geometry("C_Disc_D5.1mm_W3.2mm_P5.00mm"), chip) is None
    pol = dict(S.DEFAULT_POLICY)
    sp = S.infer_spec({"ref": "R1", "value": "10K", "lib_id": "Device:R",
                       "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"})
    orig = search.jlc_query
    search.jlc_query = lambda *a, **k: [r207, chip]
    try:
        rec, why, _, notes = S._choose(sp, pol, True)
        assert rec and rec["lcsc"] == "C119347" and "THT" in why and notes, (rec, why)
    finally:
        search.jlc_query = orig
    # offline: an exact, kind-appropriate fix (a capacitor is never told to use R_0603)
    sp = S.infer_spec({"ref": "C3", "value": "47uF/63V", "lib_id": "Device:C_Polarized",
                       "footprint": "Capacitor_THT:CP_Radial_D6.3mm_P2.50mm"})
    rec, why, _, _ = S._choose(sp, dict(pol, live=False), False)
    assert rec is None and "CP_Elec" in why and "63V" in why and "R_0603" not in why, why
    sp = S.infer_spec({"ref": "RV1", "value": "4,7K", "lib_id": "Device:R_Potentiometer",
                       "footprint": "Potentiometer_THT:Potentiometer_Bourns_3266W_Vertical"})
    rec, why, opts, _ = S._choose(sp, pol, False)
    assert rec is None and "potentiometer" in why and not opts


def test_regress_mpn_field_package():
    """MPN field '1N4007' on a DO-41 footprint was assigned the SMA part; '78L05' on TO-92 found nothing."""
    from copper_kicad.parts import sourcing as S
    dio = dict(category="Diodes", subcategory="Diodes - General Purpose")
    sma = _r(lcsc="C727081", mfr="1N4007", package="SMA(DO-214AC)", stock=900000, basic=True, **dio)
    do41 = _r(lcsc="C106903", mfr="1N4007", package="DO-41", stock=50000, **dio)
    sp = S.infer_spec({"ref": "D101", "value": "1N4007", "lib_id": "Device:D_Small",
                       "footprint": "Diode_THT:D_DO-41_SOD81_P12.70mm_Horizontal", "fields": {"MPN": "1N4007"}})
    orig = S.by_mpn
    try:
        S.by_mpn = lambda mpn, live=True, family=False: [sma, do41]
        rec, why, _, _ = S._choose(sp, S.DEFAULT_POLICY, False)
        assert rec["lcsc"] == "C106903" and "DO-41" in why, (rec["lcsc"], why)
        S.by_mpn = lambda mpn, live=True, family=False: [sma]
        rec, why, opts, _ = S._choose(sp, S.DEFAULT_POLICY, False)
        assert rec is None and "DO-41" in why and opts, why
    finally:
        S.by_mpn = orig


def _pin(num, name, elec="0", x=-30, y=-10):
    return (f"P~show~{elec}~{num}~{x}~{y}~180~g{num}~0^^{x}~{y}^^M {x} {y} h 10~#880000^^1~{x + 13}~{y + 4}~0~{name}~start~~~#0000FF"
            f"^^1~{x + 6}~{y - 1}~0~{num}~end~~~#0000FF^^0~{x + 3}~{y}^^0~M 0 0")


def _sym_comp(pins, pre="U?"):
    return {"title": "T", "dataStr": {"head": {"x": 0, "y": 0, "c_para": {"pre": pre}},
                                      "shape": ["R~-20~-40~2~2~40~80~#880000~1~0~none~gge1~0~"] + pins}}


def _pin_info(sym):
    out = {}
    for unit in [e for e in sym if isinstance(e, list) and e and e[0] == "symbol"]:
        for p in [e for e in unit if isinstance(e, list) and e and e[0] == "pin"]:
            num = next(e for e in p if isinstance(e, list) and e[0] == "number")[1]
            name = next(e for e in p if isinstance(e, list) and e[0] == "name")[1]
            out[num] = (str(p[1]), name)
    return out


def test_regress_easyeda_symbol_prefix_types_names():
    """Pin header got 'U'; every STM32 pin was passive; header pin names repeated the numbers."""
    from copper_kicad import sexpr
    mcu = _sym_comp([_pin(1, "VDD/VDDA", y=-30), _pin(2, "VSSA/SSA", y=-20), _pin(3, "PA0", y=-10), _pin(4, "NRST", y=0),
                     _pin(5, "VBUS", y=10)])
    sym, info = E.build_symbol(mcu, "MCU_C1", {"Value": "MCU"}, kind="mcu")
    pi = _pin_info(sym)
    assert pi["1"][0] == "power_in" and pi["2"][0] == "power_in", pi
    assert pi["3"][0] == "passive" and pi["4"][0] == "passive" and pi["5"][0] == "passive", pi
    assert info["prefix"] == "U"
    ldo = _sym_comp([_pin(1, "GND", y=-10), _pin(2, "VOUT", y=0), _pin(3, "VIN", y=10)])
    pi = _pin_info(E.build_symbol(ldo, "LDO_C2", {}, kind="ldo")[0])
    assert pi["1"][0] == "power_in" and pi["2"][0] == "power_out" and pi["3"][0] == "power_in", pi
    typed = _sym_comp([_pin(1, "VDD", elec="4", y=-10), _pin(2, "IO", elec="3", y=0)])     # EasyEDA typed: kept
    assert _pin_info(E.build_symbol(typed, "T_C3", {}, kind="mcu")[0])["2"][0] == "bidirectional"
    hdr = _sym_comp([_pin(i, str(i), y=-30 + 10 * i) for i in range(1, 6)], pre="U?")
    sym, info = E.build_symbol(hdr, "HDR_C4", {}, kind="header")
    assert info["prefix"] == "J"
    assert all(n == "~" for _, n in _pin_info(sym).values())
    txt = sexpr.dumps(sym)
    assert "(pin_names" in txt and "hide" in txt.split("(pin_names", 1)[1][:60]
    # known item: EasyEDA prefixes H / HDR / P / PIN map to J even without a kind
    for pre in ("H?", "HDR?", "P?", "PIN?"):
        assert E.build_symbol(_sym_comp([_pin(1, "1")], pre=pre), "X", {})[1]["prefix"] == "J", pre
    assert E.build_symbol(_sym_comp([_pin(1, "1")], pre="U?"), "X", {}, kind="crystal")[1]["prefix"] == "Y"


def test_regress_3d_model_uses_referenced_file():
    """C5383109: transform came from the OBJ but KiCad loaded the STEP (different origin) → header 4 mm in the air."""
    tmp = Path(tempfile.mkdtemp(prefix="copper-test-3d-"))
    obj = "v -12.7 -1.11 -6.97\nv 12.7 3.97 4.57\nv 0 0 0\nf 1 2 3\n"
    step = (b"#1=CARTESIAN_POINT('',(-12.66,-2.5,-3.0));\n#2=CARTESIAN_POINT('',(12.65,2.5,8.5));\n"
            b"#3=VERTEX_POINT('',#1);\n#4=VERTEX_POINT('',#2);\n")
    orig = E.fetch_3d
    E.fetch_3d = lambda uuid: (obj, step)
    try:
        paths = {"3d": tmp / "x.3dshapes"}
        warnings = []
        mi = importer._write_3d(paths, "HDR", {"uuid": "u", "origin_mm": (0.0, 0.0), "z_mm": -3.0, "rotation": [0, 0, 0]},
                                warnings)
        assert mi["path"].endswith("HDR.step"), mi
        assert [round(v, 3) for v in mi["offset"]] == [0.005, 0.0, 0.0], mi["offset"]
        fmeta = {"pad_extent_mm": [-12.0, -2.0, 12.0, 2.0]}
        assert importer._model_sanity(mi["bbox"], fmeta) is None
        floating = E.model_bbox([(-1, -1, 4.0), (1, 1, 6.0)], [0, 0, 0], [0, 0, 0])
        assert "floats" in importer._model_sanity(floating, fmeta)
        shifted = E.model_bbox([(20, 20, 0), (22, 22, 1)], [0, 0, 0], [0, 0, 0])
        assert "pad centre" in importer._model_sanity(shifted, fmeta)
    finally:
        E.fetch_3d = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_regress_live_queries():
    """Live JLC: the tester's exact queries now return the right kind of part (skips offline)."""
    try:
        r = search.search("Header 2x10 2.54mm", limit=3)
        if not str(r["sources"].get("jlc", "")).startswith("ok"):
            skip("JLC unreachable")
        assert r["results"] and all(x["kind"] == "header" for x in r["results"]), r["results"]
        assert "2x10" in (r["results"][0]["description"] + r["results"][0]["mfr_part"]).lower().replace("*", "x")
        r = search.search("USB-A", limit=3)
        assert r["results"] and all("type-c" not in (x["description"] + x["mfr_part"]).lower() for x in r["results"])
        r = search.search("22uH inductor 3A", limit=5)
        assert r["results"] and all((x.get("params") or {}).get("A", 3) >= 3 or x.get("match") for x in r["results"])
        r = search.search("7805 TO-220", limit=3)
        assert r["results"] and "7805" in r["results"][0]["mfr_part"] and "TO-220" in r["results"][0]["package"]
        r = search.search("8MHz crystal", {"package": "3225"}, limit=3)
        assert r["results"] and all("3225" in x["package"] for x in r["results"])
    except C.NetError as e:
        skip(f"network: {e}")


# ── regression: cross-area patch requests (schematic / verify testers) ────
def test_regress_easyeda_connector_footprint_gets_prefix_j():
    """A pin header whose LCSC category is unknown and whose EasyEDA prefix is 'U?' still becomes J when its
    footprint name says connector (HDR-…, CONN-…, USB-…, JST…); a real IC keeps U."""
    hdr = _sym_comp([_pin(1, "1", y=-10), _pin(2, "2", y=0), _pin(3, "3", y=10)])
    for fp in ("copper-lcsc:HDR-TH_3P-P2.54-V-M", "copper-lcsc:CONN-SMD_4P-P1.00", "copper-lcsc:USB-C_SMD-TYPE-C-31-M-12",
               "copper-lcsc:JST-XH-2A"):
        _sym, info = E.build_symbol(hdr, "X_C9", {"Value": "X"}, footprint=fp, kind="")
        assert info["prefix"] == "J", (fp, info["prefix"])
    _sym, info = E.build_symbol(hdr, "X_C9", {"Value": "X"}, footprint="copper-lcsc:SOT-23-3_L2.9-W1.3", kind="")
    assert info["prefix"] == "U", info["prefix"]


def test_regress_source_bom_refuses_before_searching_and_reports_honestly():
    """parts_source_bom on a schematic open in KiCad spent the whole (rate-limited) search pass before refusing;
    and with write omitted it said 'not written (write=false)' when there was simply nothing to write."""
    env, d = _demo("stickhub")
    try:
        import copper_kicad.tools  # noqa: F401
        from copper_kicad import project as P
        from copper_kicad.mcp import registry as R
        from copper_kicad.parts import sourcing as S
        pr = P.resolve(str(d), activate=False)
        lock = d / f"~{pr.sch.name}.lck"
        lock.write_text("{}", encoding="utf-8")
        orig = S._choose
        S._choose = lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched before checking the lock"))
        try:
            for pol in ({"live": False, "write": True}, {"live": False, "write": False, "will_write": True}):
                try:
                    S.source_bom(pr, pol)
                    assert False, "expected FILE_OPEN_IN_KICAD"
                except Exception as e:  # noqa: BLE001
                    assert getattr(e, "code", "") == "FILE_OPEN_IN_KICAD", repr(e)
            out = R.call_tool("parts_source_bom", {"project": str(d), "live": False})       # the tool: same early refusal
            assert out["isError"] and out["structuredContent"]["error"] == "FILE_OPEN_IN_KICAD", out["structuredContent"]
        finally:
            S._choose = orig
            lock.unlink()
        # write=false is a report only and never needs the lock
        r = S.source_bom(pr, {"live": False, "write": False})
        assert r["written"]["note"] == "not written (write=false)", r["written"]
        # the tool's default IS write=true: first call writes, second has nothing left to write and says so
        out = R.call_tool("parts_source_bom", {"project": str(d), "live": False})
        assert not out["isError"], out["structuredContent"]
        out = R.call_tool("parts_source_bom", {"project": str(d), "live": False})
        w = out["structuredContent"]["written"]
        assert w.get("ops", 0) == 0 and w.get("note") == "nothing to write", w
        assert "would be assigned" not in out["content"][0]["text"].splitlines()[0]
    finally:
        shutil.rmtree(d.parent, ignore_errors=True)


def test_regress_bom_lines_name_the_sheet_for_sub_sheet_refs():
    """Fix ops for refs on a sub-sheet (or one instance of a multi-instance sheet) need sch_apply(sheet=…):
    each BOM line carries it, and the tool's `next` hint uses it."""
    env, d = _demo("complex_hierarchy")
    try:
        import copper_kicad.tools  # noqa: F401
        from copper_kicad import project as P
        from copper_kicad.mcp import registry as R
        from copper_kicad.parts import sourcing as S
        from copper_kicad.sch import ops as O
        pr = P.resolve(str(d), activate=False)
        r = S.source_bom(pr, {"live": False, "write": False})
        lines = r["lines"] + r["manual"]
        sub = [l for l in lines if any(ref.startswith(("R2", "R3", "C2", "C3", "Q2", "Q3")) and len(ref) == 4 for ref in l["refs"])]
        assert sub and all(l.get("sheet") for l in sub), [(l["refs"][:2], l.get("sheet")) for l in sub][:5]
        root = [l for l in lines if l not in sub and not l.get("sheet")]
        assert root, "root-sheet lines must not carry a sheet argument"
        l0 = sub[0]
        # the sheet argument really reaches the ref
        res = O.apply_ops(pr, [{"op": "set_field", "ref": l0["refs"][0], "fields": {"Notes": "x"}}], sheet=l0["sheet"], dry_run=True)
        assert res.get("changed"), res
        out = R.call_tool("parts_source_bom", {"project": str(d), "live": False, "write": False})["structuredContent"]
        m = out["manual"][0] if out["manual"] else None
        if m and m.get("sheet"):
            assert f'sheet="{m["sheet"]}"' in out["next"], out["next"]
    finally:
        shutil.rmtree(d.parent, ignore_errors=True)


def test_regress_import_labels_snapshot_stock_like_search():
    """parts_search labelled snapshot stock ('stock_note'); parts_import showed a bare number from another source."""
    env = _kicad()
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.kicad import env as kenv
    from copper_kicad.mcp import registry as R
    from copper_kicad.review import project_info
    d = Path(tempfile.mkdtemp(prefix="copper-parts-stock-"))
    old = os.environ.get("COPPER_OFFLINE")
    os.environ["COPPER_OFFLINE"] = "1"
    try:
        pr = project_info.create_project("stocklabel", str(d), "jlc-2layer")["project"]
        s = R.call_tool("parts_search", {"query": "10k 0603", "limit": 1})["structuredContent"]["results"][0]
        i = R.call_tool("parts_import", {"lcsc": "C25804", "project": pr["dir"], "preview": False})
        assert not i["isError"], i["structuredContent"]
        i = i["structuredContent"]
        assert s["lcsc"] == i["lcsc"] == "C25804" and s["stock"] == i["stock"], (s["stock"], i["stock"])
        # one source for both tools: the same number with the same label (a snapshot date, or nothing when it is live)
        assert i.get("stock_note") == s.get("stock_note"), (s.get("stock_note"), i.get("stock_note"))
        from copper_kicad import project as P
        off = importer.resolve_lcsc(P.resolve(pr["dir"], activate=False), "C25804", live=False)
        assert str(off["stock_source"]).startswith("snapshot 20"), off["stock_source"]      # offline record: dated snapshot
        o = R.call_tool("parts_search", {"query": "10k 0603", "limit": 1, "live": False})["structuredContent"]["results"][0]
        assert "snapshot 20" in o.get("stock_note", ""), o
        assert i["provider"] == "lcsc" and i["fields"]["MPN"] and i["fields"]["Manufacturer"]      # neutral fields always ride along
    finally:
        if old is None:
            os.environ.pop("COPPER_OFFLINE", None)
        else:
            os.environ["COPPER_OFFLINE"] = old
        shutil.rmtree(d, ignore_errors=True)


def test_regress_tools_take_provider_and_say_who_answered():
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.mcp import registry as R
    for name in ("parts_search", "parts_get", "parts_import", "parts_source_bom", "parts_alternatives"):
        assert "provider" in R.TOOLS[name].schema["properties"], name
    old = os.environ.get("COPPER_OFFLINE")
    os.environ["COPPER_OFFLINE"] = "1"
    try:
        out = R.call_tool("parts_search", {"query": "10k 0603", "limit": 2})["structuredContent"]
        assert out["provider"] == "lcsc" and out["providers_asked"] == ["lcsc"], out.get("provider")
        r0 = out["results"][0]
        assert r0["provider"] == "lcsc" and r0["id"] == r0["lcsc"] == "C25804" and r0["mpn"] == r0["mfr_part"], r0
        k = R.call_tool("parts_search", {"query": "10k 0603", "provider": "kicad"})["structuredContent"]
        assert k["provider"] == "kicad" and k["results"][0]["lib_id"] == "Device:R", k["results"][:1]
        assert k["results"][0]["footprint"] == "Resistor_SMD:R_0603_1608Metric"
        bad = R.call_tool("parts_search", {"query": "10k", "provider": "nope"})
        assert bad["isError"] and bad["structuredContent"]["error"] == "UNKNOWN_PROVIDER" and "lcsc" in bad["structuredContent"]["fix"]
    finally:
        if old is None:
            os.environ.pop("COPPER_OFFLINE", None)
        else:
            os.environ["COPPER_OFFLINE"] = old


def test_regress_provider_failures_are_named_and_generic_hint_is_placeable():
    """Review of the provider layer: (1) a third-party provider that raises or returns junk must come back as
    PROVIDER_FAILED / a provider_errors note, never INTERNAL; (2) the `next` hint for a generic KiCad part must
    carry value + footprint (place part='Device:C' loses both); (3) a spreadsheet's ANSI CSV export is readable."""
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.mcp import registry as R
    home = Path(tempfile.mkdtemp(prefix="copper-test-prov home ü-"))
    (home / "prov.py").write_text(
        "class Boom:\n    name = 'boom'\n"
        "    def search(self, q, filters=None, limit=8, live=True):\n        raise RuntimeError('API key missing')\n"
        "    def get(self, part_id, live=True):\n        return {'nothing': 1}\n", encoding="utf-8")
    (home / "stock.csv").write_bytes("Mfr Part #;Manufacturer;Value;Package\r\nWE-744;Würth;10uH;0805\r\n".encode("cp1252"))
    (home / "config.json").write_text(json.dumps({"parts": {"providers": ["boom", "local", "kicad"], "provider_paths": ["prov.py"],
                                                            "catalogue": "stock.csv"}}), encoding="utf-8")
    old = os.environ.get("COPPER_HOME")
    os.environ["COPPER_HOME"] = str(home)
    try:
        r = R.call_tool("parts_search", {"query": "100nF 0402", "live": False})["structuredContent"]
        assert r["provider"] == "kicad" and "API key missing" in r["provider_errors"]["boom"], r
        assert '"lib_id":"Device:C"' in r["next"] and '"value":"100nF"' in r["next"] and "C_0402" in r["next"], r["next"]
        w = R.call_tool("parts_search", {"query": "WE-744", "live": False})["structuredContent"]
        assert w["provider"] == "local" and w["results"][0]["manufacturer"] == "Würth", w
        for tool, args in (("parts_get", {"part": "X1", "provider": "boom"}), ("parts_alternatives", {"target": "X1", "provider": "boom"})):
            e = R.call_tool(tool, args)
            assert e["isError"] and e["structuredContent"]["error"] == "PROVIDER_FAILED", (tool, e["structuredContent"])
            assert "boom" in e["structuredContent"]["message"], e["structuredContent"]
    finally:
        if old is None:
            os.environ.pop("COPPER_HOME", None)
        else:
            os.environ["COPPER_HOME"] = old


def _edge_component(shapes) -> dict:
    comp = synthetic_component()
    comp["packageDetail"]["dataStr"]["shape"] += shapes
    return comp


def test_regress_easyeda_edge_cuts_graphics_move_to_a_documentation_layer():
    # found designing a board: a USB-C plug footprint carried its "board edge here" line on Edge.Cuts; KiCad
    # counts footprint Edge.Cuts as board outline, so placing the part corrupted the outline
    line = ["TRACK~1~10~~3970 3030 4030 3030~gge20~0"]                     # EasyEDA layer 10 = BoardOutLine
    text, meta = E.build_footprint(_edge_component(line), "TEST-PKG", "d", "t")
    assert "Edge.Cuts" not in text and text.count('(layer "Dwgs.User")') == 1, text
    assert "(start -7.62 7.62)" in text and "(end 7.62 7.62)" in text, text           # geometry kept, only re-layered
    e = meta["edge_cuts"]
    assert e == {"count": 1, "moved_to": "Dwgs.User", "bbox_mm": [-7.62, 7.62, 7.62, 7.62],
                 "board_edge": {"axis": "y", "at_mm": 7.62}}, e
    # every graphic kind that can land on the outline layer, and a shape that is not one straight edge
    many = line + ["CIRCLE~4000~3040~3~1~10~gge21~0", "RECT~3990~3035~20~10~10~gge22~0~1~none~~~",
                   "ARC~1~10~~M 3985 3010 A 5 5 0 0 1 3995 3010~~gge23~0", "SOLIDREGION~10~~M 3990 3050 L 4010 3050 L 4010 3060 Z~solid~gge24~~~~0"]
    text, meta = E.build_footprint(_edge_component(many), "TEST-PKG", "d", "t")
    assert "Edge.Cuts" not in text and meta["edge_cuts"]["count"] == 5 and "board_edge" not in meta["edge_cuts"], meta["edge_cuts"]
    # opt-out keeps real cuts; a footprint without any reports nothing
    text, meta = E.build_footprint(_edge_component(line), "TEST-PKG", "d", "t", edge_cuts="Edge.Cuts")
    assert text.count('(layer "Edge.Cuts")') == 1 and meta["edge_cuts"] is None, meta["edge_cuts"]
    assert E.build_footprint(synthetic_component(), "TEST-PKG", "d", "t")[1]["edge_cuts"] is None


def test_regress_import_reports_moved_edge_cuts_with_a_board_edge_hint():
    pr = _project()
    try:
        def imp(comp):
            fields = {"LCSC": "C1", "MPN": "TEST-PART", "Manufacturer": "", "Datasheet": "", "Description": "", "JLC Class": ""}
            return importer._import_easyeda(pr, "C1", comp, {"kind": "connector", "package": "SMD"}, fields, {"lcsc": "C1"},
                                            [], False, False)
        r = imp(_edge_component(["TRACK~1~10~~3970 3030 4030 3030~gge20~0"]))
        f = pr.dir / "libs" / "copper-lcsc.pretty" / (r["footprint"].split(":")[1] + ".kicad_mod")
        fp = f.read_text(encoding="utf-8")
        assert "Edge.Cuts" not in fp and "Dwgs.User" in fp, fp
        assert r["edge_cuts_moved"]["count"] == 1 and r["edge_cuts_moved"]["board_edge"] == {"axis": "y", "at_mm": 7.62}, r
        w = [x for x in r["warnings"] if "Edge.Cuts" in x]
        assert len(w) == 1 and "moved to Dwgs.User" in w[0] and "board edge" in w[0] and "y = 7.62 mm" in w[0], r["warnings"]
        # a footprint that was imported before the fix is rewritten clean by a plain re-import
        f.write_text(fp.replace('(layer "Dwgs.User")', '(layer "Edge.Cuts")'), encoding="utf-8")
        r2 = imp(_edge_component(["TRACK~1~10~~3970 3030 4030 3030~gge20~0"]))
        assert str(f) in r2["files_written"] and "Edge.Cuts" not in f.read_text(encoding="utf-8"), r2["files_written"]
        # nothing to report for an ordinary part
        r3 = imp(synthetic_component())
        assert "edge_cuts_moved" not in r3 and not [x for x in r3["warnings"] if "Edge.Cuts" in x], r3["warnings"]
    finally:
        shutil.rmtree(pr.dir, ignore_errors=True)


# ── regression: descriptive queries for long-tail parts ('alcohol gas sensor', 'MQ-3') ──
_GAS = dict(category="Sensors", subcategory="Gas Sensors")


def _catalogue():
    """A slice of the live catalogue as recorded on 2026-11-10 (descriptions shortened)."""
    return {
        "ags10": _r(lcsc="C3012632", mfr="AGS10", description="0ppm~100ppm 28mA Hazardous Gas I2C SMD-4P,5x4mm Gas Sensors ROHS",
                    package="SMD-4P,5x4mm", stock=1972, source="jlc", **_GAS),
        "scd40": _r(lcsc="C3659421", mfr="SCD40-D-R2", description="Air quality I2C LGA-20(10.1x10.1) Gas Sensors ROHS",
                    package="LGA-20", stock=2381, source="jlc", **_GAS),
        "alcohol": _r(lcsc="C7421564", mfr="SY-AG-37BM", category="Sensors", subcategory="Sensor Modules", stock=25, source="jlc",
                      description="17.8mm 20mA 5V Alcohol Sensor Temperature compensation UART、I2C - Sensor Modules ROHS"),
        "mq3b": _r(lcsc="C22397691", mfr="MQ-3B", category="Others", subcategory="New Arrivals", stock=0, source="jlc",
                   description="MQ-3B New Arrivals ROHS"),
        "mlcc": _r(lcsc="C53067", mfr="CL03A105MQ3CSNH", category="Capacitors", package="0201", source="jlc",
                   subcategory="Multilayer Ceramic Capacitors MLCC - SMD/SMT",
                   description="1uF 6.3V X5R ±20% 0201 Multilayer Ceramic Capacitors MLCC - SMD/SMT ROHS"),
        "mq360": _r(lcsc="C4015792", mfr="MQ360R0", category="Resistors", subcategory="Chip Resistor - Surface Mount", stock=0,
                    description="125mW 360Ω Metal Foil Resistor ±0.05% Chip Resistor - Surface Mount ROHS", source="jlc"),
        "fpga": _r(lcsc="C1521753", mfr="XC7A50T-2CPG236I", category="Embedded Processors & Controllers", package="CSBGA-236",
                   subcategory="Programmable Logic Device (CPLDs/FPGAs)", source="jlcsearch",
                   description="52160 CSBGA-236 Programmable Logic Device (CPLDs/FPGAs) ROHS"),
        "esp": _r(lcsc="C2838502", mfr="ESP32-C3-MINI-1-N4", category="IoT/Communication Modules", subcategory="WiFi Modules",
                  description="350mA 3V~3.6V Integrated temperature sensor、Integrated antenna SMD,13.2x16.6mm WiFi Modules ROHS"),
        # jlcsearch spells the sub-category in Title Case and its stock is a snapshot
        "aht20_js": _r(lcsc="C2757850", mfr="AHT20", category="Sensors", subcategory="Temperature And Humidity Sensor", stock=32141,
                       stock_source="jlcsearch-snapshot", source="jlcsearch",
                       description="0%RH~100%RH 2.2V~5.5V I2C ±0.3℃ ±2%RH SMD Temperature and Humidity Sensor ROHS"),
        "aht20": _r(lcsc="C2757850", mfr="AHT20", category="Sensors", subcategory="Temperature and Humidity Sensor", stock=30000,
                    stock_source="jlc-assembly", source="jlc",
                    description="0%RH~100%RH 2.2V~5.5V I2C ±0.3℃ ±2%RH SMD Temperature and Humidity Sensor ROHS"),
    }


class _FakeLive:
    """Stands in for the two live sources, answering like they did when recorded: JLC's keyword matcher only
    knows the catalogue's own wording ('gas sensors', never 'gas sensor'); jlcsearch reads descriptions."""

    def __init__(self):
        c = self.c = _catalogue()
        self.jlc_calls, self.js_calls = [], []
        self.jlc = {"gas sensors": [c["ags10"], c["scd40"]], "alcohol sensor": [c["alcohol"]],
                    "mq-3": [c["mlcc"]]}                       # in stock: only the Samsung MLCC that contains 'MQ3'
        self.jlc_any_stock = {"mq-3": [c["mq3b"], c["mq360"], c["mlcc"]], "mq-3b": [c["mq3b"]]}
        self.cats = {"Temperature and Humidity Sensor": [c["aht20"]]}
        self.js = {"gas sensor": [c["ags10"], c["fpga"]], "humidity sensor": [c["aht20_js"]]}

    def jlc_query(self, keyword, *, page_size=50, basic_only=False, preferred_only=False, in_stock=True, ttl=0, category=None):
        self.jlc_calls.append((keyword, in_stock, category))
        if category:
            return [dict(r) for r in self.cats.get(category, [])]
        rows = self.jlc.get(keyword.lower(), []) if in_stock else self.jlc_any_stock.get(keyword.lower(), [])
        return [dict(r) for r in rows]

    def jlcsearch_components(self, text, limit=30):
        self.js_calls.append(text)
        return [dict(r) for r in self.js.get(text.lower(), [])]

    def __enter__(self):
        self._orig = (search.jlc_query, search.jlcsearch_components)
        search.jlc_query, search.jlcsearch_components = self.jlc_query, self.jlcsearch_components
        return self

    def __exit__(self, *exc):
        search.jlc_query, search.jlcsearch_components = self._orig


def test_regress_descriptive_words_are_whole_words():
    c = _catalogue()
    sp = basic.parse_spec("alcohol gas sensor")
    words = search.query_words(sp, "alcohol gas sensor")
    assert words == ["alcohol", "gas", "sensor"], words
    # a gas sensor that does not say 'alcohol' is a PARTIAL answer and says which word is missing
    assert search.describe_score(c["ags10"], sp, words) == (1, ["alcohol"])
    assert search.describe_score(c["alcohol"], sp, words) == (1, ["gas"])
    # one shared word is no match at all: the seed table used to answer this query with an ESP32 ('temperature sensor')
    assert search.describe_score(c["esp"], sp, words)[0] == 9
    two = basic.parse_spec("gas sensor")
    assert search.describe_score(c["ags10"], two, ["gas", "sensor"]) == (0, [])
    assert search.describe_score(c["fpga"], two, ["gas", "sensor"])[0] == 9        # 'gas' is not in 'CSBGA'
    assert search.describe_score(c["esp"], two, ["gas", "sensor"])[0] == 9
    # the category counts as description ('module' is only in 'WiFi Modules'), plural or not
    m = basic.parse_spec("ESP32 module")
    assert search.query_words(m, "ESP32 module") == ["module"]
    assert search.describe_score(c["esp"], m, ["module"]) == (0, [])
    assert search.describe_score(c["ags10"], m, ["module"])[0] == 9               # neither ESP32 nor a module
    # 'socket' with no connector kind is a describing word (a synonym of the catalogue's 'connector'), not a gender filter
    sd = _r(lcsc="C266603", mfr="SD-106M", category="Connectors", subcategory="SD Card / Memory Card Connector",
            description="Push-Push Standard SD Card SMD SD Card / Memory Card Connector ROHS")
    xtal = _r(lcsc="C12674", mfr="X49SM8MSD2SC", category="Crystals", subcategory="Crystals", description="8MHz HC-49S-SMD Crystals")
    s3 = basic.parse_spec("SD card socket")
    w3 = search.query_words(s3, "SD card socket")
    assert w3 == ["sd", "card", "socket"] and search.describe_score(sd, s3, w3) == (0, []), w3
    assert search.describe_score(xtal, s3, w3)[0] == 9                           # 'sd' is not in 'X49SM8MSD2SC'
    # a rating in a description is confirmed or the result is partial — never silently exact
    rl = basic.parse_spec("relay 5V")
    r5 = _r(lcsc="C35449", mfr="SRD-05VDC-SL-C", category="Relays", subcategory="Power Relays", description="15A 5V 70Ω Power Relays")
    r24 = _r(lcsc="C52146266", mfr="KT78-124LM-S", category="Relays", subcategory="Power Relays", description="24V Power Relays")
    assert search.describe_score(r5, rl, ["relay"], search.soft_terms(rl, "relay 5V")) == (0, [])
    assert search.describe_score(r24, rl, ["relay"], search.soft_terms(rl, "relay 5V")) == (1, ["5v"])
    # structured specs keep their own matcher: nothing here turns '10k 0603' into words
    assert search.query_words(basic.parse_spec("10k 0603"), "10k 0603") == []
    assert search.keyword_variants(["gas", "sensor"]) == ["gas sensor", "gas sensors"]
    v = search.keyword_variants(["alcohol", "gas", "sensor"])
    assert v[:2] == ["alcohol gas sensor", "alcohol gas sensors"] and {"gas sensor", "alcohol sensor", "gas sensors"} <= set(v), v
    assert search._toggle_plural("battery") == "batteries" and search._toggle_plural("relays") == "relay"
    assert search._toggle_plural("switch") == "switches" and search._singular("gas") == "gas"
    # category filter: catalogue wording in any number, as well as a kind
    assert search._category_match("sensor", c["ags10"]) and search._category_match("gas sensor", c["ags10"])
    assert search._category_match("wifi module", c["esp"]) and not search._category_match("gas sensor", c["esp"])
    assert search._category_match("capacitor", c["mlcc"])


def test_regress_short_part_number_is_not_a_substring():
    sp = basic.parse_spec("MQ-3")
    assert sp.get("mpn") == "MQ-3"
    c = _catalogue()
    assert search.describe_score(c["mq3b"], sp)[0] == 0                            # the family member
    assert search.describe_score(c["mlcc"], sp)[0] == 9                            # CL03A105-MQ3-CSNH: a coincidence
    assert search.describe_score(c["mq360"], sp)[0] == 9                           # MQ360R0: the number goes on
    led = _r(lcsc="C7284158", mfr="GW J9LHS1.4M-C1C7-2+58+MQ+30A-1-100-R33", category="Optoelectronics", subcategory="RGB LEDs")
    assert search.describe_score(led, sp)[0] == 9
    # longer or separator-bounded numbers are untouched
    for q, mfr in (("AMS1117", "AMS1117-3.3"), ("ESP32-C3", "ESP32-C3-MINI-1-N4"), ("ESP32", "ESP32-WROOM-32E-N4"),
                   ("CH340", "CH340C"), ("STM32F103", "STM32F103C8T6")):
        r = _r(lcsc="C1", mfr=mfr, category="ICs", subcategory="ICs", description=mfr)
        assert search.describe_score(r, basic.parse_spec(q))[0] <= 1, (q, mfr)
    co2 = _r(lcsc="C2982917", mfr="SY-CO2-24BPT", description="Carbon dioxide TO-39-4 Gas Sensors", **_GAS)
    assert search.describe_score(co2, basic.parse_spec("co2 sensor"), ["sensor"])[0] == 0


def test_regress_descriptive_search_asks_in_catalogue_wording():
    live = ("jlc", "jlcsearch")
    with _FakeLive() as fk:
        r = search.search("gas sensor", limit=5, sources=live, expand=True)
        assert [k for k, _s, c in fk.jlc_calls if not c] == ["gas sensor", "gas sensors"], fk.jlc_calls
        assert {x["lcsc"] for x in r["results"]} == {"C3659421", "C3012632"}, r["results"]
        assert all("match" not in x for x in r["results"]) and "match_note" not in r, r
        assert "also asked 'gas sensors'" in r["sources"]["jlc"], r["sources"]
    with _FakeLive() as fk:                                # without expand (internal callers): one query, as before
        r = search.search("gas sensor", limit=5, sources=live)
        assert [k for k, _s, _c in fk.jlc_calls] == ["gas sensor"] and "C1521753" not in [x["lcsc"] for x in r["results"]], r
    with _FakeLive() as fk:
        r = search.search("alcohol gas sensor", limit=5, sources=live, expand=True)
        asked = [k for k, _s, c in fk.jlc_calls if not c]
        assert "gas sensors" in asked and "alcohol sensor" in asked and len(asked) <= 1 + search.JLC_VARIANT_CALLS, asked
        assert len(fk.jlc_calls) - len(asked) <= search.JLC_CATEGORY_CALLS, fk.jlc_calls      # the live budget is bounded
        res = r["results"]
        # the part that confirms the query's rarest word leads; every result is marked partial with what it lacks
        assert res[0]["lcsc"] == "C7421564" and res[0]["match"] == "partial" and res[0]["unconfirmed"] == ["gas"], res[0]
        assert {x["lcsc"] for x in res} == {"C7421564", "C3012632", "C3659421"}, res
        assert all(x["match"] == "partial" and x["unconfirmed"] for x in res) and "partial" in r["match_note"], r
        assert r["best_relevance"] == 1 and r["words"] == ["alcohol", "gas", "sensor"]


def test_regress_part_number_family_and_out_of_stock():
    live = ("jlc", "jlcsearch")
    with _FakeLive() as fk:
        r = search.search("MQ-3", limit=5, sources=live, expand=True)
        # nothing in stock carries the number: no coincidental MLCC as a 'partial' hit, and the real part is reported apart
        assert r["results"] == [] and [x["lcsc"] for x in r["out_of_stock"]] == ["C22397691"], r
        assert r["out_of_stock"][0]["warning"] == "out of JLC assembly stock" and "match" not in r["out_of_stock"][0]
        assert ("MQ-3", False, None) in fk.jlc_calls
    with _FakeLive():
        r = search.search("MQ-3 gas sensor", limit=5, sources=live, expand=True)
        res = r["results"]
        assert {x["lcsc"] for x in res} == {"C3012632", "C3659421"}, res
        assert all(x["match"] == "partial" and x["unconfirmed"] == ["MQ-3"] for x in res), res
        assert "MQ-3" in r["match_note"] and [x["lcsc"] for x in r["out_of_stock"]] == ["C22397691"], r
    with _FakeLive():                                      # in_stock=false lists it as a normal (warned) result
        r = search.search("MQ-3B", {"in_stock": False}, limit=5, sources=live, expand=True)
        assert [x["lcsc"] for x in r["results"]] == ["C22397691"] and "out_of_stock" not in r, r
        assert r["results"][0]["warning"] == "out of JLC assembly stock"


def test_regress_descriptive_search_falls_back_to_description_source_then_category():
    with _FakeLive() as fk:
        r = search.search("humidity sensor", limit=5, sources=("jlc", "jlcsearch"), expand=True)
        # JLC's keyword matcher knows neither wording; jlcsearch finds it by description; the category those parts
        # live in (in the catalogue's own spelling, recovered from the description) is then asked from JLC for live stock
        assert fk.js_calls == ["humidity sensor"], fk.js_calls
        assert ("", True, "Temperature and Humidity Sensor") in fk.jlc_calls, fk.jlc_calls
        top = r["results"][0]
        assert top["lcsc"] == "C2757850" and top["stock"] == 30000 and "stock_note" not in top and "match" not in top, top
        assert "category 'Temperature and Humidity Sensor'" in r["sources"]["jlc"], r["sources"]


def test_regress_parts_search_tool_reports_partial_and_out_of_stock():
    import copper_kicad.tools  # noqa: F401
    from copper_kicad.mcp import registry as R
    assert "unconfirmed" in R.TOOLS["parts_search"].description and "out_of_stock" in R.TOOLS["parts_search"].description
    with _FakeLive():
        out = R.call_tool("parts_search", {"query": "MQ-3", "limit": 5})
        sc = out["structuredContent"]
        assert not out.get("isError") and sc["results"] == [] and sc["out_of_stock"][0]["id"] == "C22397691", sc
        assert "C22397691" in sc["next"] and "in_stock=false" in sc["next"] and "parts_alternatives" in sc["next"], sc["next"]
        sc = R.call_tool("parts_search", {"query": "alcohol gas sensor", "limit": 5, "live": True})["structuredContent"]
        top = sc["results"][0]
        assert top["match"] == "partial" and top["unconfirmed"] and sc["match_note"], top
        assert sc["next"].startswith("check it first: parts_get(part='%s')" % top["lcsc"]), sc["next"]
        assert "ESP32" not in json.dumps(sc["results"]), sc["results"]
        # a description is not a spec: nothing is auto-picked unless a part confirms every word
        from copper_kicad.parts.providers.lcsc import LcscProvider
        assert LcscProvider().pick("alcohol gas sensor") is None
        p = LcscProvider().pick("gas sensor")
        assert p and p["id"] in ("C3012632", "C3659421") and p["match"] == "exact", p


if __name__ == "__main__":
    _tmp_home()
    passed = failed = skipped = 0
    for name, fn in sorted((k, v) for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            passed += 1
            print(f"PASS {name}")
        except Skip as e:
            skipped += 1
            print(f"SKIP {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
