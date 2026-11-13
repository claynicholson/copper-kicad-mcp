"""Stock-symbol selection tests (parts.stock): KiCad's own symbol whenever a correct one exists, the converted one
otherwise — and NEVER a stock symbol with the wrong pinout.

Offline: the supplier drawings are fixtures built here (an EasyEDA-format component whose pads are copied from a
KiCad footprint, so pad geometry is real), nothing is fetched. The KiCad stock libraries are the real ones; tests
that need them skip without KiCad. Run with `python -m pytest tests/test_stock_symbols.py` or
`python tests/test_stock_symbols.py`.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

# never the real ~/.copper or the real KiCad config: both must be set before copper_kicad is imported
os.environ.setdefault("COPPER_HOME", tempfile.mkdtemp(prefix="copper-test-home-"))
os.environ.setdefault("KICAD_CONFIG_HOME", tempfile.mkdtemp(prefix="copper-test-kicfg-"))
from copper_kicad.parts import common as C, easyeda as E, importer, stock as S  # noqa: E402
from copper_kicad.mcp.registry import ToolError  # noqa: E402


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
        if importer.stock_symbol("Device:R") is None:
            raise RuntimeError("stock symbol libraries not found")
        return env
    except Skip:
        raise
    except Exception as e:  # noqa: BLE001
        skip(f"KiCad not available: {e}")


# ── fixtures: a supplier (EasyEDA) drawing built from pin names and real pad geometry ─────────────────────────
U = 0.254


def _ee_pin(i: int, num: str, name: str) -> str:
    y = -10 * (i + 1)
    return (f"P~show~0~{num}~-30~{y}~180~gge{i + 2}~0^^-30~{y}^^M -30 {y} h 10~#880000^^1~-17~{y + 4}~0~{name}~start~~~#0000FF"
            f"^^1~-24~{y - 1}~0~{num}~end~~~#0000FF^^0~-27~{y}^^0~M -20 {y + 3} L -17 {y} L -20 {y - 3}")


def _ee_pad(i: int, p: dict) -> str:
    cx, cy = 4000 + p["x"] / U, 3000 + p["y"] / U
    w, h = 2 * p["hw"] / U, 2 * p["hh"] / U
    pts = f"{cx - w / 2} {cy - h / 2} {cx + w / 2} {cy - h / 2} {cx + w / 2} {cy + h / 2} {cx - w / 2} {cy + h / 2}"
    if p["type"] == "thru_hole":
        return f"PAD~ELLIPSE~{cx}~{cy}~{w}~{h}~11~~{p['n']}~{min(w, h) / 4}~~0~gge{100 + i}~0~~Y~0~0~0.2~{cx},{cy}"
    return f"PAD~RECT~{cx}~{cy}~{w}~{h}~1~~{p['n']}~0~{pts}~0~gge{100 + i}~0~~Y~0~0~0.2~{cx},{cy}"


def component(mpn: str, pins: dict, pads: list[dict], title="FIX-PKG") -> dict:
    """EasyEDA-format component: pins {number: name}, pads [{'n','type','x','y','hw','hh'}] (mm)."""
    body = ["R~-20~-200~2~2~40~210~#880000~1~0~none~gge1~0~"]
    return {"title": mpn, "dataStr": {"head": {"x": 0, "y": 0, "c_para": {"pre": "U?", "Manufacturer Part": mpn}},
                                      "shape": body + [_ee_pin(i, n, nm) for i, (n, nm) in enumerate(pins.items())]},
            "packageDetail": {"title": title, "uuid": "fixture-" + mpn,
                              "dataStr": {"head": {"x": 4000, "y": 3000}, "shape": [_ee_pad(i, p) for i, p in enumerate(pads)]}}}


def kpads(fp_id: str, renumber: dict | None = None, rot180=False, scale=1.0) -> list[dict]:
    """The pads of a KiCad footprint as supplier pads (optionally renumbered / turned 180° / scaled)."""
    out = []
    for p in S.footprint_pads(fp_id) or []:
        q = dict(p)
        q["n"] = (renumber or {}).get(p["n"], p["n"])
        q["x"], q["y"] = q["x"] * scale, q["y"] * scale
        if rot180:
            q["x"], q["y"] = -q["x"], -q["y"]
        out.append(q)
    assert out, fp_id
    return out


def ref_of(mpn: str, pins: dict, pads: list[dict], kind="", title="FIX-PKG") -> dict:
    ref = importer.supplier_ref(component(mpn, pins, pads, title), kind)
    assert ref is not None and set(ref["pins"]) == set(pins), ref
    return ref


def rec_of(mpn: str, package: str, kind: str, desc: str = "", sub: str = "") -> dict:
    return {"lcsc": "C9999999", "mfr": mpn, "package": package, "kind": kind, "description": desc, "subcategory": sub,
            "manufacturer": "Fixture", "basic": False, "preferred": False, "class": "Extended", "datasheet": "", "typed": {}}


def choose(rec, ref):
    return S.select(rec, None, (lambda: ref) if ref is not None else None)


def ev(lib_id, rec, ref, match="exact", **kw):
    return S.evaluate(dict({"lib_id": lib_id, "match": match, "why": "test"}, **kw), rec, ref)


SOT23 = "Package_TO_SOT_SMD:SOT-23"
SOIC8 = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
NPN = "NPN 25V 500mA 300mW SOT-23 Bipolar (BJT)"
NMOS = "1 N-channel 20V 3A SOT-23 MOSFETs"


# ══ pure logic (no KiCad) ════════════════════════════════════════════════
def test_pin_name_agreement():
    a = S.agree
    assert a("VI", "VIN") == "same" and a("VO", "VOUT(TAB)") == "same" and a("GND", "ADJ(GND)") == "same"
    assert a("V_{IN}", "VIN") == "same" and a("~{CS}", "CS#") == "same" and a("~{SLEEP}", "nSleep") == "same"
    assert a("~{WP}/IO_{2}", "IO2") == "same" and a("GPIO26/ADC0", "GPIO26_ADC0") == "same"
    assert a("PA13", "PA13(SWDIO)") == "same" and a("PF0", "PF0-OSC_IN(PF0)") == "same" and a("VDD", "VDD_1") == "same"
    assert a("K", "C") == "same" and a("FB", "FEEDBACK") == "same" and a("B", "C") == "differs"
    assert a("XTAL1/PB6", "(PCINT6/XTAL1/TOSC1)PB6") == "same" and a("D+", "DP1") == "same" and a("SHIELD", "EH") == "same"
    # op-amp sign pins and supplies
    assert a("+", "1IN+") == "same" and a("-", "IN2-") == "same" and a("+", "+INA") == "same"
    assert a("+", "1IN-") == "differs" and a("-", "OUT1") == "differs" and a("+", "VCC+") == "differs"
    assert a("V+", "VCC") == "same" and a("V-", "VCC-/GND") == "same" and a("V-", "-VSS") == "same"
    # things that must NOT pass as the same pin
    assert a("VI", "GND") == "clash" and a("GND", "VIN") == "clash" and a("V+", "GND") == "clash"
    assert a("PA1", "PA13") == "differs" and a("OUT1", "OUT2") == "differs" and a("USB_DP", "USB_DM") == "differs"
    assert a("E", "B") == "differs" and a("K", "REF") == "differs" and a("IO1", "IO19") == "differs"
    assert a("IN1", "VCC") == "differs", "an indexed IN is a signal, not the supply"
    # no information / no-connect
    assert a("", "GND") == "unknown" and a("K", "") == "unknown" and a("K", "1", "1") == "unknown"
    assert a("NC", "NC.") == "same" and a("NC", "R232") == "soft" and a("EN", "NC") == "soft"
    assert a("GND", "EP") == "unknown" and a("GNDPAD", "PowerPAD") == "same"


def test_compare_names_flags_permutation_clash_and_direction():
    sym = {"pins": {"1": "E", "2": "B", "3": "C"}, "types": {}}
    r = S.compare_names(sym, {"1": "B", "2": "E", "3": "C"})
    assert len(r["permuted"]) == 2 and r["same"] == 1, r
    sym = {"pins": {"1": "VI", "2": "GND", "3": "VO"}, "types": {}}
    r = S.compare_names(sym, {"1": "GND", "2": "OUT", "3": "IN"})
    assert r["clash"], r
    # unnamed gate pins: only the electrical type can tell an input from an output
    sym = {"pins": {"1": "", "2": ""}, "types": {"1": "input", "2": "output"}}
    assert not S.compare_names(sym, {"1": "1A", "2": "1Y"})["type"]
    assert len(S.compare_names(sym, {"1": "1Y", "2": "1A"})["type"]) == 2


def test_unit_check():
    lm358 = {"pins": {"1": "", "2": "-", "3": "+", "5": "+", "6": "-", "7": "", "4": "V-", "8": "V+"},
             "types": {"1": "output", "2": "input", "3": "input", "5": "input", "6": "input", "7": "output", "4": "power_in", "8": "power_in"},
             "units": {1: ["3", "2", "1"], 2: ["5", "6", "7"], 3: ["4", "8"]}}
    good = {"1": "1OUT", "2": "1IN-", "3": "1IN+", "4": "GND", "5": "2IN+", "6": "2IN-", "7": "2OUT", "8": "VCC"}
    u = S.unit_check(lm358, good, 1)
    assert u["ok"] and u["confirmed"] == {"1", "2", "3", "5", "6", "7"}, u
    assert S.unit_check(lm358, good, 2)["ok"] and S.unit_check(lm358, good, 3)["ok"]      # 2 amplifiers (+ power unit)
    assert not S.unit_check(lm358, good, 4)["ok"], "a quad drawing is not a dual symbol"
    mixed = dict(good, **{"1": "2OUT", "7": "1OUT"})                                        # outputs of the two halves swapped
    u = S.unit_check(lm358, mixed, 1)
    assert not u["ok"] and "mixes supplier channels" in u["notes"][0], u
    # single-unit symbols and units that are not repeats (an MCU split into port blocks) are left alone
    mcu = {"pins": {"1": "PA1", "2": "PA2", "3": "PB1"}, "types": {}, "units": {1: ["1", "2"], 2: ["3"]}}
    assert S.unit_check(mcu, {"1": "PA1", "2": "PA2", "3": "PB1"}, 1)["ok"]


def _p(n, x, y, w=0.6, h=1.0, t="smd"):
    return {"n": str(n), "type": t, "x": x, "y": y, "hw": w / 2, "hh": h / 2}


def test_pad_geometry():
    sot23 = [_p(1, -0.95, 1.1), _p(2, 0.95, 1.1), _p(3, 0, -1.1)]
    # another land pattern of the same package, turned 180°: fits, numbers on the same pads
    other = [_p(1, 0.95, -1.0, 0.7, 1.2), _p(2, -0.95, -1.0, 0.7, 1.2), _p(3, 0, 1.0, 0.7, 1.2)]
    g = S.match_pads(sot23, other)
    assert g["ok"] and g["rotation"] == 180 and g["pairs"] == [("1", "1"), ("2", "2"), ("3", "3")], g
    # same pads, pins 1 and 2 swapped: geometry fits but the numbering is reported, never hidden by a rotation
    swapped = [_p(2, 0.95, -1.0), _p(1, -0.95, -1.0), _p(3, 0, 1.0)]
    g = S.match_pads(sot23, swapped)
    assert g["ok"] and ("1", "2") in g["pairs"] and ("2", "1") in g["pairs"], g
    # SOT-323 under a SOT-23 footprint: pads overlap, the pitch gives it away
    sot323 = [_p(1, -0.65, 0.95, 0.5, 0.9), _p(2, 0.65, 0.95, 0.5, 0.9), _p(3, 0, -0.95, 0.5, 0.9)]
    g = S.match_pads(sot23, sot323)
    assert not g["ok"], g
    # through-hole part under an SMD footprint
    g = S.match_pads(sot23, [_p(1, -0.95, 1.1, t="thru_hole"), _p(2, 0.95, 1.1, t="thru_hole"), _p(3, 0, -1.1, t="thru_hole")])
    assert not g["ok"] and "mounting" in g["reason"], g
    # narrow SOIC-8 footprint for a wide (208 mil) body
    narrow = [_p(i + 1, -2.475, -1.905 + 1.27 * i, 1.95, 0.6) for i in range(4)] + [_p(8 - i, 2.475, -1.905 + 1.27 * i, 1.95, 0.6) for i in range(4)]
    wide = [dict(p, x=p["x"] * 3.6 / 2.475) for p in narrow]
    assert S.match_pads(narrow, narrow)["ok"] and not S.match_pads(narrow, wide)["ok"]
    # a supplier pad the stock footprint lacks is reported for the caller to judge
    g = S.match_pads(sot23, sot23 + [_p(4, 3.0, 0)])
    assert g["ok"] and g["uncovered"] == ["4"], g


def test_custom_pad_extent_is_its_copper():
    text = ('(footprint "X" (pad "2" smd custom (at -1.86 0) (size 1.48 0.9) (layers "F.Cu") (primitives (gr_poly (pts '
            '(xy 0.74 0.87) (xy 3.86 0.87) (xy 3.86 -0.87) (xy 0.74 -0.87)) (width 0) (fill yes))))\n'
            ' (pad "1" smd rect (at 0 2 90) (size 2 1) (layers "F.Cu")) (pad "" np_thru_hole circle (at 0 0) (size 1 1) (drill 1)))')
    pads = {p["n"]: p for p in S.pads_of(text)}
    assert set(pads) == {"1", "2"}
    assert abs(pads["2"]["x"] - (-0.3)) < 1e-6 and abs(pads["2"]["hw"] - 2.3) < 1e-6, pads["2"]      # anchor + polygon
    assert abs(pads["1"]["hw"] - 0.5) < 1e-6 and abs(pads["1"]["hh"] - 1.0) < 1e-6, pads["1"]        # rotated 90°


def test_tvs_direction_never_guesses():
    f = importer.stock_candidate
    uni = {"kind": "tvs", "package": "SOD-123FL", "mfr": "SMF5.0A", "description": "5V 9.2V 200W SOD-123FL TVS"}
    bi = dict(uni, mfr="SMF5.0CA")
    said = {"kind": "esd", "package": "SOD-323", "mfr": "H5VLD3B", "description": "Bidirectional 5V ESD"}
    said_uni = dict(said, description="Unidirectional 5V ESD", mfr="XYZ5")
    unknown = dict(said, description="5V ESD protection", mfr="XYZ5")
    assert importer.tvs_unidirectional(uni) is True and importer.tvs_unidirectional(bi) is False
    assert f(bi)["lib_id"] == "Device:D_TVS" and f(said)["lib_id"] == "Device:D_TVS"
    # Device:D_TVS has pins A1/A2 and no polarity: a unidirectional part must not get it
    assert f(uni)["lib_id"] == "Device:D_Zener" and "pin 1 = K" in f(uni)["why"]
    assert f(said_uni)["lib_id"] == "Device:D_Zener"
    assert f(unknown) is None, "direction not stated → no rule candidate (fall through to the supplier data)"


def test_mpn_heads_grid_and_positions():
    assert S._heads("BAT54C,215") == ["BAT54C,215", "BAT54C"]
    assert S._heads("S8050 J3Y(RANGE:200-350)")[-1] == "S8050" and S._heads("MMBT3904(RANGE:100-300)")[-1] == "MMBT3904"
    row = [_p(i + 1, 2.54 * i, 0, t="thru_hole") for i in range(6)]
    assert S._grid(row) == {"rows": 1, "per": 6, "pitch": 2.54, "scheme": ""}
    assert S._grid(row[::-1])["rows"] == 1
    oe = [_p(2 * i + 1 + r, 2.54 * r, 2.54 * i, t="thru_hole") for i in range(3) for r in range(2)]
    assert S._grid(oe)["scheme"] == "Odd_Even" and S._grid(oe)["per"] == 3
    ccw = [_p(i + 1, 0, 2.54 * i) for i in range(4)] + [_p(8 - i, 7.62, 2.54 * i) for i in range(4)]
    assert S._grid(ccw)["scheme"] == "Counter_Clockwise"
    tb = [_p(i + 1, 2.54 * i, 0) for i in range(4)] + [_p(5 + i, 2.54 * i, 2.54) for i in range(4)]
    assert S._grid(tb)["scheme"] == "Top_Bottom"
    assert S._grid(row[:4] + [_p(5, 3.0, 4.0), _p(6, 8.0, 4.0)]) is None, "two contacts + mechanical pads is not a 1x6"
    assert S._grid([_p(1, 0, 0), _p(2, 2.54, 0), _p(3, 6.0, 0)]) is None, "irregular pitch"
    assert S._grid([_p(1, 0, 0), _p(3, 2.54, 0), _p(2, 5.08, 0)]) is None, "numbers not in row order"
    assert S._positions({"description": "1 1x6P 2.54mm 3A Pin Headers", "mfr": "X"}) == 6
    assert S._positions({"description": "2 2.54mm 6P Black", "mfr": "2.54-2*3PPin"}) == 6
    assert S._positions({"description": "1x2P 2 250V 2A 2P 2mm", "mfr": "WAFER-PH2.0-2PWB"}) == 2


# ══ against the real stock libraries ═════════════════════════════════════
def test_stock_symbol_reader():
    _kicad()
    r = importer.stock_symbol("Device:R")
    assert r["pins"] == {"1": "", "2": ""} and r["unit_count"] == 1
    esp = importer.stock_symbol("RF_Module:ESP32-WROOM-32E")
    if esp and esp["stacked"]:          # KiCad 10: one pin numbered '[1,15,38,39]'
        assert {"1", "15", "38", "39"} <= set(esp["pins"]) and not any(n.startswith("[") for n in esp["pins"])
        assert esp["pins"]["1"] == esp["pins"]["39"] == "GND"
    op = importer.stock_symbol("Amplifier_Operational:LM358")
    assert op["unit_count"] == 3 and sorted(op["units"][1]) == ["1", "2", "3"] and sorted(op["units"][3]) == ["4", "8"]
    assert op["types"]["1"] == "output" and op["pins"]["3"] == "+" and op["fp_filters"]
    ams = importer.stock_symbol("Regulator_Linear:AMS1117-3.3")          # a derived symbol: pins come from its parent
    assert ams["pins"] == {"1": "GND", "2": "VO", "3": "VI"} and ams["footprint"].endswith("SOT-223-3_TabPin2")
    # exact cover: no pin without a pad AND no numbered pad without a pin
    ok, notes, _ = importer.verify_stock("Device:R", SOT23, {}, None)
    assert not ok and "pads ['3'] have no pin" in notes[0], notes
    ok, notes, _ = importer.verify_stock("Transistor_FET:Q_NMOS_GSD", "Resistor_SMD:R_0603_1608Metric", {}, None)
    assert not ok and "have no pad" in notes[0], notes
    assert importer.verify_stock("Transistor_FET:Q_NMOS_GSD", SOT23, {"package": "SOT-23"}, None)[0]


def test_mpn_candidates():
    _kicad()
    def c(mpn):
        return {x["lib_id"]: x["match"] for x in S.mpn_candidates(mpn)}
    assert c("AMS1117-3.3")["Regulator_Linear:AMS1117-3.3"] == "exact"
    assert c("BAT54C,215")["Diode:BAT54C"] == "exact" and c("MMBT3904(RANGE:100-300)")["Transistor_BJT:MMBT3904"] == "exact"
    assert c("STM32F103C8T6")["MCU_ST_STM32F1:STM32F103C8Tx"] == "exact"
    assert c("STM32G030F6P6TR")["MCU_ST_STM32G0:STM32G030F6Px"] == "exact", "wildcard + packaging suffix"
    assert c("TPS5430DDAR")["Regulator_Switching:TPS5430DDA"] == "exact"
    assert c("LM358DR2G")["Amplifier_Operational:LM358"] == "family"
    assert c("ESP32-WROOM-32E-N4")["RF_Module:ESP32-WROOM-32E"] == "family"
    assert c("MCP6002T-I/SN").get("Amplifier_Operational:MCP6002-xSN") == "family", "an x after a hyphen is a wildcard too"
    assert c("TP4056").get("Battery_Management:TP4056-42-ESOP8") == "family"
    assert c("78L05G-AB3-R").get("Regulator_Linear:L78L05_SOT89") == "family", "the same part under a vendor prefix"
    assert "Transistor_BJT:SS8050" not in c("S8050"), "an S8050 is not an SS8050"
    assert c("AP7361C-33ER-13").get("Regulator_Linear:AP7361C-33E"), "the tempting neighbour IS offered — and must fail the pin check"
    assert c("") == {} and c("ZZZ-NO-SUCH-PART-123") == {}
    # a stock name that ends inside a number of the MPN is another part: ABS210 is not Diode_Bridge:ABS2
    assert not any(k.endswith(":ABS2") for k in c("ABS210")), c("ABS210")
    assert c("BC817-40").get("Transistor_BJT:BC817") == "family" and c("LM2596S-5.0").get("Regulator_Switching:LM2596S-5") == "family"


def test_generic_transistor_follows_the_real_pin_order():
    _kicad()
    rec = rec_of("SI2302", "SOT-23", "mosfet", NMOS)
    ref = ref_of("SI2302", {"1": "G", "2": "S", "3": "D"}, kpads(SOT23, rot180=True), "mosfet")
    ch = choose(rec, ref)["choice"]
    assert ch["lib_id"] == "Transistor_FET:Q_NMOS_GSD" and ch["footprint"] == SOT23 and ch["footprint_source"] == "stock", ch
    assert ch["match"] == "generic" and "pad geometry" in ch["verified"]
    # the same package with source and gate on the other pins: another symbol, and GSD is refused for it
    ref2 = ref_of("XX2302", {"1": "S", "2": "G", "3": "D"}, kpads(SOT23), "mosfet")
    ch2 = choose(rec_of("XX2302", "SOT-23", "mosfet", NMOS), ref2)["choice"]
    assert ch2["lib_id"] == "Transistor_FET:Q_NMOS_SGD", ch2
    bad = ev("Transistor_FET:Q_NMOS_GSD", rec, ref2, match="generic", names="check", footprints=[SOT23])
    assert not bad["ok"] and "permuted" in bad["notes"][0], bad
    # P-channel, PNP, and things that are not a plain transistor
    assert choose(rec_of("XP", "SOT-23", "mosfet", "1 P-Channel 30V 4A MOSFETs"), ref)["choice"]["lib_id"] == "Transistor_FET:Q_PMOS_GSD"
    bjt = ref_of("XB", {"1": "B", "2": "E", "3": "C"}, kpads(SOT23), "bjt")
    assert choose(rec_of("XB", "SOT-23", "bjt", "PNP 25V SOT-23"), bjt)["choice"]["lib_id"] == "Transistor_BJT:Q_PNP_BEC"
    for desc in ("NPN Pre-Biased digital transistor", "NPN Darlington", "2 N-Channel dual MOSFETs array", "N-Channel + P-Channel complementary"):
        k = "bjt" if "NPN" in desc else "mosfet"
        r = ref_of("XD", {"1": "B", "2": "E", "3": "C"} if k == "bjt" else {"1": "G", "2": "S", "3": "D"}, kpads(SOT23), k)
        assert choose(rec_of("XD", "SOT-23", k, desc), r)["choice"] is None, desc
    # polarity may come from the catalogue attributes when the description is empty; without either: no guess
    attr = dict(rec_of("2SC1623", "SOT-23", "bjt"), attributes={"Number": "1 NPN", "type": "NPN"})
    assert choose(attr, ref_of("2SC1623", {"1": "B", "2": "E", "3": "C"}, kpads(SOT23), "bjt"))["choice"]["lib_id"] == "Transistor_BJT:Q_NPN_BEC"
    assert choose(rec_of("2SC1623", "SOT-23", "bjt"), ref_of("2SC1623", {"1": "B", "2": "E", "3": "C"}, kpads(SOT23), "bjt"))["choice"] is None
    two = dict(attr, attributes={"Number": "2 NPN", "type": "NPN"})
    assert choose(two, ref_of("2SC1623", {"1": "B", "2": "E", "3": "C"}, kpads(SOT23), "bjt"))["choice"] is None
    # unnamed supplier pins: the pin order is unknown, no generic symbol
    assert choose(rec_of("XU", "SOT-23", "mosfet", NMOS), ref_of("XU", {"1": "", "2": "", "3": ""}, kpads(SOT23), "mosfet"))["choice"] is None


def test_negative_to92_symbol_for_a_sot23_transistor():
    """Transistor_BJT:S8050 is the TO-92 part (1=E 2=B 3=C). The SOT-23 S8050 is 1=B 2=E 3=C."""
    _kicad()
    rec = rec_of("S8050", "SOT-23", "bjt", NPN)
    ref = ref_of("S8050", {"1": "B", "2": "E", "3": "C"}, kpads(SOT23), "bjt")
    bad = ev("Transistor_BJT:S8050", rec, ref)
    assert not bad["ok"] and "permuted" in " ".join(bad["notes"]), bad
    sel = choose(rec, ref)
    assert sel["choice"]["lib_id"] == "Transistor_BJT:Q_NPN_BEC" and sel["choice"]["footprint"] == SOT23, sel["choice"]
    assert [t["lib_id"] for t in sel["tried"] if not t["ok"]] == ["Transistor_BJT:S8050"], sel["tried"]
    # even when the supplier names nothing, the through-hole footprint cannot pass for the SMD package
    blank = ref_of("S8050", {"1": "", "2": "", "3": ""}, kpads(SOT23), "bjt")
    bad = ev("Transistor_BJT:S8050", rec, blank)
    assert not bad["ok"], bad
    assert choose(rec, blank)["choice"] is None
    # without any supplier drawing: the TO-92 default footprint does not name the SOT-23 package → refused
    sel = choose(rec, None)
    assert sel["choice"] is None and "TO-92" in sel["fallback"], sel


def test_negative_reverse_pinout_regulator():
    """AP7361C-33ER (SOT-223R: GND/OUT/IN) must not get Regulator_Linear:AP7361C-33E (IN/GND/OUT)."""
    _kicad()
    fp = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    rec = rec_of("AP7361C-33ER-13", "SOT-223R", "ldo")
    ref = ref_of("AP7361C-33ER-13", {"1": "GND", "2": "OUT", "3": "IN"}, kpads(fp), "ldo")
    assert "Regulator_Linear:AP7361C-33E" in [c["lib_id"] for c in S.mpn_candidates(rec["mfr"])]
    bad = ev("Regulator_Linear:AP7361C-33E", rec, ref)
    assert not bad["ok"] and "ground/supply on different pins" in bad["notes"][0], bad
    sel = choose(rec, ref)
    assert sel["choice"] is None and "AP7361C-33E rejected" in sel["fallback"], sel
    # the forward part, same package, gets it
    good = ref_of("AP7361C-33E-13", {"1": "IN", "2": "GND", "3": "OUT"}, kpads(fp), "ldo")
    ch = choose(rec_of("AP7361C-33E-13", "SOT-223", "ldo"), good)["choice"]
    assert ch["lib_id"] == "Regulator_Linear:AP7361C-33E" and ch["footprint"] == fp, ch


def test_negative_tl431_pinout_variants():
    """TL431 in SOT-23 ships as REF/CATHODE/ANODE and as CATHODE/REF/ANODE. KiCad's TL431DBZ is the second."""
    _kicad()
    rec = rec_of("TL431", "SOT-23", "ic")
    other = ref_of("TL431", {"1": "REF", "2": "CAT", "3": "AN"}, kpads(SOT23), "ic")
    bad = ev("Reference_Voltage:TL431DBZ", rec, other, match="family")
    assert not bad["ok"] and "permuted" in bad["notes"][0], bad
    same = ref_of("TL431DBZR", {"1": "CATHODE", "2": "REF", "3": "ANODE"}, kpads(SOT23), "ic")
    ok = choose(rec_of("TL431DBZR", "SOT-23", "ic"), same)["choice"]
    assert ok and ok["lib_id"] == "Reference_Voltage:TL431DBZ" and ok["footprint"] == SOT23, ok


def test_tab_pad_numbered_differently():
    """SOT-223: KiCad numbers the tab '2' (TabPin2), suppliers number it 4. Accepted only when the supplier itself
    calls pad 4 the same net as pad 2."""
    _kicad()
    fp = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    pads = kpads(fp)
    tab = max((p for p in pads if p["n"] == "2"), key=lambda p: p["hw"] * p["hh"])
    tab["n"] = "4"
    rec = rec_of("AMS1117-3.3", "SOT-223", "ldo")
    good = ref_of("AMS1117-3.3", {"1": "GND", "2": "VOUT", "3": "VIN", "4": "VOUT"}, pads, "ldo")
    ch = choose(rec, good)["choice"]
    assert ch["lib_id"] == "Regulator_Linear:AMS1117-3.3" and ch["footprint"] == fp, ch
    assert any("also covers the supplier's pad 4" in n for n in ch["notes"]), ch["notes"]
    # a tab the supplier ties to another net than pin 2: KiCad's TabPin2 footprint would short it → refused
    bad = ref_of("AMS1117-3.3", {"1": "GND", "2": "VOUT", "3": "VIN", "4": "GND"}, pads, "ldo")
    sel = choose(rec, bad)
    assert sel["choice"] is None and "pad numbering differs" in sel["fallback"], sel


def test_family_symbol_with_units_and_a_listed_footprint():
    _kicad()
    names = {"1": "1OUT", "2": "1IN-", "3": "1IN+", "4": "GND", "5": "2IN+", "6": "2IN-", "7": "2OUT", "8": "VCC"}
    rec = rec_of("LM358DR2G", "SOIC-8", "opamp")
    ch = choose(rec, ref_of("LM358DR2G", names, kpads(SOIC8, rot180=True), "opamp"))["choice"]
    assert ch["lib_id"] == "Amplifier_Operational:LM358" and ch["match"] == "family" and ch["units"] == 3, ch
    assert ch["footprint"] == SOIC8 and ch["footprint_source"] == "stock", ch        # LM358 names no footprint: found by its filter
    assert any("2 units match" in n for n in ch["notes"]), ch["notes"]
    # inputs of amplifier A swapped in the supplier drawing: not this symbol
    swapped = dict(names, **{"2": "1IN+", "3": "1IN-"})
    sel = choose(rec, ref_of("LM358DR2G", swapped, kpads(SOIC8), "opamp"))
    assert sel["choice"] is None and "Amplifier_Operational:LM358 rejected" in sel["fallback"], sel
    # a pinout where the two halves interleave differently
    weird = {"1": "1OUT", "2": "2IN-", "3": "1IN+", "4": "GND", "5": "2IN+", "6": "1IN-", "7": "2OUT", "8": "VCC"}
    assert choose(rec, ref_of("LM358DR2G", weird, kpads(SOIC8), "opamp"))["choice"] is None
    # supply and ground exchanged
    flipped = dict(names, **{"4": "VCC", "8": "GND"})
    sel = choose(rec, ref_of("LM358DR2G", flipped, kpads(SOIC8), "opamp"))
    assert sel["choice"] is None and "ground/supply" in sel["fallback"], sel
    # a family match with no pin names to confirm it is not accepted on its name
    blank = {str(i): "" for i in range(1, 9)}
    sel = choose(rec, ref_of("LM358DR2G", blank, kpads(SOIC8), "opamp"))
    assert sel["choice"] is None and "confirmed" in sel["fallback"], sel


def test_logic_gates_checked_by_unit_and_direction():
    _kicad()
    names = {"1": "1A", "2": "1Y", "3": "2A", "4": "2Y", "5": "3A", "6": "3Y", "7": "GND",
             "8": "4Y", "9": "4A", "10": "5Y", "11": "5A", "12": "6Y", "13": "6A", "14": "VCC"}
    fp = "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm"
    rec = rec_of("74HC14D,653", "SOIC-14", "ic")
    ch = choose(rec, ref_of("74HC14D,653", names, kpads(fp), "ic"))["choice"]
    assert ch["lib_id"] == "74xx:74HC14" and ch["footprint"] == fp and ch["units"] == 7, ch
    # a part whose gates run output-first (1Y on pin 1): KiCad's 74HC14 has its inputs there
    rev = {k: (v[0] + ("Y" if v[1] == "A" else "A") if v[0].isdigit() else v) for k, v in names.items()}
    sel = choose(rec, ref_of("74HC14D,653", rev, kpads(fp), "ic"))
    assert sel["choice"] is None and "direction" in sel["fallback"], sel


def test_stock_symbol_on_converted_footprint():
    _kicad()
    names = {"1": "1OUT", "2": "1IN-", "3": "1IN+", "4": "GND", "5": "2IN+", "6": "2IN-", "7": "2OUT", "8": "VCC"}
    rec = rec_of("LM358XYZ", "ODD-8", "opamp")
    odd = kpads(SOIC8, scale=1.6)                         # a package no KiCad footprint of LM358's filter fits
    ch = choose(rec, ref_of("LM358XYZ", names, odd, "opamp"))["choice"]
    assert ch["lib_id"] == "Amplifier_Operational:LM358" and ch["footprint"] is None and ch["footprint_source"] == "converted", ch
    assert "symbol pin numbers equal its pad numbers" in ch["notes"][0]
    # the supplier footprint has a 9th pad (exposed pad): the 8-pin symbol does not cover it → no stock symbol
    ep = odd + [_p(9, 0, 0, 2, 2)]
    sel = choose(rec, ref_of("LM358XYZ", dict(names, **{"9": "EP"}), ep, "opamp"))
    assert sel["choice"] is None and "pads ['9'] have no pin" in sel["fallback"], sel
    # a family symbol that IS drawn for one package is not moved onto another package's converted footprint
    reg = rec_of("78L05G-AB3-R", "ODD-3", "ldo")
    r3 = ref_of("78L05G-AB3-R", {"1": "OUT", "2": "GND", "3": "IN"}, kpads(SOT23, scale=2.0), "ldo")
    bad = ev("Regulator_Linear:L78L05_TO92", reg, r3, match="family")
    assert not bad["ok"] and "is drawn for" in bad["notes"][0], bad
    # …while an EXACT MPN in a package KiCad did not draw keeps its symbol (pins confirmed by name)
    ams = rec_of("AMS1117-3.3", "TO-252-2", "ldo")
    r4 = ref_of("AMS1117-3.3", {"1": "GND", "2": "VOUT", "3": "VIN"}, kpads(SOT23, scale=2.5), "ldo")
    ch = choose(ams, r4)["choice"]
    assert ch["lib_id"] == "Regulator_Linear:AMS1117-3.3" and ch["footprint_source"] == "converted", ch


def test_connectors_by_count_and_numbering():
    _kicad()
    hdr = "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical"
    rec = rec_of("2.54-1x6P", "Plugin,P=2.54mm", "header", "1 1x6P 2.54mm 3A Pin Headers", "Pin Headers")
    blank6 = {str(i): "" for i in range(1, 7)}
    ch = choose(rec, ref_of("2.54-1x6P", blank6, kpads(hdr), "header", title="HDR-TH_6P-P2.54-V-M-1"))["choice"]
    assert ch["lib_id"] == "Connector_Generic:Conn_01x06" and ch["footprint"] == hdr, ch
    # a wire-to-board connector: generic symbol, its own (converted) footprint
    jst = rec_of("XH-6A", "Plugin,P=2.5mm", "connector", "1x6P 2.5mm Wire To Board Connector", "Wire To Board Connector")
    row = [_p(i + 1, 2.5 * i, 0, 1.6, 1.6, "thru_hole") for i in range(6)]
    ch = choose(jst, ref_of("XH-6A", blank6, row, "connector", title="CONN-TH_6P-P2.50"))["choice"]
    assert ch["lib_id"] == "Connector_Generic:Conn_01x06" and ch["footprint_source"] == "converted", ch
    # 2-row header numbered odd/even
    h23 = "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical"
    rec2 = rec_of("2.54-2*3P", "Plugin,P=2.54mm", "header", "2 2.54mm 6P Pin Headers", "Pin Headers")
    ch = choose(rec2, ref_of("2.54-2*3P", blank6, kpads(h23), "header", title="HDR-TH_6P-P2.54-V-M-R2-C3-S2.54-1"))["choice"]
    assert ch["lib_id"] == "Connector_Generic:Conn_02x03_Odd_Even" and ch["footprint"] == h23, ch
    # …and one numbered around the body: another symbol, never Odd_Even
    ccw = [_p(i + 1, 0, 2.54 * i, 1.7, 1.7, "thru_hole") for i in range(3)] + [_p(6 - i, 2.54, 2.54 * i, 1.7, 1.7, "thru_hole") for i in range(3)]
    ch = choose(rec2, ref_of("2.54-2*3P", blank6, ccw, "header", title="HDR-TH_6P"))["choice"]
    assert ch["lib_id"] == "Connector_Generic:Conn_02x03_Counter_Clockwise" and ch["footprint_source"] == "converted", ch
    # 2 contacts + 2 mechanical pads, all four numbered and unnamed: NOT a Conn_01x04
    mech = [_p(1, 0, 0), _p(2, 2.0, 0), _p(3, -2.3, 3.0, 1.2, 2.5), _p(4, 4.3, 3.0, 1.2, 2.5)]
    wafer = rec_of("WAFER-PH2.0-2PWB", "SMD,P=2mm", "connector", "1x2P 2 250V 2A 2P 2mm Wire To Board Connector", "Wire To Board Connector")
    sel = choose(wafer, ref_of("WAFER-PH2.0-2PWB", {"1": "", "2": "", "3": "", "4": ""}, mech, "connector"))
    assert sel["choice"] is None, sel["choice"]
    # four pads in a row but the catalogue says 2 contacts: still not a 1x4
    row4 = [_p(i + 1, 2.0 * i, 0) for i in range(4)]
    assert choose(wafer, ref_of("WAFER-PH2.0-2PWB", {"1": "", "2": "", "3": "", "4": ""}, row4, "connector"))["choice"] is None


def test_crystals_by_case_pads():
    _kicad()
    fp = "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"
    rec = rec_of("X322512MSB4SI", "SMD3225-4P", "crystal", "12MHz 20pF Crystals", "Crystals")
    ch = choose(rec, ref_of(rec["mfr"], {"1": "OSC1", "2": "GND", "3": "OSC2", "4": "GND"}, kpads(fp), "crystal"))["choice"]
    assert ch["lib_id"] == "Device:Crystal_GND24" and ch["footprint"] == fp, ch
    # case on pads 2 and 3: the other symbol
    ch = choose(rec, ref_of(rec["mfr"], {"1": "OSC1", "2": "GND", "3": "GND", "4": "OSC2"}, kpads(fp), "crystal"))["choice"]
    assert ch["lib_id"] == "Device:Crystal_GND23", ch
    # which pads are the case is not stated: no guess
    assert choose(rec, ref_of(rec["mfr"], {"1": "", "2": "", "3": "", "4": ""}, kpads(fp), "crystal"))["choice"] is None
    two = rec_of("X50328MSB2GI", "SMD5032-2P", "crystal", "8MHz Crystals", "Crystals")
    ch = choose(two, ref_of(two["mfr"], {"1": "OSC1", "2": "OSC2"}, kpads("Crystal:Crystal_SMD_5032-2Pin_5.0x3.2mm"), "crystal"))["choice"]
    assert ch["lib_id"] == "Device:Crystal" and ch["footprint"] == "Crystal:Crystal_SMD_5032-2Pin_5.0x3.2mm", ch


def test_two_pad_parts_keep_their_polarity():
    _kicad()
    pads = [_p(1, -2.0, 0, 1.5, 2.0), _p(2, 2.0, 0, 1.5, 2.0)]
    led = rec_of("SIDE-LED", "SMD,3x1mm", "led", "red side view LED", "LED Indication - Discrete")
    ch = choose(led, ref_of("SIDE-LED", {"1": "K", "2": "A"}, pads, "led"))["choice"]
    assert ch["lib_id"] == "Device:LED" and ch["footprint_source"] == "converted", ch
    # the supplier numbers the ANODE 1: Device:LED (pin 1 = K) on that footprint would be reversed → converted symbol
    assert choose(led, ref_of("SIDE-LED", {"1": "A", "2": "K"}, pads, "led"))["choice"] is None
    assert choose(led, ref_of("SIDE-LED", {"1": "", "2": ""}, pads, "led"))["choice"] is None
    cap = rec_of("POLY-CAP", "SMD,7.3x4.3mm", "tantalum", "100uF polymer tantalum")
    assert choose(cap, ref_of("POLY-CAP", {"1": "+", "2": "-"}, pads, "tantalum"))["choice"]["lib_id"] == "Device:C_Polarized"
    assert choose(cap, ref_of("POLY-CAP", {"1": "-", "2": "+"}, pads, "tantalum"))["choice"] is None
    ind = rec_of("PRS8040-220MT", "SMD,8x8mm", "inductor", "22uH Power Inductors")
    ch = choose(ind, ref_of(ind["mfr"], {"1": "", "2": ""}, pads, "inductor"))["choice"]
    assert ch["lib_id"] == "Device:L" and ch["footprint_source"] == "converted", ch
    # tantalum in an EIA case: KiCad symbol + KiCad footprint by rule
    tan = importer.stock_candidate(rec_of("TAJA106K016RNJ", "CASE-A-3216-18(mm)", "tantalum"))
    assert tan and tan["lib_id"] == "Device:C_Polarized" and tan["footprint"].startswith("Capacitor_Tantalum_SMD:CP_EIA-3216-18_"), tan


def test_bridge_rectifier_by_pin_order():
    _kicad()
    pads = [_p(1, -2.5, 2.0, 1.2, 2.0), _p(2, 2.5, 2.0, 1.2, 2.0), _p(3, 2.5, -2.0, 1.2, 2.0), _p(4, -2.5, -2.0, 1.2, 2.0)]
    rec = rec_of("MB10XX", "MBF-X", "bridge", "1A 1kV Bridge Rectifiers")
    ch = choose(rec, ref_of("MB10XX", {"1": "+", "2": "-", "3": "&#126;", "4": "AC"}, pads, "bridge"))["choice"]
    assert ch["lib_id"] == "Device:D_Bridge_+-AA" and ch["footprint_source"] == "converted", ch
    ch = choose(rec, ref_of("MB10XX", {"1": "-", "2": "AC", "3": "AC", "4": "+"}, pads, "bridge"))["choice"]
    assert ch["lib_id"] == "Device:D_Bridge_-AA+", ch
    assert choose(rec, ref_of("MB10XX", {"1": "", "2": "", "3": "", "4": ""}, pads, "bridge"))["choice"] is None
    # an order KiCad has no symbol for stays converted
    assert choose(rec, ref_of("MB10XX", {"1": "AC", "2": "+", "3": "AC", "4": "-"}, pads, "bridge"))["choice"] is None


def test_variant_with_other_no_connects_is_refused():
    """ESP32-WROOM-32 vs -32E style: the symbol offers functions on pins the part leaves unconnected."""
    sym = {"pins": {str(i): f"IO{i}" for i in range(1, 13)}, "types": {}, "units": {1: [str(i) for i in range(1, 13)]}}
    sup = {str(i): f"IO{i}" for i in range(1, 13)}
    ok, _ = S._names_verdict({"match": "exact"}, sym, {"pins": sup, "units": 1}, None, True)
    assert ok
    ok, notes = S._names_verdict({"match": "exact"}, sym, {"pins": dict(sup, **{"3": "NC", "4": "NC", "5": "NC"}), "units": 1}, None, True)
    assert not ok and "no-connect on one side only" in notes[0], notes
    ok, _ = S._names_verdict({"match": "exact"}, sym, {"pins": dict(sup, **{"3": "NC"}), "units": 1}, None, True)
    assert ok, "one NC pin is a naming difference, not a variant"


def test_usb_receptacle_only_on_kicads_own_footprint():
    _kicad()
    fp = "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12"
    if S.footprint_pads(fp) is None:
        skip("this KiCad has no HRO TYPE-C-31-M-12 footprint")
    merge = {"A1": "A1B12", "B12": "A1B12", "A4": "A4B9", "B9": "A4B9", "A12": "B1A12", "B1": "B1A12", "A9": "B4A9", "B4": "B4A9"}
    pads, seen, shell = [], set(), 0
    for p in kpads(fp):
        if p["n"] == "SH":
            shell += 1
            p["n"] = str(shell)
        else:
            p["n"] = merge.get(p["n"], p["n"])
            if p["n"] in seen:
                continue
            seen.add(p["n"])
        pads.append(p)
    pins = {"A1B12": "GND", "A4B9": "VBUS", "B8": "SBU2", "A5": "CC1", "B7": "DN2", "A6": "DP1", "A7": "DN1", "B6": "DP2",
            "A8": "SBU1", "B5": "CC2", "B4A9": "VBUS", "B1A12": "GND", "1": "EH", "2": "EH", "3": "EH", "4": "EH"}
    rec = rec_of("TYPE-C-31-M-12", "SMD", "usb_connector", "16P Female Type-C USB Connectors", "USB Connectors")
    ch = choose(rec, ref_of(rec["mfr"], pins, pads, "usb_connector"))["choice"]
    assert ch["lib_id"] == "Connector:USB_C_Receptacle_USB2.0_16P" and ch["footprint"] == fp, ch
    # D+ and D- exchanged on the A row in the supplier drawing: refused
    sel = choose(rec, ref_of(rec["mfr"], dict(pins, A6="DN1", A7="DP1"), pads, "usb_connector"))
    assert sel["choice"] is None, sel["choice"]
    # the same pinout from a maker KiCad has no footprint for: compound pad names need a mapping nobody verified
    other = rec_of("TYPE-C16PIN-XYZ", "SMD", "usb_connector", "16P Female Type-C USB Connectors", "USB Connectors")
    sel = choose(other, ref_of(other["mfr"], pins, pads, "usb_connector"))
    assert sel["choice"] is None and "no footprint for exactly this part" in sel["fallback"], sel


def test_no_supplier_drawing_is_strict_and_says_so():
    _kicad()
    ch = choose(rec_of("AMS1117-3.3", "SOT-223", "ldo"), None)["choice"]
    assert ch["lib_id"] == "Regulator_Linear:AMS1117-3.3" and "NOT checked against supplier data" in ch["notes"][0], ch
    assert ch["verified"].startswith("package name only")
    sel = choose(rec_of("LM358DR2G", "SOIC-8", "opamp"), None)            # family match: needs pin data
    assert sel["choice"] is None and "supplier" in sel["fallback"], sel
    sel = choose(rec_of("AMS1117-3.3", "TO-252-2", "ldo"), None)          # another package than the symbol's footprint
    assert sel["choice"] is None and "does not name the catalogue package" in sel["fallback"], sel


# ══ through the importer: what the placement gets, and what it is told ═══
class _Catalogue:
    """Patches the catalogue and the EasyEDA fetch with fixtures (and counts fetches)."""

    def __init__(self, parts: dict):
        self.parts, self.fetched = parts, []

    def __enter__(self):
        self._rec, self._fetch = importer.part_record, E.fetch
        importer.part_record = lambda code, live=True, fresh=False: dict(self.parts[code][0], lcsc=code)

        def fetch(code, refresh=False):
            self.fetched.append(code)
            comp = self.parts[code][1]
            if comp is None:
                raise C.NetError(f"{code}: no EasyEDA symbol/footprint available (fixture)")
            return comp
        E.fetch = fetch
        return self

    def __exit__(self, *a):
        importer.part_record, E.fetch = self._rec, self._fetch


def _project():
    tmp = Path(tempfile.mkdtemp(prefix="copper-test-stock-"))
    (tmp / "t.kicad_pro").write_text("{}", encoding="utf-8")
    from copper_kicad.project import Project
    return Project(tmp, "t")


def test_resolve_reports_choice_source_and_reason():
    _kicad()
    names = {"1": "1OUT", "2": "1IN-", "3": "1IN+", "4": "GND", "5": "2IN+", "6": "2IN-", "7": "2OUT", "8": "VCC"}
    parts = {
        "C1": (rec_of("0603WAF1002T5E", "0603", "resistor", "10kΩ ±1% 0603 Chip Resistor"), None),
        "C2": (rec_of("SI2302", "SOT-23", "mosfet", NMOS), component("SI2302", {"1": "G", "2": "S", "3": "D"}, kpads(SOT23))),
        "C3": (rec_of("LM358XYZ", "ODD-8", "opamp"), component("LM358XYZ", names, kpads(SOIC8, scale=1.6), "ODD-8_FIX")),
        "C4": (rec_of("S8050", "SOT-23", "bjt", "transistor SOT-23"), component("S8050", {"1": "B", "2": "E", "3": "C"}, kpads(SOT23), "SOT-23_FIX")),
        "C5": (rec_of("NOSUCH-123", "SOT-23", "ic"), None),
    }
    pr = _project()
    try:
        with _Catalogue(parts) as cat:
            # rule: KiCad symbol + KiCad footprint, no supplier drawing asked for, nothing written
            r = importer.resolve_lcsc(pr, "C1", with_3d=False)
            assert (r["source"], r["symbol_source"], r["footprint_source"]) == ("kicad-stock", "stock", "stock"), r
            assert r["lib_id"] == "Device:R" and r["selection"]["match"] == "rule" and "Device:R" in r["why"]
            assert cat.fetched == [] and r["files_written"] == [] and not (pr.dir / "libs").exists()
            # generic symbol from the pinout, stock footprint
            r = importer.resolve_lcsc(pr, "C2", with_3d=False)
            assert r["lib_id"] == "Transistor_FET:Q_NMOS_GSD" and r["footprint"] == SOT23 and r["source"] == "kicad-stock", r
            assert r["selection"]["reference"] == "supplier drawing" and r["selection"]["checks"] and "pin order GSD" in r["why"]
            assert r["pins"] == {"1": "G", "2": "S", "3": "D"} and r["files_written"] == []
            # stock symbol on the converted footprint: the footprint is written and KiCad loads it
            r = importer.resolve_lcsc(pr, "C3", with_3d=False)
            assert r["lib_id"] == "Amplifier_Operational:LM358" and r["footprint"] == "copper-lcsc:ODD-8_FIX", r
            assert (r["source"], r["symbol_source"], r["footprint_source"]) == ("kicad-stock", "stock", "converted")
            assert "footprint converted from the supplier drawing" in r["why"] and r["units"] == 3 and r["pads"] == 8
            assert (pr.dir / "libs" / "copper-lcsc.pretty" / "ODD-8_FIX.kicad_mod").exists() and (pr.dir / "fp-lib-table").exists()
            assert not (pr.dir / "libs" / "copper-lcsc.kicad_sym").exists(), "no converted symbol was needed"
            v = importer.verify_import(pr, r)
            assert v["ok"], v["errors"]
            assert importer.resolve_lcsc(pr, "C3", with_3d=False)["files_written"] == [], "idempotent"
            # explicit, reported fallback: the tempting stock symbol is named with the reason it was refused
            r = importer.resolve_lcsc(pr, "C4", with_3d=False)
            assert (r["source"], r["symbol_source"], r["footprint_source"]) == ("easyeda", "converted", "converted"), r
            assert r["lib_id"] == "copper-lcsc:S8050_C4" and r["why"].startswith("symbol converted from the supplier")
            fb = r["selection"]["fallback"]
            assert "Transistor_BJT:S8050 rejected" in fb and "permuted" in fb, fb
            assert any(t["lib_id"] == "Transistor_BJT:S8050" and not t["ok"] for t in r["selection"]["tried"])
            assert any(w.startswith("stock candidate rejected: Transistor_BJT:S8050") for w in r["warnings"])
            # (the catalogue text of C4 does not say NPN/PNP, so no generic transistor symbol may be assumed either)
            # prefer='stock' fails loudly instead of converting; prefer='easyeda' says it was asked to
            try:
                importer.resolve_lcsc(pr, "C4", prefer="stock", with_3d=False)
                raise AssertionError("prefer='stock' must not fall back")
            except ToolError as e:
                assert e.code == "NO_STOCK_FIT" and "Transistor_BJT:S8050" in str(e.message), e.message
            r = importer.resolve_lcsc(pr, "C2", prefer="easyeda", with_3d=False)
            assert r["source"] == "easyeda" and "prefer='easyeda'" in r["selection"]["fallback"], r["selection"]
            # no stock symbol and no supplier drawing: a clear error, not a guess
            try:
                importer.resolve_lcsc(pr, "C5", with_3d=False)
                raise AssertionError("expected EASYEDA_UNAVAILABLE")
            except ToolError as e:
                assert e.code == "EASYEDA_UNAVAILABLE", e.code
    finally:
        shutil.rmtree(pr.dir, ignore_errors=True)


def test_generic_provider_does_not_put_a_to92_symbol_on_an_smd_part():
    _kicad()
    from copper_kicad.parts.providers import kicad_fit
    got = kicad_fit.fit({"mpn": "S8050", "package": "SOT-23"})
    assert got["lib_id"] == "" and any("TO-92" in w for w in got["warnings"]), got
    assert kicad_fit.fit({"mpn": "S8050", "package": "TO-92"})["lib_id"] == "Transistor_BJT:S8050"
    assert kicad_fit.fit({"mpn": "AMS1117-3.3", "package": "SOT-223"})["lib_id"] == "Regulator_Linear:AMS1117-3.3"
    assert kicad_fit.fit({"mpn": "S8050"})["lib_id"] == "Transistor_BJT:S8050", "no package stated: the catalogue's choice stands"


if __name__ == "__main__":
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
            traceback.print_exc(limit=4)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
