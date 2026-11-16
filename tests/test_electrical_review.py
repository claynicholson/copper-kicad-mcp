"""Datasheet-rule checks in design_review (review/electrical.py + review/data/electrical.json) and their
post-write pass in sch_apply.

Every fixture reproduces a defect an adversarial review found on a real 106-part FOC motor controller
(STM32G431 + NSG2066 + TPS54202) that design_review used to call clean:

  * a 4-pin tact switch wired 1/3 = NRST, 2/4 = GND — pins 1-2 are one contact, so NRST was shorted to GND;
  * the gate driver's VCC on the 20 V battery (NSG2066: 5-20 V) instead of a 12 V regulator;
  * an SMBJ24A on the motor bus: 38.9 V clamp above the TPS54202's 30 V absolute maximum;
  * a 10 V ceramic on the 20 V bus;
  * 22 pF load caps on a 10 pF crystal;
  * the encoder's SPI clock on PB3, which is also SWO on the debug header;
and checks the corrected board is clean of all of them except the real PB8/BOOT0 warning.

Run:  "C:/Program Files/KiCad/10.0/bin/python.exe" tests/test_electrical_review.py [test_name ...]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_TMP = Path(tempfile.mkdtemp(prefix="copper-test-electrical-"))
os.environ.setdefault("COPPER_HOME", str(_TMP / "home"))

from copper_kicad.review import design_review as DR  # noqa: E402
from copper_kicad.review import electrical as E  # noqa: E402

FOC_ORIG = Path(r"C:\t\speed\foc-orig")
NEW_TYPES = {"cap_overvoltage", "cap_derating", "tvs_standoff_below_rail", "tvs_clamp_above_abs_max",
             "ic_supply_over_abs_max", "ic_supply_over_max", "ic_supply_no_headroom", "ic_supply_under_min",
             "internally_joined_pins", "mcu_debug_pin_conflict", "mcu_swd_pin_reused", "mcu_boot0_shared",
             "crystal_load_mismatch", "crystal_caps_unequal"}


# ── tiny netlist builder (the shape load_netlist returns) ───────────────────
class NL:
    def __init__(self):
        self.comps, self.nets = {}, {}

    def part(self, ref, value, lib="Device:C", footprint="", **fields):
        self.comps[ref] = {"value": value, "lib_id": lib, "footprint": footprint, "fields": fields, "description": "",
                           "sheet": "/"}
        return self

    def pin(self, net, ref, pin, name="", ptype="passive"):
        self.nets.setdefault(net, []).append({"ref": ref, "pin": str(pin), "pin_name": name, "pin_type": ptype})
        return self

    def cap(self, ref, value, a, b="GND", **fields):
        self.part(ref, value, **fields)
        return self.pin(a, ref, 1).pin(b, ref, 2)

    def dict(self):
        return {"components": self.comps, "nets": self.nets}


def _stm32(n: NL, ref="U1"):
    n.part(ref, "STM32G431CBT6", "MCU_ST_STM32G4:STM32G431CBTx", "Package_QFP:LQFP-48_7x7mm_P0.5mm", LCSC="C529355")
    for pin, name in (("24", "VDD"), ("36", "VDD"), ("48", "VDD"), ("1", "VBAT")):
        n.pin("+3V3", ref, pin, name, "power_in")
    for pin, name in (("23", "VSS"), ("19", "VSSA")):
        n.pin("GND", ref, pin, name, "power_in")
    n.pin("+3V3", "C90", 1).pin("GND", "C90", 2).part("C90", "100nF", LCSC="C14663")
    return n


def _debug_header(n: NL, swo_net="SWO"):
    n.part("J1", "SWD", "Connector_Generic:Conn_02x05_Odd_Even")
    n.pin("SWDIO", "J1", 2).pin("SWCLK", "J1", 4).pin(swo_net, "J1", 6).pin("GND", "J1", 3).pin("NRST", "J1", 10)
    n.pin("SWDIO", "U1", 37, "PA13", "bidirectional").pin("SWCLK", "U1", 38, "PA14", "bidirectional")
    return n


def _review(n: NL, checks=None, rail_volts=None):
    r = DR.review(None, checks=checks, include_erc=False, netlist=n.dict(), rail_volts=rail_volts)
    by = {}
    for f in r["findings"]:
        by.setdefault(f["type"], []).append(f)
    return r, by


# ── e) internally joined switch pins ──────────────────────────────────────
def _switch(n: NL, pins: dict, value="TS-1187A-B-A-B", lib="copper-lcsc:TS-1187A-B-A-B_C318884",
            footprint="copper-lcsc:SW-SMD_4P-L5.1-W5.1-P3.70-LS6.5-TL_H1.5", **fields):
    n.part("SW1", value, lib, footprint, **fields)
    for p, net in pins.items():
        n.pin(net, "SW1", p, "ABCD"[int(p) - 1])
    return n


def test_tact_switch_shorting_nrst_to_gnd_is_an_error():
    n = _stm32(NL()).pin("NRST", "U1", 7, "PG10/NRST", "bidirectional")
    _switch(n, {"1": "NRST", "2": "GND", "3": "NRST", "4": "GND"}, LCSC="C318884", MPN="TS-1187A-B-A-B")
    _, by = _review(n, checks=["joined_pins"])
    f = by["internally_joined_pins"]
    assert len(f) == 1 and f[0]["severity"] == "error" and {"SW1.1", "SW1.2"} <= set(f[0]["items"]), f
    assert "NRST" in f[0]["message"] and "GND" in f[0]["message"], f[0]["message"]
    # the fix moves exactly the two wrong pins: 2 → NRST, 3 → GND
    fx = f[0]["fix"]
    assert '"pins":["SW1.2", "SW1.3"]' in fx.replace(", ", ", ") or ('SW1.2' in fx and 'SW1.3' in fx), fx
    assert '"pin":"SW1.2","net":"NRST"' in fx and '"pin":"SW1.3","net":"GND"' in fx, fx


def test_correct_tact_switch_and_kicad_dual_symbol():
    n = _stm32(NL()).pin("NRST", "U1", 7, "PG10/NRST", "bidirectional")
    _switch(n, {"1": "NRST", "2": "NRST", "3": "GND", "4": "GND"}, LCSC="C318884")
    _, by = _review(n, checks=["joined_pins"])
    assert "internally_joined_pins" not in by, by
    # KiCad's own SW_Push_Dual / SW_Push_Open_Dual is a ganged DPST button: 1-2 and 3-4 are the SWITCHED pairs,
    # so 1->A, 2->GND, 3->B, 4->GND is correct, even on a 4-pad tactile footprint (no assumed-join fallback)
    for lib in ("Switch:SW_Push_Dual", "Switch:SW_Push_Open_Dual"):
        n = NL()
        _switch(n, {"1": "BTN_A", "2": "GND", "3": "BTN_B", "4": "GND"}, value=lib.split(":")[1], lib=lib,
                footprint="Button_Switch_SMD:SW_SPST_TL3342")
        _, by = _review(n, checks=["joined_pins"])
        assert "internally_joined_pins" not in by, (lib, by)


def test_unlisted_tactile_switch_is_a_warning_that_says_assumed():
    n = NL()
    _switch(n, {"1": "BTN", "2": "GND", "3": "BTN", "4": "GND"}, value="K2-1102SP", lib="mylib:K2",
            footprint="mylib:SW-SMD_4P-L6.0-W6.0")
    _, by = _review(n, checks=["joined_pins"])
    f = by["internally_joined_pins"][0]
    assert f["severity"] == "warning" and "assumed" in f["message"], f
    # a 2-pin switch is never touched — even carrying a 4-pad tact switch's LCSC number (SW_Push on pads 1,1,2,2)
    n = NL().part("SW2", "SW_Push", "Switch:SW_Push", "Button_Switch_SMD:SW_SPST_TL3342", LCSC="C318884")
    n.pin("EN", "SW2", 1).pin("GND", "SW2", 2)
    assert "internally_joined_pins" not in _review(n, checks=["joined_pins"])[1]


# ── c) IC supply range ────────────────────────────────────────────────────
def _gate_driver(n: NL, vcc_net, value="NSG2066", lcsc="C49023244"):
    n.part("U6", value, f"copper-lcsc:{value}", LCSC=lcsc)
    n.pin(vcc_net, "U6", 7, "VCC", "power_in").pin("GND", "U6", 8, "COM", "passive")
    return n


def test_gate_driver_vcc_on_the_battery():
    _, by = _review(_gate_driver(NL(), "+20V"), checks=["ic_supply"])
    f = by["ic_supply_no_headroom"][0]
    assert f["items"] == ["U6.7"] and "5.0–20.0" in f["message"] and f["fix"], f
    # the same rail declared at its charged voltage → over the range
    _, by = _review(_gate_driver(NL(), "VBAT"), checks=["ic_supply"], rail_volts={"VBAT": 21})
    assert by["ic_supply_over_max"][0]["severity"] == "error", by
    # IR2101 on 24 V → over its 25 V abs max? no: 24 V is over the 20 V operating max (error), 26 V over abs max
    _, by = _review(_gate_driver(NL(), "+26V", value="IR2101S", lcsc=""), checks=["ic_supply"])
    assert by["ic_supply_over_abs_max"][0]["severity"] == "error", by
    # on its 12 V regulator: clean
    _, by = _review(_gate_driver(NL(), "+12V"), checks=["ic_supply"])
    assert not by, by


def test_regulator_and_mcu_ranges():
    n = NL().part("U3", "AMS1117-3.3", "Regulator_Linear:AMS1117-3.3")
    n.pin("+3V3", "U3", 2, "VO", "power_out").pin("GND", "U3", 1, "GND", "power_in").pin("+3V3_IN", "U3", 3, "VI", "power_in")
    _, by = _review(n, checks=["ic_supply"], rail_volts={"+3V3_IN": 3.6})
    assert by["ic_supply_under_min"][0]["items"] == ["U3.3"], by            # 3.6 V < 3.3 + 1.3 V dropout
    _, by = _review(n, checks=["ic_supply"], rail_volts={"+3V3_IN": 16})
    assert "ic_supply_over_abs_max" in by, by
    # STM32 on 5 V
    n = NL().part("U1", "STM32G431CBT6", "MCU_ST_STM32G4:STM32G431CBTx").pin("+5V", "U1", 24, "VDD", "power_in")
    _, by = _review(n, checks=["ic_supply"])
    assert by["ic_supply_over_abs_max"][0]["items"] == ["U1.24"], by


def test_regulator_output_names_an_unnamed_rail():
    n = NL().part("U7", "L78L12ABUTR", "copper-lcsc:L78L12ABUTR")
    n.pin("Net-(U7-VOUT)", "U7", 1, "VOUT", "power_out").pin("+24V", "U7", 3, "VIN", "power_in").pin("GND", "U7", 2, "GND", "power_in")
    n.cap("C1", "10uF", "Net-(U7-VOUT)", MPN="CL21A106KPFNNNE")      # 10 V part on the 12 V output
    _, by = _review(n, checks=["cap_voltage", "ic_supply"])
    assert by["cap_overvoltage"][0]["items"] == ["C1"] and "regulator output" in by["cap_overvoltage"][0]["message"], by
    assert "ic_supply_over_max" not in by and "ic_supply_no_headroom" not in by      # 24 V < 35 V abs max


# ── b) TVS ────────────────────────────────────────────────────────────────
def _motor_bus(n: NL, tvs="SMBJ24A", tvs_lcsc=""):
    n.part("D4", tvs, "Device:D_TVS", "Diode_SMD:D_SMB", **({"LCSC": tvs_lcsc} if tvs_lcsc else {}))
    n.pin("VBUS_M", "D4", 1, "A1").pin("GND", "D4", 2, "A2")
    n.part("U2", "TPS54202DDCR", "Regulator_Switching:TPS54202DDC", LCSC="C191884")
    n.pin("VBUS_M", "U2", 3, "VIN", "power_in").pin("GND", "U2", 1, "GND", "power_in")
    return n


def test_tvs_clamp_above_buck_abs_max():
    _, by = _review(_motor_bus(NL()), checks=["tvs_clamp"])
    f = by["tvs_clamp_above_abs_max"][0]
    assert f["severity"] == "error" and "38.9" in f["message"] and "30" in f["message"] and "U2.3" in f["items"], f
    # the corrected part (LCSC record: 18 V stand-off, 29.2 V clamp) is fine
    _, by = _review(_motor_bus(NL(), "SMBJ18A", "C19077573"), checks=["tvs_clamp"])
    assert not by, by
    # and its stand-off becomes the bus's (upper-bound) voltage for the other checks
    r, _ = _review(_motor_bus(NL(), "SMBJ18A", "C19077573"), checks=["cap_voltage"])
    assert "18" in str(r["stats"]["rail_volts"]["VBUS_M"]), r["stats"]


def test_tvs_standoff_below_rail():
    _, by = _review(_motor_bus(NL(), "SMBJ15A"), checks=["tvs_clamp"], rail_volts={"VBUS_M": 20})
    assert by["tvs_standoff_below_rail"][0]["severity"] == "error", by


# ── a) capacitor voltage derating ─────────────────────────────────────────
def test_ten_volt_cap_on_twenty_volt_rail():
    n = NL().cap("C13", "10uF", "+20V", MPN="CL21A106KPFNNNE")      # Samsung 'P' = 10 V
    _, by = _review(n, checks=["cap_voltage"])
    f = by["cap_overvoltage"][0]
    assert f["severity"] == "error" and "rated 10 V" in f["message"] and "MPN code" in f["message"], f
    assert "35V" in f["fix"] or "≥35 V" in f["fix"], f["fix"]       # 1.5 × 20 = 30 → next standard rating 35 V
    # 25 V ceramic on 20 V: under the 1.5× derating
    _, by = _review(NL().cap("C13", "10uF 25V", "+20V"), checks=["cap_voltage"])
    assert by["cap_derating"][0]["severity"] == "warning", by
    # 50 V part (LCSC record) and a 35 V electrolytic (1.25×) are fine
    n = NL().cap("C13", "10uF", "+20V", LCSC="C13585").cap("C11", "470uF", "+20V", lib="Device:C_Polarized",
                                                          footprint="Capacitor_THT:CP_Elec_10x10.5", MPN="JVJ35V470M10x10")
    _, by = _review(n, checks=["cap_voltage"])
    assert not by, by
    # unknown rating on a 20 V rail: one note, never a warning
    _, by = _review(NL().cap("C5", "1uF", "+20V"), checks=["cap_voltage"])
    assert list(by) == ["cap_voltage_unknown"] and by["cap_voltage_unknown"][0]["severity"] == "info", by


def test_mlcc_codes_and_voltage_across():
    for mpn, v in (("CL10A475KO8NNNC", 16), ("CC0603KRX7R9BB104", 50), ("GRM188R71H104KA93D", 50),
                   ("CL31A226KAHNNNE", 25), ("CC0402KRX5R6BB105", 10)):
        assert E.cap_rating({"value": "x", "fields": {"MPN": mpn}}) == (v, "MPN code"), mpn
    assert E.cap_rating({"value": "100uF/35V", "fields": {}})[0] == 35
    # a cap between two rails sees the difference: 12 V − 5 V = 7 V on a 16 V part is fine
    n = NL().cap("C1", "1uF", "+12V", "+5V", MPN="CL10A105KO8NNNC")
    assert not _review(n, checks=["cap_voltage"])[1]
    # a VBUS_M (motor bus) is not USB VBUS: no voltage from its name
    n = NL().cap("C1", "1uF", "VBUS_M", MPN="CL10A105KQ8NNNC")
    assert not _review(n, checks=["cap_voltage"])[1]


# ── d) crystal load caps ──────────────────────────────────────────────────
def _crystal(n: NL, c_val, cl_lcsc="C400090"):
    n.part("Y1", "8MHz", "copper-lcsc:TAXM8M4RDBCCT2T_C400090", LCSC=cl_lcsc)
    n.pin("HSE_IN", "Y1", 1).pin("GND", "Y1", 2).pin("HSE_OUT", "Y1", 3).pin("GND", "Y1", 4)
    n.cap("C8", c_val, "HSE_IN").cap("C9", c_val, "HSE_OUT")
    return n


def test_wrong_crystal_caps():
    _, by = _review(_crystal(NL(), "22pF"), checks=["crystal_caps"])
    f = by["crystal_load_mismatch"][0]
    assert f["severity"] == "warning" and "CL=10 pF" in f["message"] and set(f["items"]) == {"Y1", "C8", "C9"}, f
    assert '"value":"12pF"' in f["fix"], f["fix"]                  # 2 × (10 − 4) = 12 pF
    _, by = _review(_crystal(NL(), "12pF"), checks=["crystal_caps"])
    assert not by, by
    # load capacitance from the Value when there is no LCSC record
    n = NL().part("Y2", "16MHz 20pF", "Device:Crystal").pin("XI", "Y2", 1).pin("XO", "Y2", 2)
    n.cap("C1", "12pF", "XI").cap("C2", "12pF", "XO")
    assert "crystal_load_mismatch" in _review(n, checks=["crystal_caps"])[1]


def test_one_load_cap_is_still_a_warning():
    """The old 'series cap' test counted the other terminal's load cap and downgraded a missing cap to a note."""
    n = NL().part("Y1", "8MHz", "Device:Crystal").pin("XI", "Y1", 1).pin("XO", "Y1", 2).cap("C8", "12pF", "XI")
    f = _review(n, checks=["crystal_caps"])[1]["crystal_no_load_caps"][0]
    assert f["severity"] == "warning" and "XO" in f["message"], f
    # a real series cap (to the MCU pin, not to ground) still makes it a note
    n = NL().part("Y1", "8MHz", "Device:Crystal").pin("XI", "Y1", 1).pin("XO", "Y1", 2).cap("C8", "12pF", "XI")
    n.cap("C7", "100pF", "XO", "OSC_PIN")
    assert _review(n, checks=["crystal_caps"])[1]["crystal_no_load_caps"][0]["severity"] == "info"


# ── f) STM32 pins ─────────────────────────────────────────────────────────
def test_swo_shared_with_spi_clock_on_pb3():
    n = _debug_header(_stm32(NL()), swo_net="ENC_SCK")
    n.pin("ENC_SCK", "U1", 40, "PB3", "bidirectional")
    n.part("U8", "AS5047P", "Sensor_Magnetic:AS5047P").pin("ENC_SCK", "U8", 2, "CLK", "input")
    _, by = _review(n, checks=["mcu_pins"])
    f = by["mcu_debug_pin_conflict"][0]
    assert f["severity"] == "warning" and "PB3" in f["message"] and "U8.CLK" in f["items"], f
    # PB3 only on the header as SWO: fine
    n = _debug_header(_stm32(NL())).pin("SWO", "U1", 40, "PB3", "bidirectional")
    assert "mcu_debug_pin_conflict" not in _review(n, checks=["mcu_pins"])[1]
    # PA13 used as an LED output: SWD lost
    n = _stm32(NL()).pin("LED_STATUS", "U1", 37, "PA13", "bidirectional").part("D1", "LED", "Device:LED")
    n.pin("LED_STATUS", "D1", 2)
    assert "mcu_swd_pin_reused" in _review(n, checks=["mcu_pins"])[1]


def test_pb8_boot0_driven_by_can_transceiver():
    n = _stm32(NL()).pin("CAN_RX", "U1", 45, "PB8", "bidirectional")
    n.part("U5", "TJA1051T/3/1J", "copper-lcsc:TJA1051T").pin("CAN_RX", "U5", 4, "RXD", "output")
    f = _review(n, checks=["mcu_pins"])[1]["mcu_boot0_shared"][0]
    assert f["severity"] == "warning" and "nSWBOOT0" in f["fix"], f
    # an STM32F1 has a dedicated BOOT0 pin: PB8 is just a GPIO
    n = NL().part("U1", "STM32F103C8T6", "MCU_ST_STM32F1:STM32F103C8Tx").pin("CAN_RX", "U1", 45, "PB8", "bidirectional")
    n.part("U5", "TJA1051T/3/1J", "x").pin("CAN_RX", "U5", 4, "RXD", "output")
    assert "mcu_boot0_shared" not in _review(n, checks=["mcu_pins"])[1]


# ── data table + defaults ─────────────────────────────────────────────────
def test_data_table_is_grounded_and_checks_are_on_by_default():
    d = E.data()
    for e in d["ic_supply"] + d["joined_pins"]:
        assert e.get("source"), e
    for k in ("derating", "crystal", "tvs_families"):
        assert d[k].get("source"), k
    assert d["tvs_families"]["_clamp"][18.0] == 29.2 and d["tvs_families"]["_clamp"][24.0] == 38.9
    for c in E.CHECK_NAMES:
        assert c in DR.CHECKS and c in DR.DEFAULT_CHECKS, c


def test_rail_volts_are_remembered_per_project():
    from copper_kicad.project import Project
    d = _TMP / "railproj"
    d.mkdir(parents=True, exist_ok=True)
    pr = Project(d, "railproj")
    assert DR.rail_volts_for(pr) == {}
    assert DR.rail_volts_for(pr, {"VBUS_M": 24}) == {"VBUS_M": 24.0}
    assert DR.rail_volts_for(pr) == {"VBUS_M": 24.0}
    assert DR.rail_volts_for(pr, {"VBUS_M": None}) == {}


# ── sch_apply's post-write pass ───────────────────────────────────────────
def test_quick_check_reports_only_touched_parts():
    n = _stm32(NL()).pin("NRST", "U1", 7, "PG10/NRST", "bidirectional")
    _switch(n, {"1": "NRST", "2": "GND", "3": "NRST", "4": "GND"}, LCSC="C318884")
    n.cap("C13", "10uF", "+20V", MPN="CL21A106KPFNNNE")
    ops = [{"op": "place", "lcsc": "C318884", "id": "sw"}, {"op": "connect", "pins": ["sw.1", "sw.3"], "net": "NRST"}]
    touched = E.touched_refs(ops, {"sw": "SW1"})
    assert touched == {"SW1"}, touched
    q = E.quick_check(n.dict(), touched)
    assert q and q["errors"] == 1 and [f["type"] for f in q["findings"]] == ["internally_joined_pins"], q
    assert E.quick_check(n.dict(), {"C99"}) is None
    q = E.quick_check(n.dict(), E.touched_refs([{"op": "set_field", "ref": "C13", "field": "Value", "value": "10uF"}], {}))
    assert q and q["findings"][0]["type"] == "cap_overvoltage", q


def test_sch_apply_surfaces_an_overvoltage_cap():
    """End to end through KiCad: a stock SW_Push_Dual wired as a DPST button adds nothing; a 6.3 V cap on +12V
    is reported under `electrical`, and fixing its Value clears it."""
    from copper_kicad.mcp import registry
    from copper_kicad.project import Project
    import copper_kicad.tools  # noqa: F401
    d = _TMP / "swproj"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    pr = Project(d, "swproj")
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "preview": False, "ops": [
        {"op": "place", "lib_id": "Switch:SW_Push_Dual", "id": "sw"},
        {"op": "connect", "pin": "sw.1", "net": "BTN_A"}, {"op": "connect", "pin": "sw.3", "net": "BTN_B"},
        {"op": "connect", "pins": ["sw.2", "sw.4"], "net": "GND"}]}, lambda *a: None)
    assert not out["isError"], out["content"][0]["text"][:600]
    assert "electrical" not in out["structuredContent"], out["structuredContent"].get("electrical")
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "preview": False, "ops": [
        {"op": "place", "lib_id": "Device:C", "value": "10uF 6.3V", "id": "c"},
        {"op": "connect", "pin": "c.1", "net": "+12V"}, {"op": "connect", "pin": "c.2", "net": "GND"}]}, lambda *a: None)
    assert not out["isError"], out["content"][0]["text"][:600]
    data = out["structuredContent"]
    el = data.get("electrical")
    assert el and el["findings"][0]["type"] == "cap_overvoltage", data
    assert "electrical rules" in out["content"][0]["text"] or el, out["content"][0]["text"]
    ref = el["findings"][0]["items"][0]
    out = registry.call_tool("sch_apply", {"project": str(pr.pro), "preview": False, "ops": [
        {"op": "set_field", "ref": ref, "field": "Value", "value": "10uF 25V"}]}, lambda *a: None)
    assert not out["isError"], out["content"][0]["text"][:600]
    assert "electrical" not in out["structuredContent"], out["structuredContent"].get("electrical")
    assert pr.sch.exists()


# ── review follow-ups: false negatives / positives found on real part data ─
def test_gate_driver_whose_lcsc_description_mentions_a_diode():
    """NSG2066's LCSC description says 'Built-in bootstrap diode': comp_kind calls it a diode, the checks must not."""
    n = _gate_driver(NL(), "VBAT")
    n.comps["U6"]["description"] = "Three-phase gate driver, Built-in bootstrap diode"
    from copper_kicad.review.common import comp_kind
    assert comp_kind("U6", n.comps["U6"]) == "diode"            # the classification the checks work around
    _, by = _review(n, checks=["ic_supply"], rail_volts={"VBAT": 21})
    assert by["ic_supply_over_max"][0]["items"] == ["U6.7"], by
    # a U-part the IC table does not know stays out (no new noise from ESD arrays etc.)
    n = NL().part("U5", "USBLC6-2SC6", "Power_Protection:USBLC6-2SC6")
    n.comps["U5"]["description"] = "TVS diode array"
    n.pin("+24V", "U5", 5, "VBUS", "passive").pin("GND", "U5", 2, "GND", "passive")
    assert not _review(n, checks=["ic_supply", "tvs_clamp"])[1]


def test_swd_header_with_plain_wires_or_pin_named_nets_is_clean():
    for a, b in (("Net-(J1-Pin_2)", "Net-(J1-Pin_3)"), ("PA13", "PA14"), ("/U1_PA13", "/U1_PA14")):
        n = _stm32(NL()).part("J1", "SWD", "Connector_Generic:Conn_01x04")
        n.pin(a, "J1", 2).pin(a, "U1", 37, "PA13", "bidirectional").pin(b, "J1", 3).pin(b, "U1", 38, "PA14", "bidirectional")
        _, by = _review(n, checks=["mcu_pins"])
        assert not by, (a, by)
    # a pin-named net that also drives an LED is still a reuse
    n = _stm32(NL()).pin("PA13", "U1", 37, "PA13", "bidirectional").part("Q1", "2N7002", "Device:Q_NMOS")
    n.pin("PA13", "Q1", 1, "G", "input")
    assert "mcu_swd_pin_reused" in _review(n, checks=["mcu_pins"])[1]
    # only one SWD pin on a connector (e.g. a UART header) is not a debug header
    n = _stm32(NL()).part("J2", "UART", "Connector_Generic:Conn_01x03")
    n.pin("Net-(J2-Pin_2)", "J2", 2).pin("Net-(J2-Pin_2)", "U1", 37, "PA13", "bidirectional")
    assert "mcu_swd_pin_reused" in _review(n, checks=["mcu_pins"])[1]


def test_esd_array_on_swd_lines_is_not_a_conflict():
    n = _debug_header(_stm32(NL()))
    n.part("U5", "USBLC6-2SC6", "Power_Protection:USBLC6-2SC6")
    n.pin("SWDIO", "U5", 1, "I/O1", "passive").pin("SWCLK", "U5", 3, "I/O2", "passive")
    _, by = _review(n, checks=["mcu_pins"])
    assert not by, by
    assert E.is_protection({"value": "X", "description": "Unidirectional TVS diode array SOT-23-6"})
    assert not E.is_protection({"value": "NSG2066", "lib_id": "copper-lcsc:NSG2066",
                                "description": "Under Voltage Protection、Built-in bootstrap diode TSSOP-20 Gate Drivers"})
    # a real second user on SWDIO still conflicts
    n.part("U8", "AS5047P", "Sensor_Magnetic:AS5047P").pin("SWDIO", "U8", 2, "CLK", "input")
    assert "mcu_debug_pin_conflict" in _review(n, checks=["mcu_pins"])[1]


def test_vbus_name_with_a_voltage_keeps_it():
    n = NL().cap("C1", "10uF 16V", "VBUS_24V")
    n.part("U9", "TPS54202", "Regulator_Switching:TPS54202").pin("VBUS_24V", "U9", 3, "VIN", "power_in")
    r, by = _review(n, checks=["cap_voltage", "ic_supply"])
    assert by["cap_overvoltage"][0]["severity"] == "error" and r["stats"]["rail_volts"]["VBUS_24V"] == 24, (by, r["stats"])
    # VBUS_M (motor bus, no voltage in the name, no USB connector) stays unknown
    n = NL().cap("C1", "10uF 16V", "VBUS_M")
    assert "cap_overvoltage" not in _review(n, checks=["cap_voltage"])[1]


def test_rail_volts_accepts_units_and_rejects_garbage():
    assert DR.rail_volts_for(None, {"VBAT": "21V", "+3V3": "3V3", "VM": " 24 V ", "X": 5}) == \
        {"VBAT": 21.0, "+3V3": 3.3, "VM": 24.0, "X": 5.0}
    for bad in ("abc", "21mV", True, [21], float("nan")):
        try:
            DR.rail_volts_for(None, {"VBAT": bad})
        except ValueError as e:
            assert "VBAT" in str(e) and "not a voltage" in str(e), e
        else:
            raise AssertionError(f"accepted {bad!r}")
    # nothing is remembered when one value is bad
    from copper_kicad.project import Project
    d = _TMP / "railproj2"
    d.mkdir(parents=True, exist_ok=True)
    pr = Project(d, "railproj2")
    try:
        DR.rail_volts_for(pr, {"A": 5, "B": "x"})
    except ValueError:
        pass
    assert DR.rail_volts_for(pr) == {}
    # design_review stays annotated read-only: a live host must route it to the shadow of the open schematic
    # (its rail_volts write is side state, like verify's); see test_rail_volts_in_a_shadow_checkout
    from copper_kicad.mcp import registry
    import copper_kicad.tools  # noqa: F401
    t = registry.TOOLS["design_review"]
    assert t.read_only and t.idempotent, t


def test_ts1187a_rule_matches_only_its_own_lcsc_number():
    n = NL().part("J5", "Conn_01x04", "Connector_Generic:Conn_01x04", LCSC="C3188841")
    for p, net in {"1": "A", "2": "B", "3": "C", "4": "D"}.items():
        n.pin(net, "J5", p)
    assert not _review(n, checks=["joined_pins"])[1]
    n = NL().part("SW3", "Button", "mylib:SW", LCSC="C3188841")      # same prefix on a switch: not the TS-1187A
    for p, net in {"1": "A", "2": "B", "3": "A", "4": "B"}.items():
        n.pin(net, "SW3", p)
    assert not _review(n, checks=["joined_pins"])[1]


# ── the corrected FOC board ───────────────────────────────────────────────
def test_corrected_foc_board_has_no_false_positives():
    if not (FOC_ORIG / "foc-g4.kicad_sch").exists():
        print("  skip: FOC board copy not present")
        return
    d = _TMP / "foc"
    shutil.copytree(FOC_ORIG, d)                   # never touch the original
    from copper_kicad import project as P
    pr = P.resolve(str(d), must_exist=True)
    r = DR.review(pr, include_erc=False)
    new = [f for f in r["findings"] if f["type"] in NEW_TYPES]
    assert not [f for f in new if f["severity"] == "error"], new
    assert [f["type"] for f in new] == ["mcu_boot0_shared"], new        # the real one: CAN RXD drives PB8/BOOT0
    assert "U5.RXD" in new[0]["items"], new[0]
    rv = r["stats"]["rail_volts"]
    assert rv["+12V"] == 12 and rv["+3V3"] == 3.3 and "18" in str(rv["VBUS_M"]), rv
    # declaring the bus voltage keeps it clean at 16.8 V (4S) and catches a bus beyond the parts at 28 V
    r = DR.review(pr, include_erc=False, rail_volts={"VBUS_M": 16.8},
                  checks=["cap_voltage", "tvs_clamp", "ic_supply"])
    assert not [f for f in r["findings"] if f["severity"] != "info"], r["findings"]
    r = DR.review(pr, include_erc=False, rail_volts={"VBUS_M": 28}, checks=["cap_voltage", "tvs_clamp", "ic_supply"])
    t = {f["type"] for f in r["findings"]}
    # SMBJ18A conducts, TPS54202 (28 V max) has no margin; L78L12 (35 V abs max, no recommended max) is fine;
    # 35 V electrolytics still pass 1.25x
    assert {"tvs_standoff_below_rail", "ic_supply_no_headroom"} <= t and not t & {"cap_overvoltage", "ic_supply_over_max"}, t
    # the original bug on real part data: NSG2066 (LCSC description mentions its bootstrap diode) VCC on the bus
    import copy
    nl = copy.deepcopy(DR.load_netlist(pr))
    assert "diode" in (nl["components"]["U6"].get("description") or "").lower()
    node = next(n for n in nl["nets"]["+12V"] if n["ref"] == "U6" and n["pin"] == "7")
    nl["nets"]["+12V"].remove(node)
    nl["nets"]["/VBUS_M"].append(node)
    r = DR.review(None, include_erc=False, netlist=nl, rail_volts={"VBUS_M": 21}, checks=["ic_supply"])
    assert any(f["type"] == "ic_supply_over_max" and "U6.7" in f["items"] for f in r["findings"]), r["findings"]
    assert (FOC_ORIG / "foc-g4.kicad_sch").stat().st_size > 0


# -- review round 2: regressions ----------------------------------------------
def test_rail_volts_in_a_shadow_checkout():
    """The app runs sch_apply / verify / design_review in <proj>/.copper/shadow/<rev>/: remembered rail voltages
    must be the real project's, in both directions."""
    from copper_kicad.project import Project
    d = _TMP / "shadowproj"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "shadowproj.kicad_pro").write_text("{}", encoding="utf-8")
    pr = Project(d, "shadowproj")
    assert DR.rail_volts_for(pr, {"VBUS_M": 24}) == {"VBUS_M": 24.0}
    sh = d / ".copper" / "shadow" / "r7"
    sh.mkdir(parents=True)
    (sh / "shadowproj.kicad_pro").write_text("{}", encoding="utf-8")
    spr = Project(sh, "shadowproj")
    assert DR.rail_volts_for(spr) == {"VBUS_M": 24.0}                  # read back through the shadow
    assert DR.rail_volts_for(spr, {"VBAT": 16.8}) == {"VBUS_M": 24.0, "VBAT": 16.8}
    assert DR.rail_volts_for(pr) == {"VBUS_M": 24.0, "VBAT": 16.8}     # written to the real project
    assert not (sh / ".copper" / "verify.json").exists()
    # a host may keep shadows anywhere (short temp path): the checkout names the real project
    from copper_kicad.host import route
    ext = _TMP / "elsewhere" / "s1"
    shutil.rmtree(ext, ignore_errors=True)
    ext.mkdir(parents=True)
    (ext / "shadowproj.kicad_pro").write_text("{}", encoding="utf-8")
    route.note_shadow(ext, d)
    epr = Project(ext, "shadowproj")
    assert DR.rail_volts_for(epr) == {"VBUS_M": 24.0, "VBAT": 16.8}
    DR.rail_volts_for(epr, {"VIN": 12})
    assert DR.rail_volts_for(pr)["VIN"] == 12.0 and not (ext / ".copper" / "verify.json").exists()


def test_electrolytic_eia_voltage_codes():
    def cp(mpn):
        return E.cap_rating({"value": "100uF", "lib_id": "Device:C_Polarized", "footprint": "Capacitor_SMD:CP_Elec_6.3x7.7",
                             "fields": {"MPN": mpn}})
    assert cp("EEE-FK1V101P") == (35.0, "MPN code")
    assert cp("UWT1V101MCL1GS") == (35.0, "MPN code")
    assert cp("EEE-FK1E101P") == (25.0, "MPN code")
    assert cp("JVJ35V470M10x10") == (35.0, "MPN")
    n = NL().cap("C1", "100uF", "+12V", lib="Device:C_Polarized", footprint="Capacitor_SMD:CP_Elec_6.3x7.7",
                 MPN="EEE-FK1V101P")
    assert not _review(n, checks=["cap_voltage"])[1]


def test_tvs_on_a_logic_rail_is_not_an_error():
    n = NL().part("D1", "SMBJ5.0A", "Device:D_TVS", "Diode_SMD:D_SMB").pin("+5V", "D1", 1, "A1").pin("GND", "D1", 2, "A2")
    n.part("U1", "ATmega328P-AU", "MCU_Microchip_ATmega:ATmega328P-A")
    n.pin("+5V", "U1", 4, "VCC", "power_in").pin("+5V", "U1", 18, "AVCC", "power_in").pin("GND", "U1", 3, "GND", "power_in")
    r, by = _review(n, checks=["tvs_clamp"])
    f = by.get("tvs_clamp_above_abs_max", [])
    assert f and all(x["severity"] == "info" for x in f), r["findings"]
    assert E.quick_check(n.dict(), {"D1", "U1"}) is None


def test_78xx_on_24v_is_legal():
    for val in ("L78L05ACUTR", "L7805CV"):
        n = NL().part("U2", val, "x:y").pin("+24V", "U2", 1, "VI", "power_in").pin("+5V", "U2", 3, "VO", "power_out")
        n.pin("GND", "U2", 2, "GND", "power_in")
        _, by = _review(n, checks=["ic_supply"])
        assert not by, (val, by)
    n = NL().part("U2", "L78L05ACUTR", "x:y").pin("+36V", "U2", 1, "VI", "power_in")
    assert "ic_supply_over_abs_max" in _review(n, checks=["ic_supply"])[1]


def test_bare_number_load_caps_mean_pf():
    n = NL().part("Y1", "8MHz 18pF", "Device:Crystal").pin("XI", "Y1", 1).pin("XO", "Y1", 2)
    n.cap("C1", "27", "XI").cap("C2", "27", "XO")
    f = _review(n, checks=["crystal_caps"])[1].get("crystal_load_mismatch")
    assert not f, f                                   # 27 pF each -> 13.5 + 3..5 pF stray: matches CL=18 pF
    n = NL().part("Y1", "8MHz 18pF", "Device:Crystal").pin("XI", "Y1", 1).pin("XO", "Y1", 2)
    n.cap("C1", "47", "XI").cap("C2", "47", "XO")
    f = _review(n, checks=["crystal_caps"])[1]["crystal_load_mismatch"][0]
    assert "47 pF" in f["message"] and "e+" not in f["message"], f["message"]
    n = NL().part("Y1", "8MHz 18pF", "Device:Crystal").pin("XI", "Y1", 1).pin("XO", "Y1", 2)
    n.cap("C1", "100n", "XI").cap("C2", "100n", "XO")
    assert "crystal_load_mismatch" not in _review(n, checks=["crystal_caps"])[1]


def test_negative_rail_keeps_its_sign():
    r, by = _review(NL().cap("C1", "100nF 16V", "+12V", "-12V"), checks=["cap_voltage"])
    assert by["cap_overvoltage"][0]["severity"] == "error", by           # 24 V across a 16 V part
    assert r["stats"]["rail_volts"]["-12V"] in (-12, "-12 V") or "-12" in str(r["stats"]["rail_volts"]["-12V"]), r["stats"]
    assert "cap_overvoltage" in _review(NL().cap("C1", "100nF 16V", "+5V", "-12V"), checks=["cap_voltage"])[1]   # 17 V
    assert not _review(NL().cap("C1", "100nF 25V", "-12V"), checks=["cap_voltage"])[1]                            # 12 V
    # an IC supply range is never compared with a negative rail
    n = NL().part("U1", "ATmega328P-AU", "x:y").pin("-12V", "U1", 4, "VCC", "power_in")
    assert not _review(n, checks=["ic_supply"])[1]


def test_stm32f0_vddio2_at_1v8():
    n = NL().part("U1", "STM32F072CBT6", "MCU_ST_STM32F0:STM32F072CBTx")
    n.pin("+3V3", "U1", 24, "VDD", "power_in").pin("+1V8", "U1", 36, "VDDIO2", "power_in")
    assert not _review(n, checks=["ic_supply"])[1]
    n = NL().part("U1", "STM32F072CBT6", "MCU_ST_STM32F0:STM32F072CBTx").pin("+1V8", "U1", 24, "VDD", "power_in")
    assert "ic_supply_under_min" in _review(n, checks=["ic_supply"])[1]


def test_tvs_clamp_error_only_when_a_standoff_capable_tvs_could_fix_it():
    # SMBJ12A on a known 12 V rail feeding an AMS1117 (abs max 15 V): no TVS that stands off 12 V clamps below
    # 19.9 V, so an error with "pick a lower clamp" advice would be impossible to follow -> info
    n = NL().part("D1", "SMBJ12A", "Device:D_TVS", "Diode_SMD:D_SMB").pin("+12V", "D1", 1, "A1").pin("GND", "D1", 2, "A2")
    n.part("U1", "AMS1117-3.3", "Regulator_Linear:AMS1117-3.3").pin("+12V", "U1", 3, "VI", "power_in")
    n.pin("GND", "U1", 1, "GND", "power_in")
    f = _review(n, checks=["tvs_clamp"])[1].get("tvs_clamp_above_abs_max", [])
    assert f and all(x["severity"] == "info" for x in f), f
    # the standoff fix suggestion uses the rail's magnitude on a negative rail
    n = NL().part("D1", "SMBJ5.0A", "Device:D_TVS", "Diode_SMD:D_SMB").pin("-12V", "D1", 1, "A1").pin("GND", "D1", 2, "A2")
    f = _review(n, checks=["tvs_clamp"])[1]["tvs_standoff_below_rail"][0]
    assert "SMBJ12A" in f["fix"] and "SMBJ5A" not in f["fix"], f["fix"]
    # IC pins on a negative rail are not compared with the clamp
    n.part("U1", "AMS1117-3.3", "Regulator_Linear:AMS1117-3.3").pin("-12V", "U1", 3, "VI", "power_in")
    assert "tvs_clamp_above_abs_max" not in _review(n, checks=["tvs_clamp"])[1]


def test_tantalum_case_code_is_not_read_as_volts():
    cp = E.cap_rating({"value": "100uF", "lib_id": "Device:C_Polarized", "footprint": "Capacitor_Tantalum_SMD:CP_EIA-7343-31_Kemet-D",
                       "fields": {"MPN": "T520V107M006ATE015"}})
    assert cp is None or cp[0] != 520.0, cp


def _run_all(names=None):
    import time
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f) and (not names or n in names)]
    failed = 0
    for n, f in tests:
        t = time.time()
        try:
            f()
            print(f"PASS {n} ({time.time() - t:.1f}s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FAIL {n}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)
    print(f"\n{len(tests) - failed}/{len(tests)} passed — artifacts in {_TMP}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all(sys.argv[1:]) else 0)
